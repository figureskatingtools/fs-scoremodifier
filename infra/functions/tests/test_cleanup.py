"""Competition history endpoints and the daily auto-delete sweep."""

import json
import re
from datetime import datetime, timedelta, timezone

from conftest import call, make_request

import function_app as fa

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=None).isoformat() + "Z"


def _seed(storage, comp_id, *, days_from_now, visible=True, deletion=True):
    folder = f"Comp {comp_id}-{comp_id}"
    row = {
        "PartitionKey": "GLOBAL", "RowKey": comp_id, "Name": f"Comp {comp_id}", "FolderPath": folder,
        "Segment": "SEG", "CreatedBy": "x@y", "CreatedDate": _iso(datetime.now(timezone.utc)), "Visible": visible,
    }
    if deletion:
        row["DeletionDate"] = _iso(datetime.now(timezone.utc) + timedelta(days=days_from_now))
    storage.competitions[("GLOBAL", comp_id)] = row
    storage.blobs[f"{folder}/source.pdf"] = b"%PDF-src"
    storage.blobs[f"{folder}/output/per-skater.pdf"] = b"%PDF-out"
    storage.papers[(comp_id, "per-skater.pdf")] = {"PartitionKey": comp_id, "RowKey": "per-skater.pdf", "Url": "u", "FileName": "per-skater.pdf"}
    return folder


def test_list_and_details(storage):
    _seed(storage, "aaaaaaaa", days_from_now=10)
    _seed(storage, "bbbbbbbb", days_from_now=10, visible=False)
    _seed(storage, "cccccccc", days_from_now=0, deletion=False)  # pre-feature row

    resp = call(fa.list_competitions, make_request("list_competitions", method="GET"))
    assert resp.status_code == 200
    rows = json.loads(resp.get_body())
    assert {r["id"] for r in rows} == {"aaaaaaaa", "cccccccc"}  # soft-deleted rows hidden
    legacy = next(r for r in rows if r["id"] == "cccccccc")
    assert legacy["deletionDate"] == fa.LEGACY_DELETION_DATE
    assert storage.competitions[("GLOBAL", "cccccccc")]["DeletionDate"] == fa.LEGACY_DELETION_DATE  # backfilled

    resp = call(fa.get_competition_details, make_request("get_competition_details", method="GET", params={"id": "aaaaaaaa"}))
    body = json.loads(resp.get_body())
    assert resp.status_code == 200
    assert body["name"] == "Comp aaaaaaaa"
    assert [f["fileName"] for f in body["generatedFiles"]] == ["per-skater.pdf"]
    assert call(fa.get_competition_details, make_request("get_competition_details", method="GET", params={"id": "nope"})).status_code == 404
    assert call(fa.get_competition_details, make_request("get_competition_details", method="GET")).status_code == 400


def test_delete_competition_removes_blobs_and_soft_deletes_row(storage):
    folder = _seed(storage, "dddddddd", days_from_now=10)
    other = _seed(storage, "eeeeeeee", days_from_now=10)

    assert call(fa.delete_competition, make_request("delete_competition", params={"id": "nope"})).status_code == 404
    assert call(fa.delete_competition, make_request("delete_competition")).status_code == 400
    resp = call(fa.delete_competition, make_request("delete_competition", params={"id": "dddddddd"}))
    assert resp.status_code == 200, resp.get_body()

    assert not any(b.startswith(f"{folder}/") for b in storage.blobs)
    assert all(b.startswith(f"{other}/") for b in storage.blobs)
    assert ("dddddddd", "per-skater.pdf") not in storage.papers
    row = storage.competitions[("GLOBAL", "dddddddd")]
    assert row["Visible"] is False
    assert row["DeletedBy"] == "tester@example.com"
    assert _ISO_Z.match(row["DeletedDate"]), row["DeletedDate"]
    assert storage.competitions[("GLOBAL", "eeeeeeee")]["Visible"] is True


def test_auto_delete_sweep(storage):
    expired = _seed(storage, "11111111", days_from_now=-1)
    _seed(storage, "22222222", days_from_now=+1)
    _seed(storage, "33333333", days_from_now=-1, visible=False)  # already gone, skipped
    legacy = _seed(storage, "44444444", days_from_now=0, deletion=False)  # backfilled to 2026-06-12 -> expired

    fa.auto_delete_expired_competitions._function.get_user_function()(timer=None)

    rows = storage.competitions
    assert rows[("GLOBAL", "11111111")]["Visible"] is False
    assert rows[("GLOBAL", "11111111")]["DeletedBy"] == fa.AUTO_CLEANUP_ACTOR
    assert rows[("GLOBAL", "22222222")]["Visible"] is True
    assert rows[("GLOBAL", "44444444")]["Visible"] is False
    assert rows[("GLOBAL", "44444444")]["DeletionDate"] == fa.LEGACY_DELETION_DATE
    assert not any(b.startswith(f"{expired}/") or b.startswith(f"{legacy}/") for b in storage.blobs)
    assert any(b.startswith("Comp 22222222-22222222/") for b in storage.blobs)
    # The soft-deleted one was skipped: its blobs are left alone.
    assert any(b.startswith("Comp 33333333-33333333/") for b in storage.blobs)


def test_parse_iso_utc():
    assert fa._parse_iso_utc("2026-06-12T00:00:00Z") == datetime(2026, 6, 12, tzinfo=timezone.utc)
    assert fa._parse_iso_utc("2026-06-12T03:00:00+03:00") == datetime(2026, 6, 12, tzinfo=timezone.utc)
    assert fa._parse_iso_utc("") is None and fa._parse_iso_utc("garbage") is None and fa._parse_iso_utc(None) is None


def test_utcnow_is_naive_utc():
    now = fa._utcnow()
    assert now.tzinfo is None
    assert abs((now - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()) < 5
