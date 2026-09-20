"""Shared fixtures for the Score Modifier backend tests.

The Functions host loads ``function_app.py`` flat from ``infra/functions`` and
the deploy bundle copies the repo-root ``scoremodifier/`` package next to it,
so both directories go on ``sys.path`` the same way.

Storage is replaced by dict-backed fakes exposing just the surface
``function_app`` uses (blob container upload/list/delete, table
get/create/upsert/update/delete/query). ``AzureWebJobsStorage`` is set to a
dummy connection string so ``create_and_store_sas_link`` takes the account-key
branch and runs the real ``generate_blob_sas`` with no network.

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

import fitz  # noqa: E402
import pytest  # noqa: E402
import azure.functions as func  # noqa: E402
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError  # noqa: E402

import function_app as fa  # noqa: E402

TEST_EMAIL = "tester@example.com"


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

    def upsert_entity(self, entity):
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
    monkeypatch.delenv("PROXY_SHARED_SECRET", raising=False)
    return st


# ── requests ──────────────────────────────────────────────────────────────────

def make_request(route, *, body=b"", params=None, headers=None, method="POST", email=TEST_EMAIL):
    """A real azure.functions.HttpRequest the way the router proxy sends it."""
    h = {}
    if email:
        h["x-forwarded-user-email"] = email
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
