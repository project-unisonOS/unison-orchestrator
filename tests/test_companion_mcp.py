import json

import pytest

from orchestrator.companion import CompanionSessionManager, ToolRegistry
from orchestrator.clients import ServiceClients, ServiceHttpClient


class _StubClient(ServiceHttpClient):
    def __init__(self):
        super().__init__(host="stub", port="0")
        self.posts = []
        self.gets = []
        self.responses_post = []
        self.responses_get = []

    def enqueue_post(self, ok, status, body):
        self.responses_post.append((ok, status, body))

    def enqueue_get(self, ok, status, body):
        self.responses_get.append((ok, status, body))

    def post(self, path: str, payload, *, headers=None):
        self.posts.append((path, payload, headers))
        if self.responses_post:
            return self.responses_post.pop(0)
        return False, 500, {"error": "no stub"}

    def get(self, path: str, *, headers=None):
        self.gets.append((path, headers))
        if self.responses_get:
            return self.responses_get.pop(0)
        return True, 200, {"capabilities": []}


@pytest.fixture
def stub_clients(monkeypatch):
    ctx = _StubClient()
    storage = _StubClient()
    policy = _StubClient()
    inf = _StubClient()
    clients = ServiceClients(context=ctx, storage=storage, policy=policy, inference=inf)

    # Stub MCP discovery URL fetch
    registry_payload = {
        "servers": [
            {
                "id": "local",
                "base_url": "http://mcp-server",
                "tools": [
                    {
                        "name": "time.now",
                        "description": "current time",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        ]
    }

    def fake_get(url, *_, **__):
        class Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return registry_payload

        return Resp()

    def fake_post(url, json=None, *_, **__):
        class Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"now": "123"}

        return Resp()

    # Patch httpx.Client methods
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return fake_get(url)

        def post(self, url, json=None, headers=None):
            return fake_post(url, json=json)

    monkeypatch.setattr("httpx.Client", FakeClient)

    return clients, ctx, storage, inf


def test_companion_executes_mcp_tool(stub_clients):
    clients, ctx, storage, inf = stub_clients
    registry = ToolRegistry(mcp_discovery_url="http://mcp-registry")
    manager = CompanionSessionManager(clients, registry)

    # First inference returns a tool call to time.now (MCP)
    inf.enqueue_post(
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
                            "function": {"name": "time.now", "arguments": "{}"},
                        }
                    ],
                }
            ],
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "time.now", "arguments": "{}"}}
            ],
        },
    )
    # Follow-up inference returns final text
    inf.enqueue_post(
        True,
        200,
        {"result": "It is now 123", "messages": [{"role": "assistant", "content": "It is now 123"}]},
    )

    envelope = {"intent": "companion.turn", "payload": {"person_id": "p1", "session_id": "s1", "text": "time"}}
    resp = manager.process_turn(envelope)
    assert resp.get("text") == "It is now 123"
    assert resp.get("tool_calls")
    assert any(act.get("tool") == "time.now" for act in resp.get("tool_activity", []))
