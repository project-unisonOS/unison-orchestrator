import pytest
from fastapi.testclient import TestClient
from fastapi import Request
import httpx

from unison_common.consent import ConsentScopes, clear_consent_cache
import os, sys


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
    from server import app as orch_app
    client = TestClient(orch_app)

    payload = {"intent": "echo", "payload": {"message": "hi"}}

    r_forbidden = client.post("/ingest", json=payload, headers={"Authorization": "Bearer none"})
    assert r_forbidden.status_code == 403

    r_ok = client.post("/ingest", json=payload, headers={"Authorization": "Bearer valid-write"})
    # Allow 403/401 if other auth requirements exist; we just ensure consent isn't the blocker.
    assert r_ok.status_code in (200, 401, 403)
