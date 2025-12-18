from orchestrator.skills import build_skill_state
from orchestrator.clients import ServiceClients, ServiceHttpClient


class _StubClient(ServiceHttpClient):
    def __init__(self):
        super().__init__(host="stub", port="0")
        self.posts = []
        self.responses_post = []

    def enqueue_post(self, ok, status, body):
        self.responses_post.append((ok, status, body))

    def post(self, path: str, payload, *, headers=None):
        self.posts.append((path, payload, headers))
        if self.responses_post:
            return self.responses_post.pop(0)
        return True, 200, {}

    def get(self, path: str, *, headers=None):
        return True, 200, {}


def _clients():
    ctx = _StubClient()
    storage = _StubClient()
    policy = _StubClient()
    inf = _StubClient()
    capability = _StubClient()
    return ServiceClients(context=ctx, storage=storage, policy=policy, inference=inf, capability=capability)


def test_meeting_handlers_call_comms():
    service_clients = _clients()
    cap = service_clients.capability
    cap.enqueue_post(True, 200, {"candidate": {"manifest": {"id": "comms.join_meeting"}}})
    cap.enqueue_post(True, 200, {"result": {"cards": [{"origin_intent": "comms.join_meeting"}]}})
    state = build_skill_state(service_clients)
    handler = state["handlers"]["comms_join_meeting"]
    result = handler({"intent": "comms.join_meeting", "payload": {"person_id": "p1", "meeting_id": "m1"}})
    assert result.get("ok") is True
    assert any("/capability/run" in p[0] for p in cap.posts)

    cap.enqueue_post(True, 200, {"candidate": {"manifest": {"id": "comms.prepare_meeting"}}})
    cap.enqueue_post(True, 200, {"result": {"cards": [{"origin_intent": "comms.prepare_meeting"}]}})
    prep = state["handlers"]["comms_prepare_meeting"]({"intent": "comms.prepare_meeting", "payload": {"person_id": "p1", "meeting_id": "m1"}})
    assert prep.get("ok") is True
    assert any("/capability/run" in p[0] for p in cap.posts)

    cap.enqueue_post(True, 200, {"candidate": {"manifest": {"id": "comms.debrief_meeting"}}})
    cap.enqueue_post(True, 200, {"result": {"cards": [{"origin_intent": "comms.debrief_meeting"}]}})
    debrief = state["handlers"]["comms_debrief_meeting"]({"intent": "comms.debrief_meeting", "payload": {"person_id": "p1", "meeting_id": "m1"}})
    assert debrief.get("ok") is True
    assert any("/capability/run" in p[0] for p in cap.posts)
