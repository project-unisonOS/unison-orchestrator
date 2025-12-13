from orchestrator.clients import ServiceClients
from orchestrator.interaction.vdi_executor import VdiExecutor
from unison_common import ActionEnvelope, TraceRecorder


class _FakeHttp:
    def __init__(self):
        self.calls = []
        self.routes = {}

    def when_post(self, path, body, status=200, ok=True):
        self.routes[("POST", path)] = (ok, status, body)

    def post(self, path, payload, *, headers=None):
        self.calls.append(("POST", path, headers, payload))
        return self.routes.get(("POST", path), (False, 500, None))

    def get(self, path, *, headers=None):
        self.calls.append(("GET", path, headers, None))
        return (False, 404, None)

    def put(self, path, payload, *, headers=None):
        self.calls.append(("PUT", path, headers, payload))
        return (False, 404, None)


def test_vdi_executor_calls_actuation_proxy():
    act = _FakeHttp()
    act.when_post("/vdi/tasks/browse", {"status": "ok"}, status=200, ok=True)
    dummy = _FakeHttp()
    clients = ServiceClients(context=dummy, storage=dummy, policy=dummy, inference=dummy, actuation=act)  # type: ignore[arg-type]

    action = ActionEnvelope(
        action_id="a1",
        kind="vdi",
        name="vdi.browse",
        args={"person_id": "p1", "session_id": "s1", "url": "https://example.com"},
        risk_level="low",
    )
    trace = TraceRecorder(service="test")
    res = VdiExecutor().execute(action=action, clients=clients, trace=trace)
    assert res.ok is True
    assert act.calls
    method, path, _, payload = act.calls[0]
    assert method == "POST"
    assert path == "/vdi/tasks/browse"
    assert payload["person_id"] == "p1"
