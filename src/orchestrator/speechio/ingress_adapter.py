from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Any, AsyncIterator, Dict, Optional

import httpx
from asyncio import QueueFull

from orchestrator.interaction.input_runner import RendererEmitter
from unison_common import Phase1NdjsonTrace, TraceRecorder
from unison_common.contracts.v1.speechio import (
    AsrProfile,
    EndpointingPolicy,
    SpeakOptions,
    SpeakResult,
    SpeechCapabilities,
    SpeechStatus,
    TranscriptEvent,
)


def _now_monotonic_ns() -> int:
    return time.perf_counter_ns()


@dataclass
class _CaptureState:
    active: bool = False
    asr_profile: AsrProfile = "fast"
    endpointing: EndpointingPolicy = field(default_factory=EndpointingPolicy)


class IngressSpeechIOAdapter:
    """
    SpeechIO implementation for devstack.

    - Capture is "ingress-driven": the adapter yields TranscriptEvents that are ingested
      via orchestrator `POST /input` (typically forwarded by io-speech).
    - TTS is rendered via io-speech HTTP `/speech/tts`, then played by the renderer
      via `POST /events` (`tts.play` / `tts.stop` envelopes).
    """

    def __init__(self) -> None:
        self._initialized = False
        self._trace: Optional[TraceRecorder] = None
        self._phase1_trace: Optional[Phase1NdjsonTrace] = None
        self._renderer_url: Optional[str] = None
        self._renderer_emitter: Optional[RendererEmitter] = None
        self._speech_http_url: Optional[str] = None
        self._status = SpeechStatus(ready=False, reason="not_initialized")
        self._capabilities = SpeechCapabilities(
            streaming_partials=True,
            barge_in=True,
            endpointing=True,
            local_asr=True,
            neural_tts=True,
            engines={"asr": ["faster-whisper"], "tts": ["piper"]},
        )

        self._capture = _CaptureState()
        self._queue: asyncio.Queue[Optional[TranscriptEvent]] = asyncio.Queue(maxsize=200)

        self._speaking_lock = asyncio.Lock()
        self._speaking = False
        self._tts_profile: str = "lightweight"
        self._speaking_trace_id: Optional[str] = None

    async def initialize(self, config: dict) -> None:
        self._renderer_url = (config.get("renderer_url") or os.getenv("UNISON_RENDERER_URL") or "").rstrip("/") or None
        if self._renderer_url:
            self._renderer_emitter = RendererEmitter(self._renderer_url)
        self._speech_http_url = (config.get("speech_http_url") or os.getenv("UNISON_IO_SPEECH_URL") or "").rstrip("/") or None
        self._trace = config.get("trace") if isinstance(config.get("trace"), TraceRecorder) else None
        if os.getenv("UNISON_PHASE1_TRACE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}:
            self._phase1_trace = Phase1NdjsonTrace.from_env()

        self._initialized = True
        self._status = SpeechStatus(ready=True, reason=None)

    def getCapabilities(self) -> SpeechCapabilities:
        return self._capabilities

    def getStatus(self) -> SpeechStatus:
        return self._status

    async def setActiveProfiles(self, *, asr_profile: AsrProfile, tts_profile: str) -> None:
        self._capture.asr_profile = asr_profile
        self._tts_profile = tts_profile
        self._status.active_asr_profile = asr_profile
        self._status.active_tts_profile = tts_profile  # type: ignore[assignment]

    async def startCapture(
        self,
        *,
        asr_profile: AsrProfile,
        endpointing: EndpointingPolicy,
        locale: Optional[str] = None,
        streaming_facade: bool = True,
    ) -> AsyncIterator[TranscriptEvent]:
        if not self._initialized:
            raise RuntimeError("SpeechIO not initialized")
        _ = locale, streaming_facade

        self._capture.active = True
        self._capture.asr_profile = asr_profile
        self._capture.endpointing = endpointing
        self._status.active_asr_profile = asr_profile
        self._status.chosen_asr_engine = "faster-whisper"

        if self._trace:
            self._trace.emit_event(
                "speech.capture_start",
                {
                    "asr_profile": asr_profile,
                    "endpointing": endpointing.model_dump(mode="json"),
                },
            )

        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield item

    async def stopCapture(self) -> None:
        self._capture.active = False
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass

    def ingest(self, event: TranscriptEvent) -> None:
        """
        Ingest a TranscriptEvent from an external source (io-speech → orchestrator `/input`).

        Enforces barge-in (hard interrupt) on `vad_start`.
        """
        if event.ts_monotonic_ns is None:
            event.ts_monotonic_ns = _now_monotonic_ns()
        event.profile = event.profile or self._capture.asr_profile
        event.engine = event.engine or "faster-whisper"

        if self._phase1_trace and self._trace:
            if event.type == "partial":
                self._phase1_trace.emit(
                    trace_id=self._trace.trace_id,
                    source="speech.asr",
                    type="speech.asr.partial",
                    level="info",
                    payload={"text_len": len((event.text or "").strip()), "engine": event.engine, "profile": event.profile},
                )
            elif event.type == "final":
                self._phase1_trace.emit(
                    trace_id=self._trace.trace_id,
                    source="speech.asr",
                    type="speech.asr.final",
                    level="info",
                    payload={"text_len": len((event.text or "").strip()), "engine": event.engine, "profile": event.profile},
                )
            elif event.type == "vad_start" and self._speaking:
                self._phase1_trace.emit(
                    trace_id=self._trace.trace_id,
                    source="speech.tts",
                    type="speech.tts.barge_in",
                    level="info",
                    payload={"reason": "vad_start"},
                )

        if event.type == "vad_start":
            # Hard interrupt contract enforced inside SpeechIO.
            if self._speaking:
                asyncio.create_task(self.stopSpeaking(reason="barge_in"))

        try:
            self._queue.put_nowait(event)
        except QueueFull:
            pass
        except Exception:
            pass

    async def speak(self, text: str, options: SpeakOptions) -> SpeakResult:
        if not self._initialized:
            return SpeakResult(ok=False, error="SpeechIO not initialized")
        if not self._speech_http_url:
            return SpeakResult(ok=False, error="speech service unavailable")

        profile = options.profile
        allow_barge_in = options.allow_barge_in if options.allow_barge_in is not None else True

        async with self._speaking_lock:
            self._speaking = True
            self._status.active_tts_profile = profile
            self._status.chosen_tts_engine = "piper"
            self._speaking_trace_id = self._trace.trace_id if self._trace else None
            if self._trace:
                self._trace.emit_event("tts.start", {"tts_profile": profile, "engine": self._status.chosen_tts_engine})
            if self._phase1_trace and self._trace:
                self._phase1_trace.emit(
                    trace_id=self._trace.trace_id,
                    source="speech.tts",
                    type="speech.tts.start",
                    level="info",
                    payload={"tts_profile": profile, "engine": self._status.chosen_tts_engine},
                )

            audio_url: Optional[str] = None
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.post(
                        f"{self._speech_http_url.rstrip('/')}/speech/tts",
                        json={"text": text, "profile": profile, "person_id": None, "session_id": "voice-loop"},
                    )
                    body = resp.json() if resp.status_code == 200 else {}
                    audio_url = body.get("audio_url") if isinstance(body, dict) else None
                    engine_name = body.get("engine") if isinstance(body, dict) else None
                    if isinstance(engine_name, str) and engine_name.strip():
                        self._status.chosen_tts_engine = engine_name.strip()
            except Exception as exc:
                self._speaking = False
                if self._trace:
                    self._trace.emit_event("tts.end", {"ok": False, "error": str(exc)})
                if self._phase1_trace and self._trace:
                    self._phase1_trace.emit(
                        trace_id=self._trace.trace_id,
                        source="speech.tts",
                        type="speech.tts.stop",
                        level="warn",
                        payload={"ok": False, "error": str(exc)},
                    )
                return SpeakResult(ok=False, error=str(exc), engine=self._status.chosen_tts_engine, profile=profile)

            if not isinstance(audio_url, str) or not audio_url:
                self._speaking = False
                if self._trace:
                    self._trace.emit_event("tts.end", {"ok": False, "error": "missing_audio_url"})
                if self._phase1_trace and self._trace:
                    self._phase1_trace.emit(
                        trace_id=self._trace.trace_id,
                        source="speech.tts",
                        type="speech.tts.stop",
                        level="warn",
                        payload={"ok": False, "error": "missing_audio_url"},
                    )
                return SpeakResult(ok=False, error="missing_audio_url", engine=self._status.chosen_tts_engine, profile=profile)

            # Best-effort playback: if a renderer is available, ask it to play; otherwise remain headless.
            if self._renderer_emitter:
                self._renderer_emitter.emit(
                    trace_id=self._trace.trace_id if self._trace else os.getenv("UNISON_TRACE_ID", "tts"),
                    session_id="voice-loop",
                    person_id=None,
                    type="tts.play",
                    payload={"audio_url": audio_url, "allow_barge_in": allow_barge_in, "profile": profile},
                )
                if self._trace:
                    self._trace.emit_event("tts.first_audio", {"best_effort": True})

            # No reliable completion callback in this dev wiring; treat as ended once enqueued.
            if self._trace:
                self._trace.emit_event("tts.end", {"ok": True})
            if self._phase1_trace and self._trace:
                self._phase1_trace.emit(
                    trace_id=self._trace.trace_id,
                    source="speech.tts",
                    type="speech.tts.stop",
                    level="info",
                    payload={"ok": True, "audio_url": "present" if bool(audio_url) else "missing"},
                )

            return SpeakResult(ok=True, engine=self._status.chosen_tts_engine, profile=profile, audio_url=audio_url)

    async def stopSpeaking(self, reason: Optional[str] = None) -> None:
        async with self._speaking_lock:
            if not self._speaking:
                return
            self._speaking = False
            if reason == "barge_in" and self._trace:
                self._trace.emit_event("tts.interrupt.barge_in", {})
            if reason == "barge_in" and self._phase1_trace and self._trace:
                self._phase1_trace.emit(
                    trace_id=self._trace.trace_id,
                    source="speech.tts",
                    type="speech.tts.barge_in",
                    level="info",
                    payload={"reason": "barge_in"},
                )
            if self._phase1_trace and self._trace:
                self._phase1_trace.emit(
                    trace_id=self._trace.trace_id,
                    source="speech.tts",
                    type="speech.tts.stop",
                    level="info",
                    payload={"ok": True, "reason": reason or "cancel"},
                )
            if self._renderer_emitter:
                self._renderer_emitter.emit(
                    trace_id=self._trace.trace_id if self._trace else os.getenv("UNISON_TRACE_ID", "tts"),
                    session_id="voice-loop",
                    person_id=None,
                    type="tts.stop",
                    payload={"reason": reason or "cancel"},
                )
