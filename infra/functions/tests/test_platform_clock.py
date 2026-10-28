"""The platform retention clock (platform_clock.py) and how Score Modifier follows it.

Every generation is bound to a platform competition (required ``platformId``
query param -> ``PlatformId``); the row's DeletionDate is only a cached copy of
the platform's date. A bound row's data is deleted only on the platform's
verdict — the competition is deleted or purged — never on a date alone and
never because something was unreadable. The tool has no clock of its own:
legacy rows without a PlatformId keep their stored date.
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
THIRD_GUID = "7c9e6679-7425-40de-944b-e07fc1f90000"
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


def test_normalize_platform_id():
    assert pc.normalize_platform_id(f"  {GUID.upper()} ") == GUID
    for bad in (None, "", "not-a-guid", "12345678", GUID + "0"):
        assert pc.normalize_platform_id(bad) is None


def test_read_platform_clock(platform):
    platform.add(GUID, deletion="2026-12-01T00:00:00Z")
    clock = pc.read_platform_clock(GUID.upper())  # looked up normalized
    assert clock == {"deletionDate": datetime(2026, 12, 1, tzinfo=timezone.utc), "status": "active", "purged": False}

    platform.add(OTHER_GUID, deletion="2026-12-01T00:00:00Z", status="deleted", purged="2026-12-01T03:00:00Z")
    assert pc.read_platform_clock(OTHER_GUID) == {
        "deletionDate": datetime(2026, 12, 1, tzinfo=timezone.utc), "status": "deleted", "purged": True,
    }


def test_read_platform_clock_not_found_and_bad_ids(platform):
    assert pc.read_platform_clock(GUID) is None  # no such clock row
    reads = platform.reads
    for bad in (None, "", "not-a-guid", "12345678"):
        assert pc.read_platform_clock(bad) is None
    assert platform.reads == reads  # rejected before any lookup


def test_read_platform_clock_unreachable_or_table_missing(platform):
    platform.unreachable = True
    with pytest.raises(pc.PlatformClockUnavailable):
        pc.read_platform_clock(GUID)
    platform.unreachable = False
    platform.table_missing = True  # a missing table is an outage, not an unknown competition
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


def test_platform_says_delete_only_on_the_platform_verdict():
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert pc.platform_says_delete(None) is False
    # A passed date alone is not a verdict (the platform may be in dry-run, or just extended it).
    assert pc.platform_says_delete({"deletionDate": past, "status": "active", "purged": False}) is False
    assert pc.platform_says_delete({"deletionDate": past, "status": "deleted", "purged": False}) is True
    assert pc.platform_says_delete({"deletionDate": None, "status": "active", "purged": True}) is True


def test_platform_errors_are_not_found_vs_unavailable():
    """An absent entity means "no such competition"; anything else is an outage."""

    class Client:
        def __init__(self, exc):
            self.exc = exc

        def get_entity(self, pk, rk):
            raise self.exc

    assert pc.read_platform_clock(GUID, table_client=Client(ResourceNotFoundError("gone"))) is None
    with pytest.raises(pc.PlatformClockUnavailable):
        pc.read_platform_clock(GUID, table_client=Client(ResourceNotFoundError("ErrorCode:TableNotFound")))
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


def test_no_local_retention_clock():
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
    resp, body = run(fake_split, {"platformId": f" {GUID.upper()} "})
    assert resp.status_code == 200, resp.get_body()
    row = storage.competitions[("GLOBAL", body["id"])]
    assert row["PlatformId"] == GUID  # normalized
    assert row["DeletionDate"] == "2026-12-24T00:00:00Z"
    assert row["CreatedBy"] == TEST_EMAIL


@GENERATORS
@pytest.mark.parametrize("failure", ["unreachable", "table_missing"])
def test_generation_refused_when_platform_unreadable(run, failure, fake_split, fake_results, storage, platform):
    """Fail closed: an unconfirmed binding could never be deleted, so nothing is stored."""
    platform.add(GUID, deletion="2026-12-24T00:00:00Z")
    setattr(platform, failure, True)
    resp, _ = run(fake_split, {"platformId": GUID})
    assert resp.status_code == 503
    assert json.loads(resp.get_body()) == {"error": "platform_unavailable"}
    assert storage.competitions == {} and storage.blobs == {}


@GENERATORS
def test_generation_without_a_platform_date_stores_none(run, fake_split, fake_results, storage, platform, monkeypatch):
    """Never invent a local date: no date on the platform yet, or the setting unset (local dev)."""
    platform.add(GUID, deletion=None)  # not yet backfilled on the platform
    resp, body = run(fake_split, {"platformId": GUID})
    assert resp.status_code == 200
    row = storage.competitions[("GLOBAL", body["id"])]
    assert row["PlatformId"] == GUID and "DeletionDate" not in row

    monkeypatch.delenv("PLATFORM_STORAGE_ACCOUNT")
    resp, body = run(fake_split, {"platformId": OTHER_GUID})
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


# ── details + the nightly sweep ───────────────────────────────────────────────

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


def _details(comp_id):
    # admin: some of these rows are legacy (no PlatformId), which only admins see.
    return json.loads(call(fa.get_competition_details, make_request(
        "get_competition_details", method="GET", params={"id": comp_id}, admin=True)).get_body())


def test_details_report_platform_binding(storage):
    _seed(storage, "aaaaaaaa", local_days=3, platform_id=GUID)
    body = _details("aaaaaaaa")
    assert body["platformId"] == GUID
    assert body["deletionDate"] == _z(_midnight(3))
    assert "retentionManagedBy" not in body


def test_details_and_list_agree_on_legacy_dates(storage):
    _seed(storage, "aaaaaaaa")  # pre-feature row, no date
    _seed(storage, "bbbbbbbb", platform_id=GUID)  # bound, platform date not cached yet
    assert _details("aaaaaaaa")["deletionDate"] == fa.LEGACY_DELETION_DATE  # backfilled first
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["DeletionDate"] == fa.LEGACY_DELETION_DATE
    assert _details("bbbbbbbb")["deletionDate"] == "-"  # bound rows are never backfilled locally
    assert "DeletionDate" not in storage.competitions[("GLOBAL", "bbbbbbbb")]
    listed = json.loads(call(fa.list_competitions, make_request("list_competitions", method="GET", admin=True)).get_body())
    assert {r["id"]: r["deletionDate"] for r in listed} == {"aaaaaaaa": fa.LEGACY_DELETION_DATE, "bbbbbbbb": "-"}


def test_timer_runs_after_the_platform_sweep():
    assert fa.auto_delete_expired_competitions._function.get_trigger().schedule == "0 0 4 * * *"


def test_sweep_deletes_only_on_the_platform_verdict(storage, platform):
    deleted = _seed(storage, "aaaaaaaa", local_days=30, platform_id=GUID)
    platform.add(GUID, deletion=_z(_midnight(30)), status="deleted")
    _seed(storage, "bbbbbbbb", local_days=30, platform_id=OTHER_GUID)
    platform.add(OTHER_GUID, deletion=_z(_midnight(-1)), status="deleted", purged=_z(_midnight(0)))
    # Date passed but still active (platform in dry-run, or an extension in flight): kept.
    _seed(storage, "cccccccc", local_days=-2, platform_id=THIRD_GUID)
    platform.add(THIRD_GUID, deletion=_z(_midnight(-1)))

    _sweep()

    assert _visible(storage, "aaaaaaaa") is False
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["DeletedBy"] == fa.AUTO_CLEANUP_ACTOR
    assert not any(b.startswith(f"{deleted}/") for b in storage.blobs)
    assert _visible(storage, "bbbbbbbb") is False
    assert _visible(storage, "cccccccc") is True
    assert storage.competitions[("GLOBAL", "cccccccc")]["DeletionDate"] == _z(_midnight(-1))  # cache refreshed


def test_sweep_refreshes_the_cached_date(storage, platform):
    _seed(storage, "aaaaaaaa", platform_id=GUID)  # stored without a date
    platform.add(GUID, deletion=_z(_midnight(20)))
    _seed(storage, "bbbbbbbb", local_days=-2, platform_id=OTHER_GUID)  # extended on the platform since
    platform.add(OTHER_GUID, deletion=_z(_midnight(7)))
    _sweep()
    assert _visible(storage, "aaaaaaaa") is True and _visible(storage, "bbbbbbbb") is True
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["DeletionDate"] == _z(_midnight(20))
    assert storage.competitions[("GLOBAL", "bbbbbbbb")]["DeletionDate"] == _z(_midnight(7))


def test_sweep_keeps_rows_without_a_platform_clock_row_under_60_days(storage, platform, caplog):
    platform.add(THIRD_GUID, deletion=_z(_midnight(30)))  # the clock table itself is healthy
    _seed(storage, "aaaaaaaa", local_days=-5, platform_id=GUID)  # overdue cached copy
    _seed(storage, "bbbbbbbb", platform_id=OTHER_GUID)  # no cached date
    with caplog.at_level(logging.WARNING):
        _sweep()
    assert _visible(storage, "aaaaaaaa") is True and _visible(storage, "bbbbbbbb") is True
    assert "DeletionDate" not in storage.competitions[("GLOBAL", "bbbbbbbb")]  # never backfilled locally
    assert any("no platform clock row" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("failure", ["unreachable", "table_missing"])
def test_sweep_never_deletes_when_platform_unreadable(failure, storage, platform, caplog):
    folder = _seed(storage, "aaaaaaaa", local_days=-5, platform_id=GUID)
    platform.add(GUID, deletion=_z(_midnight(-5)), status="deleted")
    setattr(platform, failure, True)

    with caplog.at_level(logging.ERROR):
        _sweep()

    assert _visible(storage, "aaaaaaaa") is True
    assert f"{folder}/source.pdf" in storage.blobs
    assert any("platform clock unavailable" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


def test_sweep_never_deletes_when_platform_client_cannot_be_built(storage, platform, monkeypatch, caplog):
    def broken():
        raise RuntimeError("credential")

    monkeypatch.setattr(fa, "get_platform_table_client", broken)
    _seed(storage, "aaaaaaaa", local_days=-5, platform_id=GUID)
    platform.add(GUID, deletion=_z(_midnight(-5)), status="deleted")
    with caplog.at_level(logging.ERROR):
        _sweep()
    assert _visible(storage, "aaaaaaaa") is True
    assert any("platform clock unavailable" in r.getMessage() for r in caplog.records)


def test_sweep_never_deletes_bound_rows_with_setting_unset(storage, caplog):
    """PLATFORM_STORAGE_ACCOUNT unset: bound rows are skipped with an error, legacy rows still run."""
    _seed(storage, "aaaaaaaa", local_days=-1, platform_id=GUID)
    _seed(storage, "cccccccc", local_days=-1)  # legacy, no PlatformId
    with caplog.at_level(logging.ERROR):
        _sweep()
    assert _visible(storage, "aaaaaaaa") is True
    assert _visible(storage, "cccccccc") is False
    assert any("PLATFORM_STORAGE_ACCOUNT unset" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


def test_sweep_legacy_rows_keep_their_stored_date(storage, platform):
    _seed(storage, "aaaaaaaa", local_days=-1)
    _seed(storage, "bbbbbbbb", local_days=+25)  # an old 30-day row keeps its date
    _sweep()
    assert _visible(storage, "aaaaaaaa") is False
    assert _visible(storage, "bbbbbbbb") is True
    assert storage.competitions[("GLOBAL", "bbbbbbbb")]["DeletionDate"] == _z(_midnight(25))
    assert platform.reads == 0  # legacy rows never consult the platform


def test_sweep_uses_one_client_and_one_read_per_competition(storage, platform):
    for cid in ("aaaaaaaa", "bbbbbbbb", "cccccccc"):
        _seed(storage, cid, local_days=10, platform_id=GUID)
    _seed(storage, "dddddddd", local_days=10, platform_id=OTHER_GUID)
    platform.add(GUID, deletion=_z(_midnight(20)))
    platform.add(OTHER_GUID, deletion=_z(_midnight(20)))
    _sweep()
    assert platform.clients == 1
    assert platform.reads == 2


# ── orphans: no platform competition behind the record ───────────────────────

NOW = datetime.now(timezone.utc)
OLD = _z(NOW - timedelta(days=61))
YOUNG = _z(NOW - timedelta(days=59))


@pytest.fixture
def purge(monkeypatch):
    monkeypatch.setenv("ORPHAN_PURGE_ENABLED", "true")


def _orphan(storage, comp_id, created, *, platform_id="", local_days=30, **extra):
    folder = _seed(storage, comp_id, local_days=local_days, platform_id=platform_id)
    storage.competitions[("GLOBAL", comp_id)].update(
        {"CreatedDate": created, "UploadedFileCount": 1, "GenerateRunCount": 3, "OutputPages": 12, **extra})
    storage.papers[(comp_id, "per-skater.pdf")] = {"PartitionKey": comp_id, "RowKey": "per-skater.pdf",
                                                   "FileName": "per-skater.pdf"}
    return folder


def _run():
    return fa._run_cleanup_sweep(datetime.now(timezone.utc))


def _assert_scrubbed(storage, comp_id, folder):
    row = storage.competitions[("GLOBAL", comp_id)]
    assert row["Visible"] is False
    assert row["CreatedBy"] == "" and row["DeletedBy"] == fa.AUTO_CLEANUP_ACTOR
    assert "@" not in repr(row)
    # Statistics stay.
    assert row["UploadedFileCount"] == 1 and row["GenerateRunCount"] == 3 and row["OutputPages"] == 12
    assert not any(b.startswith(f"{folder}/") for b in storage.blobs)
    assert (comp_id, "per-skater.pdf") not in storage.papers


def test_old_record_without_platform_id_is_deleted_and_scrubbed(storage, platform, purge):
    platform.add(THIRD_GUID, deletion=_z(_midnight(30)))
    folder = _orphan(storage, "aaaaaaaa", OLD)  # its stored legacy date is still ahead

    stats = _run()

    assert stats["orphaned"] == 1 and stats["deleted"] == 0
    _assert_scrubbed(storage, "aaaaaaaa", folder)


def test_old_record_whose_clock_row_is_not_found_is_deleted_and_scrubbed(storage, platform, purge):
    platform.add(THIRD_GUID, deletion=_z(_midnight(30)))
    folder = _orphan(storage, "aaaaaaaa", OLD, platform_id=GUID)

    assert _run()["orphaned"] == 1
    _assert_scrubbed(storage, "aaaaaaaa", folder)


def test_a_platform_id_that_is_not_a_guid_counts_as_orphaned(storage, platform, purge):
    platform.add(THIRD_GUID, deletion=_z(_midnight(30)))
    folder = _orphan(storage, "aaaaaaaa", OLD, platform_id="not-a-guid")

    assert _run()["orphaned"] == 1
    _assert_scrubbed(storage, "aaaaaaaa", folder)


@pytest.mark.parametrize("platform_id", ["", GUID])
def test_orphan_younger_than_60_days_is_untouched(storage, platform, purge, platform_id):
    platform.add(THIRD_GUID, deletion=_z(_midnight(30)))
    _orphan(storage, "aaaaaaaa", YOUNG, platform_id=platform_id)

    stats = _run()

    assert stats["orphaned"] == 0 and stats["orphan_kept"] == 1
    row = storage.competitions[("GLOBAL", "aaaaaaaa")]
    assert row["Visible"] is True and row["CreatedBy"] == "x@y"


def test_without_a_created_date_the_newest_timestamp_counts(storage, platform, purge):
    platform.add(THIRD_GUID, deletion=_z(_midnight(30)))
    _orphan(storage, "aaaaaaaa", "-", platform_id=GUID)  # no timestamp at all: never "old"
    _orphan(storage, "bbbbbbbb", "-", platform_id=GUID, DeletedDate=OLD)

    stats = _run()

    assert stats["orphaned"] == 1 and stats["orphan_kept"] == 1
    assert _visible(storage, "aaaaaaaa") is True and _visible(storage, "bbbbbbbb") is False


def test_a_clock_read_error_is_never_not_found(storage, platform, purge, monkeypatch):
    platform.add(THIRD_GUID, deletion=_z(_midnight(30)))
    _orphan(storage, "aaaaaaaa", OLD, platform_id=GUID)

    def throttled(guid, table_client=None):
        raise pc.PlatformClockUnavailable("HttpResponseError")
    monkeypatch.setattr(fa, "read_platform_clock", throttled)

    stats = _run()

    assert stats["orphaned"] == 0 and stats["skipped"] == 1
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["CreatedBy"] == "x@y"


def test_an_empty_clock_table_aborts_the_orphan_step(storage, platform, purge, caplog):
    # Zero clock rows while bound records exist: the wrong table or account.
    _orphan(storage, "aaaaaaaa", OLD, platform_id=GUID)
    _orphan(storage, "bbbbbbbb", OLD)

    with caplog.at_level(logging.ERROR):
        stats = _run()

    assert stats["orphaned"] == 0 and stats["orphan_skipped"] == 2
    assert _visible(storage, "aaaaaaaa") is True and _visible(storage, "bbbbbbbb") is True
    assert any("orphan step aborted" in r.getMessage() and "empty" in r.getMessage() for r in caplog.records)


def test_an_empty_clock_table_is_fine_when_nothing_is_bound(storage, platform, purge):
    _orphan(storage, "bbbbbbbb", OLD)

    assert _run()["orphaned"] == 1


@pytest.mark.parametrize("failure", ["unreachable", "table_missing"])
def test_an_unreadable_clock_table_aborts_the_orphan_step(storage, platform, purge, failure):
    setattr(platform, failure, True)
    _orphan(storage, "bbbbbbbb", OLD)

    stats = _run()

    assert stats["orphaned"] == 0 and stats["orphan_skipped"] == 1
    assert _visible(storage, "bbbbbbbb") is True


def test_without_a_registry_configured_the_orphan_step_is_aborted(storage, purge):
    _orphan(storage, "bbbbbbbb", OLD)

    stats = _run()

    assert stats["orphaned"] == 0 and stats["orphan_skipped"] == 1


def test_a_live_bound_record_is_untouched(storage, platform, purge):
    _orphan(storage, "aaaaaaaa", OLD, platform_id=GUID)
    platform.add(GUID, deletion=_z(_midnight(5)))

    stats = _run()

    assert stats["orphaned"] == 0 and stats["deleted"] == 0
    row = storage.competitions[("GLOBAL", "aaaaaaaa")]
    assert row["Visible"] is True and row["CreatedBy"] == "x@y"


def test_the_kill_switch_only_logs(storage, platform, monkeypatch, caplog):
    monkeypatch.setenv("ORPHAN_PURGE_ENABLED", "false")
    platform.add(THIRD_GUID, deletion=_z(_midnight(30)))
    _orphan(storage, "aaaaaaaa", OLD, platform_id=GUID)

    with caplog.at_level(logging.WARNING):
        stats = _run()

    assert stats["orphaned"] == 0 and stats["orphan_dry_run"] == 1
    assert _visible(storage, "aaaaaaaa") is True
    assert any("would delete" in r.getMessage() for r in caplog.records)


def test_the_timer_logs_the_orphan_counts(storage, platform, purge, caplog):
    platform.add(THIRD_GUID, deletion=_z(_midnight(30)))
    _orphan(storage, "aaaaaaaa", OLD, platform_id=GUID)

    with caplog.at_level(logging.INFO):
        _sweep()

    assert _visible(storage, "aaaaaaaa") is False
    assert any("Orphans: deleted 1" in r.getMessage() for r in caplog.records)
