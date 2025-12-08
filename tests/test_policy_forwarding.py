import sys
from pathlib import Path

import pytest

from orchestrator.skills import build_skill_state
from orchestrator.clients import ServiceClients, ServiceHttpClient


class RecordingClient(ServiceHttpClient):
    def __init__(self):
        super().__init__("dummy", "0")
        self.posts = []
        self.gets = []
        self.stub_grants = []

    def post(self, path, payload, *, headers=None):
        self.posts.append({"path": path, "payload": payload, "headers": headers})
        return True, 200, {"allowed": True}

    def get(self, path, *, headers=None):
        self.gets.append({"path": path, "headers": headers})
        # Simulate consent grants endpoint
        if path.startswith("/grants/"):
            return True, 200, {"grants": self.stub_grants}
        return True, 200, {}


@pytest.fixture
def service_clients():
    policy = RecordingClient()
    consent = RecordingClient()
    actuation = RecordingClient()
    clients = ServiceClients(
        context=RecordingClient(),
        storage=RecordingClient(),
        policy=policy,
        inference=RecordingClient(),
        comms=None,
        actuation=actuation,
        consent=consent,
        payments=None,
    )
    return policy, consent, actuation, clients


def test_policy_context_and_consent_forwarding(service_clients):
    policy_client, consent_client, actuation_client, clients = service_clients
    consent_client.stub_grants = [{"jti": "grant-1", "scopes": ["actuation.home.read"]}]
    skills = build_skill_state(clients)["skills"]
    handler = skills["proposed_action"]

    envelope = {
        "event_id": "evt-1",
        "correlation_id": "corr-1",
        "payload": {
            "person_id": "person-1",
            "target": {"device_id": "light-1", "device_class": "light"},
            "intent": {"name": "turn_on", "parameters": {"level": 20}},
            "risk_level": "high",
            "policy_context": {"scopes": ["actuation.home.*"]},
        },
    }

    result = handler(envelope)
    assert result["ok"] is True
    sent = actuation_client.posts[-1]
    payload = sent["payload"]
    assert payload["policy_context"]["consent_reference"] == "grant-1"
    assert payload["policy_context"]["scopes"] == ["actuation.home.*"]
