"""Links to generated files: only the blob name is stored; every response that
lists or returns files mints a fresh read-only SAS valid 15 minutes, after the
access check. Rows written by older versions held a 5-day SAS URL — it is
never returned again, only its blob name is used."""

import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from azure.storage.blob import UserDelegationKey

from conftest import DUMMY_KEY, TEST_PLATFORM_ID, call, make_request

import function_app as fa

GUID = TEST_PLATFORM_ID
COMP_ID = "aaaaaaaa"
FOLDER = f"Spring Cup-{COMP_ID}"
OLD_URL = (f"https://teststorage.blob.core.windows.net/{fa.CONTAINER_NAME}/Spring%20Cup-{COMP_ID}/output/"
           "results.pdf?se=2026-11-12T10%3A00%3A00Z&sp=r&sig=OLDSIG")


@pytest.fixture
def seeded(storage):
    storage.competitions[("GLOBAL", COMP_ID)] = {
        "PartitionKey": "GLOBAL", "RowKey": COMP_ID, "Name": "Spring Cup", "FolderPath": FOLDER,
        "CreatedBy": "x@y", "CreatedDate": "2026-10-01T00:00:00Z", "Visible": True, "PlatformId": GUID,
    }
    storage.papers[(COMP_ID, "per-skater.pdf")] = {
        "PartitionKey": COMP_ID, "RowKey": "per-skater.pdf", "FileName": "per-skater.pdf",
        "BlobName": f"{FOLDER}/output/per-skater.pdf"}
    # Written by an older version: a stored 5-day SAS URL.
    storage.papers[(COMP_ID, "results.pdf")] = {
        "PartitionKey": COMP_ID, "RowKey": "results.pdf", "FileName": "results.pdf",
        "Url": OLD_URL, "ExpirationDate": "2026-11-12T10:00:00"}
    # No BlobName, no Url: the default <folder>/output/<FileName>.
    storage.papers[(COMP_ID, "results.htm")] = {
        "PartitionKey": COMP_ID, "RowKey": "results.htm", "FileName": "results.htm"}
    return storage


def _details(**kw):
    resp = call(fa.get_competition_details, make_request("get_competition_details", method="GET", params={"id": COMP_ID}, **kw))
    return resp, (json.loads(resp.get_body()) if resp.status_code == 200 else None)


def _by_file(body):
    return {f["fileName"]: f for f in body["generatedFiles"]}


@pytest.mark.parametrize("role", ["read", "write", "owner"])
def test_details_mint_fresh_15_minute_links(role, seeded, access):
    access.grant_all = None
    access.grant(GUID, role)
    resp, body = _details()
    assert resp.status_code == 200
    files = _by_file(body)
    assert set(files) == {"per-skater.pdf", "results.pdf", "results.htm"}
    for name, f in files.items():
        url = urlparse(f["url"])
        assert url.path == f"/{fa.CONTAINER_NAME}/Spring%20Cup-{COMP_ID}/output/{name}"
        query = parse_qs(url.query)
        assert query["sp"] == ["r"]
        assert f["expiration"] == query["se"][0]
        expiry = fa._parse_iso_utc(f["expiration"]).replace(tzinfo=None)
        assert timedelta(minutes=14) <= expiry - fa._utcnow() <= timedelta(minutes=16)
    assert "OLDSIG" not in json.dumps(body) and "2026-11-12" not in json.dumps(body)


def test_a_link_never_points_outside_the_records_folder(seeded):
    seeded.papers[(COMP_ID, "evil.pdf")] = {
        "PartitionKey": COMP_ID, "RowKey": "evil.pdf", "FileName": "evil.pdf",
        "BlobName": "Other-bbbbbbbb/output/evil.pdf"}
    seeded.papers[(COMP_ID, "evil2.pdf")] = {
        "PartitionKey": COMP_ID, "RowKey": "evil2.pdf", "FileName": "evil2.pdf",
        "Url": f"https://x.blob.core.windows.net/{fa.CONTAINER_NAME}/Other-bbbbbbbb/output/x.pdf?sig=1"}
    seeded.papers[(COMP_ID, "nothing")] = {
        "PartitionKey": COMP_ID, "RowKey": "nothing", "BlobName": "Other-bbbbbbbb/source.pdf"}
    _, body = _details()
    files = _by_file(body)
    for name in ("evil.pdf", "evil2.pdf"):
        assert urlparse(files[name]["url"]).path == f"/{fa.CONTAINER_NAME}/Spring%20Cup-{COMP_ID}/output/{name}"
    assert files[None]["url"] is None and files[None]["expiration"] is None
    assert "Other" not in json.dumps(body)


def test_no_link_is_signed_before_the_access_check(seeded, access, monkeypatch):
    signed = []
    monkeypatch.setattr(fa, "generate_blob_read_sas_url", lambda *a, **kw: signed.append(a) or "u")
    access.grant_all = None
    resp, _ = _details()
    assert resp.status_code == 404
    assert signed == []


def _delegation_key(start, expiry):
    key = UserDelegationKey()
    key.signed_oid = key.signed_tid = "00000000-0000-0000-0000-000000000000"
    key.signed_start = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    key.signed_expiry = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
    key.signed_service, key.signed_version = "b", "2021-08-06"
    key.value = DUMMY_KEY
    return key


def test_managed_identity_fetches_one_delegation_key_per_response(seeded, storage, monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage__accountName", "teststorage")
    requested = []
    monkeypatch.setattr(storage.blob_service, "get_user_delegation_key",
                        lambda start, expiry: requested.append((start, expiry)) or _delegation_key(start, expiry))
    _, body = _details()
    assert len(requested) == 1
    start, expiry = requested[0]
    assert expiry - start == timedelta(minutes=20)  # 5 min skew allowance + 15 min
    assert all(f["url"] and "skoid=" in f["url"] for f in body["generatedFiles"])


def test_a_signing_failure_leaves_the_links_empty(seeded, storage, monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage__accountName", "teststorage")

    def broken(start, expiry):
        raise RuntimeError("no delegation key")

    monkeypatch.setattr(storage.blob_service, "get_user_delegation_key", broken)
    resp, body = _details()
    assert resp.status_code == 200
    assert all(f["url"] is None and f["expiration"] is None for f in body["generatedFiles"])


def test_regenerating_over_an_old_row_drops_its_stored_url(storage):
    storage.papers[("x", "per-skater.pdf")] = {"PartitionKey": "x", "RowKey": "per-skater.pdf", "Url": OLD_URL,
                                               "ExpirationDate": "2026-11-12T10:00:00"}
    fa.store_generated_file("F-x/output/per-skater.pdf", "x", "per-skater.pdf", 3)
    assert storage.papers[("x", "per-skater.pdf")] == {
        "PartitionKey": "x", "RowKey": "per-skater.pdf", "BlobName": "F-x/output/per-skater.pdf",
        "Description": "Per-skater scores (PDF)", "FileName": "per-skater.pdf", "FileSize": 3}


def test_a_failed_history_row_still_returns_a_link(synthetic_pdf, storage, monkeypatch):
    monkeypatch.setattr(fa, "split_per_skater", lambda pdf, **kw: synthetic_pdf)

    def no_papers(table_name="generatedpapers"):
        if table_name == "generatedpapers":
            raise RuntimeError("table down")
        return storage.table(table_name)

    monkeypatch.setattr(fa, "get_table_client", no_papers)
    resp = call(fa.generate, make_request("generate", body=synthetic_pdf, params={"platformId": GUID}))
    assert resp.status_code == 200, resp.get_body()
    assert json.loads(resp.get_body())["downloadUrl"]


def test_generate_results_returns_fresh_links_and_stores_blob_names(synthetic_pdf, storage, monkeypatch):
    monkeypatch.setattr(fa, "extract_results", lambda pdf: ([], "TULOKKAAT"))
    monkeypatch.setattr(fa, "render_results_pdf", lambda meta, teams: b"%PDF-results")
    monkeypatch.setattr(fa, "render_results_html", lambda meta, teams: "<html></html>")
    resp = call(fa.generate_results, make_request(
        "generate_results", body=synthetic_pdf, params={"platformId": GUID, "catFile": "CAT001RS.htm"}))
    assert resp.status_code == 200, resp.get_body()
    body = json.loads(resp.get_body())
    folder = storage.competitions[("GLOBAL", body["id"])]["FolderPath"]
    for name, url in ((body["fileName"], body["downloadUrl"]), ("CAT001RS.htm", body["htmlUrl"])):
        assert storage.papers[(body["id"], name)]["BlobName"] == f"{folder}/output/{name}"
        assert "Url" not in storage.papers[(body["id"], name)]
        parsed = urlparse(url)
        assert parsed.path.endswith(f"/output/{name}")
        assert parse_qs(parsed.query)["se"][0] == body["expiration"]
