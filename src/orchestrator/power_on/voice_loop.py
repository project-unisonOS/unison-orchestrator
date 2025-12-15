from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from orchestrator.clients import ServiceClients
from orchestrator.interaction.input_runner import RendererEmitter, run_input_event
from orchestrator.speechio.ingress_adapter import IngressSpeechIOAdapter
from unison_common import InputEventEnvelope, TraceRecorder
from unison_common.contracts.v1.speechio import EndpointingPolicy, SpeakOptions, TranscriptEvent


def _now_unix_ms() -> int:
    return int(time.time() * 1000)


def _best_effort_summary(rom: Dict[str, Any]) -> str:
    blocks = rom.get("blocks") if isinstance(rom, dict) else None
    if isinstance(blocks, list):
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                return b["text"].strip()
    return "Done."


@dataclass(frozen=True)
class VoiceLoopConfig:
    trace: TraceRecorder
    manifest: Dict[str, Any]
    clients: ServiceClients | None
    renderer_url: Optional[str]
    trace_dir: str


class VoiceIntentLoop:
    def __init__(self, cfg: VoiceLoopConfig) -> None:
        self._cfg = cfg
        self._speechio = IngressSpeechIOAdapter()
        self._renderer_emitter = RendererEmitter(cfg.renderer_url) if cfg.renderer_url else None
        self._first_partial = False
        self._first_feedback_emitted = False

    @property
    def speechio(self) -> IngressSpeechIOAdapter:
        return self._speechio

    async def run(self) -> None:
        trace = self._cfg.trace
        speech_cfg = (self._cfg.manifest.get("io") or {}).get("speech") if isinstance(self._cfg.manifest.get("io"), dict) else {}
        asr_profile = "fast"
        tts_profile = "lightweight"
        endpointing = EndpointingPolicy()
        if isinstance(speech_cfg, dict):
            if speech_cfg.get("default_asr_profile") in {"fast", "accurate"}:
                asr_profile = speech_cfg["default_asr_profile"]
            if speech_cfg.get("default_tts_profile") in {"lightweight", "natural"}:
                tts_profile = speech_cfg["default_tts_profile"]
            if isinstance(speech_cfg.get("endpointing"), dict):
                endpointing = EndpointingPolicy(**speech_cfg["endpointing"])

        trace.emit_event("speech.initialize_start", {})
        await self._speechio.initialize(
            {
                "renderer_url": self._cfg.renderer_url,
                "speech_http_url": (speech_cfg.get("endpoint") if isinstance(speech_cfg, dict) else None),
                "trace": trace,
            }
        )
        trace.emit_event("speech.initialize_end", {"ok": True})

        await self._speechio.setActiveProfiles(asr_profile=asr_profile, tts_profile=tts_profile)

        async for evt in self._speechio.startCapture(asr_profile=asr_profile, endpointing=endpointing, locale=None, streaming_facade=True):
            await self._handle_event(evt)

    async def _handle_event(self, evt: TranscriptEvent) -> None:
        trace = self._cfg.trace

        if evt.type == "vad_start":
            trace.emit_event("speech.vad_start", {"engine": evt.engine, "asr_profile": evt.profile})
            return

        if evt.type == "partial":
            if not self._first_partial:
                self._first_partial = True
                trace.emit_event("speech.first_partial_transcript", {"engine": evt.engine, "asr_profile": evt.profile})
            text = (evt.text or "").strip()
            if text and self._renderer_emitter:
                ok, st = self._renderer_emitter.emit(
                    trace_id=trace.trace_id,
                    session_id="voice-loop",
                    person_id=None,
                    type="speech.partial",
                    payload={"text": text},
                )
                if ok and not self._first_feedback_emitted:
                    self._first_feedback_emitted = True
                    trace.emit_event("renderer.emitted_first_feedback", {"status": st})
            return

        if evt.type != "final":
            return

        final_text = (evt.text or "").strip()
        if not final_text:
            return

        trace.emit_event("speech.final_transcript", {"text_len": len(final_text), "engine": evt.engine, "asr_profile": evt.profile})

        # Build an InputEventEnvelope and reuse existing interaction runner (but keep this trace artifact).
        input_event = InputEventEnvelope(
            event_id=str(uuid.uuid4()),
            trace_id=trace.trace_id,
            ts_unix_ms=_now_unix_ms(),
            source="speechio",
            modality="speech",
            payload={"text": final_text, "transcript": final_text},
            person_id=None,
            session_id="voice-loop",
            auth={},
        )

        result = run_input_event(
            input_event=input_event,
            clients=self._cfg.clients,
            trace_dir=self._cfg.trace_dir,
            renderer_url=self._cfg.renderer_url,
            trace=trace,
            write_trace=False,
        )

        # Speak response summary (best-effort).
        summary = _best_effort_summary(result.rom.model_dump(mode="json"))
        tts_profile = self._speechio.getStatus().active_tts_profile or "lightweight"
        await self._speechio.speak(summary, SpeakOptions(profile=tts_profile))  # type: ignore[arg-type]

        # For the demo run, write the combined trace artifact after the first response.
        out_dir = self._cfg.trace_dir
        os.makedirs(out_dir, exist_ok=True)
        trace.write_json(os.path.join(out_dir, f"poweron-voice-{int(time.time())}-{trace.trace_id}.json"))
        await self._speechio.stopCapture()
