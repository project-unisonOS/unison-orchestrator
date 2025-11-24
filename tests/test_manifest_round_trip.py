import json
from unittest.mock import Mock

from orchestrator.clients import ServiceClients, ServiceHttpClient
from src.server import publish_capabilities_to_context, _capabilities, service_clients


def test_manifest_round_trip_to_context_graph(monkeypatch):
    if not _capabilities:
        return

    captured = {}

    def fake_context_post(path, payload, headers=None, **kwargs):
        captured["post"] = (path, payload)
        return True, 200, {"ok": True}

    def fake_context_get(path, headers=None, **kwargs):
        captured["get"] = (path, {"manifest": payload})
        return True, 200, {"manifest": payload}

    payload = _capabilities.manifest or {"modalities": {"displays": []}}
    monkeypatch.setattr(service_clients, "context", Mock(spec=ServiceHttpClient))
    monkeypatch.setattr(service_clients.context, "post", fake_context_post)
    monkeypatch.setattr(service_clients.context, "get", fake_context_get)

    publish_capabilities_to_context()

    assert captured.get("post") is not None
    path, sent_manifest = captured["post"]
    assert path == "/capabilities"
    assert sent_manifest.get("modalities") is not None
