"""The proxy auth contract (PROXY-CONTRACT.md): the shared-secret gate fails
closed, identity comes only from the router's x-forwarded-user-* headers, and
every route but health answers 401 without an identity."""

import base64
import json

import pytest

from conftest import TEST_EMAIL, TEST_OID, TEST_SECRET, TEST_TID, call, make_request

import function_app as fa


def _jwt(payload: dict) -> str:
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{b({'alg': 'none'})}.{b(payload)}.sig"


def _gated_calls(synthetic_pdf, **kw):
    """Every gated route, called with the given request options."""
    return {
        "check_user_permission": call(fa.check_user_permission, make_request("check_user_permission", method="GET", **kw)),
        "generate": call(fa.generate, make_request("generate", body=synthetic_pdf, **kw)),
        "generate_results": call(fa.generate_results, make_request("generate_results", body=synthetic_pdf, **kw)),
        "parse_index": call(fa.parse_index, make_request("parse_index", method="GET", params={"url": "x"}, **kw)),
        "list_competitions": call(fa.list_competitions, make_request("list_competitions", method="GET", **kw)),
        "get_competition_details": call(fa.get_competition_details, make_request("get_competition_details", method="GET", params={"id": "x"}, **kw)),
        "delete_competition": call(fa.delete_competition, make_request("delete_competition", params={"id": "x"}, **kw)),
    }


# ── the proxy secret ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("configured", [None, ""], ids=["unset", "empty"])
def test_secret_fails_closed_when_not_configured(configured, monkeypatch, synthetic_pdf):
    if configured is None:
        monkeypatch.delenv("PROXY_SHARED_SECRET", raising=False)
    else:
        monkeypatch.setenv("PROXY_SHARED_SECRET", configured)
    req = make_request("x")
    assert fa._proxy_secret_ok(req) is False
    assert fa._proxy_secret_ok(make_request("x", secret="")) is False
    assert fa.get_user(req) is None
    # Admin header included: it is worthless without the secret.
    for name, resp in _gated_calls(synthetic_pdf, admin=True).items():
        assert resp.status_code == 401, name


def test_secret_mismatch_rejects_before_identity():
    assert fa.get_user(make_request("x")) is not None
    assert fa.get_user(make_request("x", secret="nope")) is None
    assert fa.get_user(make_request("x", secret=None)) is None
    assert fa.get_user(make_request("x", secret=TEST_SECRET + "x")) is None


def test_secret_is_compared_in_constant_time(monkeypatch):
    seen = []
    real = fa.hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(fa.hmac, "compare_digest", spy)
    assert fa._proxy_secret_ok(make_request("x")) is True
    assert seen == [(TEST_SECRET.encode(), TEST_SECRET.encode())]


# ── identity: x-forwarded-user-* only ────────────────────────────────────────

def test_identity_from_forwarded_headers():
    user = fa.get_user(make_request("x", headers={"x-forwarded-user-name": "Matti%20M%C3%A4kinen"}))
    assert user == fa.User(tid=TEST_TID, oid=TEST_OID, email=TEST_EMAIL, name="Matti Mäkinen", is_admin=False)


def test_identity_is_normalised():
    user = fa.get_user(make_request(
        "x", tid=f" {TEST_TID.upper()} ", oid=TEST_OID.upper(), email="  Some.One@Example.COM ",
    ))
    assert (user.tid, user.oid, user.email) == (TEST_TID, TEST_OID, "some.one@example.com")


def test_email_and_name_are_optional():
    user = fa.get_user(make_request("x", email=None))
    assert user is not None and user.email == "" and user.name == ""


@pytest.mark.parametrize("tid,oid", [
    (None, TEST_OID),
    (TEST_TID, None),
    (None, None),
    ("not-a-guid", TEST_OID),
    (TEST_TID, "1234"),
    (TEST_TID, TEST_OID + "0"),
    ("tester@example.com", TEST_OID),
])
def test_missing_or_invalid_tid_oid_is_no_identity(tid, oid):
    assert fa.get_user(make_request("x", tid=tid, oid=oid)) is None


def test_legacy_identity_sources_are_ignored():
    """Easy Auth principal headers, a base64 principal and an unsigned Bearer
    JWT gave an identity before; behind the router none of them may."""
    principal = base64.b64encode(json.dumps({
        "userDetails": "swa@example.com",
        "claims": [{"typ": "oid", "val": TEST_OID}, {"typ": "tid", "val": TEST_TID}],
    }).encode()).decode()
    bearer = f"Bearer {_jwt({'preferred_username': 'jwt@example.com', 'oid': TEST_OID, 'tid': TEST_TID})}"
    req = make_request("x", anonymous=True, headers={
        "X-MS-CLIENT-PRINCIPAL-NAME": "direct@example.com",
        "X-MS-CLIENT-PRINCIPAL-ID": TEST_OID,
        "x-ms-client-principal": principal,
        "Authorization": bearer,
        "x-forwarded-user-email": "fwd@example.com",  # an e-mail alone is not an identity
    })
    assert fa.get_user(req) is None
    assert not hasattr(fa, "get_user_email_from_header")
    assert not hasattr(fa, "_decode_jwt_payload")


def test_spoofed_identity_without_the_secret_gives_nothing(synthetic_pdf):
    """A direct caller can set every x-forwarded-user-* header (admin too), but
    without the router's secret it is anonymous."""
    for name, resp in _gated_calls(synthetic_pdf, secret="guessed", admin=True).items():
        assert resp.status_code == 401, name


@pytest.mark.parametrize("value,expected", [("1", True), ("true", False), ("yes", False), ("0", False), ("", False), (" 1", False)])
def test_admin_only_on_exact_header(value, expected):
    req = make_request("x", headers={"x-forwarded-user-admin": value})
    assert fa.get_user(req).is_admin is expected
    assert fa.get_user(make_request("x")).is_admin is False


# ── routes ────────────────────────────────────────────────────────────────────

def test_check_user_permission_probe():
    resp = call(fa.check_user_permission, make_request("check_user_permission", method="GET"))
    assert resp.status_code == 200
    assert json.loads(resp.get_body()) == {"allowed": True, "email": TEST_EMAIL}

    resp = call(fa.check_user_permission, make_request("check_user_permission", method="GET", anonymous=True))
    assert resp.status_code == 401
    assert json.loads(resp.get_body()) == {"allowed": False, "email": None}


def test_every_route_requires_identity(synthetic_pdf):
    for name, resp in _gated_calls(synthetic_pdf, anonymous=True).items():
        assert resp.status_code == 401, name


def test_health_needs_no_secret_even_when_unset(monkeypatch):
    monkeypatch.delenv("PROXY_SHARED_SECRET", raising=False)
    resp = call(fa.health, make_request("health", method="GET", secret=None, anonymous=True))
    assert resp.status_code == 200


def test_missing_identity_log_has_no_header_values(caplog):
    req = make_request("x", anonymous=True, headers={"X-Client-IP": "203.0.113.7", "X-Other": "someone@example.com"})
    with caplog.at_level("WARNING"):
        assert fa.get_user(req) is None
    logged = caplog.text
    assert "x-client-ip" in logged and "x-other" in logged  # names only
    assert "203.0.113.7" not in logged and "someone@example.com" not in logged
    assert TEST_SECRET not in logged
