"""Competition access (ACCESS-CONTRACT / platform_access.py): every route that
touches a competition asks the platform which role the caller has.

generate / generate_results need write, delete_competition (POST only) needs
owner, get_competition_details / list_competitions need read, parse_index only
a sign-in. No role -> 404 platform_not_found, read on a write route -> 403
platform_read_only, read/write on an owner route -> 403
platform_owner_required, unreadable access tables -> 503 platform_unavailable.
Records without a PlatformId (legacy) are admin-only.
"""

import json

import pytest

from conftest import TEST_OID, TEST_PLATFORM_ID, TEST_TID, TEST_USER_ID, call, make_request

import function_app as fa
import platform_access

GUID = TEST_PLATFORM_ID
OTHER_GUID = "6f9619ff-8b86-4d11-b42d-00c04fc964ff"
THIRD_GUID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
STRANGER_TID = TEST_TID
STRANGER_OID = "00000000-1111-4222-8333-444444444444"
SHARED_USER_ID = "12345678-1234-4234-8234-123456789abc"
OTHER_USER_ID = "fedcba98-7654-4321-8fed-cba987654321"

# Captured at import, before the autouse `access` fixture replaces them.
REAL_IDENTITIES_CLIENT = platform_access.get_identities_table_client
REAL_ACCESS_CLIENT = platform_access.get_access_table_client


@pytest.fixture
def strict(access):
    """Explicit grants only: the test user has no role until granted."""
    access.grant_all = None
    return access


@pytest.fixture
def fake_split(monkeypatch, synthetic_pdf):
    monkeypatch.setattr(fa, "split_per_skater", lambda pdf, **kw: synthetic_pdf)
    return synthetic_pdf


@pytest.fixture
def fake_results(monkeypatch):
    monkeypatch.setattr(fa, "extract_results", lambda pdf: ([], "TULOKKAAT"))
    monkeypatch.setattr(fa, "render_results_pdf", lambda meta, teams: b"%PDF-results")
    monkeypatch.setattr(fa, "render_results_html", lambda meta, teams: "<html></html>")


def _generate(pdf, platform_id=GUID, **kw):
    return call(fa.generate, make_request("generate", body=pdf, params={"platformId": platform_id}, **kw))


def _generate_results(pdf, platform_id=GUID, **kw):
    return call(fa.generate_results, make_request("generate_results", body=pdf, params={"platformId": platform_id}, **kw))


GENERATORS = pytest.mark.parametrize("run", [_generate, _generate_results], ids=["generate", "generate_results"])


def _error(resp):
    return json.loads(resp.get_body())


# ── generate / generate_results: write ────────────────────────────────────────

@GENERATORS
@pytest.mark.parametrize("role", ["owner", "write"])
def test_generation_with_write_access(run, role, strict, fake_split, fake_results, storage):
    strict.grant(GUID, role)
    resp = run(fake_split)
    assert resp.status_code == 200, resp.get_body()
    body = json.loads(resp.get_body())
    assert storage.competitions[("GLOBAL", body["id"])]["PlatformId"] == GUID


@GENERATORS
def test_generation_read_only_is_403(run, strict, fake_split, fake_results, storage):
    strict.grant(GUID, "read")
    resp = run(fake_split)
    assert resp.status_code == 403
    assert _error(resp) == {"error": "platform_read_only"}
    assert storage.competitions == {} and storage.blobs == {}


@GENERATORS
def test_generation_without_access_is_404(run, strict, fake_split, fake_results, storage):
    strict.grant(OTHER_GUID, "owner")  # a grant elsewhere does not count
    strict.grant(GUID, "owner", user_id=OTHER_USER_ID)
    resp = run(fake_split)
    assert resp.status_code == 404
    assert _error(resp) == {"error": "platform_not_found"}
    assert storage.competitions == {} and storage.blobs == {}


@GENERATORS
def test_generation_unknown_identity_is_404(run, access, fake_split, fake_results, storage):
    """grant_all is on, but this tid/oid was never mapped to a platform user."""
    resp = run(fake_split, oid=STRANGER_OID)
    assert resp.status_code == 404
    assert _error(resp) == {"error": "platform_not_found"}
    assert storage.competitions == {}


@GENERATORS
def test_generation_admin_needs_no_grant(run, strict, fake_split, fake_results, storage):
    resp = run(fake_split, admin=True, oid=STRANGER_OID)
    assert resp.status_code == 200, resp.get_body()
    assert strict.reads == 0  # admin is owner without any lookup


@GENERATORS
@pytest.mark.parametrize("failure", ["unreachable", "table_missing"])
def test_generation_access_unreadable_is_503(run, failure, access, fake_split, fake_results, storage):
    setattr(access, failure, True)
    resp = run(fake_split)
    assert resp.status_code == 503
    assert _error(resp) == {"error": "platform_unavailable"}
    assert storage.competitions == {} and storage.blobs == {}


@GENERATORS
def test_generation_without_platform_account_is_503(run, monkeypatch, fake_split, fake_results, storage):
    """The real client factories fail closed when PLATFORM_STORAGE_ACCOUNT is unset."""
    monkeypatch.setattr(platform_access, "get_identities_table_client", REAL_IDENTITIES_CLIENT)
    monkeypatch.setattr(platform_access, "get_access_table_client", REAL_ACCESS_CLIENT)
    monkeypatch.delenv("PLATFORM_STORAGE_ACCOUNT", raising=False)
    resp = run(fake_split)
    assert resp.status_code == 503
    assert _error(resp) == {"error": "platform_unavailable"}
    assert storage.competitions == {}


@GENERATORS
def test_access_is_checked_before_the_upload(run, strict):
    """A read-only caller learns nothing about body validation."""
    strict.grant(GUID, "read")
    assert run(b"").status_code == 403
    assert run(b"not a pdf", platform_id=OTHER_GUID).status_code == 404


@GENERATORS
def test_platform_id_still_required(run, strict):
    resp = run(b"%PDF-", platform_id="")
    assert resp.status_code == 400
    assert _error(resp) == {"error": "platform_id_required"}
    assert strict.reads == 0


def test_revoked_share_stops_after_the_cache(strict, fake_split, storage):
    strict.grant(GUID, "write")
    assert _generate(fake_split).status_code == 200
    del strict.grants[(GUID, TEST_USER_ID)]
    assert _generate(fake_split).status_code == 200  # cached answer (<= CACHE_SECONDS)
    platform_access.clear_cache()
    assert _generate(fake_split).status_code == 404


# ── records: details (read), delete (write), list (read) ─────────────────────

def _seed(storage, comp_id, platform_id="", visible=True):
    folder = f"Comp {comp_id}-{comp_id}"
    row = {
        "PartitionKey": "GLOBAL", "RowKey": comp_id, "Name": f"Comp {comp_id}", "FolderPath": folder,
        "CreatedBy": "x@y", "CreatedDate": "2026-10-01T00:00:00Z", "Visible": visible,
        "DeletionDate": "2026-12-01T00:00:00Z",
    }
    if platform_id:
        row["PlatformId"] = platform_id
    storage.competitions[("GLOBAL", comp_id)] = row
    storage.blobs[f"{folder}/source.pdf"] = b"%PDF-src"
    return folder


def _details(comp_id, **kw):
    return call(fa.get_competition_details, make_request("get_competition_details", method="GET", params={"id": comp_id}, **kw))


def _delete(comp_id, **kw):
    return call(fa.delete_competition, make_request("delete_competition", params={"id": comp_id}, **kw))


def _list(**kw):
    resp = call(fa.list_competitions, make_request("list_competitions", method="GET", **kw))
    return resp, (json.loads(resp.get_body()) if resp.status_code == 200 else None)


@pytest.mark.parametrize("role", ["read", "write", "owner"])
def test_details_with_read_access(role, strict, storage):
    _seed(storage, "aaaaaaaa", GUID)
    strict.grant(GUID, role)
    resp = _details("aaaaaaaa")
    assert resp.status_code == 200
    assert json.loads(resp.get_body())["platformId"] == GUID


def test_details_without_access_looks_like_an_unknown_record(strict, storage):
    _seed(storage, "aaaaaaaa", GUID)
    no_access = _details("aaaaaaaa")
    unknown = _details("zzzzzzzz")
    assert no_access.status_code == unknown.status_code == 404
    assert _error(no_access) == _error(unknown) == {"error": "platform_not_found"}


@pytest.mark.parametrize("platform_id", ["", "not-a-guid"])
def test_legacy_records_are_admin_only(platform_id, access, storage):
    _seed(storage, "aaaaaaaa", platform_id)
    _seed(storage, "bbbbbbbb", platform_id)
    assert _error(_details("aaaaaaaa")) == {"error": "platform_not_found"}
    assert _error(_delete("aaaaaaaa")) == {"error": "platform_not_found"}
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["Visible"] is True
    assert _details("aaaaaaaa", admin=True).status_code == 200
    assert _delete("bbbbbbbb", admin=True).status_code == 200
    assert storage.competitions[("GLOBAL", "bbbbbbbb")]["Visible"] is False


def test_details_access_unreadable_is_503(access, storage):
    _seed(storage, "aaaaaaaa", GUID)
    access.unreachable = True
    resp = _details("aaaaaaaa")
    assert resp.status_code == 503
    assert _error(resp) == {"error": "platform_unavailable"}


@pytest.mark.parametrize("role", ["read", "write"])
def test_delete_needs_owner(role, strict, storage):
    folder = _seed(storage, "aaaaaaaa", GUID)
    strict.grant(GUID, role)
    resp = _delete("aaaaaaaa")
    assert resp.status_code == 403
    assert _error(resp) == {"error": "platform_owner_required"}
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["Visible"] is True
    assert f"{folder}/source.pdf" in storage.blobs

    strict.grant(GUID, "owner")
    platform_access.clear_cache()
    assert _delete("aaaaaaaa").status_code == 200
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["Visible"] is False
    assert f"{folder}/source.pdf" not in storage.blobs


def test_admin_deletes_without_a_grant(strict, storage):
    _seed(storage, "aaaaaaaa", GUID)
    assert _delete("aaaaaaaa", admin=True, oid=STRANGER_OID).status_code == 200
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["Visible"] is False


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT"])
def test_delete_is_post_only(method, storage):
    folder = _seed(storage, "aaaaaaaa", GUID)
    resp = call(fa.delete_competition, make_request("delete_competition", method=method, params={"id": "aaaaaaaa"}))
    assert resp.status_code == 405
    assert resp.headers["Allow"] == "POST"
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["Visible"] is True
    assert f"{folder}/source.pdf" in storage.blobs


def test_delete_route_declares_post_only():
    bindings = fa.delete_competition._function.get_bindings_dict()["bindings"]
    (trigger,) = [b for b in bindings if b.get("type") == "httpTrigger"]
    assert [str(getattr(m, "value", m)).upper() for m in trigger["methods"]] == ["POST"]


def test_delete_takes_the_id_from_a_json_body(storage):
    _seed(storage, "aaaaaaaa", GUID)
    resp = call(fa.delete_competition, make_request(
        "delete_competition", body=json.dumps({"id": "aaaaaaaa"}).encode(),
        headers={"content-type": "application/json"}))
    assert resp.status_code == 200, resp.get_body()
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["Visible"] is False


@pytest.mark.parametrize("body", [b"", b"{}", b'{"id": ""}', b'{"id": 5}', b'["x"]', b"not json"])
def test_delete_without_an_id_is_400(body, storage):
    _seed(storage, "aaaaaaaa", GUID)
    assert call(fa.delete_competition, make_request("delete_competition", body=body)).status_code == 400
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["Visible"] is True


def test_delete_without_access_is_404(strict, storage):
    _seed(storage, "aaaaaaaa", GUID)
    resp = _delete("aaaaaaaa")
    assert resp.status_code == 404
    assert _error(resp) == {"error": "platform_not_found"}
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["Visible"] is True
    assert _error(_delete("zzzzzzzz")) == {"error": "platform_not_found"}


def test_delete_access_unreadable_is_503(access, storage):
    _seed(storage, "aaaaaaaa", GUID)
    access.table_missing = True
    resp = _delete("aaaaaaaa")
    assert resp.status_code == 503
    assert storage.competitions[("GLOBAL", "aaaaaaaa")]["Visible"] is True


def test_list_shows_only_readable_records(strict, storage):
    _seed(storage, "aaaaaaaa", GUID)  # read
    _seed(storage, "bbbbbbbb", OTHER_GUID)  # no grant
    _seed(storage, "cccccccc")  # legacy
    _seed(storage, "dddddddd", THIRD_GUID)  # write
    _seed(storage, "eeeeeeee", GUID)  # read, second record of the same competition
    _seed(storage, "ffffffff", GUID, visible=False)  # soft-deleted
    strict.grant(GUID, "read")
    strict.grant(THIRD_GUID, "write")
    strict.grant(OTHER_GUID, "owner", user_id=OTHER_USER_ID)

    resp, rows = _list()
    assert resp.status_code == 200
    assert {r["id"] for r in rows} == {"aaaaaaaa", "dddddddd", "eeeeeeee"}
    # One identity read + one grant read per distinct competition.
    assert strict.reads == 1 + 3
    assert strict.clients == 2  # one client pair for the whole request

    resp, rows = _list(admin=True)
    assert {r["id"] for r in rows} == {"aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd", "eeeeeeee"}


def test_list_for_an_unknown_identity_is_empty(access, storage):
    _seed(storage, "aaaaaaaa", GUID)
    resp, rows = _list(oid=STRANGER_OID)
    assert resp.status_code == 200 and rows == []


def test_list_without_bound_records_reads_no_access_tables(strict, storage):
    _seed(storage, "cccccccc")  # legacy only
    strict.unreachable = True
    resp, rows = _list()
    assert resp.status_code == 200 and rows == []
    assert strict.clients == 0


@pytest.mark.parametrize("failure", ["unreachable", "table_missing"])
def test_list_access_unreadable_is_503(failure, access, storage):
    _seed(storage, "aaaaaaaa", GUID)
    setattr(access, failure, True)
    resp, _ = _list()
    assert resp.status_code == 503
    assert _error(resp) == {"error": "platform_unavailable"}


def test_list_admin_needs_no_access_tables(access, storage):
    _seed(storage, "aaaaaaaa", GUID)
    access.unreachable = True
    resp, rows = _list(admin=True)
    assert resp.status_code == 200 and [r["id"] for r in rows] == ["aaaaaaaa"]


def test_shared_user_sees_the_record(strict, storage):
    """A second platform user granted read sees it; the test user without a grant does not."""
    _seed(storage, "aaaaaaaa", GUID)
    strict.add_user(STRANGER_TID, STRANGER_OID, SHARED_USER_ID)
    strict.grant(GUID, "read", user_id=SHARED_USER_ID)
    assert _details("aaaaaaaa", oid=STRANGER_OID).status_code == 200
    assert _details("aaaaaaaa", tid=TEST_TID, oid=TEST_OID).status_code == 404


# ── parse_index: sign-in only ─────────────────────────────────────────────────

def test_parse_index_needs_no_competition_access(strict, monkeypatch):
    strict.unreachable = True  # would 503 if it were consulted
    monkeypatch.setattr(fa, "fetch_index_html", lambda url, allowed_hosts: "<html><title>Cup</title></html>")
    resp = call(fa.parse_index, make_request("parse_index", method="GET", params={"url": "https://figureskatingresults.fi/x/index.htm"}))
    assert resp.status_code == 200
    assert strict.reads == 0
    assert call(fa.parse_index, make_request("parse_index", method="GET", params={"url": "x"}, anonymous=True)).status_code == 401
