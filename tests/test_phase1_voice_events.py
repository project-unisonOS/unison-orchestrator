from __future__ import annotations

import json
from pathlib import Path

import asyncio

from orchestrator.speechio.ingress_adapter import IngressSpeechIOAdapter
from unison_common import TraceRecorder
from unison_common.contracts.v1.speechio import SpeakOptions, TranscriptEvent


class _FakeResp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        _ = args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, json: dict):
        _ = url, json
        return _FakeResp(200, {"audio_url": "data:audio/wav;base64,AAAA"})


def test_phase1_voice_emits_asr_and_tts_events(tmp_path, monkeypatch):
    trace_path = tmp_path / "unison-phase1.ndjson"
    monkeypatch.setenv("UNISON_PHASE1_TRACE_ENABLED", "true")
    monkeypatch.setenv("UNISON_PHASE1_TRACE_PATH", str(trace_path))

    # Patch TTS HTTP call.
    import orchestrator.speechio.ingress_adapter as module

    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    async def _run():
        adapter = IngressSpeechIOAdapter()
        trace = TraceRecorder(service="test", trace_id="trace_voice_1")
        await adapter.initialize({"renderer_url": None, "speech_http_url": "http://io-speech:8084", "trace": trace})

        adapter.ingest(TranscriptEvent(type="partial", text="hel", profile="fast", engine="stub"))
        adapter.ingest(TranscriptEvent(type="final", text="hello", profile="fast", engine="stub"))

        await adapter.speak("ok", SpeakOptions(profile="lightweight"))

    asyncio.run(_run())

    lines = trace_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    types = [e.get("type") for e in events]
    assert "speech.asr.partial" in types
    assert "speech.asr.final" in types
    assert "speech.tts.start" in types
    assert "speech.tts.stop" in types


def test_phase1_voice_barge_in_stops_speaking(tmp_path, monkeypatch):
    trace_path = tmp_path / "unison-phase1.ndjson"
    monkeypatch.setenv("UNISON_PHASE1_TRACE_ENABLED", "true")
    monkeypatch.setenv("UNISON_PHASE1_TRACE_PATH", str(trace_path))

    import orchestrator.speechio.ingress_adapter as module

    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    async def _run():
        adapter = IngressSpeechIOAdapter()
        trace = TraceRecorder(service="test", trace_id="trace_voice_2")
        await adapter.initialize({"renderer_url": None, "speech_http_url": "http://io-speech:8084", "trace": trace})

        # Start speaking, then ingest a VAD start to trigger hard interrupt.
        speak_task = asyncio.create_task(adapter.speak("hello", SpeakOptions(profile="lightweight")))
        await asyncio.sleep(0)  # let speak acquire lock
        adapter.ingest(TranscriptEvent(type="vad_start", profile="fast", engine="stub"))
        await asyncio.sleep(0.05)
        await speak_task

    asyncio.run(_run())

    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    types = [e.get("type") for e in events]
    assert "speech.tts.barge_in" in types
    assert "speech.tts.stop" in types
