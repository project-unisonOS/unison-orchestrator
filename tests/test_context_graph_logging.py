import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("DISABLE_AUTH_FOR_TESTS", "true")
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))
from orchestrator import companion  # noqa: E402


def test_log_context_graph_posts_payload(monkeypatch):
    posts = []

    class DummyResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class DummyClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            posts.append({"url": url, "json": json, "headers": headers})
            return DummyResp()

    monkeypatch.setattr(companion, "httpx", type("X", (), {"Client": lambda *a, **k: DummyClient()}))
    companion._CONTEXT_GRAPH_URL = "http://context-graph"

    # Avoid registry churn
    monkeypatch.setattr("src.orchestrator.companion.ToolRegistry.refresh_from_mcp", lambda *a, **k: None)
    monkeypatch.setattr("src.orchestrator.companion.ToolRegistry.refresh_from_context_graph", lambda *a, **k: None)
    monkeypatch.setattr("src.orchestrator.companion.ToolRegistry.publish_to_context_graph", lambda *a, **k: None)

    dummy_context = type("Ctx", (), {"get": lambda *a, **k: (True, 200, {"capabilities": []})})
    clients = type("C", (), {"context": dummy_context, "storage": dummy_context, "policy": dummy_context, "inference": dummy_context})
    mgr = companion.CompanionSessionManager(clients, companion.ToolRegistry())
    mgr._log_context_graph("p1", "s1", "hello", [{"tool": "t"}], [{"title": "c"}])

    assert posts
    assert posts[0]["url"].endswith("/context/update")
    assert posts[0]["json"]["user_id"] == "p1"
    assert posts[0]["json"]["session_id"] == "s1"
    dims = posts[0]["json"]["dimensions"]
    assert dims and dims[0]["value"]["transcript"] == "hello"
    assert dims[0]["value"]["tool_activity"] == [{"tool": "t"}]
    assert dims[0]["value"]["cards"] == [{"title": "c"}]
