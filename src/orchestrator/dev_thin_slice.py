from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from unison_common import ActionResult, PolicyDecision, ResponseObjectModel, InputEventEnvelope

from orchestrator.clients import ServiceClients
from orchestrator.interaction.input_runner import run_input_event


@dataclass(frozen=True)
class ThinSliceResult:
    trace_id: str
    session_id: str
    rom: ResponseObjectModel
    tool_result: ActionResult
    policy: PolicyDecision
    trace_path: str
    renderer_ok: bool
    renderer_status: Optional[int]


def _now_unix_ms() -> int:
    return int(time.time() * 1000)

def run_thin_slice(
    *,
    text: str,
    person_id: str = "local-person",
    session_id: Optional[str] = None,
    renderer_url: Optional[str] = None,
    trace_dir: str = "traces",
    clients: ServiceClients | None = None,
) -> ThinSliceResult:
    sid = session_id or f"dev-session-{uuid.uuid4().hex[:8]}"
    input_event = InputEventEnvelope(
        event_id=str(uuid.uuid4()),
        trace_id=str(uuid.uuid4().hex),
        ts_unix_ms=_now_unix_ms(),
        source="dev_thin_slice",
        modality="text",
        payload={"text": text},
        person_id=person_id,
        session_id=sid,
    )
    out = run_input_event(input_event=input_event, clients=clients, trace_dir=trace_dir, renderer_url=renderer_url)
    return ThinSliceResult(
        trace_id=out.trace_id,
        session_id=out.session_id,
        rom=out.rom,
        tool_result=out.tool_result,
        policy=out.policy,
        trace_path=out.trace_path,
        renderer_ok=out.renderer_ok,
        renderer_status=out.renderer_status,
    )
