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
        # Default to an empty dashboard if no response enqueued.
        return True, 200, {"ok": True, "dashboard": {"cards": [], "preferences": {}}}


def _make_service_clients():
    context = _StubClient()
    storage = _StubClient()
    policy = _StubClient()
    inference = _StubClient()
    service_clients = ServiceClients(context=context, storage=storage, policy=policy, inference=inference)
    return service_clients, context, storage, policy, inference


def test_workflow_recall_handler_uses_dashboard_state(monkeypatch):
    service_clients, context_client, _, _, _ = _make_service_clients()

    # Ensure renderer emits are best-effort and do not hit the network.
    monkeypatch.setenv("UNISON_RENDERER_URL", "http://renderer")

    class FakeResp:
        status_code = 200

        def json(self):
            return {}

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return FakeResp()

    monkeypatch.setattr("httpx.Client", FakeClient)

    now = time.time()
    cards = [
        {
            "id": "w1",
            "type": "workflow.step",
            "title": "Onboarding workflow",
            "body": "Designing onboarding workflow details.",
            "tags": ["workflow", "design"],
            "created_at": now - 60,
        },
        {
            "id": "other",
            "type": "summary",
            "title": "Other card",
            "body": "Not workflow related.",
            "tags": ["misc"],
            "created_at": now - 60,
        },
    ]
    dashboard_body = {"ok": True, "dashboard": {"cards": cards, "preferences": {"layout": "default"}}}
    # First GET will be for capabilities via ToolRegistry.refresh_from_context_graph.
    context_client.enqueue_get(True, 200, {"ok": True, "capabilities": []})
    # Second GET will be the dashboard fetch used by workflow recall.
    context_client.enqueue_get(True, 200, dashboard_body)

    state = build_skill_state(service_clients)
    handler = state["handlers"]["workflow_recall"]

    result = handler(
        {
            "intent": "workflow.recall",
            "payload": {
                "person_id": "p1",
                "query": "that workflow we were designing",
                "time_hint_days": 14,
            },
        }
    )

    assert result.get("ok") is True
    assert result.get("person_id") == "p1"
    recap_cards = result.get("cards") or []
    # Recall should succeed even if no cards match; when cards are present,
    # they should be tagged as workflow-related.
    if recap_cards:
        for card in recap_cards:
            tags = card.get("tags") or []
            assert "workflow" in tags
            assert card.get("origin_intent") == "workflow.recall"

    # Dashboard state should have been written back to context.
    assert any("/dashboard/p1" in post[0] for post in context_client.posts)


def test_workflow_recall_tool_executes_via_companion(monkeypatch):
    service_clients, context_client, _, _, _ = _make_service_clients()

    monkeypatch.setenv("UNISON_RENDERER_URL", "http://renderer")

    class FakeResp:
        status_code = 200

        def json(self):
            return {}

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return FakeResp()

    monkeypatch.setattr("httpx.Client", FakeClient)

    now = time.time()
    cards = [
        {
            "id": "w1",
            "type": "workflow.step",
            "title": "Onboarding workflow",
            "body": "Designing onboarding workflow details.",
            "tags": ["workflow", "design"],
            "created_at": now - 60,
        }
    ]
    dashboard_body = {"ok": True, "dashboard": {"cards": cards, "preferences": {}}}
    # First GET for capabilities, second for dashboard fetch.
    context_client.enqueue_get(True, 200, {"ok": True, "capabilities": []})
    context_client.enqueue_get(True, 200, dashboard_body)

    state = build_skill_state(service_clients)
    companion = state["companion_manager"]

    result = companion._execute_single_tool(  # type: ignore[attr-defined]
        "workflow.recall",
        {"person_id": "p1", "query": "workflow design", "time_hint_days": 7},
        person_id="local-user",
        event_id="e1",
    )

    assert result.get("ok") is True
    recap_cards = result.get("cards") or []
    if recap_cards:
        for card in recap_cards:
            tags = card.get("tags") or []
            assert "workflow" in tags

    # Dashboard state should have been written via the tool as well.
    assert any("/dashboard/p1" in post[0] for post in context_client.posts)
