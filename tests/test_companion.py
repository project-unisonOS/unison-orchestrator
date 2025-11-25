import json

import pytest

from orchestrator.companion import CompanionSessionManager, ToolRegistry
from orchestrator.clients import ServiceClients, ServiceHttpClient


class _StubClient(ServiceHttpClient):
    def __init__(self):
        super().__init__(host="stub", port="0")
        self.posts = []
        self.responses = []

    def enqueue(self, ok: bool, status: int, body):
        self.responses.append((ok, status, body))

    def post(self, path: str, payload, *, headers=None):
        self.posts.append((path, payload, headers))
        if self.responses:
            return self.responses.pop(0)
        return False, 500, {"error": "no stub"}

    def get(self, path: str, *, headers=None):
        return True, 200, {"capabilities": []}


@pytest.fixture
def stub_clients():
    ctx = _StubClient()
    storage = _StubClient()
    policy = _StubClient()
    inf = _StubClient()
    clients = ServiceClients(context=ctx, storage=storage, policy=policy, inference=inf)
    return clients, ctx, storage, inf


def test_companion_turn_tool_followup(stub_clients):
    clients, ctx, storage, inf = stub_clients
    registry = ToolRegistry()
    manager = CompanionSessionManager(clients, registry)

    # First inference returns a tool call to context.get
    inf.enqueue(
        True,
        200,
        {
            "result": None,
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "context.get",
                                "arguments": json.dumps({"keys": ["k1"]}),
                            },
                        }
                    ],
                }
            ],
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "context.get",
                        "arguments": json.dumps({"keys": ["k1"]}),
                    },
                }
            ],
        },
    )
    # Follow-up inference returns final text
    inf.enqueue(
        True,
        200,
        {
            "result": "Here is your data",
            "provider": "ollama",
            "model": "qwen2.5",
            "messages": [{"role": "assistant", "content": "Here is your data"}],
        },
    )
    # Context service returns value for context.get
    ctx.enqueue(True, 200, {"k1": "v1"})

    envelope = {
        "intent": "companion.turn",
        "payload": {
            "person_id": "p1",
            "session_id": "s1",
            "text": "fetch my data",
        },
    }

    resp = manager.process_turn(envelope)
    assert resp.get("text") == "Here is your data"
    assert resp.get("tool_calls")
    assert resp.get("tool_activity")
    # Ensure follow-up inference was called after tool execution
    assert len(inf.posts) == 2
    assert resp.get("display_intent")
    assert resp.get("speak_intent")
