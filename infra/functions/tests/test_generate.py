"""POST /api/generate — the per-skater split end to end against fake storage."""

import json
import re
from urllib.parse import urlparse

from conftest import TEST_EMAIL, call, make_request, page_texts, rank_anchor_count, teams_or_none

import function_app as fa

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def test_rejects_empty_and_non_pdf_bodies():
    assert call(fa.generate, make_request("generate", body=b"")).status_code == 400
    resp = call(fa.generate, make_request("generate", body=b"hello, not a pdf"))
    assert resp.status_code == 400
    assert b"Only PDF" in resp.get_body()


def test_rejects_oversized_uploads():
    too_big = fa.MAX_UPLOAD_SIZE + 1
    resp = call(fa.generate, make_request("generate", body=b"%PDF-", headers={"Content-Length": str(too_big)}))
    assert resp.status_code == 413
    resp = call(fa.generate, make_request("generate", body=b"%PDF-" + b"\0" * too_big))
    assert resp.status_code == 413


def test_rejects_a_pdf_that_is_not_the_report(synthetic_pdf, storage):
    resp = call(fa.generate, make_request("generate", body=synthetic_pdf))
    assert resp.status_code == 400
    assert b"JUDGES DETAILS PER SKATER" in resp.get_body()
    assert storage.blobs == {} and storage.competitions == {}


def test_generate_happy_path(sample_pdf, storage):
    expected_pages = rank_anchor_count(sample_pdf)  # one page per skater/team block
    _, segment, printed = fa.derive_name_and_meta(sample_pdf)
    assert segment and printed

    resp = call(fa.generate, make_request("generate", body=sample_pdf, params={"includeRanks": "false"}))
    assert resp.status_code == 200, resp.get_body()
    body = json.loads(resp.get_body())
    assert set(body) == {"id", "name", "fileName", "downloadUrl", "pages", "expiration"}
    assert re.fullmatch(r"[0-9a-f]{8}", body["id"])
    assert body["fileName"] == fa.OUTPUT_FILENAME
    assert body["pages"] == expected_pages
    assert body["name"] == f"{segment} — {printed}"

    # One competitions row, derived name, folder = sanitized name + id.
    row = storage.competitions[("GLOBAL", body["id"])]
    assert row["Name"] == body["name"]
    assert row["Segment"] == segment
    assert row["PrintedDate"] == printed
    assert row["FolderPath"] == f"{fa.sanitize_name(body['name'])}-{body['id']}"
    assert row["IncludeRanks"] is False
    assert row["OutputPages"] == expected_pages
    assert row["Visible"] is True
    assert row["CreatedBy"] == TEST_EMAIL
    assert row["UploadedFileCount"] == 1 and row["GenerateRunCount"] == 1
    # Naive-UTC ISO + literal Z, never an offset (the format every reader expects).
    assert _ISO_Z.match(row["CreatedDate"]), row["CreatedDate"]
    assert _ISO_Z.match(row["DeletionDate"]), row["DeletionDate"]

    # Source + output blobs under that folder; output is the split PDF.
    folder = row["FolderPath"]
    assert storage.blobs[f"{folder}/source.pdf"] == sample_pdf
    out = storage.blobs[f"{folder}/output/{fa.OUTPUT_FILENAME}"]
    assert out[:5] == b"%PDF-"
    assert len(page_texts(out)) == expected_pages

    # generatedpapers row: SAS link with a percent-encoded path.
    paper = storage.papers[(body["id"], fa.OUTPUT_FILENAME)]
    assert paper["Url"] == body["downloadUrl"]
    assert paper["FileSize"] == len(out)
    assert paper["ExpirationDate"] == body["expiration"]
    url = urlparse(body["downloadUrl"])
    assert url.hostname == f"{storage.blob_service.account_name}.blob.core.windows.net"
    assert " " not in url.path and url.path.startswith(f"/{fa.CONTAINER_NAME}/")
    assert "sig=" in url.query and "se=" in url.query


def test_include_ranks_flag_is_persisted_and_changes_output(sample_pdf, storage):
    hidden = call(fa.generate, make_request("generate", body=sample_pdf))
    shown = call(fa.generate, make_request("generate", body=sample_pdf, params={"includeRanks": "true"}))
    assert hidden.status_code == shown.status_code == 200
    h, s = (json.loads(r.get_body()) for r in (hidden, shown))
    assert storage.competitions[("GLOBAL", h["id"])]["IncludeRanks"] is False
    assert storage.competitions[("GLOBAL", s["id"])]["IncludeRanks"] is True
    out_h = storage.blobs[f"{storage.competitions[('GLOBAL', h['id'])]['FolderPath']}/output/{fa.OUTPUT_FILENAME}"]
    out_s = storage.blobs[f"{storage.competitions[('GLOBAL', s['id'])]['FolderPath']}/output/{fa.OUTPUT_FILENAME}"]
    assert len(page_texts(out_h)) == len(page_texts(out_s))
    teams = teams_or_none(sample_pdf)
    if teams is not None and len(teams) > 3:
        assert out_h != out_s  # someone outside the podium lost their rank number


def test_storage_failure_is_a_500_not_a_leak(sample_pdf, monkeypatch):
    monkeypatch.setattr(fa, "get_blob_service_client", lambda: None)
    resp = call(fa.generate, make_request("generate", body=sample_pdf))
    assert resp.status_code == 500
    assert b"Storage configuration not found" in resp.get_body()
