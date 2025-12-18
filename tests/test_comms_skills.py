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


def _clients_with_comms() -> ServiceClients:
    ctx = _StubClient()
    storage = _StubClient()
    policy = _StubClient()
    inference = _StubClient()
    capability = _StubClient()
    return ServiceClients(context=ctx, storage=storage, policy=policy, inference=inference, capability=capability)


def test_comms_check_calls_service_and_returns_cards():
    service_clients = _clients_with_comms()
    cap = service_clients.capability
    cap.enqueue_post(True, 200, {"candidate": {"manifest": {"id": "comms.check"}}})
    cap.enqueue_post(True, 200, {"result": {"messages": [{"id": 1}], "cards": [{"origin_intent": "comms.check"}]}})
    state = build_skill_state(service_clients)
    handler = state["handlers"]["comms_check"]
    result = handler({"intent": "comms.check", "payload": {"person_id": "p1", "channel": "email"}})
    assert result.get("ok") is True
    assert any("/capability/resolve" in p[0] for p in cap.posts)
    assert any("/capability/run" in p[0] for p in cap.posts)
    assert result.get("cards")[0]["origin_intent"] == "comms.check"


def test_comms_compose_validates_and_calls_service():
    service_clients = _clients_with_comms()
    cap = service_clients.capability
    cap.enqueue_post(True, 200, {"candidate": {"manifest": {"id": "comms.compose"}}})
    cap.enqueue_post(True, 200, {"result": {"status": "sent"}})
    state = build_skill_state(service_clients)
    handler = state["handlers"]["comms_compose"]
    result = handler(
        {
            "intent": "comms.compose",
            "payload": {"person_id": "p1", "channel": "email", "recipients": ["a@example.com"], "subject": "Hi", "body": "Hello"},
        }
    )
    assert result.get("ok") is True
    assert any("/capability/run" in p[0] for p in cap.posts)
