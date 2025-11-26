import uuid

import pytest

from src.orchestrator import companion


class _DummyInferenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, path, payload, headers=None):
        self.calls.append({"path": path, "payload": payload, "headers": headers})
        if not self.responses:
            return False, 500, {}
        return self.responses.pop(0)


class _DummyClients:
    def __init__(self, inference_client):
        self.inference = inference_client
        self.context = self.storage = self.policy = inference_client

    def get(self, *a, **k):
        return True, 200, {}

    def post(self, *a, **k):
        return True, 200, {}


def _no_op_registry(monkeypatch):
    monkeypatch.setattr("src.orchestrator.companion.ToolRegistry.refresh_from_mcp", lambda *a, **k: None)
    monkeypatch.setattr("src.orchestrator.companion.ToolRegistry.refresh_from_context_graph", lambda *a, **k: None)
    monkeypatch.setattr("src.orchestrator.companion.ToolRegistry.publish_to_context_graph", lambda *a, **k: None)


def test_process_turn_emits_cards_and_tool_activity(monkeypatch):
    _no_op_registry(monkeypatch)

    first_response = (True, 200, {"tool_calls": [{"function": {"name": "context.get", "arguments": "{}"}, "id": "1"}]})
    second_response = (
        True,
        200,
        {
            "result": "here you go",
            "messages": [{"role": "assistant", "content": "here you go"}],
            "tool_activity": [{"tool": "context.get", "status": "ok"}],
            "cards": [{"title": "card-a"}],
        },
    )
    inference_client = _DummyInferenceClient([first_response, second_response])
    clients = _DummyClients(inference_client)

    seen_emit = {}

    def fake_emit(text, tool_activity, person_id, session_id, cards=None):
        seen_emit["text"] = text
        seen_emit["tool_activity"] = tool_activity
        seen_emit["cards"] = cards
        seen_emit["person_id"] = person_id
        seen_emit["session_id"] = session_id

    def fake_log(*args, **kwargs):
        return None

    mgr = companion.CompanionSessionManager(clients, companion.ToolRegistry())
    monkeypatch.setattr(mgr, "_emit_downstream", fake_emit)
    monkeypatch.setattr(mgr, "_log_context_graph", fake_log)

    envelope = {
        "intent": "companion.turn",
        "payload": {
            "person_id": "p1",
            "session_id": "s1",
            "messages": [{"role": "user", "content": "hi"}],
        },
    }
    result = mgr.process_turn(envelope)

    assert any(a.get("tool") == "context.get" for a in seen_emit["tool_activity"])


def test_inference_payload_includes_tools(monkeypatch):
    _no_op_registry(monkeypatch)

    tool = companion.ToolDescriptor(name="demo.tool", description="demo", parameters={"type": "object", "properties": {}})
    reg = companion.ToolRegistry()
    reg._tools[tool.name] = tool

    first_response = (True, 200, {"result": "ok"})
    inference_client = _DummyInferenceClient([first_response])
    clients = _DummyClients(inference_client)

    mgr = companion.CompanionSessionManager(clients, reg)
    mgr._emit_downstream = lambda *a, **k: None
    mgr._log_context_graph = lambda *a, **k: None

    envelope = {
        "intent": "companion.turn",
        "payload": {
            "person_id": "p1",
            "session_id": "s1",
            "messages": [{"role": "user", "content": "hi"}],
        },
    }
    mgr.process_turn(envelope)

    # First inference call should include tool list with demo.tool
    first_call = inference_client.calls[0]["payload"]
    names = [t.get("name") for t in first_call.get("tools", [])]
    assert "demo.tool" in names
