import json
from unittest.mock import Mock

from orchestrator.clients import ServiceHttpClient
from src.server import publish_capabilities_to_context, _capabilities, service_clients


def test_publish_capabilities_to_context(monkeypatch):
    if not _capabilities:
        return

    captured = {}

    def fake_post(host, port, path, payload, headers=None, **kwargs):
        captured["args"] = (host, port, path, payload)
        return True, 200, {"ok": True}

    monkeypatch.setattr(service_clients, "context", Mock(spec=ServiceHttpClient))
    monkeypatch.setattr(service_clients.context, "post", fake_post)

    publish_capabilities_to_context()

    assert captured.get("args") is not None
    host, port, path, payload = captured["args"]
    assert path == "/capabilities"
    assert isinstance(payload, dict)
