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


def test_startup_status_reports_ready_listening():
    app = _build_app()
    app.state.poweron = SimpleNamespace(
        onboarding_required=False,
        bootstrap_required=False,
        renderer_ready=True,
        core_ready=True,
        auth_ready=True,
        speech_ready=True,
        speech_reason=None,
        renderer_url="http://renderer:8092",
        checks={"auth": {"ready": True, "bootstrap_required": False}},
        trace_id="trace-ready",
        stage="READY_LISTENING",
    )
    app.state.poweron_error = None
    app.state.poweron_task = object()

    with TestClient(app) as client:
        resp = client.get("/startup/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["state"] == "READY_LISTENING"
    assert body["onboarding_required"] is False
    assert body["bootstrap_required"] is False
    assert body["renderer_ready"] is True
    assert body["core_ready"] is True
    assert body["speech_ready"] is True
