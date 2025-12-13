import json

import orchestrator.dev_thin_slice as thin_slice


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


def test_thin_slice_emits_renderer_event_and_propagates_trace_headers(tmp_path, monkeypatch):
    stub = _Client()

    def _client_factory(*args, **kwargs):
        return stub

    monkeypatch.setattr(thin_slice.httpx, "Client", _client_factory)

    result = thin_slice.run_thin_slice(
        text="hello",
        renderer_url="http://renderer.local",
        trace_dir=str(tmp_path),
    )

    assert result.renderer_ok is True
    assert stub.calls, "expected renderer POST"
    call = stub.calls[0]
    headers = call["headers"] or {}
    assert headers.get("x-request-id") == result.trace_id
    assert isinstance(headers.get("traceparent"), str)

    trace_path = tmp_path / f"{result.trace_id}.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert any(evt.get("name") == "renderer_emitted" and (evt.get("attrs") or {}).get("ok") is True for evt in trace["events"])
    assert any(span.get("name") == "renderer_emitted" for span in trace["spans"])

