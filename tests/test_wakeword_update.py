import os

from src.orchestrator.skills import build_skill_state
from src.orchestrator.clients import ServiceClients, ServiceHttpClient


class _StubClient(ServiceHttpClient):
    def __init__(self):
        super().__init__(host="stub", port="0")
        self.posts = []
        self.responses_get = []

    def get(self, path: str, *, headers=None):
        return True, 200, {"ok": True, "profile": {"voice": {"wakeword": "unison"}}}

    def post(self, path: str, payload, *, headers=None):
        self.posts.append((path, payload))
        return True, 200, {}


def test_wakeword_update_handler_persists(monkeypatch):
    # Stub clients for context storage
    ctx = _StubClient()
    service_clients = ServiceClients(context=ctx, storage=ctx, policy=ctx, inference=ctx)
    state = build_skill_state(service_clients)
    handler = state["handlers"]["wakeword_update"]

    result = handler({"intent": "wakeword.update", "payload": {"person_id": "p1", "wakeword": "hey unison"}})
    assert result["ok"] is True
    assert result["wakeword"] == "hey unison"
    assert any("/profile/p1" in p[0] for p in ctx.posts)
