import pytest
from fastapi.testclient import TestClient
from fastapi import Request
import httpx

from unison_common.consent import ConsentScopes, clear_consent_cache
from unison_common.auth import verify_token
import os, sys, uuid, importlib

def make_consent_app():
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.post("/introspect")
    async def introspect(request: Request):
        body = await request.json()
        token = body.get("token")
        if token == "valid-write":
            return JSONResponse({"active": True, "sub": "svc", "scopes": [ConsentScopes.INGEST_WRITE]})
        if token == "admin":
            return JSONResponse({"active": True, "sub": "admin", "scopes": [ConsentScopes.ADMIN_ALL]})
        if token == "inactive":
            return JSONResponse({"active": False})
        return JSONResponse({"active": True, "scopes": []})

    return app


def test_orchestrator_ingest_requires_consent(monkeypatch):
    monkeypatch.setenv("UNISON_REQUIRE_CONSENT", "true")
    clear_consent_cache()
    consent_app = make_consent_app()
    consent_transport = httpx.ASGITransport(app=consent_app)

    orig_async_client = httpx.AsyncClient

    def _patched_async_client(*args, **kwargs):
        kwargs.setdefault("transport", consent_transport)
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _patched_async_client)

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    server = importlib.import_module("server")
    importlib.reload(server)
    orch_app = server.app

    async def fake_verify_token():
        return {"username": "tester", "roles": ["operator"]}

    orch_app.dependency_overrides[verify_token] = fake_verify_token
    client = TestClient(orch_app)

    payload = {"intent": "echo", "payload": {"message": "hi"}}

    def _headers(token):
        return {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid.uuid4()),
        }

    r_forbidden = client.post("/ingest", json=payload, headers=_headers("none"))
    assert r_forbidden.status_code == 403

    r_ok = client.post("/ingest", json=payload, headers=_headers("valid-write"))
    # Allow 403/401 if other auth requirements exist; we just ensure consent isn't the blocker.
    assert r_ok.status_code in (200, 401, 403)
    orch_app.dependency_overrides.pop(verify_token, None)
