import time

from orchestrator.skills import build_skill_state
from orchestrator.clients import ServiceClients, ServiceHttpClient


class _StubClient(ServiceHttpClient):
    def __init__(self):
        super().__init__(host="stub", port="0")
        self.posts = []
        self.responses_post = []
        self.gets = []
        self.responses_get = []

    def enqueue_post(self, ok, status, body):
        self.responses_post.append((ok, status, body))

    def enqueue_get(self, ok, status, body):
        self.responses_get.append((ok, status, body))

    def post(self, path: str, payload, *, headers=None):
        self.posts.append((path, payload, headers))
        if self.responses_post:
            return self.responses_post.pop(0)
        return True, 200, {}

    def get(self, path: str, *, headers=None):
        self.gets.append((path, headers))
        if self.responses_get:
            return self.responses_get.pop(0)
        return True, 200, {"ok": True, "profile": {"dashboard": {"preferences": {"layout": "comms-first"}}}}


def test_dashboard_refresh_persists_and_emits(monkeypatch):
    ctx = _StubClient()
    storage = _StubClient()
    policy = _StubClient()
    inf = _StubClient()
    # renderer URL to ensure emit is attempted
    monkeypatch.setenv("UNISON_RENDERER_URL", "http://renderer")
    # stub httpx client to avoid network
    class FakeResp:
        status_code = 200
        def json(self): return {}
        def raise_for_status(self): return None
    class FakeClient:
        def __init__(self, *_, **__): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, *args, **kwargs): return FakeResp()
    monkeypatch.setattr("httpx.Client", FakeClient)

    service_clients = ServiceClients(context=ctx, storage=storage, policy=policy, inference=inf)
    state = build_skill_state(service_clients)
    handler = state["handlers"]["dashboard_refresh"]

    result = handler({"intent": "dashboard.refresh", "payload": {"person_id": "p1"}})
    assert result.get("ok") is True
    # context dashboard write
    assert any("/dashboard/p1" in p[0] for p in ctx.posts)


def test_dashboard_refresh_logs_to_context_graph_when_configured(monkeypatch):
    ctx = _StubClient()
    storage = _StubClient()
    policy = _StubClient()
    inf = _StubClient()
    monkeypatch.setenv("UNISON_RENDERER_URL", "http://renderer")
    monkeypatch.setenv("UNISON_CONTEXT_GRAPH_URL", "http://context-graph")
    calls = []

    class FakeResp:
        status_code = 200
        def json(self): return {}
        def raise_for_status(self): return None

    class FakeClient:
        def __init__(self, *_, **__): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, url, json=None, **kwargs):
            calls.append({"url": url, "json": json})
            return FakeResp()

    monkeypatch.setattr("httpx.Client", FakeClient)

    service_clients = ServiceClients(context=ctx, storage=storage, policy=policy, inference=inf)
    state = build_skill_state(service_clients)
    handler = state["handlers"]["dashboard_refresh"]

    result = handler({"intent": "dashboard.refresh", "payload": {"person_id": "p1"}})
    assert result.get("ok") is True
    # One or more calls should target the context-graph endpoints.
    assert any("/context/update" in c["url"] for c in calls)
    assert any("/traces/replay" in c["url"] for c in calls)
    update_call = next(c for c in calls if "/context/update" in c["url"])
    assert update_call["json"].get("user_id") == "p1"
