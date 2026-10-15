import json

import azure.functions as func

import function_app as fa


def _handler(fn):
    inner = getattr(fn, "_function", None)
    return inner.get_user_function() if inner else fn


def test_health_needs_no_sign_in_or_secret(monkeypatch):
    monkeypatch.setenv("PROXY_SHARED_SECRET", "s3cret")
    req = func.HttpRequest(method="GET", url="/api/health", body=b"", headers={})
    resp = _handler(fa.health)(req)
    assert resp.status_code == 200
    assert json.loads(resp.get_body()) == {"status": "ok", "service": "fs-scoremodifier"}
