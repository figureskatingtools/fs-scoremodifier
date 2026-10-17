from __future__ import annotations

import azure.functions as func
import logging
import hmac
import os
import json
import re

import fitz  # PyMuPDF — used to derive the competition name and count output pages
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote
from uuid import uuid4
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
from azure.identity import DefaultAzureCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode

from scoremodifier.per_skater import split_per_skater, NotPerSkaterReport
from scoremodifier.extract import extract_results
from scoremodifier.model import ResultsMeta
from scoremodifier.results import render_results_pdf
from scoremodifier.results_html import render_results_html
from scoremodifier.index_meta import fetch_index_html, parse_index_html, match_category

import platform_access
from platform_clock import (
    PlatformClockUnavailable,
    get_platform_table_client,
    normalize_platform_id,
    platform_says_delete,
    read_platform_clock,
    to_iso_z,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# All blobs live in this single container in the tool's own storage account.
CONTAINER_NAME = "fs-scoremodifier"

# Maximum file upload size: 25 MB
MAX_UPLOAD_SIZE = 25 * 1024 * 1024

# Generated output filename (one per generation).
OUTPUT_FILENAME = "per-skater.pdf"
# Results-summary outputs.
RESULTS_PDF_FILENAME = "results.pdf"
DEFAULT_SUPERTITLE = "MUODOSTELMALUISTELU · VAPAAOHJELMA"
# The Tulokkaat category re-uses the original FSM export filename so the results
# PDF drops straight into the existing publishing workflow. Other categories get
# a generic name unless the operator overrides it (pdfFile query param).
TULOKKAAT_PDF_FILENAME = "FSKXSYNCHRONMLTULO--01FNL-000100--_JudgesDetailsperSkater.pdf"


def _safe_pdf_filename(name: str, fallback: str) -> str:
    """Sanitize an operator-supplied PDF filename: strip any path, keep simple
    characters, and guarantee a .pdf extension. Falls back when nothing usable
    remains."""
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(c for c in name if c.isalnum() or c in (" ", "-", "_", ".")).strip()
    if not name:
        return fallback
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name

# Hosts the parse_index endpoint may fetch (SSRF guard). Override via env
# INDEX_ALLOWED_HOSTS (comma-separated); defaults to the public results service.
INDEX_ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get(
        "INDEX_ALLOWED_HOSTS",
        "www.figureskatingresults.fi,figureskatingresults.fi",
    ).split(",")
    if h.strip()
]

# Automatic competition deletion. The tool has no retention clock of its own:
# every new row is bound to a platform competition (PlatformId, see
# platform_clock.py) and is deleted when the platform says so; its DeletionDate
# is only a cached copy of the platform's date. Legacy rows without a
# PlatformId keep the date they were stored with, and legacy rows missing one
# get backfilled with the migration date.
LEGACY_DELETION_DATE = "2026-06-12T00:00:00Z"
AUTO_CLEANUP_ACTOR = "auto-cleanup"
USER_DELETE_ACTOR = "user"
# CreatedBy/DeletedBy values that are not an e-mail address. Anything else on a
# hidden (deleted) row is scrubbed by the nightly sweep.
NON_PERSONAL_ACTORS = frozenset({"", "-", USER_DELETE_ACTOR, AUTO_CLEANUP_ACTOR})


def _utcnow() -> datetime:
    """Naive UTC "now". Every timestamp this app stores is serialised as a naive
    ISO string plus a literal Z (and the SAS start/expiry are naive too), so an
    aware datetime would change the stored format. datetime.utcnow() is
    deprecated since 3.12; this keeps the exact same value and shape."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_user_allowed(user: "User") -> bool:
    """
    Check if the user is allowed to use the tool at all (before any
    per-competition access check). All signed-in users are; hook for a future
    allowlist.
    """
    return True


def _proxy_secret_ok(req: func.HttpRequest) -> bool:
    """
    Verify the request came from the router proxy by checking the shared
    secret header. The function endpoint is public, so this is what makes the
    x-forwarded-user-* identity headers trustworthy.

    Fails CLOSED: an unset/empty PROXY_SHARED_SECRET rejects every request
    (local `func start` must set one too). Compared in constant time.
    """
    expected = os.environ.get("PROXY_SHARED_SECRET") or ""
    if not expected:
        logging.error("PROXY_SHARED_SECRET is not set; rejecting request")
        return False
    provided = req.headers.get("x-proxy-secret") or ""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


@dataclass(frozen=True)
class User:
    """The signed-in caller, exactly as the router forwarded it.

    ``tid``/``oid`` (lowercase Entra tenant + object id GUIDs) are the identity
    every access decision uses; ``email``/``name`` are for display and the
    CreatedBy history only, never for authorisation.
    """

    tid: str
    oid: str
    email: str
    name: str
    is_admin: bool


def get_user(req: func.HttpRequest) -> User | None:
    """
    The caller from the router's x-forwarded-user-* headers, or None (-> 401).

    Identity comes ONLY from those headers, which the router sets and the
    proxy secret makes trustworthy: no Easy Auth principal headers, no Bearer
    tokens. A missing or non-GUID tid/oid is no identity at all.
    """
    if not _proxy_secret_ok(req):
        logging.warning("Proxy shared secret missing or mismatched; rejecting request")
        return None

    tid = (req.headers.get("x-forwarded-user-tid") or "").strip().lower()
    oid = (req.headers.get("x-forwarded-user-oid") or "").strip().lower()
    if platform_access.identity_key(tid, oid) is None:
        # Header names only: values can carry personal data (addresses, IPs).
        logging.warning(f"No identity found. Request headers: {sorted(k.lower() for k in req.headers.keys())}")
        return None

    email = (req.headers.get("x-forwarded-user-email") or "").strip().lower()
    try:
        name = unquote(req.headers.get("x-forwarded-user-name") or "").strip()
    except Exception:
        name = ""
    is_admin = req.headers.get("x-forwarded-user-admin") == "1"
    return User(tid=tid, oid=oid, email=email, name=name, is_admin=is_admin)


def _role_for(user: User, platform_id, *, identities_client=None, access_client=None):
    """The caller's role on a platform competition ("owner"/"write"/"read"/None).

    A missing or malformed PlatformId (legacy record) is admin-only. Raises
    PlatformBindingError(503, platform_unavailable) when the platform access
    tables cannot be read — never grants because of a read failure.
    """
    guid = normalize_platform_id(platform_id)
    if guid is None:
        return "owner" if user.is_admin else None
    try:
        return platform_access.role_for(
            guid, user.tid, user.oid, is_admin=user.is_admin,
            identities_client=identities_client, access_client=access_client,
        )
    except platform_access.PlatformAccessUnavailable:
        logging.error("Platform access tables unavailable; request refused")
        raise PlatformBindingError(503, "platform_unavailable")


def _require_platform_access(user: User, platform_id, need: str) -> str:
    """Require role ``need`` ("read"/"write") on the platform competition.

    No role (no grant, unknown competition or identity, legacy record for a
    non-admin) -> 404 platform_not_found, so existence never leaks; a read-only
    caller on a write route -> 403 platform_read_only; unreadable access tables
    -> 503 platform_unavailable. Returns the role.
    """
    role = _role_for(user, platform_id)
    if role is None:
        raise PlatformBindingError(404, "platform_not_found")
    if not platform_access.role_satisfies(role, need):
        raise PlatformBindingError(403, "platform_read_only")
    return role


# Liveness for the site's admin diagnostics: no sign-in, no secret, no storage
# (the same shape as the platform API's /api/health).
@app.route(route="health", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({"status": "ok", "service": "fs-scoremodifier"}), mimetype="application/json")


@app.route(route="check_user_permission", auth_level=func.AuthLevel.ANONYMOUS)
def check_user_permission(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Checking user permission...')
    user = get_user(req)
    if not user:
        return func.HttpResponse(json.dumps({"allowed": False, "email": None}), mimetype="application/json", status_code=401)
    return func.HttpResponse(json.dumps({"allowed": True, "email": user.email or None}), mimetype="application/json")


def get_blob_service_client():
    """Connect to Blob Storage via managed identity (prod) or connection string (local)."""
    try:
        account_name = os.environ.get("AzureWebJobsStorage__accountName")
        if account_name:
            credential = DefaultAzureCredential()
            account_url = f"https://{account_name}.blob.core.windows.net"
            return BlobServiceClient(account_url=account_url, credential=credential)

        connection_string = os.environ.get("AzureWebJobsStorage")
        if connection_string:
            return BlobServiceClient.from_connection_string(connection_string)

        return None
    except Exception as e:
        logging.error(f"Failed to create blob client: {e}")
        return None


def get_table_client(table_name="generatedpapers"):
    """Connect to Table Storage via managed identity (prod) or connection string (local)."""
    try:
        account_name = os.environ.get("AzureWebJobsStorage__accountName")
        if account_name:
            credential = DefaultAzureCredential()
            endpoint = f"https://{account_name}.table.core.windows.net"
            return TableClient(endpoint=endpoint, table_name=table_name, credential=credential)

        connection_string = os.environ.get("AzureWebJobsStorage")
        if connection_string:
            return TableClient.from_connection_string(conn_str=connection_string, table_name=table_name)

        return None
    except Exception as e:
        logging.error(f"Failed to create table client: {e}")
        return None


def get_container_client(blob_service_client):
    """Return the container client, creating the container if it doesn't exist (local dev)."""
    container = blob_service_client.get_container_client(CONTAINER_NAME)
    try:
        container.create_container()
    except Exception:
        pass  # already exists (prod: created by Bicep)
    return container


def sanitize_name(name: str) -> str:
    """Keep alphanumerics, spaces, hyphens, underscores; trim. Used for the blob folder."""
    return "".join([c for c in name if c.isalnum() or c in (' ', '-', '_')]).strip()


def generate_competition_id(comp_table) -> str:
    """Generate a unique 8-char hex competition id (checked against the competitions table)."""
    for _ in range(5):
        cid = uuid4().hex[:8]
        try:
            comp_table.get_entity(partition_key="GLOBAL", row_key=cid)
        except ResourceNotFoundError:
            return cid
    raise RuntimeError("Could not generate a unique competition id")


def get_competition_entity(comp_id: str):
    """Look up a competition entity by its id (RowKey). Returns None if not found."""
    try:
        comp_table = get_table_client("competitions")
        if not comp_table:
            return None
        return comp_table.get_entity(partition_key="GLOBAL", row_key=comp_id)
    except ResourceNotFoundError:
        return None
    except Exception as e:
        logging.error(f"Error fetching competition entity {comp_id}: {e}")
        return None


def _parse_iso_utc(value):
    """Parse a stored ISO timestamp (naive UTC + trailing 'Z') into an aware UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    try:
        s = value[:-1] if value.endswith("Z") else value
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception as e:
        logging.warning(f"Could not parse ISO timestamp '{value}': {e}")
        return None


def _ensure_deletion_date(comp_table, entity):
    """Lazy migration: backfill a fixed DeletionDate on legacy rows created before
    this feature. Bound rows (PlatformId) are left alone: their date belongs to
    the platform and may legitimately be missing until the sweep caches it."""
    if entity.get("DeletionDate") or entity.get("PlatformId"):
        return entity
    entity["DeletionDate"] = LEGACY_DELETION_DATE
    try:
        comp_table.update_entity({
            "PartitionKey": "GLOBAL",
            "RowKey": entity["RowKey"],
            "DeletionDate": LEGACY_DELETION_DATE,
        }, mode=UpdateMode.MERGE)
    except Exception as e:
        logging.warning(f"Failed to backfill DeletionDate for {entity['RowKey']}: {e}")
    return entity


class PlatformBindingError(Exception):
    """A generation request cannot be bound to a platform competition."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code

    def response(self) -> func.HttpResponse:
        return func.HttpResponse(json.dumps({"error": self.code}), status_code=self.status, mimetype="application/json")


def _platform_id_param(req: func.HttpRequest) -> str:
    """Required ``platformId`` query param (the site's active competition GUID),
    normalized. 400 platform_id_required when missing or malformed."""
    platform_id = normalize_platform_id(req.params.get("platformId"))
    if platform_id is None:
        raise PlatformBindingError(400, "platform_id_required")
    return platform_id


def _platform_deletion_date(platform_id: str) -> str | None:
    """Confirm the binding and return the platform's deletion date to cache on a new row.

    Raises PlatformBindingError when the binding cannot be confirmed: the
    platform clock is unreadable (503 platform_unavailable), does not know the
    competition (404) or has deleted it (410) — nothing is stored then. Returns
    None when the platform has no date yet, and when PLATFORM_STORAGE_ACCOUNT is
    unset (local dev only: such rows are never deleted).
    """
    if not os.environ.get("PLATFORM_STORAGE_ACCOUNT"):
        logging.error("PLATFORM_STORAGE_ACCOUNT unset; storing the row without a deletion date")
        return None
    try:
        clock = read_platform_clock(platform_id)
    except PlatformClockUnavailable:
        logging.error("Platform clock unavailable; generation refused")
        raise PlatformBindingError(503, "platform_unavailable")
    if clock is None:
        raise PlatformBindingError(404, "platform_not_found")
    if clock.get("purged") or clock.get("status") == "deleted":
        raise PlatformBindingError(410, "platform_deleted")
    deletion = clock.get("deletionDate")
    return to_iso_z(deletion) if deletion is not None else None


def _sync_local_deletion_date(comp_table, entity, clock):
    """Copy the platform's deletion date onto the row when it differs, so list /
    details show the date the sweep will actually use. Best effort."""
    platform_date = clock.get("deletionDate") if clock else None
    if platform_date is None or _parse_iso_utc(entity.get("DeletionDate")) == platform_date:
        return entity
    new_str = to_iso_z(platform_date)
    try:
        comp_table.update_entity({
            "PartitionKey": "GLOBAL",
            "RowKey": entity["RowKey"],
            "DeletionDate": new_str,
        }, mode=UpdateMode.MERGE)
        entity["DeletionDate"] = new_str
    except Exception as e:
        logging.warning(f"Failed to sync DeletionDate for {entity['RowKey']}: {e}")
    return entity


def create_and_store_sas_link(blob_service_client, container_name, blob_name, competition, filename, file_size=0, description="Per-skater scores (PDF)"):
    """Create a 5-day read SAS for a generated blob, store it as a generatedpapers row, and return (url, expiry_iso)."""
    try:
        table_client = get_table_client()
        if not table_client:
            logging.warning("No table client available, skipping SAS creation")
            return None, None

        try:
            table_client.create_table()
        except Exception:
            pass

        start_time = _utcnow()
        expiry = start_time + timedelta(days=5)
        sas_token = ""

        account_name = blob_service_client.account_name
        # Percent-encode the path (folder names keep spaces/special chars per
        # sanitize_name). Without this the stored URL carries raw spaces, and a
        # browser re-encoding the whole URL double-encodes the SAS query, which
        # Azure rejects with "Signature fields not well formed". safe='/' keeps
        # the path separators intact; the SAS token below is already encoded.
        encoded_path = quote(f"{container_name}/{blob_name}", safe="/")
        blob_url_base = f"https://{account_name}.blob.core.windows.net/{encoded_path}"

        if os.environ.get("AzureWebJobsStorage__accountName"):
            ud_key = blob_service_client.get_user_delegation_key(start_time, expiry)
            sas_token = generate_blob_sas(
                account_name=account_name,
                container_name=container_name,
                blob_name=blob_name,
                user_delegation_key=ud_key,
                permission=BlobSasPermissions(read=True),
                expiry=expiry,
                start=start_time,
            )
        else:
            conn_str = os.environ.get("AzureWebJobsStorage")
            if conn_str:
                items = dict(item.split('=', 1) for item in conn_str.split(';') if '=' in item)
                key = items.get('AccountKey')
                if key:
                    sas_token = generate_blob_sas(
                        account_name=items.get('AccountName'),
                        container_name=container_name,
                        blob_name=blob_name,
                        account_key=key,
                        permission=BlobSasPermissions(read=True),
                        expiry=expiry,
                        start=start_time,
                    )

        if not sas_token:
            return None, None

        full_url = f"{blob_url_base}?{sas_token}"
        entity = {
            "PartitionKey": competition,
            "RowKey": filename.replace('/', '_').replace('\\', '_'),
            "Url": full_url,
            "ExpirationDate": expiry.isoformat(),
            "Description": description,
            "FileName": filename,
            "FileSize": int(file_size),
        }
        table_client.upsert_entity(entity)
        logging.info(f"Stored SAS link for {filename}")
        return full_url, expiry.isoformat()
    except Exception as e:
        logging.error(f"Error creating SAS: {e}")
        return None, None


def derive_name_and_meta(pdf_bytes: bytes):
    """Derive a human name + metadata from the report's page-0 text.

    Page-0 lines: 1='JUDGES DETAILS PER SKATER', 2=<segment> (e.g. 'SM NOVIISIT
    FREE SKATING'), then a 'printed: <date time>' line. Name = '<segment> — <printed>'.
    """
    segment, printed = "", ""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        lines = [l.strip() for l in doc[0].get_text().splitlines() if l.strip()]
        doc.close()
        if len(lines) > 1:
            segment = lines[1]
        for line in lines:
            m = re.search(r'printed:\s*(.+)$', line, re.IGNORECASE)
            if m:
                printed = m.group(1).strip()
                break
    except Exception as e:
        logging.warning(f"Could not derive name from PDF: {e}")
    parts = [p for p in (segment, printed) if p]
    name = " — ".join(parts) if parts else "Per-skater scores"
    return name, segment, printed


@app.route(route="generate", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def generate(req: func.HttpRequest) -> func.HttpResponse:
    """Split an uploaded 'JUDGES DETAILS PER SKATER' PDF into one team per page.

    Body = the PDF bytes. Query: includeRanks=true|false (default false -> hide
    non-podium ranks). Persists source + output in the tool's storage and a
    generatedpapers SAS link; returns the download URL.
    """
    logging.info('Generating per-skater PDF...')

    user = get_user(req)
    if not user:
        return func.HttpResponse("Unauthorized", status_code=401)
    if not is_user_allowed(user):
        return func.HttpResponse("Forbidden: You are not on the allow list.", status_code=403)

    include_ranks = (req.params.get('includeRanks', 'false').lower() == 'true')
    try:
        platform_id = _platform_id_param(req)
        _require_platform_access(user, platform_id, "write")
    except PlatformBindingError as e:
        return e.response()

    content_length = req.headers.get('Content-Length')
    if content_length and int(content_length) > MAX_UPLOAD_SIZE:
        return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)

    pdf_bytes = req.get_body()
    if not pdf_bytes:
        return func.HttpResponse("No file provided", status_code=400)
    if len(pdf_bytes) > MAX_UPLOAD_SIZE:
        return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)
    if not pdf_bytes[:5].startswith(b'%PDF'):
        return func.HttpResponse("Only PDF files are allowed", status_code=400)
    try:
        deletion_date = _platform_deletion_date(platform_id)
    except PlatformBindingError as e:
        return e.response()

    # Run the transform first so we fail fast on a wrong file type
    try:
        output_bytes = split_per_skater(pdf_bytes, hide_non_podium_ranks=not include_ranks)
    except NotPerSkaterReport:
        return func.HttpResponse(
            "This doesn't look like a 'JUDGES DETAILS PER SKATER' export. Please upload that report.",
            status_code=400,
        )
    except Exception as e:
        logging.error(f"Error splitting PDF: {e}", exc_info=True)
        return func.HttpResponse("Could not process the PDF. Check that it is a valid report.", status_code=500)

    name, segment, printed = derive_name_and_meta(pdf_bytes)
    try:
        pages = fitz.open(stream=output_bytes, filetype="pdf").page_count
    except Exception:
        pages = 0

    try:
        blob_service_client = get_blob_service_client()
        if not blob_service_client:
            return func.HttpResponse("Storage configuration not found", status_code=500)

        comp_table = get_table_client("competitions")
        if not comp_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)
        try:
            comp_table.create_table()
        except Exception:
            pass

        new_id = generate_competition_id(comp_table)
        safe = sanitize_name(name) or "scores"
        folder = f"{safe}-{new_id}"

        container = get_container_client(blob_service_client)
        container.upload_blob(f"{folder}/source.pdf", pdf_bytes, overwrite=True)
        out_blob = f"{folder}/output/{OUTPUT_FILENAME}"
        container.upload_blob(out_blob, output_bytes, overwrite=True)

        now = _utcnow()
        comp_table.create_entity({
            "PartitionKey": "GLOBAL",
            "RowKey": new_id,
            "Name": name,
            "FolderPath": folder,
            "Segment": segment,
            "PrintedDate": printed,
            "IncludeRanks": include_ranks,
            "OutputPages": pages,
            "Visible": True,
            "CreatedBy": user.email,
            "CreatedDate": f"{now.isoformat()}Z",
            "PlatformId": platform_id,
            "UploadedFileCount": 1,
            "GenerateRunCount": 1,
            # Cached copy of the platform's date; absent when it was unreadable.
            **({"DeletionDate": deletion_date} if deletion_date else {}),
        })

        download_url, expiration = create_and_store_sas_link(
            blob_service_client, CONTAINER_NAME, out_blob, new_id, OUTPUT_FILENAME, len(output_bytes)
        )

        return func.HttpResponse(json.dumps({
            "id": new_id,
            "name": name,
            "fileName": OUTPUT_FILENAME,
            "downloadUrl": download_url,
            "pages": pages,
            "expiration": expiration,
        }), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error storing generated PDF: {e}", exc_info=True)
        return func.HttpResponse("Error processing request. Check server logs for details.", status_code=500)


@app.route(route="parse_index", auth_level=func.AuthLevel.ANONYMOUS)
def parse_index(req: func.HttpRequest) -> func.HttpResponse:
    """Fetch + parse a competition index.htm URL.

    Returns competition name/date/venue and the category result pages
    (CAT###RS.htm). Fetching is restricted to INDEX_ALLOWED_HOSTS (SSRF guard).
    """
    if not get_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)

    url = req.params.get("url")
    if not url:
        return func.HttpResponse("Missing url parameter", status_code=400)

    try:
        html = fetch_index_html(url, allowed_hosts=INDEX_ALLOWED_HOSTS)
    except ValueError as e:
        return func.HttpResponse(str(e), status_code=400)
    except Exception as e:
        logging.warning(f"Could not fetch index page {url}: {e}")
        return func.HttpResponse("Could not fetch the index page.", status_code=502)

    meta = parse_index_html(html)
    return func.HttpResponse(json.dumps({
        "competition": meta.competition,
        "date": meta.date,
        "venue": meta.venue,
        "categories": [{"name": c.name, "catFile": c.cat_file} for c in meta.categories],
    }), mimetype="application/json")


@app.route(route="generate_results", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def generate_results(req: func.HttpRequest) -> func.HttpResponse:
    """Build a polished podium 'Tulokset' results page from an uploaded report.

    Body = the per-skater PDF. Query: competition, date, venue, category,
    supertitle, catFile, indexUrl (optional; fills blank fields + matches the
    CAT page). Produces a results PDF + a podium-only CAT###RS.htm, stores both,
    and returns their download URLs.
    """
    logging.info('Generating results summary...')

    user = get_user(req)
    if not user:
        return func.HttpResponse("Unauthorized", status_code=401)
    if not is_user_allowed(user):
        return func.HttpResponse("Forbidden: You are not on the allow list.", status_code=403)
    try:
        platform_id = _platform_id_param(req)
        _require_platform_access(user, platform_id, "write")
    except PlatformBindingError as e:
        return e.response()

    content_length = req.headers.get('Content-Length')
    if content_length and int(content_length) > MAX_UPLOAD_SIZE:
        return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)

    pdf_bytes = req.get_body()
    if not pdf_bytes:
        return func.HttpResponse("No file provided", status_code=400)
    if len(pdf_bytes) > MAX_UPLOAD_SIZE:
        return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)
    if not pdf_bytes[:5].startswith(b'%PDF'):
        return func.HttpResponse("Only PDF files are allowed", status_code=400)
    try:
        deletion_date = _platform_deletion_date(platform_id)
    except PlatformBindingError as e:
        return e.response()

    try:
        teams, segment = extract_results(pdf_bytes)
    except NotPerSkaterReport:
        return func.HttpResponse(
            "This doesn't look like a 'JUDGES DETAILS PER SKATER' export. Please upload that report.",
            status_code=400,
        )
    except Exception as e:
        logging.error(f"Error extracting results: {e}", exc_info=True)
        return func.HttpResponse("Could not read the PDF. Check that it is a valid report.", status_code=500)

    p = req.params
    competition = p.get("competition", "")
    date = p.get("date", "")
    venue = p.get("venue", "")
    category = p.get("category", "")
    supertitle = p.get("supertitle", "") or DEFAULT_SUPERTITLE
    cat_file = p.get("catFile", "")
    index_url = p.get("indexUrl", "")
    pdf_file = p.get("pdfFile", "")

    # Proper-case category name from the index (e.g. "Tulokkaat L1") for the
    # native CAT###RS.htm caption; the `category` field above is the PDF badge.
    category_full = ""
    if index_url:
        try:
            idx = parse_index_html(fetch_index_html(index_url, allowed_hosts=INDEX_ALLOWED_HOSTS))
            competition = competition or idx.competition
            date = date or idx.date
            venue = venue or idx.venue
            matched = match_category(idx, segment)
            if matched:
                category = category or matched.name
                cat_file = cat_file or matched.cat_file
            # Caption name: prefer the explicitly selected CAT page, else the match.
            selected = next((c for c in idx.categories if c.cat_file == cat_file), None) or matched
            if selected:
                category_full = selected.name
        except Exception as e:
            logging.warning(f"Could not use index URL {index_url}: {e}")

    meta = ResultsMeta(
        competition=competition,
        date=date,
        venue=venue,
        category=category or segment,
        supertitle=supertitle,
        team_count=len(teams),
        category_full=category_full,
    )

    try:
        results_pdf = render_results_pdf(meta, teams)
        results_html = render_results_html(meta, teams).encode("utf-8")
    except Exception as e:
        logging.error(f"Error rendering results: {e}", exc_info=True)
        return func.HttpResponse("Could not render the results page.", status_code=500)

    html_filename = cat_file or "results.htm"
    name = " — ".join([x for x in (meta.category, meta.competition) if x]) or "Results"

    # Download filename for the results PDF. Tulokkaat re-uses the standard FSM
    # export name; other categories default to a generic name but the operator
    # can override it (pdfFile). The blob is named accordingly so the SAS link
    # downloads with this filename.
    default_pdf_filename = (
        TULOKKAAT_PDF_FILENAME
        if meta.category.strip().upper() == "TULOKKAAT"
        else RESULTS_PDF_FILENAME
    )
    pdf_filename = _safe_pdf_filename(pdf_file, default_pdf_filename)

    try:
        blob_service_client = get_blob_service_client()
        if not blob_service_client:
            return func.HttpResponse("Storage configuration not found", status_code=500)

        comp_table = get_table_client("competitions")
        if not comp_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)
        try:
            comp_table.create_table()
        except Exception:
            pass

        new_id = generate_competition_id(comp_table)
        safe = sanitize_name(name) or "results"
        folder = f"{safe}-{new_id}"

        container = get_container_client(blob_service_client)
        container.upload_blob(f"{folder}/source.pdf", pdf_bytes, overwrite=True)
        pdf_blob = f"{folder}/output/{pdf_filename}"
        html_blob = f"{folder}/output/{html_filename}"
        container.upload_blob(pdf_blob, results_pdf, overwrite=True)
        container.upload_blob(html_blob, results_html, overwrite=True)

        now = _utcnow()
        comp_table.create_entity({
            "PartitionKey": "GLOBAL",
            "RowKey": new_id,
            "Name": name,
            "FolderPath": folder,
            "Segment": segment,
            "Competition": meta.competition,
            "CompetitionDate": meta.date,
            "Venue": meta.venue,
            "Category": meta.category,
            "Tool": "results",
            "CatFile": html_filename,
            "OutputPages": 1,
            "Visible": True,
            "CreatedBy": user.email,
            "CreatedDate": f"{now.isoformat()}Z",
            "PlatformId": platform_id,
            "UploadedFileCount": 1,
            "GenerateRunCount": 1,
            # Cached copy of the platform's date; absent when it was unreadable.
            **({"DeletionDate": deletion_date} if deletion_date else {}),
        })

        download_url, expiration = create_and_store_sas_link(
            blob_service_client, CONTAINER_NAME, pdf_blob, new_id, pdf_filename,
            len(results_pdf), description="Results summary (PDF)",
        )
        html_url, _ = create_and_store_sas_link(
            blob_service_client, CONTAINER_NAME, html_blob, new_id, html_filename,
            len(results_html), description="Podium results page (HTML)",
        )

        return func.HttpResponse(json.dumps({
            "id": new_id,
            "name": name,
            "fileName": pdf_filename,
            "downloadUrl": download_url,
            "htmlFileName": html_filename,
            "htmlUrl": html_url,
            "pages": 1,
            "expiration": expiration,
        }), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error storing results: {e}", exc_info=True)
        return func.HttpResponse("Error processing request. Check server logs for details.", status_code=500)


def _load_competition_for(user: User, comp_id: str, need: str):
    """The competitions row ``comp_id`` when the caller has ``need`` on its
    platform competition. An unknown record answers exactly like one the caller
    may not see (404 platform_not_found); see _require_platform_access."""
    entity = get_competition_entity(comp_id)
    if not entity:
        raise PlatformBindingError(404, "platform_not_found")
    _require_platform_access(user, entity.get("PlatformId"), need)
    return entity


def _readable_filter(user: User):
    """A predicate PlatformId -> bool: may the caller read records bound to it?

    Admins read everything (legacy rows without a PlatformId included). For
    everyone else the platform access tables are read lazily — one client pair
    per request, one lookup per distinct competition. Raises
    PlatformBindingError(503) when they cannot be read.
    """
    if user.is_admin:
        return lambda platform_id: True
    clients: dict = {}
    decided: dict = {}

    def readable(platform_id) -> bool:
        guid = normalize_platform_id(platform_id)
        if guid is None:
            return False
        if guid not in decided:
            if not clients:
                try:
                    clients["identities"] = platform_access.get_identities_table_client()
                    clients["access"] = platform_access.get_access_table_client()
                except platform_access.PlatformAccessUnavailable:
                    logging.error("Platform access tables unavailable; list refused")
                    raise PlatformBindingError(503, "platform_unavailable")
                except Exception as exc:
                    logging.error(f"Platform access client failed: {type(exc).__name__}")
                    raise PlatformBindingError(503, "platform_unavailable")
            role = _role_for(user, guid, identities_client=clients["identities"], access_client=clients["access"])
            decided[guid] = platform_access.role_satisfies(role, "read")
        return decided[guid]

    return readable


@app.route(route="list_competitions", auth_level=func.AuthLevel.ANONYMOUS)
def list_competitions(req: func.HttpRequest) -> func.HttpResponse:
    """List persisted competitions (history) the caller may read: records whose
    platform competition grants them at least read (admins: every record,
    including legacy ones without a PlatformId). Not yet used by the UI."""
    user = get_user(req)
    if not user:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        table_client = get_table_client("competitions")
        if not table_client:
            return func.HttpResponse("Storage configuration invalid", status_code=500)
        try:
            table_client.create_table()
        except Exception:
            pass

        readable = _readable_filter(user)
        competitions = []
        for entity in list(table_client.query_entities("PartitionKey eq 'GLOBAL'")):
            if entity.get("Visible") is False:
                continue
            if not readable(entity.get("PlatformId")):
                continue
            _ensure_deletion_date(table_client, entity)
            competitions.append({
                "id": entity["RowKey"],
                "name": entity.get("Name", entity["RowKey"]),
                "segment": entity.get("Segment", "-"),
                "createdBy": entity.get("CreatedBy", "-"),
                "createdDate": entity.get("CreatedDate", "-"),
                "deletionDate": entity.get("DeletionDate", "-"),
            })

        return func.HttpResponse(json.dumps(competitions), mimetype="application/json")
    except PlatformBindingError as e:
        return e.response()
    except Exception as e:
        logging.error(f"Error listing competitions: {e}")
        return func.HttpResponse(json.dumps({"error": "Internal server error"}), status_code=500, mimetype="application/json")


@app.route(route="get_competition_details", auth_level=func.AuthLevel.ANONYMOUS)
def get_competition_details(req: func.HttpRequest) -> func.HttpResponse:
    """Return a competition's stored fields + its generated-file links (read
    access on its platform competition). Not yet used by the UI."""
    user = get_user(req)
    if not user:
        return func.HttpResponse("Unauthorized", status_code=401)

    comp_id = req.params.get('id')
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)

    try:
        entity = _load_competition_for(user, comp_id, "read")
    except PlatformBindingError as e:
        return e.response()
    # Same effective legacy date as list_competitions and the sweep report.
    comp_table = get_table_client("competitions")
    if comp_table:
        _ensure_deletion_date(comp_table, entity)

    generated_links = []
    try:
        table_client = get_table_client()
        if table_client:
            safe_pk = comp_id.replace("'", "''")
            for row in table_client.query_entities(f"PartitionKey eq '{safe_pk}'"):
                generated_links.append({
                    "fileName": row.get("FileName"),
                    "url": row.get("Url"),
                    "description": row.get("Description"),
                    "expiration": row.get("ExpirationDate"),
                    "size": row.get("FileSize"),
                })
    except Exception as e:
        logging.warning(f"Could not fetch generated links: {e}")

    return func.HttpResponse(json.dumps({
        "id": comp_id,
        "name": entity.get("Name", comp_id),
        "segment": entity.get("Segment", "-"),
        "printedDate": entity.get("PrintedDate", "-"),
        "includeRanks": entity.get("IncludeRanks", False),
        "createdBy": entity.get("CreatedBy", "-"),
        "createdDate": entity.get("CreatedDate", "-"),
        "deletionDate": entity.get("DeletionDate", "-"),
        "platformId": entity.get("PlatformId") or None,
        "generatedFiles": generated_links,
    }), mimetype="application/json")


def _delete_competition_data(entity, deleted_by):
    """Delete a competition's blobs + generatedpapers rows, then soft-delete the competitions row.

    ``deleted_by`` is USER_DELETE_ACTOR or AUTO_CLEANUP_ACTOR, never an address:
    the soft-deleted row is kept, so it must not hold personal data (CreatedBy is
    blanked too).
    """
    comp_id = entity["RowKey"]
    folder_path = entity.get("FolderPath", comp_id)

    blob_service_client = get_blob_service_client()
    if not blob_service_client:
        raise RuntimeError("Storage configuration invalid")

    container = blob_service_client.get_container_client(CONTAINER_NAME)
    count = 0
    for blob in container.list_blobs(name_starts_with=f"{folder_path}/"):
        container.delete_blob(blob.name)
        count += 1

    try:
        table_client = get_table_client()
        if table_client:
            safe_pk = comp_id.replace("'", "''")
            for paper in table_client.query_entities(f"PartitionKey eq '{safe_pk}'"):
                table_client.delete_entity(partition_key=paper['PartitionKey'], row_key=paper['RowKey'])
    except Exception as table_err:
        logging.warning(f"Error deleting table entities: {table_err}")

    comp_table = get_table_client("competitions")
    comp_table.update_entity({
        "PartitionKey": "GLOBAL",
        "RowKey": comp_id,
        "Visible": False,
        "DeletedDate": f"{_utcnow().isoformat()}Z",
        "DeletedBy": deleted_by,
        "CreatedBy": "",
    }, mode=UpdateMode.MERGE)

    return count


def _scrub_hidden_rows(comp_table) -> int:
    """Blank CreatedBy/DeletedBy on soft-deleted rows that still hold an e-mail
    address (rows deleted before deletion started scrubbing them). Returns the
    number of rows changed."""
    scrubbed = 0
    for entity in list(comp_table.query_entities("PartitionKey eq 'GLOBAL'")):
        if entity.get("Visible") is not False:
            continue
        patch = {
            field: ""
            for field in ("CreatedBy", "DeletedBy")
            if entity.get(field) is not None and str(entity.get(field)) not in NON_PERSONAL_ACTORS
        }
        if not patch:
            continue
        try:
            comp_table.update_entity(
                {"PartitionKey": "GLOBAL", "RowKey": entity["RowKey"], **patch},
                mode=UpdateMode.MERGE,
            )
            scrubbed += 1
        except Exception as e:
            logging.error(f"Scrubbing deleted competition {entity['RowKey']} failed: {e}")
    return scrubbed


@app.route(route="delete_competition", auth_level=func.AuthLevel.ANONYMOUS)
def delete_competition(req: func.HttpRequest) -> func.HttpResponse:
    """Delete a competition (blobs + rows); write access on its platform
    competition. Not yet used by the UI."""
    user = get_user(req)
    if not user:
        return func.HttpResponse("Unauthorized", status_code=401)

    comp_id = req.params.get('id')
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)

    try:
        entity = _load_competition_for(user, comp_id, "write")
    except PlatformBindingError as e:
        return e.response()
    try:
        count = _delete_competition_data(entity, USER_DELETE_ACTOR)
        return func.HttpResponse(json.dumps({"deleted": count}), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error deleting competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.timer_trigger(schedule="0 0 4 * * *", arg_name="timer", run_on_startup=False)
def auto_delete_expired_competitions(timer: func.TimerRequest) -> None:
    """Daily sweep (04:00 UTC, after the platform's 03:00 sweep): delete expired competitions."""
    _run_cleanup_sweep(datetime.now(timezone.utc))


def _run_cleanup_sweep(now: datetime) -> int:
    """Delete competitions that are due, then scrub old soft-deleted rows.

    Bound rows (PlatformId) are deleted only on the platform's verdict
    (``platform_says_delete``: deleted or purged — never the date alone); their
    cached DeletionDate is refreshed otherwise. A bound row whose platform clock
    row is missing is kept with a warning. When the platform cannot be read
    (outage, missing table, PLATFORM_STORAGE_ACCOUNT unset) bound rows are
    skipped with an error and retried the next night. Legacy rows without a
    PlatformId are deleted on their stored DeletionDate. Returns the number of
    competitions deleted.
    """
    logging.info("Running auto-deletion sweep for expired competitions...")
    deleted = 0
    try:
        comp_table = get_table_client("competitions")
        if not comp_table:
            logging.error("Auto-deletion: storage configuration invalid")
            return 0

        # One platform table client (and credential) for the whole sweep.
        platform_client, platform_error = None, None
        try:
            platform_client = get_platform_table_client()
            if platform_client is None:
                platform_error = "PLATFORM_STORAGE_ACCOUNT unset"
        except Exception as exc:
            platform_error = f"platform clock unavailable ({type(exc).__name__})"
        clocks: dict[str, object] = {}  # platform id -> clock | None | PlatformClockUnavailable

        for entity in list(comp_table.query_entities("PartitionKey eq 'GLOBAL'")):
            if entity.get("Visible") is False:
                continue
            row_id = entity["RowKey"]
            platform_id = entity.get("PlatformId")

            if not platform_id:
                _ensure_deletion_date(comp_table, entity)
                deletion = _parse_iso_utc(entity.get("DeletionDate"))
                due = deletion is not None and deletion <= now
            elif platform_error:
                logging.error(f"Auto-deletion: {platform_error}; {row_id} skipped")
                continue
            else:
                if platform_id not in clocks:
                    try:
                        clocks[platform_id] = read_platform_clock(platform_id, table_client=platform_client)
                    except PlatformClockUnavailable as exc:
                        clocks[platform_id] = exc
                clock = clocks[platform_id]
                if isinstance(clock, PlatformClockUnavailable):
                    logging.error(f"Auto-deletion: platform clock unavailable for {row_id}; skipped until the next run")
                    continue
                if clock is None:
                    logging.warning(f"Auto-deletion: no platform clock row for {row_id}; kept")
                    continue
                due = platform_says_delete(clock)
                if not due:
                    _sync_local_deletion_date(comp_table, entity, clock)

            if not due:
                continue
            try:
                _delete_competition_data(entity, AUTO_CLEANUP_ACTOR)
                deleted += 1
                logging.info(f"Auto-deleted expired competition {row_id} ({entity.get('Name', '?')})")
            except Exception as e:
                logging.error(f"Auto-deletion failed for {row_id}: {e}")

        logging.info(f"Auto-deletion sweep complete. Deleted {deleted} competition(s).")
        try:
            scrubbed = _scrub_hidden_rows(comp_table)
            if scrubbed:
                logging.info(f"Scrubbed e-mail addresses from {scrubbed} deleted competition row(s).")
        except Exception as e:
            logging.error(f"Scrubbing deleted competitions failed: {e}")
    except Exception as e:
        logging.error(f"Auto-deletion sweep error: {e}")
    return deleted
