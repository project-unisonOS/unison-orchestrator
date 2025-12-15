#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional


def _ensure_src_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    # When running from the workspace (not installed), make `unison_common` importable.
    common_src = repo_root.parent / "unison-common" / "src"
    if common_src.exists() and str(common_src) not in sys.path:
        sys.path.insert(0, str(common_src))


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: Optional[list[str]] = None) -> int:
    _ensure_src_on_path()

    from orchestrator import OrchestratorSettings, ServiceClients
    from orchestrator.phase1.runner import Phase1RunConfig, run_phase1_input_event
    from unison_common import InputEventEnvelope, Phase1NdjsonTrace, TraceRecorder

    parser = argparse.ArgumentParser(description="UnisonOS Phase 1 boot (Checkpoint B).")
    parser.add_argument("--mode", required=True, choices=["fullscreen", "headless-voice"])
    parser.add_argument("--text", default="hello")
    parser.add_argument("--person-id", default=os.getenv("UNISON_DEFAULT_PERSON_ID", "local-person"))
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--renderer-url", default=None, help="e.g. http://localhost:8092")
    parser.add_argument("--phase1-trace-path", default=None, help="NDJSON path; defaults to workspace var/traces/unison-phase1.ndjson")
    args = parser.parse_args(argv)

    trace_id = uuid.uuid4().hex
    session_id = args.session_id or f"phase1-{uuid.uuid4().hex[:8]}"

    default_trace_path = _workspace_root() / "var" / "traces" / "unison-phase1.ndjson"
    trace_path = Path(args.phase1_trace_path).expanduser().resolve() if args.phase1_trace_path else default_trace_path
    os.environ.setdefault("UNISON_PHASE1_TRACE_ENABLED", "true")
    os.environ.setdefault("UNISON_PHASE1_VERIFY_PROMPT_INJECTION", "true")
    os.environ.setdefault("UNISON_PHASE1_PIPELINE", "true")
    os.environ.setdefault("UNISON_PHASE1_MODE", "true")
    os.environ["UNISON_PHASE1_TRACE_PATH"] = str(trace_path)

    phase1_trace = Phase1NdjsonTrace.from_env()
    phase1_trace.emit(
        trace_id=trace_id,
        source="boot",
        type="boot.mode_selected",
        level="info",
        payload={"mode": args.mode},
    )

    settings = OrchestratorSettings.from_env()
    clients = ServiceClients.from_endpoints(settings.endpoints)

    input_event = InputEventEnvelope(
        event_id=str(uuid.uuid4()),
        trace_id=trace_id,
        ts_unix_ms=int(time.time() * 1000),
        source=f"phase1.boot.{args.mode}",
        modality="speech" if args.mode == "headless-voice" else "text",
        payload={"text": args.text, "transcript": args.text} if args.mode == "headless-voice" else {"text": args.text},
        person_id=args.person_id,
        session_id=session_id,
        auth={},
    )

    _ = TraceRecorder(service="unison-phase1.boot", trace_id=trace_id)
    result = run_phase1_input_event(
        input_event=input_event,
        clients=clients,
        cfg=Phase1RunConfig(trace_dir=str(_workspace_root() / "traces"), renderer_url=args.renderer_url),
    )

    phase1_trace.emit(
        trace_id=trace_id,
        source="boot",
        type="boot.ready",
        level="info",
        payload={"mode": args.mode, "ok": bool(result.tool_result.ok)},
    )

    summary = {
        "mode": args.mode,
        "trace_id": trace_id,
        "session_id": session_id,
        "person_id": args.person_id,
        "ok": bool(result.tool_result.ok),
        "policy": result.policy.model_dump(mode="json"),
        "rom": result.rom.model_dump(mode="json"),
        "renderer": {"ok": bool(result.renderer_ok), "status": result.renderer_status, "url": args.renderer_url},
        "trace_artifact_path": result.trace_path,
        "phase1_trace_path": str(trace_path),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result.tool_result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
