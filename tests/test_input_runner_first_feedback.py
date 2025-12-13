import orchestrator.interaction.input_runner as input_runner
from unison_common import InputEventEnvelope


class _Resp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class _Client:
    def __init__(self, *_, **__):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _Resp(200)


def test_input_runner_emits_intent_recognized_before_rom_render(tmp_path, monkeypatch):
    stub = _Client()

    def _client_factory(*args, **kwargs):
        return stub

    monkeypatch.setattr(input_runner.httpx, "Client", _client_factory)

    evt = InputEventEnvelope(
        event_id="e1",
        trace_id="t1",
        ts_unix_ms=1,
        source="test",
        modality="speech",
        payload={"transcript": "hello"},
        person_id="p1",
        session_id="s1",
        auth={},
    )
    out = input_runner.run_input_event(input_event=evt, clients=None, trace_dir=str(tmp_path), renderer_url="http://renderer.local")
    assert out.trace_id == "t1"
    types = [c["json"]["type"] for c in stub.calls]
    assert types[0] == "intent.recognized"
    assert types[-1] == "rom.render"

