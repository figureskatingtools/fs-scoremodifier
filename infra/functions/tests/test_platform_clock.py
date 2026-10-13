"""The platform retention clock (platform_clock.py) and how Score Modifier follows it.

A row generated for a platform competition (``platformId`` query param ->
``PlatformId``) is deleted on the platform's date; an unbound row keeps its own
60-day clock. An unreachable platform never causes an early deletion.
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


def _assert_local_60_days(row):
    deletion = fa._parse_iso_utc(row["DeletionDate"])
    created = fa._parse_iso_utc(row["CreatedDate"])
    assert _ISO_Z.match(row["DeletionDate"])
    assert deletion - created == timedelta(days=60)


@pytest.mark.parametrize("run", [_generate, _generate_results], ids=["generate", "generate_results"])
def test_unbound_generation_keeps_60_days(run, fake_split, fake_results, storage):
    assert fa.DELETION_RETENTION_DAYS == 60
    resp, body = run(fake_split)
    assert resp.status_code == 200, resp.get_body()
    row = storage.competitions[("GLOBAL", body["id"])]
    assert row["PlatformId"] == ""
    assert row["CreatedBy"] == TEST_EMAIL
    _assert_local_60_days(row)


@pytest.mark.parametrize("run", [_generate, _generate_results], ids=["generate", "generate_results"])
def test_bound_generation_takes_the_platform_date(run, fake_split, fake_results, storage, platform):
    platform.add(GUID, deletion="2026-12-24T00:00:00Z")
    resp, body = run(fake_split, {"platformId": GUID.upper()})
    assert resp.status_code == 200, resp.get_body()
    row = storage.competitions[("GLOBAL", body["id"])]
    assert row["PlatformId"] == GUID  # lowercased
    assert row["DeletionDate"] == "2026-12-24T00:00:00Z"


@pytest.mark.parametrize("run", [_generate, _generate_results], ids=["generate", "generate_results"])
def test_bound_generation_falls_back_to_local_clock(run, fake_split, fake_results, storage, platform):
    # Unknown on the platform -> local 60 days, binding still stored.
    resp, body = run(fake_split, {"platformId": GUID})
    assert resp.status_code == 200
    row = storage.competitions[("GLOBAL", body["id"])]
    assert row["PlatformId"] == GUID
    _assert_local_60_days(row)

    # Platform unreachable -> the generation still succeeds on the local clock.
    platform.unreachable = True
    resp, body = run(fake_split, {"platformId": GUID})
    assert resp.status_code == 200
    _assert_local_60_days(storage.competitions[("GLOBAL", body["id"])])


@pytest.mark.parametrize("run", [_generate, _generate_results], ids=["generate", "generate_results"])
def test_invalid_platform_id_is_rejected(run, fake_split, fake_results, storage):
    resp, _ = run(fake_split, {"platformId": "not-a-guid"})
    assert resp.status_code == 400
    assert b"platformId" in resp.get_body()
    assert storage.competitions == {} and storage.blobs == {}


# ── extend ────────────────────────────────────────────────────────────────────

def _seed(storage, comp_id, *, local_days, platform_id=""):
    folder = f"Comp {comp_id}-{comp_id}"
    storage.competitions[("GLOBAL", comp_id)] = {
        "PartitionKey": "GLOBAL", "RowKey": comp_id, "Name": f"Comp {comp_id}", "FolderPath": folder,
        "CreatedBy": "x@y", "CreatedDate": _z(_midnight(-1)), "Visible": True,
        "DeletionDate": _z(_midnight(local_days)), "PlatformId": platform_id,
    }
    storage.blobs[f"{folder}/source.pdf"] = b"%PDF-src"
    return folder


def _extend(comp_id):
    resp = call(fa.extend_competition_deletion, make_request("extend_competition_deletion", params={"id": comp_id}))
    return resp, (json.loads(resp.get_body()) if resp.status_code == 200 else None)


def test_extend_bound_syncs_the_platform_date(storage, platform):
    _seed(storage, "aaaaaaaa", local_days=3, platform_id=GUID)
    platform.add(GUID, deletion=_z(_midnight(40)))
    resp, body = _extend("aaaaaaaa")
    assert resp.status_code == 200
    assert body == {"deletionDate": _z(_midnight(40)), "retentionManagedBy": "platform"}
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["DeletionDate"] == _z(_midnight(40))


def test_extend_bound_platform_unknown_uses_local_clock(storage, platform):
    _seed(storage, "aaaaaaaa", local_days=3, platform_id=GUID)
    resp, body = _extend("aaaaaaaa")
    assert resp.status_code == 200
    assert body["deletionDate"] == _z(_midnight(10))


def test_extend_bound_platform_unreachable_or_deleted(storage, platform):
    _seed(storage, "aaaaaaaa", local_days=3, platform_id=GUID)
    before = dict(storage.competitions[("GLOBAL", "aaaaaaaa")])
    platform.unreachable = True
    assert _extend("aaaaaaaa")[0].status_code == 503
    platform.unreachable = False
    platform.add(GUID, deletion=_z(_midnight(1)), status="deleted")
    resp, _ = _extend("aaaaaaaa")
    assert resp.status_code == 410
    assert json.loads(resp.get_body()) == {"error": "platform_deleted"}
    assert storage.competitions[("GLOBAL", "aaaaaaaa")] == before


def test_details_report_platform_binding(storage):
    _seed(storage, "aaaaaaaa", local_days=3, platform_id=GUID)
    body = json.loads(call(fa.get_competition_details, make_request(
        "get_competition_details", method="GET", params={"id": "aaaaaaaa"})).get_body())
    assert body["platformId"] == GUID and body["retentionManagedBy"] == "platform"


# ── the nightly sweep ─────────────────────────────────────────────────────────

def _visible(storage, comp_id):
    return storage.competitions[("GLOBAL", comp_id)]["Visible"]


def test_sweep_bound_rows_follow_the_platform(storage, platform):
    # Platform date passed although the local date is far off -> deleted.
    gone = _seed(storage, "aaaaaaaa", local_days=30, platform_id=GUID)
    platform.add(GUID, deletion=_z(_midnight(0) - timedelta(hours=1)))
    # Platform extended past an overdue local date -> kept, local date synced.
    _seed(storage, "bbbbbbbb", local_days=-2, platform_id=OTHER_GUID)
    platform.add(OTHER_GUID, deletion=_z(_midnight(7)))

    _sweep()

    assert _visible(storage, "aaaaaaaa") is False
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["DeletedBy"] == fa.AUTO_CLEANUP_ACTOR
    assert not any(b.startswith(f"{gone}/") for b in storage.blobs)
    assert _visible(storage, "bbbbbbbb") is True
    assert storage.competitions[("GLOBAL", "bbbbbbbb")]["DeletionDate"] == _z(_midnight(7))


def test_sweep_deletes_when_platform_purged_or_deleted(storage, platform):
    _seed(storage, "aaaaaaaa", local_days=30, platform_id=GUID)
    platform.add(GUID, deletion=_z(_midnight(30)), status="deleted", purged=_z(_midnight(0)))
    _seed(storage, "bbbbbbbb", local_days=30, platform_id=OTHER_GUID)
    platform.add(OTHER_GUID, deletion=_z(_midnight(30)), status="deleted")
    _sweep()
    assert _visible(storage, "aaaaaaaa") is False
    assert _visible(storage, "bbbbbbbb") is False


def test_sweep_never_deletes_early_when_platform_unreachable(storage, platform, caplog):
    folder = _seed(storage, "aaaaaaaa", local_days=-5, platform_id=GUID)  # overdue locally
    _seed(storage, "cccccccc", local_days=-1)  # unbound: local clock still runs
    platform.unreachable = True

    with caplog.at_level(logging.ERROR):
        _sweep()

    assert _visible(storage, "aaaaaaaa") is True
    assert f"{folder}/source.pdf" in storage.blobs
    assert any("platform clock unavailable" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)
    assert _visible(storage, "cccccccc") is False


def test_sweep_bound_but_unknown_on_platform_uses_local_date(storage, platform):
    _seed(storage, "aaaaaaaa", local_days=-1, platform_id=GUID)
    _seed(storage, "bbbbbbbb", local_days=+1, platform_id=OTHER_GUID)
    _sweep()
    assert _visible(storage, "aaaaaaaa") is False
    assert _visible(storage, "bbbbbbbb") is True


def test_sweep_platform_row_without_date_uses_local_date(storage, platform):
    _seed(storage, "aaaaaaaa", local_days=-1, platform_id=GUID)
    platform.add(GUID, deletion=None)  # not yet backfilled on the platform
    _sweep()
    assert _visible(storage, "aaaaaaaa") is False


def test_sweep_reads_each_platform_competition_once(storage, platform):
    for cid in ("aaaaaaaa", "bbbbbbbb", "cccccccc"):
        _seed(storage, cid, local_days=10, platform_id=GUID)
    platform.add(GUID, deletion=_z(_midnight(20)))
    _sweep()
    assert platform.reads == 1


def test_sweep_without_platform_account_keeps_local_clock(storage):
    """Feature off (PLATFORM_STORAGE_ACCOUNT unset): bound rows use the local date."""
    _seed(storage, "aaaaaaaa", local_days=-1, platform_id=GUID)
    _seed(storage, "bbbbbbbb", local_days=+1, platform_id=GUID)
    _sweep()
    assert _visible(storage, "aaaaaaaa") is False
    assert _visible(storage, "bbbbbbbb") is True
