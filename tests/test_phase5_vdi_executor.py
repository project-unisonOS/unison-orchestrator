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


def test_vdi_download_preserves_file_ids_and_expected_payload():
    act = _FakeHttp()
    act.when_post(
        "/vdi/tasks/download",
        {
            "status": "ok",
            "file_ids": ["artifact-1"],
            "artifacts": ["/tmp/report.pdf"],
            "telemetry": {"download_mode": "http"},
        },
        status=200,
        ok=True,
    )
    dummy = _FakeHttp()
    clients = ServiceClients(context=dummy, storage=dummy, policy=dummy, inference=dummy, actuation=act)  # type: ignore[arg-type]

    action = ActionEnvelope(
        action_id="a2",
        kind="vdi",
        name="vdi.download",
        args={
            "person_id": "p1",
            "session_id": "s-download",
            "url": "https://example.com/report.pdf",
            "filename": "report.pdf",
            "headers": {"X-Test": "1"},
            "target_path": "downloads/report.pdf",
        },
        risk_level="low",
    )
    trace = TraceRecorder(service="test")
    res = VdiExecutor().execute(action=action, clients=clients, trace=trace)

    assert res.ok is True
    assert res.result["body"]["file_ids"] == ["artifact-1"]
    assert act.calls
    method, path, _, payload = act.calls[0]
    assert method == "POST"
    assert path == "/vdi/tasks/download"
    assert payload["person_id"] == "p1"
    assert payload["session_id"] == "s-download"
    assert payload["url"] == "https://example.com/report.pdf"
    assert payload["filename"] == "report.pdf"
    assert payload["target_path"] == "downloads/report.pdf"
    assert payload["headers"] == {"X-Test": "1"}


def test_vdi_download_surfaces_bounded_downstream_failure():
    act = _FakeHttp()
    act.when_post(
        "/vdi/tasks/download",
        {"detail": "domain not allowed", "status": "failed"},
        status=403,
        ok=False,
    )
    dummy = _FakeHttp()
    clients = ServiceClients(context=dummy, storage=dummy, policy=dummy, inference=dummy, actuation=act)  # type: ignore[arg-type]

    action = ActionEnvelope(
        action_id="a3",
        kind="vdi",
        name="vdi.download",
        args={
            "person_id": "p1",
            "session_id": "s-denied",
            "url": "https://blocked.example.com/report.pdf",
            "filename": "report.pdf",
        },
        risk_level="low",
    )
    trace = TraceRecorder(service="test")
    res = VdiExecutor().execute(action=action, clients=clients, trace=trace)

    assert res.ok is False
    assert res.error == "actuation vdi call failed status=403"
    assert res.result == {
        "status": 403,
        "body": {"detail": "domain not allowed", "status": "failed"},
    }
