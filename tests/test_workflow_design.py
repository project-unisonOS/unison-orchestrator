import time

from orchestrator.skills import build_skill_state
from orchestrator.clients import ServiceClients, ServiceHttpClient


class _StubClient(ServiceHttpClient):
    def __init__(self):
        super().__init__(host="stub", port="0")
        self.posts = []
        self.gets = []
        self.responses_post = []
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
        return True, 200, {}


def _make_service_clients():
    ctx = _StubClient()
    storage = _StubClient()
    policy = _StubClient()
    inf = _StubClient()
    service_clients = ServiceClients(context=ctx, storage=storage, policy=policy, inference=inf)
    return service_clients, ctx, storage, policy, inf


def test_workflow_design_creates_and_tags_workflow(monkeypatch):
    service_clients, ctx, _, _, _ = _make_service_clients()

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

    state = build_skill_state(service_clients)
    handler = state["handlers"]["workflow_design"]

    payload = {
        "person_id": "p1",
        "workflow_id": "onboarding",
        "project_id": "unisonos-docs",
        "mode": "design",
        "changes": [
            {
                "op": "add_step",
                "title": "Verify email and phone together",
                "position": 0,
            }
        ],
    }

    result = handler({"intent": "workflow.design", "payload": payload})
    assert result.get("ok") is True
    assert result.get("person_id") == "p1"
    assert result.get("workflow_id") == "onboarding"

    workflow = result.get("workflow") or {}
    steps = workflow.get("steps") or []
    assert len(steps) == 1
    assert steps[0].get("title") == "Verify email and phone together"

    cards = result.get("cards") or []
    assert len(cards) == 1
    card = cards[0]
    assert card.get("origin_intent") == "workflow.design"
    tags = card.get("tags") or []
    # Core tags for workflow design.
    assert "workflow" in tags
    assert "planning" in tags
    assert "workflow.design" in tags
    assert "draft" in tags
    assert "workflow:onboarding" in tags
    assert "project:unisonos-docs" in tags

    # Context writes: workflow doc and dashboard state.
    paths = [p[0] for p in ctx.posts]
    assert any("/kv/set" in p for p in paths)
    assert any("/kv/get" in p for p in paths)
    assert any("/dashboard/p1" in p for p in paths)

