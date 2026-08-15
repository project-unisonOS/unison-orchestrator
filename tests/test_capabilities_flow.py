from unittest.mock import Mock

import src.server as server
from orchestrator.clients import ServiceHttpClient


class _FakeCapabilities:
    manifest = {"modalities": {"displays": [{"id": "display.local"}]}}

    def modality_count(self, modality):
        return len(self.manifest.get("modalities", {}).get(modality, []))


def test_publish_capabilities_to_context(monkeypatch):
    captured = {}

    def fake_post(path, payload, headers=None, **kwargs):
        captured["args"] = (path, payload)
        return True, 200, {"ok": True}

    monkeypatch.setattr(server, "_capabilities", _FakeCapabilities())
    monkeypatch.setattr(server.service_clients, "context", Mock(spec=ServiceHttpClient))
    monkeypatch.setattr(server.service_clients.context, "post", fake_post)

    server.publish_capabilities_to_context()

    assert captured.get("args") is not None
    path, payload = captured["args"]
    assert path == "/capabilities"
    assert payload == _FakeCapabilities.manifest
