import asyncio
from types import SimpleNamespace

from src.orchestrator.power_on.controller import PowerOnController


class _FakeCapabilityClient:
    def __init__(self, manifest):
        self._manifest = manifest

    def refresh(self):
        return self._manifest


def test_poweron_requires_bootstrap(monkeypatch):
    monkeypatch.setenv("UNISON_RENDERER_URL", "http://renderer:8092")
    monkeypatch.setattr(
        "src.orchestrator.power_on.controller.RendererEmitter.emit",
        lambda self, **kwargs: None,
    )
    manifest = {
        "modalities": {"displays": [{"id": "display-1"}]},
        "io": {"speech": {"enabled": True, "endpoint": "http://speech:8084", "ws_endpoint": "ws://speech:8084/stream"}},
    }
    clients = SimpleNamespace()

    monkeypatch.setattr(
        "src.orchestrator.power_on.controller.CapabilityClient.from_env",
        lambda: _FakeCapabilityClient(manifest),
    )
    monkeypatch.setattr(
        "src.orchestrator.power_on.controller.fetch_core_health",
        lambda _clients: {
            "context": (True, 200, {"status": "ok"}),
            "storage": (True, 200, {"status": "ok"}),
            "policy": (True, 200, {"status": "ok"}),
            "inference": (True, 200, {"status": "ok"}),
        },
    )
    monkeypatch.setattr(PowerOnController, "_wait_renderer_ready", lambda self, renderer_url, trace: asyncio.sleep(0, result=True))
    monkeypatch.setattr(PowerOnController, "_check_speech_ready", lambda self, url, trace: asyncio.sleep(0, result=(True, None)))
    monkeypatch.setattr(
        PowerOnController,
        "_auth_bootstrap_check",
        lambda self, trace: asyncio.sleep(
            0,
            result=(False, True, {"ready": False, "status": 200, "bootstrap_required": True}),
        ),
    )

    result = asyncio.run(PowerOnController(clients=clients).run())

    assert result.bootstrap_required is True
    assert result.onboarding_required is True
    assert result.stage == "AUTH_BOOTSTRAP_REQUIRED"
    assert result.auth_ready is False
    assert result.checks["auth"]["bootstrap_required"] is True


def test_poweron_reaches_ready_listening(monkeypatch):
    monkeypatch.setenv("UNISON_RENDERER_URL", "http://renderer:8092")
    monkeypatch.setattr(
        "src.orchestrator.power_on.controller.RendererEmitter.emit",
        lambda self, **kwargs: None,
    )
    manifest = {
        "modalities": {"displays": [{"id": "display-1"}]},
        "io": {"speech": {"enabled": True, "endpoint": "http://speech:8084", "ws_endpoint": "ws://speech:8084/stream"}},
    }
    clients = SimpleNamespace()

    monkeypatch.setattr(
        "src.orchestrator.power_on.controller.CapabilityClient.from_env",
        lambda: _FakeCapabilityClient(manifest),
    )
    monkeypatch.setattr(
        "src.orchestrator.power_on.controller.fetch_core_health",
        lambda _clients: {
            "context": (True, 200, {"status": "ok"}),
            "storage": (True, 200, {"status": "ok"}),
            "policy": (True, 200, {"status": "ok"}),
            "inference": (True, 200, {"status": "ok"}),
        },
    )
    monkeypatch.setattr(PowerOnController, "_wait_renderer_ready", lambda self, renderer_url, trace: asyncio.sleep(0, result=True))
    monkeypatch.setattr(PowerOnController, "_check_speech_ready", lambda self, url, trace: asyncio.sleep(0, result=(True, None)))
    monkeypatch.setattr(
        PowerOnController,
        "_auth_bootstrap_check",
        lambda self, trace: asyncio.sleep(
            0,
            result=(True, False, {"ready": True, "status": 200, "bootstrap_required": False}),
        ),
    )

    result = asyncio.run(PowerOnController(clients=clients).run())

    assert result.bootstrap_required is False
    assert result.onboarding_required is False
    assert result.stage == "READY_LISTENING"
    assert result.auth_ready is True
    assert result.core_ready is True
    assert result.renderer_ready is True
    assert result.speech_ready is True
