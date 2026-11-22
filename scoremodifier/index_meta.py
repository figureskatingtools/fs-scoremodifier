"""Parse a Figure Skating Manager (Swiss Timing) competition ``index.htm``.

The page is loose, generated HTML (no semantic ids, some unquoted ``href=``),
so this parses heuristically with the standard library only — no third-party
dependency, so it lives in the core package and is shared by the CLI and the
Azure Function.

Pulls the competition name / date / venue and the list of category result
pages (``CAT###RS.htm``) so the caller can pick the page matching the uploaded
PDF and re-emit a podium-only version under the same filename.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import quote, urljoin, urlsplit

_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
_CAT_RE = re.compile(r'href=["\']?\s*(CAT\d+RS\.htm)', re.I)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
_TD_CAPTION3_RE = re.compile(r'<td[^>]*class="caption3"[^>]*>(.*?)</td>', re.I | re.S)
_CELL_RE = re.compile(r'class="CellLeft"[^>]*>(.*?)</td>', re.I | re.S)


@dataclass
class Category:
    name: str
    cat_file: str  # e.g. "CAT003RS.htm"


@dataclass
class IndexMeta:
    competition: str = ""
    date: str = ""
    venue: str = ""
    categories: list[Category] = field(default_factory=list)


def _clean(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html)).strip()


def parse_index_html(html: str) -> IndexMeta:
    """Best-effort parse of the competition index page."""
    meta = IndexMeta()

    m = _TITLE_RE.search(html)
    if m:
        meta.competition = _clean(m.group(1))

    dates = _DATE_RE.findall(html)
    if dates:
        meta.date = dates[0] if len(dates) < 2 or dates[1] == dates[0] else f"{dates[0]} - {dates[1]}"

    # caption3 cells hold the organizer ("Pori / PML") and the venue
    # ("Isomäen Jäähalli, Pori"); the venue is the one that reads like a place.
    cells = [_clean(c) for c in _TD_CAPTION3_RE.findall(html)]
    cells = [c for c in cells if c and not _DATE_RE.search(c)]
    venue = next((c for c in cells if "," in c), None)
    if venue is None:
        venue = next((c for c in cells if "/" not in c), cells[0] if cells else "")
    meta.venue = venue

    seen: set[str] = set()
    for chunk in re.split(r"(?=<tr)", html, flags=re.I):
        cm = _CAT_RE.search(chunk)
        if not cm:
            continue
        cat_file = cm.group(1)
        if cat_file in seen:
            continue
        seen.add(cat_file)
        name = next((t for t in (_clean(x) for x in _CELL_RE.findall(chunk)) if t), "")
        meta.categories.append(Category(name=name, cat_file=cat_file))

    return meta


# ── fetching (SSRF-guarded) ───────────────────────────────────────────────────

# An index page is a few tens of KiB; anything near this is not one.
MAX_INDEX_BYTES = 2 * 1024 * 1024
# Redirects followed by hand (each hop re-validated): the results service
# answers http->https and directory->index style redirects, nothing longer.
MAX_REDIRECTS = 3
# Wall-clock budget for the whole fetch, redirects and body included.
TOTAL_TIMEOUT = 20.0
USER_AGENT = "fs-scoremodifier/1.0"
_monotonic = time.monotonic  # test seam
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_READ_CHUNK = 64 * 1024
# Characters left as they are when re-quoting a pasted path/query.
_URL_SAFE = "/%:@!$&'()*+,;=-._~"


class IndexFetchError(Exception):
    """The index page could not be fetched. ``code`` is a short machine reason
    (logged server-side; never echoed to the caller in detail)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class IndexUrlRejected(IndexFetchError, ValueError):
    """The URL itself is not acceptable (scheme, host, form) — nothing was
    requested. A ``ValueError`` so callers can answer 400."""


def _is_public_address(ip: str) -> bool:
    """True only for a globally routable unicast address (IPv4 or IPv6)."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or not addr.is_global
    )


def _resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to (test seam)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _open_connection(host: str, port: int, ip: str, timeout: float):
    """An HTTPS connection to the already-validated ``ip``, with SNI and
    certificate checks against ``host`` (test seam). Connecting to the checked
    address, not the name, means a second DNS answer cannot swap it."""
    ctx = ssl.create_default_context()
    sock = socket.create_connection((ip, port), timeout=timeout)
    try:
        tls = ctx.wrap_socket(sock, server_hostname=host)
    except BaseException:
        sock.close()
        raise
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    conn.sock = tls
    return conn


def _host_allowed(parts, allowed: set[str] | None) -> bool:
    if allowed is None:
        return True
    host = (parts.hostname or "").lower()
    if parts.port in (None, 443):
        return host in allowed
    return f"{host}:{parts.port}" in allowed


def _check_url(url, allowed: set[str] | None, *, upgrade_http: bool):
    """Validate one hop; returns the https ``SplitResult`` to request."""
    if not isinstance(url, str) or not url.strip():
        raise IndexUrlRejected("invalid_url")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise IndexUrlRejected("invalid_url") from None
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise IndexUrlRejected("invalid_url")
    scheme = parts.scheme.lower()
    if scheme == "http" and upgrade_http and port in (None, 80):
        # A pasted http:// link is requested over https only (the results
        # service redirects http->https anyway); plain HTTP is never sent.
        netloc = parts.netloc if port is None else parts.netloc.rsplit(":", 1)[0]
        parts = parts._replace(scheme="https", netloc=netloc)
    elif scheme != "https":
        raise IndexUrlRejected("scheme_not_allowed")
    if not _host_allowed(parts, allowed):
        raise IndexUrlRejected("host_not_allowed")
    return parts


def fetch_index_html(url: str, *, allowed_hosts: list[str] | None = None, timeout: float = 10.0) -> str:
    """Fetch the index page as text, guarded against SSRF.

    ``allowed_hosts`` (when given) restricts the host — the Azure Function
    passes the public results host; the CLI passes ``None`` (any host). Always:
    https only (an ``http://`` link is upgraded before anything is sent), no
    credentials in the URL, every resolved address must be public (no private,
    loopback, link-local, multicast or reserved IPv4/IPv6), redirects are
    followed by hand at most ``MAX_REDIRECTS`` times with each hop re-checked
    the same way (https and the allow-list, no upgrade), the body is capped at
    ``MAX_INDEX_BYTES`` and the whole fetch at ``TOTAL_TIMEOUT`` seconds.

    Raises ``IndexUrlRejected`` (a ``ValueError``) for an unacceptable URL and
    ``IndexFetchError`` for anything that went wrong while fetching.
    """
    allowed = None if allowed_hosts is None else {h.strip().lower() for h in allowed_hosts if h.strip()}
    deadline = _monotonic() + TOTAL_TIMEOUT
    parts = _check_url(url, allowed, upgrade_http=True)
    for _hop in range(MAX_REDIRECTS + 1):
        status, location, body = _get_once(parts, timeout, deadline)
        if status == 200:
            return body.decode("utf-8", "replace")
        if status in _REDIRECT_STATUSES and location:
            try:
                parts = _check_url(urljoin(parts.geturl(), location), allowed, upgrade_http=False)
            except IndexUrlRejected as e:
                # The page pointed somewhere we will not go: a fetch failure,
                # not a bad input from the caller.
                raise IndexFetchError(f"redirect_{e.code}") from None
            continue
        raise IndexFetchError(f"upstream_status_{status}")
    raise IndexFetchError("too_many_redirects")


def _get_once(parts, timeout: float, deadline: float):
    """One GET without following redirects: (status, Location, body)."""
    host = parts.hostname
    port = parts.port or 443

    def remaining() -> float:
        left = deadline - _monotonic()
        if left <= 0:
            raise IndexFetchError("timeout")
        return min(timeout, left)

    try:
        addresses = _resolve(host, port)
    except (OSError, UnicodeError):
        raise IndexFetchError("unresolvable") from None
    if not addresses:
        raise IndexFetchError("unresolvable")
    if not all(_is_public_address(ip) for ip in addresses):
        raise IndexFetchError("address_not_allowed")

    target = quote(parts.path or "/", safe=_URL_SAFE)
    if parts.query:
        target += "?" + quote(parts.query, safe=_URL_SAFE + "?")

    conn = None
    try:
        conn = _open_connection(host, port, addresses[0], remaining())
        conn.request("GET", target, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
        resp = conn.getresponse()
        status = resp.status
        if status in _REDIRECT_STATUSES:
            return status, resp.getheader("Location"), b""
        if status != 200:
            return status, None, b""
        length = (resp.getheader("Content-Length") or "").strip()
        if length.isdigit() and int(length) > MAX_INDEX_BYTES:
            raise IndexFetchError("too_large")
        chunks, total = [], 0
        while True:
            if getattr(conn, "sock", None) is not None:
                conn.sock.settimeout(remaining())
            chunk = resp.read(min(_READ_CHUNK, MAX_INDEX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_INDEX_BYTES:
                raise IndexFetchError("too_large")
        return status, None, b"".join(chunks)
    except IndexFetchError:
        raise
    except Exception as e:  # socket, TLS, HTTP protocol errors
        raise IndexFetchError(f"unreachable_{type(e).__name__}") from None
    finally:
        if conn is not None:
            conn.close()


def match_category(meta: IndexMeta, segment: str) -> Category | None:
    """Pick the category whose name best matches the PDF segment line.

    Scores by shared word tokens (case-insensitive); returns ``None`` if there
    is no overlap so the caller can fall back to asking the operator.
    """
    if not meta.categories:
        return None
    seg_tokens = set(re.findall(r"\w+", segment.lower()))
    best, best_score = None, 0
    for cat in meta.categories:
        score = len(seg_tokens & set(re.findall(r"\w+", cat.name.lower())))
        if score > best_score:
            best, best_score = cat, score
    return best
