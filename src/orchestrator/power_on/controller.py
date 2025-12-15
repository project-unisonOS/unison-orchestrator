from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import httpx

from orchestrator.clients import ServiceClients
from orchestrator.interaction.input_runner import RendererEmitter
from unison_common import TraceRecorder
from unison_common.multimodal import CapabilityClient


def _now_unix_ms() -> int:
    return int(time.time() * 1000)


def _default_renderer_url() -> Optional[str]:
    url = os.getenv("UNISON_RENDERER_URL") or os.getenv("UNISON_EXPERIENCE_RENDERER_URL")
    if url:
        return url.rstrip("/")
    host = os.getenv("UNISON_EXPERIENCE_RENDERER_HOST")
    port = os.getenv("UNISON_EXPERIENCE_RENDERER_PORT")
    if host and port:
        return f"http://{host}:{port}".rstrip("/")
    return None


def _default_speech_http_url(manifest: Dict[str, Any]) -> Optional[str]:
    speech = (manifest.get("io") or {}).get("speech") if isinstance(manifest.get("io"), dict) else None
    if isinstance(speech, dict):
        endpoint = speech.get("endpoint")
        if isinstance(endpoint, str) and endpoint.strip():
            return endpoint.strip().rstrip("/")
    # devstack default service name
    return os.getenv("UNISON_IO_SPEECH_URL", "http://io-speech:8084").rstrip("/")


def _default_speech_ws_url(manifest: Dict[str, Any]) -> Optional[str]:
    speech = (manifest.get("io") or {}).get("speech") if isinstance(manifest.get("io"), dict) else None
    if isinstance(speech, dict):
        endpoint = speech.get("ws_endpoint")
        if isinstance(endpoint, str) and endpoint.strip():
            return endpoint.strip()
    # Browser-facing default is injected separately; keep container default here.
    return os.getenv("UNISON_IO_SPEECH_WS_URL", "ws://io-speech:8084/stream")


@dataclass(frozen=True)
class PowerOnResult:
    trace_id: str
    trace: TraceRecorder
    renderer_url: Optional[str]
    renderer_ready: bool
    speech_ready: bool
    speech_reason: Optional[str]
    manifest: Dict[str, Any]


class PowerOnController:
    def __init__(self, *, clients: ServiceClients, trace_dir: str = "traces") -> None:
        self._clients = clients
        self._trace_dir = trace_dir

    async def run(self) -> PowerOnResult:
        trace_id = uuid.uuid4().hex
        trace = TraceRecorder(service="unison-orchestrator.poweron", trace_id=trace_id)
        trace.emit_event("poweron.boot_start", {"ts_unix_ms": _now_unix_ms()})

        renderer_url = _default_renderer_url()
        emitter = RendererEmitter(renderer_url) if renderer_url else None

        def emit(stage_type: str, payload: Dict[str, Any]) -> None:
            if not emitter:
                return
            try:
                emitter.emit(trace_id=trace_id, session_id="poweron", person_id=None, type=stage_type, payload=payload)
            except Exception:
                pass

        emit("BOOT_START", {"stage": "BOOT_START"})

        manifest = {}
        manifest_client = CapabilityClient.from_env()
        with trace.span("poweron.manifest_load"):
            manifest = manifest_client.refresh() or {}
        trace.emit_event("poweron.manifest_loaded", {"ts_unix_ms": _now_unix_ms(), "ok": bool(manifest)})

        renderer_assets = (manifest.get("renderer") or {}).get("assets") if isinstance(manifest.get("renderer"), dict) else None
        logo = renderer_assets.get("logo") if isinstance(renderer_assets, dict) else None
        earcon = renderer_assets.get("startup_earcon") if isinstance(renderer_assets, dict) else None

        emit(
            "MANIFEST_LOADED",
            {
                "stage": "MANIFEST_LOADED",
                "logo": logo,
                "startup_earcon": earcon,
                "ts_unix_ms": _now_unix_ms(),
            },
        )

        # IO discovery (best-effort): combine manifest-declared modalities and service health.
        speech_http = _default_speech_http_url(manifest)
        speech_ws = _default_speech_ws_url(manifest)
        speech_cfg = (manifest.get("io") or {}).get("speech") if isinstance(manifest.get("io"), dict) else {}
        speech_enabled = True
        if isinstance(speech_cfg, dict) and isinstance(speech_cfg.get("enabled"), bool):
            speech_enabled = bool(speech_cfg.get("enabled"))

        io_summary = {
            "modalities": manifest.get("modalities") or {},
            "speech": {
                "enabled": speech_enabled,
                "endpoint": speech_http,
                "ws_endpoint": speech_ws,
            },
        }
        trace.emit_event("poweron.io_discovered", {"ts_unix_ms": _now_unix_ms(), "io": io_summary})
        emit("IO_DISCOVERED", {"stage": "IO_DISCOVERED", "io": io_summary, "ts_unix_ms": _now_unix_ms()})

        renderer_ready = False
        if renderer_url:
            renderer_ready = await self._wait_renderer_ready(renderer_url, trace=trace)
        trace.emit_event("poweron.renderer_ready", {"ts_unix_ms": _now_unix_ms(), "ready": renderer_ready})
        emit("RENDERER_READY", {"stage": "RENDERER_READY", "ready": renderer_ready, "ts_unix_ms": _now_unix_ms()})

        speech_ready = False
        speech_reason: Optional[str] = None
        if not speech_enabled:
            speech_reason = "disabled_by_manifest"
        else:
            speech_ready, speech_reason = await self._check_speech_ready(speech_http, trace=trace)
        if speech_ready:
            emit("SPEECH_READY", {"stage": "SPEECH_READY", "endpoint": speech_http, "ts_unix_ms": _now_unix_ms()})
        else:
            emit(
                "SPEECH_UNAVAILABLE",
                {"stage": "SPEECH_UNAVAILABLE", "reason": speech_reason or "unavailable", "ts_unix_ms": _now_unix_ms()},
            )

        # Enter "ready/listening" mode via renderer instruction (actual capture wiring is done later).
        default_asr_profile = None
        default_tts_profile = None
        endpointing = None
        if isinstance(speech_cfg, dict):
            default_asr_profile = speech_cfg.get("default_asr_profile")
            default_tts_profile = speech_cfg.get("default_tts_profile")
            endpointing = speech_cfg.get("endpointing")
        emit(
            "READY_LISTENING",
            {
                "stage": "READY_LISTENING",
                "speech_enabled": speech_enabled,
                "speech_ws_endpoint": speech_ws,
                "asr_profile": default_asr_profile,
                "tts_profile": default_tts_profile,
                "endpointing": endpointing,
                "ts_unix_ms": _now_unix_ms(),
            },
        )

        return PowerOnResult(
            trace_id=trace_id,
            trace=trace,
            renderer_url=renderer_url,
            renderer_ready=renderer_ready,
            speech_ready=speech_ready,
            speech_reason=speech_reason,
            manifest=manifest,
        )

    async def _wait_renderer_ready(self, renderer_url: str, *, trace: TraceRecorder) -> bool:
        timeout_s = float(os.getenv("UNISON_POWERON_RENDERER_READY_TIMEOUT_S", "10.0"))
        deadline = time.time() + max(0.1, timeout_s)
        url = f"{renderer_url.rstrip('/')}/ready"
        async with httpx.AsyncClient(timeout=1.2) as client:
            while time.time() < deadline:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        body = resp.json() or {}
                        if bool(body.get("ready")):
                            return True
                except Exception:
                    pass
                await asyncio.sleep(0.25)
        trace.emit_event("poweron.renderer_ready_timeout", {"timeout_s": timeout_s})
        return False

    async def _check_speech_ready(self, speech_http_url: Optional[str], *, trace: TraceRecorder) -> Tuple[bool, Optional[str]]:
        if not speech_http_url:
            return False, "missing_endpoint"
        url = f"{speech_http_url.rstrip('/')}/readyz"
        try:
            async with httpx.AsyncClient(timeout=1.2) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return True, None
                return False, f"status_{resp.status_code}"
        except Exception as exc:
            trace.emit_event("speech.ready_check_error", {"error": str(exc)})
            return False, "connection_error"
