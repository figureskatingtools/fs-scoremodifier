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

import re
import urllib.request
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urlparse

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


def fetch_index_html(url: str, *, allowed_hosts: list[str] | None = None, timeout: float = 10.0) -> str:
    """Fetch the index page as text.

    ``allowed_hosts`` (when given) restricts the host to mitigate SSRF — the
    Azure Function passes the public results host; the CLI passes ``None``.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme!r}")
    if allowed_hosts is not None and parsed.hostname not in allowed_hosts:
        raise ValueError(f"Host not allowed: {parsed.hostname!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "fs-scoremodifier/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (scheme checked)
        raw = resp.read()
    return raw.decode("utf-8", "replace")


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
