import os
from fastapi.testclient import TestClient

os.environ.setdefault("DISABLE_AUTH_FOR_TESTS", "true")

from src import server  # noqa: E402


def test_voice_ingest_invokes_companion(monkeypatch):
    called = {}

    def fake_process_turn(envelope):
        called["envelope"] = envelope
        return {"text": "hi", "session_id": envelope["payload"]["session_id"], "person_id": envelope["payload"]["person_id"]}

    assert server._companion_manager is not None
    monkeypatch.setattr(server._companion_manager, "process_turn", fake_process_turn)

    client = TestClient(server.app)
    resp = client.post("/voice/ingest", json={"transcript": "hello", "person_id": "p1", "session_id": "s1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["result"]["text"] == "hi"
    assert called["envelope"]["payload"]["person_id"] == "p1"
    assert called["envelope"]["payload"]["session_id"] == "s1"


def test_emit_downstream_forwards_audio_and_renderer(monkeypatch):
    # Avoid registry lookups and downstream context calls during manager init
    monkeypatch.setattr("src.orchestrator.companion.ToolRegistry.refresh_from_mcp", lambda *a, **k: None)
    monkeypatch.setattr("src.orchestrator.companion.ToolRegistry.refresh_from_context_graph", lambda *a, **k: None)
    monkeypatch.setattr("src.orchestrator.companion.ToolRegistry.publish_to_context_graph", lambda *a, **k: None)

    from src.orchestrator import companion
    companion._IO_SPEECH_URL = "http://speech"
    companion._RENDERER_URL = "http://renderer"
    companion._CONTEXT_GRAPH_URL = "http://context-graph"

    posts = []

    class DummyResp:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class DummyClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            posts.append({"url": url, "json": json, "headers": headers})
            if "speech/tts" in url:
                return DummyResp({"audio_url": "http://audio.test"})
            if "context-graph" in url:
                return DummyResp({"ok": True})
            return DummyResp({"ok": True})

    monkeypatch.setattr(companion, "httpx", type("X", (), {"Client": lambda *a, **k: DummyClient()}))

    class DummyClients:
        context = None
        storage = None
        policy = None
        inference = None

    mgr = companion.CompanionSessionManager(DummyClients(), companion.ToolRegistry())
    mgr._emit_downstream("hello", [{"tool": "noop"}], "p1", "s1", cards=[{"title": "c"}])
    mgr._log_context_graph("p1", "s1", "hello", [{"tool": "noop"}], [{"title": "c"}])

    assert any("speech/tts" in p["url"] for p in posts)
    renderer_posts = [p for p in posts if "renderer" in p["url"]]
    assert renderer_posts, "renderer should receive an experience"
    assert renderer_posts[0]["json"]["audio_url"] == "http://audio.test"
    assert renderer_posts[0]["json"]["person_id"] == "p1"
    assert renderer_posts[0]["json"]["session_id"] == "s1"
    assert renderer_posts[0]["json"]["cards"] == [{"title": "c"}]
    assert any("context-graph" in p["url"] for p in posts)
