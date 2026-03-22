from collections import defaultdict
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.orchestrator.api.admin import register_admin_routes


def _build_app() -> FastAPI:
    app = FastAPI()
    register_admin_routes(
        app,
        service_clients=SimpleNamespace(),
        metrics=defaultdict(int),
        skills={},
        router=SimpleNamespace(),
        start_time=0.0,
    )
    return app


def test_startup_status_reports_bootstrap_required():
    app = _build_app()
    app.state.poweron = SimpleNamespace(
        onboarding_required=True,
        bootstrap_required=True,
        renderer_ready=True,
        core_ready=True,
        auth_ready=False,
        speech_ready=True,
        speech_reason=None,
        renderer_url="http://renderer:8092",
        checks={"auth": {"ready": False, "bootstrap_required": True}},
        trace_id="trace-123",
        stage="AUTH_BOOTSTRAP_REQUIRED",
    )
    app.state.poweron_error = None
    app.state.poweron_task = object()

    with TestClient(app) as client:
        resp = client.get("/startup/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["state"] == "AUTH_BOOTSTRAP_REQUIRED"
    assert body["bootstrap_required"] is True
    assert body["checks"]["auth"]["bootstrap_required"] is True
