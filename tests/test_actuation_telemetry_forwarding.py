import os

import pytest

from orchestrator.skills import build_skill_state
from orchestrator.clients import ServiceClients, ServiceHttpClient


class RecordingClient(ServiceHttpClient):
    def __init__(self):
        super().__init__("dummy", "0")
        self.posts = []

    def post(self, path, payload, *, headers=None):
        self.posts.append({"path": path, "payload": payload})
        # Fake actuation response
        if path == "/actuate":
            return True, 200, {"status": "logged"}
        return True, 200, {}


class DummyHTTPXClient:
    def __init__(self, *args, **kwargs):
        self.targets = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json=None):
        self.targets.append({"url": url, "json": json})


@pytest.fixture
def service_clients(monkeypatch):
    actuation = RecordingClient()
    consent = RecordingClient()
    monkeypatch.setenv("UNISON_CONTEXT_GRAPH_URL", "http://context-graph:8081")
    monkeypatch.setenv("UNISON_EXPERIENCE_RENDERER_URL", "http://renderer:8082")
    # Monkeypatch httpx.Client used in skills._publish_actuation_telemetry
    import orchestrator.skills as skills

    dummy = DummyHTTPXClient()
    monkeypatch.setattr(skills.httpx, "Client", lambda timeout=2.0: dummy)
    clients = ServiceClients(
        context=RecordingClient(),
        storage=RecordingClient(),
        policy=RecordingClient(),
        inference=RecordingClient(),
        comms=None,
        actuation=actuation,
        consent=consent,
        payments=None,
    )
    return dummy, clients


def test_actuation_telemetry_forwarded(service_clients):
    httpx_dummy, clients = service_clients
    skills = build_skill_state(clients)["skills"]
    handler = skills["proposed_action"]
    envelope = {
        "event_id": "evt-telemetry",
        "correlation_id": "corr-telemetry",
        "payload": {
            "person_id": "person-1",
            "target": {"device_id": "light-1", "device_class": "light"},
            "intent": {"name": "turn_on", "parameters": {"level": 50}},
        },
    }
    result = handler(envelope)
    assert result["ok"] is True
    urls = [t["url"] for t in httpx_dummy.targets]
    assert "http://context-graph:8081/telemetry/actuation" in urls
    assert "http://renderer:8082/telemetry/actuation" in urls
