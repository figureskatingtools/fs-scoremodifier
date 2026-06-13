from __future__ import annotations

import azure.functions as func
import logging
import os
import base64
import json
import re

import fitz  # PyMuPDF — used to derive the competition name and count output pages
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
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

# Automatic competition deletion: lifetime after creation and the per-extend
# bump. Rows missing a DeletionDate get backfilled with the migration date.
DELETION_RETENTION_DAYS = 30
DELETION_EXTENSION_DAYS = 7
LEGACY_DELETION_DATE = "2026-06-12T00:00:00Z"
AUTO_CLEANUP_ACTOR = "auto-cleanup"


def is_user_allowed(email: str) -> bool:
    """
    Check if the user is allowed to perform sensitive operations.
    Policy for v1.0.0: All authenticated Entra ID users are allowed.
    To restrict access, implement an allowlist here (e.g. from Table Storage or env var).
    """
    return True


def _proxy_secret_ok(req: func.HttpRequest) -> bool:
    """
    Verify the request came from the Web App proxy by checking the shared
    secret header. The function endpoint is public, so this prevents anyone
    from spoofing the X-Forwarded-User-Email header directly.

    Enforced only when PROXY_SHARED_SECRET is set (so local dev and any
    brief pre-rollout window fail open rather than locking everyone out).
    """
    expected = os.environ.get("PROXY_SHARED_SECRET")
    if not expected:
        return True
    provided = req.headers.get("X-Proxy-Secret") or req.headers.get("x-proxy-secret")
    return provided == expected


def _decode_jwt_payload(token: str) -> dict | None:
    """Decode the payload of a JWT token without verification (base64 only)."""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        payload_b64 += '=' * (4 - len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception as e:
        logging.error(f"Error decoding JWT payload: {e}")
        return None


def get_user_email_from_header(req: func.HttpRequest) -> str | None:
    """
    Extracts the user email from the X-MS-CLIENT-PRINCIPAL header injected by
    Azure App Service Auth, the X-Forwarded-User-Email header set by the Web App
    proxy (server.js), or the Authorization Bearer JWT token.
    """
    # 0. Reject requests that didn't come through the Web App proxy
    if not _proxy_secret_ok(req):
        logging.warning("Proxy shared secret missing or mismatched; rejecting request")
        return None

    # 1. Direct Easy Auth header
    val = req.headers.get("X-MS-CLIENT-PRINCIPAL-NAME") or req.headers.get("x-ms-client-principal-name")
    if val:
        return val

    # 2. Forwarded header from Web App proxy (server.js)
    forwarded = req.headers.get("X-Forwarded-User-Email") or req.headers.get("x-forwarded-user-email")
    if forwarded:
        return forwarded

    # 3. Base64 SWA principal header
    header = req.headers.get("x-ms-client-principal") or req.headers.get("X-MS-CLIENT-PRINCIPAL")
    if header:
        try:
            decoded = base64.b64decode(header).decode("utf-8")
            principal = json.loads(decoded)
            return principal.get("userDetails")
        except Exception as e:
            logging.error(f"Error parsing auth header: {e}")

    # 4. Authorization Bearer token (JWT)
    auth_header = req.headers.get("Authorization") or req.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:]
        claims = _decode_jwt_payload(token)
        if claims:
            email = claims.get("preferred_username") or claims.get("email") or claims.get("upn") or claims.get("unique_name")
            if not email:
                emails = claims.get("emails")
                if isinstance(emails, list) and emails:
                    email = emails[0]
            if not email:
                email = claims.get("name") or claims.get("oid")
            if email:
                return email

    safe_headers = {k: v for k, v in req.headers.items() if 'auth' not in k.lower() and 'cookie' not in k.lower()}
    logging.warning(f"No Identity found. Safe Headers: {safe_headers}")
    return None


@app.route(route="check_user_permission", auth_level=func.AuthLevel.ANONYMOUS)
def check_user_permission(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Checking user permission...')
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse(json.dumps({"allowed": False, "email": None}), mimetype="application/json", status_code=401)
    return func.HttpResponse(json.dumps({"allowed": True, "email": email}), mimetype="application/json")


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
    """Lazy migration: backfill a fixed DeletionDate on rows created before this feature."""
    if entity.get("DeletionDate"):
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

        start_time = datetime.utcnow()
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

    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
    if not is_user_allowed(email):
        return func.HttpResponse("Forbidden: You are not on the allow list.", status_code=403)

    include_ranks = (req.params.get('includeRanks', 'false').lower() == 'true')

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

        now = datetime.utcnow()
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
            "CreatedBy": email,
            "CreatedDate": f"{now.isoformat()}Z",
            "DeletionDate": f"{(now + timedelta(days=DELETION_RETENTION_DAYS)).isoformat()}Z",
            "UploadedFileCount": 1,
            "GenerateRunCount": 1,
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
    email = get_user_email_from_header(req)
    if not email:
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

    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
    if not is_user_allowed(email):
        return func.HttpResponse("Forbidden: You are not on the allow list.", status_code=403)

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
        except Exception as e:
            logging.warning(f"Could not use index URL {index_url}: {e}")

    meta = ResultsMeta(
        competition=competition,
        date=date,
        venue=venue,
        category=category or segment,
        supertitle=supertitle,
        team_count=len(teams),
    )

    try:
        results_pdf = render_results_pdf(meta, teams)
        results_html = render_results_html(meta, teams).encode("utf-8")
    except Exception as e:
        logging.error(f"Error rendering results: {e}", exc_info=True)
        return func.HttpResponse("Could not render the results page.", status_code=500)

    html_filename = cat_file or "results.htm"
    name = " — ".join([x for x in (meta.category, meta.competition) if x]) or "Results"

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
        pdf_blob = f"{folder}/output/{RESULTS_PDF_FILENAME}"
        html_blob = f"{folder}/output/{html_filename}"
        container.upload_blob(pdf_blob, results_pdf, overwrite=True)
        container.upload_blob(html_blob, results_html, overwrite=True)

        now = datetime.utcnow()
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
            "CreatedBy": email,
            "CreatedDate": f"{now.isoformat()}Z",
            "DeletionDate": f"{(now + timedelta(days=DELETION_RETENTION_DAYS)).isoformat()}Z",
            "UploadedFileCount": 1,
            "GenerateRunCount": 1,
        })

        download_url, expiration = create_and_store_sas_link(
            blob_service_client, CONTAINER_NAME, pdf_blob, new_id, RESULTS_PDF_FILENAME,
            len(results_pdf), description="Results summary (PDF)",
        )
        html_url, _ = create_and_store_sas_link(
            blob_service_client, CONTAINER_NAME, html_blob, new_id, html_filename,
            len(results_html), description="Podium results page (HTML)",
        )

        return func.HttpResponse(json.dumps({
            "id": new_id,
            "name": name,
            "fileName": RESULTS_PDF_FILENAME,
            "downloadUrl": download_url,
            "htmlFileName": html_filename,
            "htmlUrl": html_url,
            "pages": 1,
            "expiration": expiration,
        }), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error storing results: {e}", exc_info=True)
        return func.HttpResponse("Error processing request. Check server logs for details.", status_code=500)


@app.route(route="list_competitions", auth_level=func.AuthLevel.ANONYMOUS)
def list_competitions(req: func.HttpRequest) -> func.HttpResponse:
    """List persisted competitions (history). Not yet used by the UI."""
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        table_client = get_table_client("competitions")
        if not table_client:
            return func.HttpResponse("Storage configuration invalid", status_code=500)
        try:
            table_client.create_table()
        except Exception:
            pass

        competitions = []
        for entity in list(table_client.query_entities("PartitionKey eq 'GLOBAL'")):
            if entity.get("Visible") is False:
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
    except Exception as e:
        logging.error(f"Error listing competitions: {e}")
        return func.HttpResponse(json.dumps({"error": "Internal server error"}), status_code=500, mimetype="application/json")


@app.route(route="get_competition_details", auth_level=func.AuthLevel.ANONYMOUS)
def get_competition_details(req: func.HttpRequest) -> func.HttpResponse:
    """Return a competition's stored fields + its generated-file links. Not yet used by the UI."""
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    comp_id = req.params.get('id')
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)

    entity = get_competition_entity(comp_id)
    if not entity:
        return func.HttpResponse("Competition not found", status_code=404)

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
        "generatedFiles": generated_links,
    }), mimetype="application/json")


def _delete_competition_data(entity, deleted_by):
    """Delete a competition's blobs + generatedpapers rows, then soft-delete the competitions row."""
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
        "DeletedDate": f"{datetime.utcnow().isoformat()}Z",
        "DeletedBy": deleted_by,
    }, mode=UpdateMode.MERGE)

    return count


@app.route(route="delete_competition", auth_level=func.AuthLevel.ANONYMOUS)
def delete_competition(req: func.HttpRequest) -> func.HttpResponse:
    """Delete a competition (blobs + rows). Not yet used by the UI."""
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    comp_id = req.params.get('id')
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)

    try:
        entity = get_competition_entity(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)
        count = _delete_competition_data(entity, email)
        return func.HttpResponse(json.dumps({"deleted": count}), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error deleting competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.timer_trigger(schedule="0 0 3 * * *", arg_name="timer", run_on_startup=False)
def auto_delete_expired_competitions(timer: func.TimerRequest) -> None:
    """Daily sweep (03:00 UTC): delete competitions whose DeletionDate has passed."""
    logging.info("Running auto-deletion sweep for expired competitions...")
    try:
        comp_table = get_table_client("competitions")
        if not comp_table:
            logging.error("Auto-deletion: storage configuration invalid")
            return

        now = datetime.now(timezone.utc)
        deleted = 0
        for entity in list(comp_table.query_entities("PartitionKey eq 'GLOBAL'")):
            if entity.get("Visible") is False:
                continue
            _ensure_deletion_date(comp_table, entity)
            deletion = _parse_iso_utc(entity.get("DeletionDate"))
            if deletion is None or deletion > now:
                continue
            try:
                _delete_competition_data(entity, AUTO_CLEANUP_ACTOR)
                deleted += 1
                logging.info(f"Auto-deleted expired competition {entity['RowKey']} ({entity.get('Name', '?')})")
            except Exception as e:
                logging.error(f"Auto-deletion failed for {entity['RowKey']}: {e}")

        logging.info(f"Auto-deletion sweep complete. Deleted {deleted} competition(s).")
    except Exception as e:
        logging.error(f"Auto-deletion sweep error: {e}")
