"""The platform retention clock (platform_clock.py) and how Score Modifier follows it.

Every generation is bound to a platform competition (required ``platformId``
query param -> ``PlatformId``) and deleted when the platform says so; the row's
DeletionDate is only a cached copy. The tool has no clock of its own: legacy
rows without a PlatformId keep their stored date, and an unreachable platform
never causes a deletion.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from conftest import TEST_EMAIL, FakeTableClient, call, make_request

import function_app as fa
import platform_clock as pc

GUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
OTHER_GUID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def _midnight(days_from_now: int) -> datetime:
    d = datetime.now(timezone.utc) + timedelta(days=days_from_now)
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sweep():
    fa.auto_delete_expired_competitions._function.get_user_function()(timer=None)


# ── platform_clock.py on its own ──────────────────────────────────────────────

def test_feature_off_without_platform_account(monkeypatch):
    monkeypatch.delenv("PLATFORM_STORAGE_ACCOUNT", raising=False)
    assert pc.get_platform_table_client() is None
    assert pc.read_platform_clock(GUID) is None


def test_read_platform_clock(platform):
    platform.add(GUID, deletion="2026-12-01T00:00:00Z")
    clock = pc.read_platform_clock(GUID.upper())  # looked up lowercased
    assert clock == {"deletionDate": datetime(2026, 12, 1, tzinfo=timezone.utc), "status": "active", "purged": False}

    platform.add(OTHER_GUID, deletion="2026-12-01T00:00:00Z", status="deleted", purged="2026-12-01T03:00:00Z")
    assert pc.read_platform_clock(OTHER_GUID) == {
        "deletionDate": datetime(2026, 12, 1, tzinfo=timezone.utc), "status": "deleted", "purged": True,
    }


def test_read_platform_clock_not_found_and_bad_ids(platform):
    assert pc.read_platform_clock(GUID) is None  # no such platform competition
    reads = platform.reads
    for bad in (None, "", "not-a-guid", "12345678"):
        assert pc.read_platform_clock(bad) is None
    assert platform.reads == reads  # rejected before any lookup


def test_read_platform_clock_unreachable(platform):
    platform.unreachable = True
    with pytest.raises(pc.PlatformClockUnavailable):
        pc.read_platform_clock(GUID)


def test_read_platform_clock_with_explicit_client():
    rows = {("COMPETITION", GUID): {"PartitionKey": "COMPETITION", "RowKey": GUID, "DeletionDate": "2026-12-01T00:00:00Z"}}
    clock = pc.read_platform_clock(GUID, table_client=FakeTableClient(rows))
    assert clock["deletionDate"] == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert clock["status"] == "active" and clock["purged"] is False


def test_parse_utc_and_to_iso_z():
    expected = datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert pc.parse_utc("2026-12-01T00:00:00Z") == expected
    assert pc.parse_utc("2026-12-01T02:00:00+02:00") == expected
    assert pc.parse_utc("2026-12-01T00:00:00") == expected  # naive = UTC
    assert pc.parse_utc(datetime(2026, 12, 1)) == expected
    assert pc.parse_utc("") is None and pc.parse_utc(None) is None and pc.parse_utc("garbage") is None
    assert pc.to_iso_z(expected) == "2026-12-01T00:00:00Z"


def test_effective_deletion_platform_date_wins():
    platform_date = datetime(2026, 12, 1, tzinfo=timezone.utc)
    local = "2026-11-01T00:00:00Z"
    assert pc.effective_deletion(local, {"deletionDate": platform_date}) == platform_date
    assert pc.effective_deletion(local, {"deletionDate": None}) == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert pc.effective_deletion(local, None) == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert pc.effective_deletion(None, None) is None


def test_platform_says_delete():
    now = datetime(2026, 12, 1, 3, tzinfo=timezone.utc)
    past, future = now - timedelta(hours=3), now + timedelta(days=1)
    assert pc.platform_says_delete(None, now) is False
    assert pc.platform_says_delete({"deletionDate": future, "status": "active", "purged": False}, now) is False
    assert pc.platform_says_delete({"deletionDate": past, "status": "active", "purged": False}, now) is True
    assert pc.platform_says_delete({"deletionDate": future, "status": "deleted", "purged": False}, now) is True
    assert pc.platform_says_delete({"deletionDate": future, "status": "active", "purged": True}, now) is True
    assert pc.platform_says_delete({"deletionDate": None, "status": "active", "purged": False}, now) is False


def test_platform_errors_are_not_found_vs_unavailable():
    """ResourceNotFoundError means "no such competition"; anything else is an outage."""

    class Client:
        def __init__(self, exc):
            self.exc = exc

        def get_entity(self, pk, rk):
            raise self.exc

    assert pc.read_platform_clock(GUID, table_client=Client(ResourceNotFoundError("gone"))) is None
    with pytest.raises(pc.PlatformClockUnavailable):
        pc.read_platform_clock(GUID, table_client=Client(HttpResponseError("403")))


# ── generate / generate_results store the binding ────────────────────────────

@pytest.fixture
def fake_split(monkeypatch, synthetic_pdf):
    """Skip the real FSM transform so the storage path runs without samples."""
    monkeypatch.setattr(fa, "split_per_skater", lambda pdf, **kw: synthetic_pdf)
    return synthetic_pdf


@pytest.fixture
def fake_results(monkeypatch):
    monkeypatch.setattr(fa, "extract_results", lambda pdf: ([], "TULOKKAAT"))
    monkeypatch.setattr(fa, "render_results_pdf", lambda meta, teams: b"%PDF-results")
    monkeypatch.setattr(fa, "render_results_html", lambda meta, teams: "<html></html>")


def _generate(pdf, params=None):
    resp = call(fa.generate, make_request("generate", body=pdf, params=params or {}))
    return resp, (json.loads(resp.get_body()) if resp.status_code == 200 else None)


def _generate_results(pdf, params=None):
    resp = call(fa.generate_results, make_request("generate_results", body=pdf, params=params or {}))
    return resp, (json.loads(resp.get_body()) if resp.status_code == 200 else None)


GENERATORS = pytest.mark.parametrize("run", [_generate, _generate_results], ids=["generate", "generate_results"])


def test_no_local_retention_constants():
    assert not hasattr(fa, "DELETION_RETENTION_DAYS")
    assert not hasattr(fa, "DELETION_EXTENSION_DAYS")
    assert not hasattr(fa, "extend_competition_deletion")


@GENERATORS
@pytest.mark.parametrize("bad", [None, "", "not-a-guid", GUID + "0"])
def test_platform_id_is_required(run, bad, fake_split, fake_results, storage, platform):
    params = {} if bad is None else {"platformId": bad}
    resp, _ = run(fake_split, params)
    assert resp.status_code == 400
    assert json.loads(resp.get_body()) == {"error": "platform_id_required"}
    assert storage.competitions == {} and storage.blobs == {}


@GENERATORS
def test_generation_caches_the_platform_date(run, fake_split, fake_results, storage, platform):
    platform.add(GUID, deletion="2026-12-24T00:00:00Z")
    resp, body = run(fake_split, {"platformId": GUID.upper()})
    assert resp.status_code == 200, resp.get_body()
    row = storage.competitions[("GLOBAL", body["id"])]
    assert row["PlatformId"] == GUID  # lowercased
    assert row["DeletionDate"] == "2026-12-24T00:00:00Z"
    assert row["CreatedBy"] == TEST_EMAIL


@GENERATORS
def test_generation_without_a_readable_date_stores_none(run, fake_split, fake_results, storage, platform, monkeypatch):
    """Never invent a local date: unreadable now -> no DeletionDate, the sweep syncs it later."""
    platform.unreachable = True
    resp, body = run(fake_split, {"platformId": GUID})
    assert resp.status_code == 200
    row = storage.competitions[("GLOBAL", body["id"])]
    assert row["PlatformId"] == GUID and "DeletionDate" not in row

    platform.unreachable = False
    platform.add(GUID, deletion=None)  # not yet backfilled on the platform
    resp, body = run(fake_split, {"platformId": GUID})
    assert resp.status_code == 200
    assert "DeletionDate" not in storage.competitions[("GLOBAL", body["id"])]

    monkeypatch.delenv("PLATFORM_STORAGE_ACCOUNT")  # feature off
    resp, body = run(fake_split, {"platformId": GUID})
    assert resp.status_code == 200
    assert "DeletionDate" not in storage.competitions[("GLOBAL", body["id"])]


@GENERATORS
def test_generation_for_unknown_or_deleted_competition_is_refused(run, fake_split, fake_results, storage, platform):
    resp, _ = run(fake_split, {"platformId": GUID})
    assert resp.status_code == 404
    assert json.loads(resp.get_body()) == {"error": "platform_not_found"}

    platform.add(GUID, deletion=_z(_midnight(1)), status="deleted")
    resp, _ = run(fake_split, {"platformId": GUID})
    assert resp.status_code == 410
    assert json.loads(resp.get_body()) == {"error": "platform_deleted"}

    platform.add(OTHER_GUID, deletion=_z(_midnight(-1)), status="deleted", purged=_z(_midnight(0)))
    assert run(fake_split, {"platformId": OTHER_GUID})[0].status_code == 410
    assert storage.competitions == {} and storage.blobs == {}


# ── the nightly sweep ─────────────────────────────────────────────────────────

def _seed(storage, comp_id, *, local_days=None, platform_id=""):
    folder = f"Comp {comp_id}-{comp_id}"
    row = {
        "PartitionKey": "GLOBAL", "RowKey": comp_id, "Name": f"Comp {comp_id}", "FolderPath": folder,
        "CreatedBy": "x@y", "CreatedDate": _z(_midnight(-1)), "Visible": True, "PlatformId": platform_id,
    }
    if local_days is not None:
        row["DeletionDate"] = _z(_midnight(local_days))
    storage.competitions[("GLOBAL", comp_id)] = row
    storage.blobs[f"{folder}/source.pdf"] = b"%PDF-src"
    return folder


def _visible(storage, comp_id):
    return storage.competitions[("GLOBAL", comp_id)]["Visible"]


def test_details_report_platform_binding(storage):
    _seed(storage, "aaaaaaaa", local_days=3, platform_id=GUID)
    body = json.loads(call(fa.get_competition_details, make_request(
        "get_competition_details", method="GET", params={"id": "aaaaaaaa"})).get_body())
    assert body["platformId"] == GUID
    assert "retentionManagedBy" not in body


def test_sweep_bound_rows_follow_the_platform(storage, platform):
    # Platform date passed although the cached date is far off -> deleted.
    gone = _seed(storage, "aaaaaaaa", local_days=30, platform_id=GUID)
    platform.add(GUID, deletion=_z(_midnight(0) - timedelta(hours=1)))
    # Platform extended past an overdue cached date -> kept, cache refreshed.
    _seed(storage, "bbbbbbbb", local_days=-2, platform_id=OTHER_GUID)
    platform.add(OTHER_GUID, deletion=_z(_midnight(7)))

    _sweep()

    assert _visible(storage, "aaaaaaaa") is False
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["DeletedBy"] == fa.AUTO_CLEANUP_ACTOR
    assert not any(b.startswith(f"{gone}/") for b in storage.blobs)
    assert _visible(storage, "bbbbbbbb") is True
    assert storage.competitions[("GLOBAL", "bbbbbbbb")]["DeletionDate"] == _z(_midnight(7))


def test_sweep_caches_the_date_on_rows_stored_without_one(storage, platform):
    _seed(storage, "aaaaaaaa", platform_id=GUID)  # platform unreadable at generation
    platform.add(GUID, deletion=_z(_midnight(20)))
    _sweep()
    assert _visible(storage, "aaaaaaaa") is True
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["DeletionDate"] == _z(_midnight(20))


def test_sweep_deletes_when_platform_purged_or_deleted(storage, platform):
    _seed(storage, "aaaaaaaa", local_days=30, platform_id=GUID)
    platform.add(GUID, deletion=_z(_midnight(30)), status="deleted", purged=_z(_midnight(0)))
    _seed(storage, "bbbbbbbb", local_days=30, platform_id=OTHER_GUID)
    platform.add(OTHER_GUID, deletion=_z(_midnight(30)), status="deleted")
    _sweep()
    assert _visible(storage, "aaaaaaaa") is False
    assert _visible(storage, "bbbbbbbb") is False


def test_sweep_platform_row_without_date_deletes_nothing(storage, platform):
    _seed(storage, "aaaaaaaa", local_days=-1, platform_id=GUID)  # overdue cached copy
    platform.add(GUID, deletion=None)  # not yet backfilled: the platform decides
    _sweep()
    assert _visible(storage, "aaaaaaaa") is True


def test_sweep_never_deletes_when_platform_unreachable(storage, platform, caplog):
    folder = _seed(storage, "aaaaaaaa", local_days=-5, platform_id=GUID)  # overdue cached copy
    platform.unreachable = True

    with caplog.at_level(logging.ERROR):
        _sweep()

    assert _visible(storage, "aaaaaaaa") is True
    assert f"{folder}/source.pdf" in storage.blobs
    assert any("platform clock unavailable" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


def test_sweep_never_deletes_bound_rows_with_feature_off(storage, caplog):
    """PLATFORM_STORAGE_ACCOUNT unset: bound rows are skipped with an error, legacy rows still run."""
    _seed(storage, "aaaaaaaa", local_days=-1, platform_id=GUID)
    _seed(storage, "cccccccc", local_days=-1)  # legacy, no PlatformId
    with caplog.at_level(logging.ERROR):
        _sweep()
    assert _visible(storage, "aaaaaaaa") is True
    assert _visible(storage, "cccccccc") is False
    assert any("PLATFORM_STORAGE_ACCOUNT unset" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


def test_sweep_unknown_platform_competition_uses_cached_date(storage, platform, caplog):
    _seed(storage, "aaaaaaaa", local_days=-1, platform_id=GUID)
    _seed(storage, "bbbbbbbb", local_days=+1, platform_id=OTHER_GUID)
    _seed(storage, "cccccccc", platform_id="7c9e6679-7425-40de-944b-e07fc1f90000")  # no cached date
    with caplog.at_level(logging.ERROR):
        _sweep()
    assert _visible(storage, "aaaaaaaa") is False
    assert _visible(storage, "bbbbbbbb") is True
    assert _visible(storage, "cccccccc") is True
    assert "DeletionDate" not in storage.competitions[("GLOBAL", "cccccccc")]  # never backfilled locally
    assert any("unknown platform competition" in r.getMessage() for r in caplog.records)


def test_sweep_legacy_rows_keep_their_stored_date(storage, platform):
    _seed(storage, "aaaaaaaa", local_days=-1)
    _seed(storage, "bbbbbbbb", local_days=+25)  # an old 30-day row keeps its date
    _sweep()
    assert _visible(storage, "aaaaaaaa") is False
    assert _visible(storage, "bbbbbbbb") is True
    assert storage.competitions[("GLOBAL", "bbbbbbbb")]["DeletionDate"] == _z(_midnight(25))
    assert platform.reads == 0  # legacy rows never consult the platform


def test_sweep_reads_each_platform_competition_once(storage, platform):
    for cid in ("aaaaaaaa", "bbbbbbbb", "cccccccc"):
        _seed(storage, cid, local_days=10, platform_id=GUID)
    platform.add(GUID, deletion=_z(_midnight(20)))
    _sweep()
    assert platform.reads == 1
