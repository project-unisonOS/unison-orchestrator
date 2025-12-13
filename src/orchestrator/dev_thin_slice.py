from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from unison_common import (
    ActionResult,
    IntentSession,
    PolicyDecision,
    ResponseObjectModel,
    TraceRecorder,
    TraceSpanStatus,
)

from orchestrator.clients import ServiceClients
from orchestrator.event_graph.store import JsonlEventGraphStore, new_event
from orchestrator.interaction.planner_stage import PlannerStage
from orchestrator.interaction.policy_gate import PolicyGate
from orchestrator.interaction.rom_builder import RomBuilder
from orchestrator.interaction.router_stage import RouterStage
from orchestrator.interaction.context_reader import ContextReader
from orchestrator.interaction.write_behind import ContextWriteBehindQueue
from orchestrator.interaction.tools import ToolRegistry


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

def _format_traceparent(trace_id_hex: str, span_id_hex16: str = "0000000000000001") -> str:
    """
    Best-effort W3C traceparent header for downstream services.
    This is not OpenTelemetry-managed; it exists to propagate correlation.
    """
    trace_id = (trace_id_hex or uuid.uuid4().hex).replace("-", "")[:32].ljust(32, "0")
    span_id = (span_id_hex16 or uuid.uuid4().hex[:16])[:16].ljust(16, "0")
    return f"00-{trace_id}-{span_id}-01"


def emit_to_renderer(*, renderer_url: str, rom: ResponseObjectModel) -> tuple[bool, Optional[int]]:
    payload = rom.model_dump(mode="json")
    envelope: Dict[str, Any] = {
        "type": "rom.render",
        "payload": payload,
        "ts": time.time(),
        "trace_id": rom.trace_id,
        "session_id": rom.session_id,
        "person_id": rom.person_id,
    }
    headers = {
        "x-request-id": rom.trace_id,
        "x-trace-id": rom.trace_id,
        "traceparent": _format_traceparent(rom.trace_id),
    }
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.post(f"{renderer_url.rstrip('/')}/events", json=envelope, headers=headers)
        return resp.status_code < 400, resp.status_code
    except Exception:
        return False, None


def run_thin_slice(
    *,
    text: str,
    person_id: str = "local-person",
    session_id: Optional[str] = None,
    renderer_url: Optional[str] = None,
    trace_dir: str = "traces",
    clients: ServiceClients | None = None,
) -> ThinSliceResult:
    trace = TraceRecorder(service="unison-orchestrator.dev_thin_slice")
    sid = session_id or f"dev-session-{uuid.uuid4().hex[:8]}"
    event_graph_enabled = os.getenv("UNISON_EVENT_GRAPH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    event_store = JsonlEventGraphStore.from_env() if event_graph_enabled else None
    last_event_id: str | None = None

    def _append(event_type: str, *, attrs: Dict[str, Any] | None = None, payload: Dict[str, Any] | None = None) -> None:
        nonlocal last_event_id
        if not event_store:
            return
        evt = new_event(
            trace_id=trace.trace_id,
            event_type=event_type,
            actor="dev_thin_slice",
            person_id=person_id,
            session_id=sid,
            attrs=attrs,
            payload=payload,
            causation_id=last_event_id,
        )
        from unison_common import EventGraphAppend

        event_store.append(
            EventGraphAppend(trace_id=trace.trace_id, session_id=sid, person_id=person_id, events=[evt])
        )
        last_event_id = evt.event_id

    trace.emit_event("input_received", {"modality": "text"})
    with trace.span("input_received", {"modality": "text"}):
        pass
    _append("input_received", attrs={"modality": "text"}, payload={"text": text})
    with trace.span("session_created", {"session_id": sid}):
        session = IntentSession(session_id=sid, trace_id=trace.trace_id, person_id=person_id, created_at_unix_ms=_now_unix_ms())
    _append("session_created", attrs={"session_id": sid})

    router = RouterStage()
    planner = PlannerStage()
    policy_gate = PolicyGate(clients=clients)
    tools = ToolRegistry.default()
    rom_builder = RomBuilder()
    context_reader = ContextReader.from_env()
    write_behind = ContextWriteBehindQueue()

    # Create a minimal v1 input envelope for routing/planning.
    from unison_common import InputEventEnvelope

    input_event = InputEventEnvelope(
        event_id=str(uuid.uuid4()),
        trace_id=trace.trace_id,
        ts_unix_ms=_now_unix_ms(),
        source="dev_thin_slice",
        modality="text",
        payload={"text": text},
        person_id=person_id,
        session_id=session.session_id,
    )

    context_snapshot = None
    if clients is not None:
        context_snapshot = context_reader.read(clients=clients, person_id=person_id, trace=trace)
        _append("context_snapshot", attrs={"has_profile": context_snapshot.profile is not None, "has_dashboard": context_snapshot.dashboard is not None})

    with trace.span("router_started"):
        _ = router.run(input_event, trace)
    with trace.span("router_ended"):
        pass
    _append("router_completed")

    with trace.span("planner_started"):
        planner_out = planner.run(text=text, trace=trace, context=context_snapshot)
    trace.emit_event("planner_ended", {"schema_version": planner_out.schema_version})
    with trace.span("planner_ended", {"schema_version": planner_out.schema_version}):
        pass
    _append("planner_output", attrs={"intent": planner_out.plan.intent.name, "actions": len(planner_out.plan.actions)})

    action = planner_out.plan.actions[0] if planner_out.plan.actions else None
    if action is None:
        tool_result = ActionResult(action_id="none", ok=False, error="planner produced no actions")
        policy = PolicyDecision(allowed=False, reason="no actions")
        rom = rom_builder.build(
            trace_id=trace.trace_id,
            session_id=session.session_id,
            person_id=person_id,
            tool_result=tool_result,
            policy=policy,
        )
        trace.emit_event("rom_built")
        trace_path = str(trace.write_json(f"{trace_dir}/{trace.trace_id}.json"))
        return ThinSliceResult(
            trace_id=trace.trace_id,
            session_id=session.session_id,
            rom=rom,
            tool_result=tool_result,
            policy=policy,
            trace_path=trace_path,
            renderer_ok=False,
            renderer_status=None,
        )

    with trace.span("policy_checked", {"action": action.name}):
        policy = policy_gate.check(
            action,
            trace=trace,
            event_id=trace.trace_id,
            actor="dev_thin_slice",
            person_id=person_id,
            auth_scope=None,
            safety_context=None,
        )
    _append("policy_decision", attrs={"allowed": policy.allowed, "reason": policy.reason, "require_confirmation": policy.require_confirmation}, payload={"action": action.name})

    if not policy.allowed:
        trace.emit_event("policy_denied", {"reason": policy.reason})
        tool_result = ActionResult(action_id=action.action_id, ok=False, error=f"policy denied: {policy.reason}")
    else:
        with trace.span("tool_started", {"tool": action.name}):
            tool_result = tools.execute(action)
        trace.emit_event("tool_ended", {"ok": tool_result.ok})
        with trace.span("tool_ended", {"ok": tool_result.ok, "tool": action.name}):
            pass
    _append("tool_result", attrs={"ok": tool_result.ok}, payload={"action_id": tool_result.action_id, "tool": action.name, "result": tool_result.result, "error": tool_result.error})

    with trace.span("rom_built"):
        rom = rom_builder.build(
            trace_id=trace.trace_id,
            session_id=session.session_id,
            person_id=person_id,
            tool_result=tool_result,
            policy=policy,
        )
    _append("rom_built", payload={"rom": rom.model_dump(mode="json")})

    renderer_ok = False
    renderer_status: Optional[int] = None
    if renderer_url is None:
        renderer_url = (
            os.getenv("UNISON_RENDERER_URL")
            or os.getenv("UNISON_EXPERIENCE_RENDERER_URL")
            or os.getenv("UNISON_EXPERIENCE_RENDERER_BASE_URL")
        )

    if renderer_url:
        with trace.span("renderer_emitted", {"renderer_url": renderer_url}):
            renderer_ok, renderer_status = emit_to_renderer(renderer_url=renderer_url, rom=rom)
        trace.emit_event(
            "renderer_emitted",
            {"ok": renderer_ok, "status": renderer_status, "renderer_url": renderer_url},
        )
        _append("renderer_emitted", attrs={"ok": renderer_ok, "status": renderer_status, "renderer_url": renderer_url})
    else:
        trace.emit_event("renderer_skipped")
        _append("renderer_skipped")

    if clients is not None:
        with trace.span("context_write_queued", {"ok": True}):
            batch = write_behind.enqueue_last_interaction(
                person_id=person_id,
                session_id=session.session_id,
                trace_id=trace.trace_id,
                input_text=text,
            )
        _append("context_write_queued", attrs={"batch_id": batch.batch_id})
        write_behind.flush_sync(clients=clients, batch=batch, trace=trace)
        _append("context_write_flushed", attrs={"batch_id": batch.batch_id})
    else:
        trace.emit_event("context_write_queued", {"ok": True, "noop": True})
        with trace.span("context_write_queued", {"ok": True, "noop": True}):
            pass
        _append("context_write_queued", attrs={"noop": True})

    status = TraceSpanStatus.OK if tool_result.ok and (renderer_url is None or renderer_ok) else TraceSpanStatus.ERROR
    trace.emit_event("completed", {"status": status.value})
    _append("completed", attrs={"status": status.value})

    trace_path = str(trace.write_json(f"{trace_dir}/{trace.trace_id}.json"))
    return ThinSliceResult(
        trace_id=trace.trace_id,
        session_id=session.session_id,
        rom=rom,
        tool_result=tool_result,
        policy=policy,
        trace_path=trace_path,
        renderer_ok=renderer_ok,
        renderer_status=renderer_status,
    )
