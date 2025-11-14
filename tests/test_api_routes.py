from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.orchestrator.api import register_event_routes
from src.orchestrator.config import ServiceEndpoints


class RateLimiterStub:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.rate = 100
        self.per = 60

    def is_allowed(self, _key):
        return self.allowed


class PerfMonitorStub:
    def __init__(self):
        self.records = []

    def record(self, metric, value):
        self.records.append((metric, value))


@pytest.fixture
def fake_user():
    return {"username": "tester", "roles": ["operator"]}


@pytest.fixture
def service_clients():
    def make_service():
        service = Mock()
        service.get.return_value = (True, 200, {"ok": True})
        service.post.return_value = (True, 200, {"ok": True})
        return service

    return SimpleNamespace(
        context=make_service(),
        storage=make_service(),
        policy=make_service(),
        inference=make_service(),
    )


@pytest.fixture
def route_app(monkeypatch, fake_user, service_clients):
    async def fake_verify_token():
        return fake_user

    monkeypatch.setattr("src.orchestrator.api.routes.verify_token", fake_verify_token)

    perf_monitor = PerfMonitorStub()
    monkeypatch.setattr(
        "src.orchestrator.api.routes.get_performance_monitor",
        lambda: perf_monitor,
    )

    user_limiter = RateLimiterStub()
    endpoint_limiter = RateLimiterStub()
    monkeypatch.setattr(
        "src.orchestrator.api.routes.get_user_rate_limiter", lambda: user_limiter
    )
    monkeypatch.setattr(
        "src.orchestrator.api.routes.get_endpoint_rate_limiter",
        lambda: endpoint_limiter,
    )

    captured_envelopes = []

    def fake_store_processing_envelope(**kwargs):
        captured_envelopes.append(kwargs)

    monkeypatch.setattr(
        "src.orchestrator.api.routes.store_processing_envelope",
        fake_store_processing_envelope,
    )
    monkeypatch.setattr(
        "src.orchestrator.api.routes.time.strftime",
        lambda fmt, struct_time: "2025-01-01T00:00:00Z",
    )

    app = FastAPI()
    metrics = defaultdict(int)
    pending_confirms = {}
    prune_calls = []

    def prune():
        prune_calls.append(True)

    endpoints = ServiceEndpoints(
        context_host="ctx",
        context_port="8100",
        storage_host="stor",
        storage_port="8101",
        policy_host="pol",
        policy_port="8102",
        inference_host="inf",
        inference_port="8103",
    )

    skills = {"echo": lambda envelope: {"echo": envelope.get("payload", {})}}

    register_event_routes(
        app,
        service_clients=service_clients,
        skills=skills,
        metrics=metrics,
        pending_confirms=pending_confirms,
        confirm_ttl_seconds=60,
        require_consent_flag=False,
        prune_pending=prune,
        endpoints=endpoints,
    )

    client = TestClient(app)
    return SimpleNamespace(
        client=client,
        metrics=metrics,
        pending=pending_confirms,
        prune_calls=prune_calls,
        store_events=captured_envelopes,
        user_limiter=user_limiter,
        endpoint_limiter=endpoint_limiter,
        perf_monitor=perf_monitor,
        service_clients=service_clients,
        skills=skills,
    )


def make_envelope():
    return {
        "timestamp": "2025-10-25T00:00:00Z",
        "source": "unit-test",
        "intent": "echo",
        "payload": {"message": "hello"},
    }


def test_event_route_executes_skill(route_app):
    route_app.service_clients.policy.post.return_value = (
        True,
        200,
        {"decision": {"allowed": True}},
    )

    resp = route_app.client.post("/event", json=make_envelope())
    if resp.status_code != 200:
        pytest.fail(f"Unexpected response: {resp.status_code} -> {resp.text}")
    body = resp.json()
    assert body["ok"] is True
    assert body["intent"] == "echo"
    assert route_app.metrics["/event"] == 1
    route_app.service_clients.policy.post.assert_called_once()


def test_event_policy_denied(route_app):
    route_app.service_clients.policy.post.return_value = (
        True,
        200,
        {"decision": {"allowed": False, "reason": "nope"}},
    )
    resp = route_app.client.post("/event", json=make_envelope())
    assert resp.status_code == 403
    assert "Policy denied" in resp.text


def test_introspect_returns_service_health(route_app):
    route_app.service_clients.context.get.return_value = (True, 200, {})
    route_app.service_clients.storage.get.return_value = (True, 200, {})
    route_app.service_clients.inference.get.return_value = (True, 200, {})

    def policy_get(path, headers=None):
        if path == "/rules/summary":
            return True, 200, {"count": 2}
        return True, 200, {}

    route_app.service_clients.policy.get.side_effect = policy_get

    resp = route_app.client.get("/introspect")
    assert resp.status_code == 200
    data = resp.json()
    assert data["services"]["context"]["ok"] is True
    assert data["services"]["storage"]["ok"] is True
    assert data["services"]["policy"]["ok"] is True
    assert data["policy_rules"]["summary"]["count"] == 2


def test_confirm_event_loads_from_storage(route_app):
    route_app.pending.clear()
    route_app.service_clients.storage.get.return_value = (
        True,
        200,
        {
            "ok": True,
            "envelope": {
                "intent": "echo",
                "payload": {"message": "confirmed"},
                "source": "cli",
            },
        },
    )

    resp = route_app.client.post("/event/confirm", json={"confirmation_token": "abc"})
    assert resp.status_code == 200
    assert resp.json()["confirmed"] is True
    route_app.service_clients.storage.post.assert_called_with(
        "/kv/delete/confirm/abc", {}
    )
    assert "abc" not in route_app.pending


def test_ingest_success_records_events(route_app):
    resp = route_app.client.post(
        "/ingest",
        json={"intent": "echo", "payload": {"message": "hi"}, "source": "cli"},
        headers={"content-type": "application/json"},
    )
    if resp.status_code != 200:
        pytest.fail(f"Unexpected response: {resp.status_code} -> {resp.text}")
    assert route_app.metrics["/ingest"] == 1
    assert len(route_app.store_events) >= 3  # request, skill_start, skill_complete
    assert route_app.perf_monitor.records  # latency recorded


def test_ingest_rate_limited(route_app):
    route_app.user_limiter.allowed = False
    resp = route_app.client.post(
        "/ingest",
        json={"intent": "echo", "payload": {"message": "hi"}, "source": "cli"},
        headers={"content-type": "application/json"},
    )
    if resp.status_code != 429:
        pytest.fail(f"Unexpected response: {resp.status_code} -> {resp.text}")
