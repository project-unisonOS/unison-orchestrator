import os

from orchestrator.clients import ServiceClients
from orchestrator.interaction.context_reader import ContextReader
from orchestrator.interaction.policy_gate import PolicyGate
from orchestrator.interaction.write_behind import ContextWriteBehindQueue
from unison_common import ActionEnvelope, TraceRecorder


class _FakeHttp:
    def __init__(self):
        self.calls = []
        self.routes = {}

    def when_get(self, path, body, status=200, ok=True):
        self.routes[("GET", path)] = (ok, status, body)

    def when_post(self, path, body, status=200, ok=True):
        self.routes[("POST", path)] = (ok, status, body)

    def get(self, path, *, headers=None):
        self.calls.append(("GET", path, headers, None))
        return self.routes.get(("GET", path), (False, 500, None))

    def post(self, path, payload, *, headers=None):
        self.calls.append(("POST", path, headers, payload))
        return self.routes.get(("POST", path), (False, 500, None))

    def put(self, path, payload, *, headers=None):
        self.calls.append(("PUT", path, headers, payload))
        return self.routes.get(("PUT", path), (False, 500, None))


def _clients(context: _FakeHttp, policy: _FakeHttp) -> ServiceClients:
    dummy = _FakeHttp()
    return ServiceClients(context=context, storage=dummy, policy=policy, inference=dummy)


def test_context_reader_uses_x_test_role(monkeypatch):
    monkeypatch.setenv("UNISON_CONTEXT_ROLE", "service")
    ctx = _FakeHttp()
    pol = _FakeHttp()
    ctx.when_get("/profile/p1", {"ok": True, "profile": {"name": "A"}})
    ctx.when_get("/dashboard/p1", {"ok": True, "dashboard": {"cards": []}})
    trace = TraceRecorder(service="test")

    reader = ContextReader.from_env()
    snap = reader.read(clients=_clients(ctx, pol), person_id="p1", trace=trace)

    assert snap.person_id == "p1"
    assert snap.profile == {"name": "A"}
    assert snap.dashboard == {"cards": []}
    assert ("GET", "/profile/p1", {"x-test-role": "service"}, None) in ctx.calls
    assert ("GET", "/dashboard/p1", {"x-test-role": "service"}, None) in ctx.calls


def test_policy_gate_calls_policy_service():
    ctx = _FakeHttp()
    pol = _FakeHttp()
    pol.when_post("/evaluate", {"decision": {"allowed": False, "require_confirmation": False, "reason": "nope"}}, status=200, ok=True)
    gate = PolicyGate(clients=_clients(ctx, pol))
    trace = TraceRecorder(service="test")
    action = ActionEnvelope(action_id="a1", kind="tool", name="tool.echo", args={"text": "hi"}, policy_context={"scopes": ["tools.echo"]})

    decision = gate.check(action, trace=trace, event_id="evt1", actor="me", person_id="p1")
    assert decision.allowed is False
    assert decision.reason == "nope"

    method, path, headers, payload = pol.calls[0]
    assert method == "POST" and path == "/evaluate"
    assert payload["capability_id"] == "tool.echo"


def test_write_behind_flush_writes_kv_put(monkeypatch):
    monkeypatch.setenv("UNISON_CONTEXT_ROLE", "service")
    ctx = _FakeHttp()
    pol = _FakeHttp()
    ctx.when_post("/kv/put", {"ok": True}, status=200, ok=True)
    clients = _clients(ctx, pol)
    trace = TraceRecorder(service="test")

    q = ContextWriteBehindQueue()
    batch = q.enqueue_last_interaction(person_id="p1", session_id="s1", trace_id="t1", input_text="hello")
    ok, err = q.flush_sync(clients=clients, batch=batch, trace=trace)
    assert ok is True
    assert err is None

    post_calls = [c for c in ctx.calls if c[0] == "POST" and c[1] == "/kv/put"]
    assert post_calls, "expected /kv/put write"
    _, _, headers, payload = post_calls[0]
    assert headers == {"x-test-role": "service"}
    assert payload["person_id"] == "p1"
    assert payload["tier"] == "B"
    keys = list(payload["items"].keys())
    assert keys == ["p1:profile:last_interaction"]

