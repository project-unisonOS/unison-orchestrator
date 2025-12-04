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
    comms = _StubClient()
    return ServiceClients(context=ctx, storage=storage, policy=policy, inference=inference, comms=comms)


def test_comms_check_calls_service_and_returns_cards():
    service_clients = _clients_with_comms()
    comms_client = service_clients.comms
    comms_client.enqueue_post(True, 200, {"messages": [{"id": 1}], "cards": [{"origin_intent": "comms.check"}]})
    state = build_skill_state(service_clients)
    handler = state["handlers"]["comms_check"]
    result = handler({"intent": "comms.check", "payload": {"person_id": "p1", "channel": "email"}})
    assert result.get("ok") is True
    assert any("/comms/check" in p[0] for p in comms_client.posts)
    assert result.get("cards")[0]["origin_intent"] == "comms.check"


def test_comms_compose_validates_and_calls_service():
    service_clients = _clients_with_comms()
    comms_client = service_clients.comms
    comms_client.enqueue_post(True, 200, {"status": "sent"})
    state = build_skill_state(service_clients)
    handler = state["handlers"]["comms_compose"]
    result = handler(
        {
            "intent": "comms.compose",
            "payload": {"person_id": "p1", "channel": "email", "recipients": ["a@example.com"], "subject": "Hi", "body": "Hello"},
        }
    )
    assert result.get("ok") is True
    assert any("/comms/compose" in p[0] for p in comms_client.posts)
