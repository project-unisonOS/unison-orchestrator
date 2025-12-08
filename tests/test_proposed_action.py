import pytest

from orchestrator.skills import build_skill_state
from orchestrator.clients import ServiceHttpClient, ServiceClients


class DummyClient(ServiceHttpClient):
    def __init__(self):
        super().__init__("dummy", "0")
        self.last = None

    def post(self, path, payload, *, headers=None):
        self.last = {"path": path, "payload": payload, "headers": headers}
        return True, 200, {"status": "logged", "driver": "logging"}


@pytest.fixture
def service_clients():
    actuation = DummyClient()
    return actuation, ServiceClients(
        context=DummyClient(),
        storage=DummyClient(),
        policy=DummyClient(),
        inference=DummyClient(),
        comms=None,
        actuation=actuation,
        payments=None,
    )


def test_proposed_action_envelope(service_clients):
    actuation_client, clients = service_clients
    state = build_skill_state(clients)
    skills = state["skills"]
    handler = skills.get("proposed_action")
    assert handler is not None

    envelope = {
        "event_id": "evt-123",
        "correlation_id": "corr-1",
        "payload": {
            "person_id": "person-1",
            "target": {"device_id": "light-1", "device_class": "light"},
            "intent": {"name": "turn_on", "parameters": {"level": 10}},
            "risk_level": "medium",
            "constraints": {"max_duration_ms": 1000},
        },
    }

    result = handler(envelope)
    assert result["ok"] is True
    sent = actuation_client.last
    assert sent is not None
    assert sent["path"] == "/actuate"
    payload = sent["payload"]
    assert payload["person_id"] == "person-1"
    assert payload["target"]["device_class"] == "light"
    assert payload["intent"]["name"] == "turn_on"
    assert payload["risk_level"] == "medium"
    assert payload["constraints"]["max_duration_ms"] == 1000
    # Ensure provenance/correlation is attached
    assert payload["provenance"]["orchestrator_task_id"] == "evt-123"
    assert payload["correlation_id"] == "corr-1"
