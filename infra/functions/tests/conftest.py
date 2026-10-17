"""Shared fixtures for the Score Modifier backend tests.

The Functions host loads ``function_app.py`` flat from ``infra/functions`` and
the deploy bundle copies the repo-root ``scoremodifier/`` package next to it,
so both directories go on ``sys.path`` the same way.

Storage is replaced by dict-backed fakes exposing just the surface
``function_app`` uses (blob container upload/list/delete, table
get/create/upsert/update/delete/query). ``AzureWebJobsStorage`` is set to a
dummy connection string so ``generate_blob_read_sas_url`` takes the account-key
branch and runs the real ``generate_blob_sas`` with no network.

Every request carries the router's headers by default (``make_request``): the
proxy secret (the gate fails closed, so the suite sets ``PROXY_SHARED_SECRET``)
and the test user's ``x-forwarded-user-tid/-oid/-email``. Platform access
(``platform_access.py``) reads in-memory ``useridentities`` /
``competitionaccess`` tables from the autouse ``access`` fixture; by default
the test user is owner of every competition (``access.grant_all``) — tests of
the role matrix set it to None and grant explicitly.

Real FSM "Judges Details Per Skater" exports are not committed (they carry
skaters' names). Tests that need one read ``SCOREMODIFIER_SAMPLE_DIR``
(default ``<repo>/../fs-samples/scoremodifier``) and skip when it is missing.
Expected layout, one directory per competition, exactly as the tool stores it:
``<name>/source.pdf`` plus optionally ``<name>/output/per-skater.pdf`` or
``<name>/output/results.pdf`` as golden outputs from a deployed backend.
"""

from __future__ import annotations

import base64
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FUNC_DIR = HERE.parent
REPO_ROOT = FUNC_DIR.parent.parent
for p in (str(FUNC_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ACCOUNT = "teststorage"
DUMMY_KEY = base64.b64encode(b"\x00" * 32).decode()
os.environ["AzureWebJobsStorage"] = (
    f"DefaultEndpointsProtocol=https;AccountName={DUMMY_ACCOUNT};"
    f"AccountKey={DUMMY_KEY};EndpointSuffix=core.windows.net"
)
os.environ.pop("AzureWebJobsStorage__accountName", None)
os.environ.pop("PROXY_SHARED_SECRET", None)
os.environ.pop("PLATFORM_STORAGE_ACCOUNT", None)

import fitz  # noqa: E402
import pytest  # noqa: E402
import azure.functions as func  # noqa: E402
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError  # noqa: E402

import function_app as fa  # noqa: E402
import platform_access  # noqa: E402
import platform_clock  # noqa: E402

TEST_EMAIL = "tester@example.com"
TEST_SECRET = "test-proxy-secret"
TEST_TID = "11111111-2222-4333-8444-555555555555"
TEST_OID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
TEST_USER_ID = "99999999-8888-4777-8666-555555555555"
# The site's active competition (platformId is required on every generation).
TEST_PLATFORM_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


# ── dict-backed stand-ins for blob + table storage ────────────────────────────

class _BlobProps:
    def __init__(self, name: str):
        self.name = name


class FakeContainerClient:
    def __init__(self, blobs: dict):
        self.blobs = blobs

    def create_container(self):
        raise ResourceExistsError("already exists")

    def upload_blob(self, name, data, overwrite=False):
        if name in self.blobs and not overwrite:
            raise ResourceExistsError(name)
        self.blobs[name] = bytes(data) if isinstance(data, (bytes, bytearray)) else str(data).encode()

    def list_blobs(self, name_starts_with=""):
        return [_BlobProps(n) for n in sorted(self.blobs) if n.startswith(name_starts_with)]

    def delete_blob(self, name):
        self.blobs.pop(name)


class FakeBlobServiceClient:
    account_name = DUMMY_ACCOUNT

    def __init__(self):
        self.blobs: dict[str, bytes] = {}

    def get_container_client(self, name):
        assert name == fa.CONTAINER_NAME, name
        return FakeContainerClient(self.blobs)

    def get_user_delegation_key(self, start, expiry):  # managed-identity path only
        raise AssertionError("tests run the account-key SAS path")


_PK_FILTER = re.compile(r"PartitionKey eq '((?:[^']|'')*)'")


class FakeTableClient:
    def __init__(self, rows: dict):
        self.rows = rows  # (PartitionKey, RowKey) -> entity dict

    @staticmethod
    def _key(entity):
        return entity["PartitionKey"], entity["RowKey"]

    def create_table(self):
        raise ResourceExistsError("already exists")

    def get_entity(self, partition_key, row_key):
        try:
            return dict(self.rows[(partition_key, row_key)])
        except KeyError:
            raise ResourceNotFoundError(f"{partition_key}/{row_key}") from None

    def create_entity(self, entity):
        k = self._key(entity)
        if k in self.rows:
            raise ResourceExistsError(str(k))
        self.rows[k] = dict(entity)

    def upsert_entity(self, entity, mode=None):
        self.rows[self._key(entity)] = dict(entity)

    def update_entity(self, entity, mode=None):
        k = self._key(entity)
        if k not in self.rows:
            raise ResourceNotFoundError(str(k))
        self.rows[k].update(entity)

    def delete_entity(self, partition_key, row_key):
        self.rows.pop((partition_key, row_key), None)

    def query_entities(self, query_filter):
        m = _PK_FILTER.fullmatch(query_filter)
        assert m, f"fake table only supports PartitionKey filters, got {query_filter!r}"
        pk = m.group(1).replace("''", "'")
        return [dict(v) for (p, _), v in self.rows.items() if p == pk]


class FakeStorage:
    """The tool's whole storage account in memory."""

    def __init__(self):
        self.blob_service = FakeBlobServiceClient()
        self.tables: dict[str, dict] = {}

    def table(self, name="generatedpapers") -> FakeTableClient:
        return FakeTableClient(self.tables.setdefault(name, {}))

    @property
    def blobs(self) -> dict[str, bytes]:
        return self.blob_service.blobs

    @property
    def competitions(self) -> dict:
        return self.tables.setdefault("competitions", {})

    @property
    def papers(self) -> dict:
        return self.tables.setdefault("generatedpapers", {})


@pytest.fixture(autouse=True)
def storage(monkeypatch) -> FakeStorage:
    st = FakeStorage()
    monkeypatch.setattr(fa, "get_blob_service_client", lambda: st.blob_service)
    monkeypatch.setattr(fa, "get_table_client", lambda table_name="generatedpapers": st.table(table_name))
    monkeypatch.setenv("PROXY_SHARED_SECRET", TEST_SECRET)
    # Platform clock off unless a test opts in with the `platform` fixture.
    monkeypatch.delenv("PLATFORM_STORAGE_ACCOUNT", raising=False)
    return st


class _PlatformTableClient:
    """One client on the platform `retentionclock` table; behaviour follows the
    FakePlatform's switches at read time."""

    def __init__(self, pf):
        self.pf = pf

    def get_entity(self, partition_key, row_key):
        pf = self.pf
        pf.reads += 1
        if pf.unreachable:
            raise HttpResponseError("platform registry unreachable")
        if pf.table_missing:
            exc = ResourceNotFoundError("The table specified does not exist. ErrorCode:TableNotFound")
            exc.error_code = "TableNotFound"
            raise exc
        try:
            return dict(pf.rows[(partition_key, row_key)])
        except KeyError:
            exc = ResourceNotFoundError("The specified resource does not exist. ErrorCode:ResourceNotFound")
            exc.error_code = "ResourceNotFound"
            raise exc from None


class FakePlatform:
    """The site's platform clock table (`retentionclock`, PK "COMPETITION", RK guid)."""

    def __init__(self):
        self.rows: dict = {}
        self.unreachable = False
        self.table_missing = False
        self.clients = 0  # get_platform_table_client() calls
        self.reads = 0  # get_entity() calls

    def add(self, guid, *, deletion, status="active", purged=None):
        row = {"PartitionKey": "COMPETITION", "RowKey": guid.lower(), "Status": status, "DeletionDate": deletion}
        if purged:
            row["PurgedUtc"] = purged
        self.rows[("COMPETITION", guid.lower())] = row
        return row

    def client(self):
        self.clients += 1
        return _PlatformTableClient(self)


@pytest.fixture
def platform(monkeypatch) -> FakePlatform:
    """Turn the platform clock on, backed by an in-memory registry."""
    pf = FakePlatform()
    monkeypatch.setenv("PLATFORM_STORAGE_ACCOUNT", "stfsplattest")
    monkeypatch.setattr(platform_clock, "get_platform_table_client", pf.client)
    monkeypatch.setattr(fa, "get_platform_table_client", pf.client)  # the sweep's one client
    return pf


class _AccessTableClient:
    """One client on a platform access table; behaviour follows FakeAccess at read time."""

    def __init__(self, fa_, table):
        self.fa = fa_
        self.table = table

    def get_entity(self, partition_key, row_key):
        acc = self.fa
        acc.reads += 1
        if acc.unreachable:
            raise HttpResponseError("platform access unreachable")
        if acc.table_missing:
            exc = ResourceNotFoundError("The table specified does not exist. ErrorCode:TableNotFound")
            exc.error_code = "TableNotFound"
            raise exc
        if self.table == "identities":
            user_id = acc.identities.get(row_key) if partition_key == "IDENTITY" else None
            if user_id:
                return {"PartitionKey": partition_key, "RowKey": row_key, "UserId": user_id}
        else:
            role = acc.grants.get((partition_key, row_key))
            if role is None and acc.grant_all and row_key in acc.identities.values():
                role = acc.grant_all
            if role:
                return {"PartitionKey": partition_key, "RowKey": row_key, "Role": role}
        exc = ResourceNotFoundError("The specified resource does not exist. ErrorCode:ResourceNotFound")
        exc.error_code = "ResourceNotFound"
        raise exc


class FakeAccess:
    """The platform's `useridentities` + `competitionaccess` tables."""

    def __init__(self):
        self.identities = {f"{TEST_TID}_{TEST_OID}": TEST_USER_ID}  # "<tid>_<oid>" -> userId
        self.grants: dict = {}  # (competition guid, userId) -> role
        self.grant_all = "owner"  # role every known user has on every competition; None = explicit grants only
        self.unreachable = False
        self.table_missing = False
        self.reads = 0
        self.clients = 0

    def add_user(self, tid, oid, user_id):
        self.identities[f"{tid}_{oid}"] = user_id

    def grant(self, guid, role, user_id=TEST_USER_ID):
        self.grants[(guid.lower(), user_id)] = role

    def identities_client(self):
        self.clients += 1
        return _AccessTableClient(self, "identities")

    def access_client(self):
        self.clients += 1
        return _AccessTableClient(self, "access")


@pytest.fixture(autouse=True)
def access(monkeypatch) -> FakeAccess:
    acc = FakeAccess()
    platform_access.clear_cache()
    monkeypatch.setattr(platform_access, "get_identities_table_client", acc.identities_client)
    monkeypatch.setattr(platform_access, "get_access_table_client", acc.access_client)
    yield acc
    platform_access.clear_cache()


# ── requests ──────────────────────────────────────────────────────────────────

def make_request(route, *, body=b"", params=None, headers=None, method="POST", email=TEST_EMAIL,
                 tid=TEST_TID, oid=TEST_OID, admin=False, secret=TEST_SECRET, anonymous=False):
    """A real azure.functions.HttpRequest the way the router proxy sends it.
    ``anonymous=True`` drops every identity header (the secret stays)."""
    h = {}
    if secret is not None:
        h["x-proxy-secret"] = secret
    if not anonymous:
        if tid:
            h["x-forwarded-user-tid"] = tid
        if oid:
            h["x-forwarded-user-oid"] = oid
        if email:
            h["x-forwarded-user-email"] = email
        if admin:
            h["x-forwarded-user-admin"] = "1"
    h.update(headers or {})
    return func.HttpRequest(
        method=method,
        url=f"http://localhost:7071/api/{route}",
        headers=h,
        params=dict(params or {}),
        body=body,
    )


def call(handler, req):
    """Invoke a decorated handler: ``@app.route`` hands back a FunctionBuilder,
    so unwrap to the user function the way the sibling tools' suites do."""
    return handler._function.get_user_function()(req)


# ── sample data ───────────────────────────────────────────────────────────────

SAMPLE_DIR = Path(os.environ.get("SCOREMODIFIER_SAMPLE_DIR", REPO_ROOT.parent / "fs-samples" / "scoremodifier"))


def sample_dirs() -> list[Path]:
    if not SAMPLE_DIR.is_dir():
        return []
    return sorted(d for d in SAMPLE_DIR.iterdir() if (d / "source.pdf").is_file())


def sample_ids() -> list[str]:
    return [d.name for d in sample_dirs()]


@pytest.fixture(params=sample_dirs() or [None], ids=sample_ids() or ["no-samples"])
def sample(request) -> Path:
    """One sample competition directory; skips when no samples are available."""
    if request.param is None:
        pytest.skip(f"no FSM sample exports under {SAMPLE_DIR} (set SCOREMODIFIER_SAMPLE_DIR)")
    return request.param


@pytest.fixture
def sample_pdf(sample) -> bytes:
    return (sample / "source.pdf").read_bytes()


@pytest.fixture
def synthetic_pdf() -> bytes:
    """A valid PDF that is not a Judges Details Per Skater export."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Quarterly sales report")
    page.insert_text((72, 96), "Nothing to do with figure skating.")
    data = doc.tobytes()
    doc.close()
    return data


# ── helpers shared by several test modules ────────────────────────────────────

_STAMP_RE = re.compile(r"\d{1,2}[./]\d{1,2}[./]\d{4}|\d{1,2}:\d{2}")


def page_texts(pdf_bytes: bytes) -> list[str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [p.get_text() for p in doc]
    finally:
        doc.close()


def page_sizes(pdf_bytes: bytes) -> list[tuple[int, int]]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [(round(p.rect.width), round(p.rect.height)) for p in doc]
    finally:
        doc.close()


def stable_lines(text: str) -> list[str]:
    """Non-empty lines with anything carrying a date or clock time dropped, so
    'generated at' stamps don't make two otherwise identical renders differ."""
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not _STAMP_RE.search(ln)]


@pytest.fixture
def team_sample_pdf(sample_pdf) -> bytes:
    """A sample the *results* tool can read. Singles-category exports (one
    skater, no team row) split fine per skater but carry no parseable result
    rows — a known limitation of the results tool, not a regression — so the
    results-based tests skip them."""
    from scoremodifier.extract import extract_results
    from scoremodifier.per_skater import NotPerSkaterReport

    try:
        extract_results(sample_pdf)
    except NotPerSkaterReport:
        pytest.skip("results tool cannot read this export (singles category); per-skater split still covered")
    return sample_pdf


def teams_or_none(pdf_bytes: bytes):
    """Result rows when the results tool can read the export, else None."""
    from scoremodifier.extract import extract_results
    from scoremodifier.per_skater import NotPerSkaterReport

    try:
        return extract_results(pdf_bytes)[0]
    except NotPerSkaterReport:
        return None


def rank_anchor_count(pdf_bytes: bytes) -> int:
    """How many skater/team blocks the export holds: each block's 3-line column
    header carries exactly one ``Rank`` word (the anchor the splitter uses)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return sum(1 for page in doc for w in page.get_text("words") if w[4] == "Rank")
    finally:
        doc.close()
