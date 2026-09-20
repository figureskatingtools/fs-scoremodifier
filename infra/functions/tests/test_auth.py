"""The proxy auth contract (PROXY-CONTRACT.md): shared-secret gate first, then
identity header precedence; every route answers 401 without an identity."""

import base64
import json

from conftest import call, make_request

import function_app as fa


def _jwt(payload: dict) -> str:
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{b({'alg': 'none'})}.{b(payload)}.sig"


def test_secret_not_enforced_when_unset():
    req = make_request("check_user_permission", email=None, headers={"x-forwarded-user-email": "a@b.c"})
    assert fa._proxy_secret_ok(req) is True
    assert fa.get_user_email_from_header(req) == "a@b.c"


def test_secret_mismatch_rejects_before_identity(monkeypatch):
    monkeypatch.setenv("PROXY_SHARED_SECRET", "s3cret")
    ok = make_request("x", headers={"X-Proxy-Secret": "s3cret"})
    bad = make_request("x", headers={"X-Proxy-Secret": "nope"})
    missing = make_request("x")
    assert fa.get_user_email_from_header(ok) == "tester@example.com"
    assert fa.get_user_email_from_header(bad) is None
    assert fa.get_user_email_from_header(missing) is None


def test_identity_header_precedence():
    principal = base64.b64encode(json.dumps({"userDetails": "swa@example.com"}).encode()).decode()
    bearer = f"Bearer {_jwt({'preferred_username': 'jwt@example.com'})}"

    req = make_request("x", email="fwd@example.com", headers={
        "X-MS-CLIENT-PRINCIPAL-NAME": "direct@example.com",
        "x-ms-client-principal": principal,
        "Authorization": bearer,
    })
    assert fa.get_user_email_from_header(req) == "direct@example.com"

    req = make_request("x", email="fwd@example.com", headers={"x-ms-client-principal": principal, "Authorization": bearer})
    assert fa.get_user_email_from_header(req) == "fwd@example.com"

    req = make_request("x", email=None, headers={"x-ms-client-principal": principal, "Authorization": bearer})
    assert fa.get_user_email_from_header(req) == "swa@example.com"

    req = make_request("x", email=None, headers={"Authorization": bearer})
    assert fa.get_user_email_from_header(req) == "jwt@example.com"

    assert fa.get_user_email_from_header(make_request("x", email=None)) is None


def test_jwt_claim_fallbacks():
    for claims, expected in [
        ({"email": "e@x"}, "e@x"),
        ({"upn": "u@x"}, "u@x"),
        ({"unique_name": "n@x"}, "n@x"),
        ({"emails": ["first@x", "second@x"]}, "first@x"),
        ({"name": "Some Name"}, "Some Name"),
        ({"oid": "1234"}, "1234"),
    ]:
        req = make_request("x", email=None, headers={"Authorization": f"Bearer {_jwt(claims)}"})
        assert fa.get_user_email_from_header(req) == expected, claims
    req = make_request("x", email=None, headers={"Authorization": "Bearer not.a.jwt.at.all"})
    assert fa.get_user_email_from_header(req) is None


def test_check_user_permission_probe():
    resp = call(fa.check_user_permission, make_request("check_user_permission", method="GET"))
    assert resp.status_code == 200
    assert json.loads(resp.get_body()) == {"allowed": True, "email": "tester@example.com"}

    resp = call(fa.check_user_permission, make_request("check_user_permission", method="GET", email=None))
    assert resp.status_code == 401
    assert json.loads(resp.get_body()) == {"allowed": False, "email": None}


def test_every_route_requires_identity(synthetic_pdf):
    anon = dict(email=None)
    assert call(fa.generate, make_request("generate", body=synthetic_pdf, **anon)).status_code == 401
    assert call(fa.generate_results, make_request("generate_results", body=synthetic_pdf, **anon)).status_code == 401
    assert call(fa.parse_index, make_request("parse_index", method="GET", params={"url": "x"}, **anon)).status_code == 401
    assert call(fa.list_competitions, make_request("list_competitions", method="GET", **anon)).status_code == 401
    assert call(fa.get_competition_details, make_request("get_competition_details", method="GET", params={"id": "x"}, **anon)).status_code == 401
    assert call(fa.delete_competition, make_request("delete_competition", params={"id": "x"}, **anon)).status_code == 401
