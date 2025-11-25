import pytest

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
        return False, 500, {"error": "no stub"}

    def get(self, path: str, *, headers=None):
        self.gets.append((path, headers))
        if self.responses_get:
            return self.responses_get.pop(0)
        return True, 200, {}


@pytest.fixture
def stub_clients():
    ctx = _StubClient()
    storage = _StubClient()
    policy = _StubClient()
    inf = _StubClient()
    return ServiceClients(context=ctx, storage=storage, policy=policy, inference=inf), ctx


def test_person_enroll_and_verify(stub_clients):
    service_clients, ctx = stub_clients
    state = build_skill_state(service_clients)
    handler_enroll = state["handlers"]["person_enroll"]
    handler_verify = state["handlers"]["person_verify"]

    profile = {"preferences": {"language": "en"}, "auth": {"pin": "1234"}}
    ctx.enqueue_post(True, 200, {"ok": True})
    enroll_result = handler_enroll(
        {"intent": "person.enroll", "payload": {"person_id": "p1", "profile": profile}}
    )
    assert enroll_result.get("ok") is True

    # Verification success
    ctx.enqueue_get(True, 200, {"ok": True, "profile": profile})
    verify_result = handler_verify(
        {"intent": "person.verify", "payload": {"person_id": "p1", "verification_token": "1234"}}
    )
    assert verify_result.get("ok") is True

    # Verification failure
    ctx.enqueue_get(True, 200, {"ok": True, "profile": profile})
    verify_fail = handler_verify(
        {"intent": "person.verify", "payload": {"person_id": "p1", "verification_token": "0000"}}
    )
    assert verify_fail.get("ok") is False
