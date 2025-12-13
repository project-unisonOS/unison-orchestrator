from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import httpx

from orchestrator.clients import ServiceClients
from orchestrator.event_graph.store import JsonlEventGraphStore, new_event
from orchestrator.interaction.context_reader import ContextReader
from orchestrator.interaction.planner_stage import PlannerStage
from orchestrator.interaction.policy_gate import PolicyGate
from orchestrator.interaction.rom_builder import RomBuilder
from orchestrator.interaction.router_stage import RouterStage
from orchestrator.interaction.tools import ToolRegistry
from orchestrator.interaction.write_behind import ContextWriteBehindQueue
from orchestrator.interaction.vdi_executor import VdiExecutor
from unison_common import (
    ActionResult,
    EventGraphAppend,
    InputEventEnvelope,
    IntentSession,
    PolicyDecision,
    ResponseObjectModel,
    TraceRecorder,
    TraceSpanStatus,
)


def _now_unix_ms() -> int:
    return int(time.time() * 1000)


def _format_traceparent(trace_id_hex: str, span_id_hex16: str = "0000000000000001") -> str:
    trace_id = (trace_id_hex or uuid.uuid4().hex).replace("-", "")[:32].ljust(32, "0")
    span_id = (span_id_hex16 or uuid.uuid4().hex[:16])[:16].ljust(16, "0")
    return f"00-{trace_id}-{span_id}-01"


@dataclass(frozen=True)
class InputRunResult:
    trace_id: str
    session_id: str
    person_id: Optional[str]
    rom: ResponseObjectModel
    tool_result: ActionResult
    policy: PolicyDecision
    trace_path: str
    renderer_ok: bool
    renderer_status: Optional[int]


@dataclass(frozen=True)
class RendererEmitter:
    renderer_url: str

    def emit(self, *, trace_id: str, session_id: str, person_id: Optional[str], type: str, payload: Dict[str, Any]) -> tuple[bool, Optional[int]]:
        envelope: Dict[str, Any] = {
            "type": type,
            "payload": payload,
            "ts": time.time(),
            "trace_id": trace_id,
            "session_id": session_id,
            "person_id": person_id,
        }
        headers = {
            "x-request-id": trace_id,
            "x-trace-id": trace_id,
            "traceparent": _format_traceparent(trace_id),
        }
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.post(f"{self.renderer_url.rstrip('/')}/events", json=envelope, headers=headers)
            return resp.status_code < 400, resp.status_code
        except Exception:
            return False, None


def run_input_event(
    *,
    input_event: InputEventEnvelope,
    clients: ServiceClients | None,
    trace_dir: str = "traces",
    renderer_url: Optional[str] = None,
) -> InputRunResult:
    trace = TraceRecorder(service="unison-orchestrator.input", trace_id=input_event.trace_id or None)
    sid = input_event.session_id or f"session-{uuid.uuid4().hex[:8]}"
    person_id = input_event.person_id

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
            actor="orchestrator",
            person_id=person_id,
            session_id=sid,
            attrs=attrs,
            payload=payload,
            causation_id=last_event_id,
        )
        event_store.append(EventGraphAppend(trace_id=trace.trace_id, session_id=sid, person_id=person_id, events=[evt]))
        last_event_id = evt.event_id

    with trace.span("input_received", {"modality": input_event.modality, "source": input_event.source}):
        trace.emit_event("input_received", {"modality": input_event.modality})
        _append("input_received", attrs={"modality": input_event.modality, "source": input_event.source}, payload={"payload": input_event.payload})

    with trace.span("session_created", {"session_id": sid}):
        _ = IntentSession(session_id=sid, trace_id=trace.trace_id, person_id=person_id, created_at_unix_ms=_now_unix_ms())
    _append("session_created", attrs={"session_id": sid})

    router = RouterStage()
    planner = PlannerStage()
    policy_gate = PolicyGate(clients=clients)
    tools = ToolRegistry.default()
    vdi = VdiExecutor()
    rom_builder = RomBuilder()
    context_reader = ContextReader.from_env()
    write_behind = ContextWriteBehindQueue()

    context_snapshot = None
    if clients is not None and person_id:
        context_snapshot = context_reader.read(clients=clients, person_id=person_id, trace=trace)
        _append("context_snapshot", attrs={"has_profile": context_snapshot.profile is not None, "has_dashboard": context_snapshot.dashboard is not None})

    with trace.span("router_started"):
        router_out = router.run(input_event, trace)
    _append("router_completed", attrs=router_out.model_dump(mode="json"))

    # Choose text from input payload.
    text = str((input_event.payload or {}).get("text") or (input_event.payload or {}).get("transcript") or "").strip()

    # Early renderer feedback: intent recognized.
    renderer_ok = False
    renderer_status: Optional[int] = None
    if renderer_url is None:
        renderer_url = os.getenv("UNISON_RENDERER_URL") or os.getenv("UNISON_EXPERIENCE_RENDERER_URL")
        if not renderer_url:
            host = os.getenv("UNISON_EXPERIENCE_RENDERER_HOST")
            port = os.getenv("UNISON_EXPERIENCE_RENDERER_PORT")
            if host and port:
                renderer_url = f"http://{host}:{port}"
    emitter = RendererEmitter(renderer_url) if renderer_url else None
    if emitter:
        with trace.span("first_feedback_emitted"):
            renderer_ok, renderer_status = emitter.emit(
                trace_id=trace.trace_id,
                session_id=sid,
                person_id=person_id,
                type="intent.recognized",
                payload={"person_id": person_id, "session_id": sid},
            )
        trace.emit_event("renderer_first_feedback", {"ok": renderer_ok, "status": renderer_status})
        _append("renderer_emitted", attrs={"type": "intent.recognized", "ok": renderer_ok, "status": renderer_status})

    with trace.span("planner_started"):
        planner_out = planner.run(text=text, trace=trace, context=context_snapshot)
    trace.emit_event("planner_ended", {"intent": planner_out.plan.intent.name, "actions": len(planner_out.plan.actions)})
    _append("planner_output", attrs={"intent": planner_out.plan.intent.name, "actions": len(planner_out.plan.actions)})

    action = planner_out.plan.actions[0] if planner_out.plan.actions else None
    if action is None:
        tool_result = ActionResult(action_id="none", ok=False, error="planner produced no actions")
        policy = PolicyDecision(allowed=False, reason="no actions")
        rom = rom_builder.build(trace_id=trace.trace_id, session_id=sid, person_id=person_id or "unknown", tool_result=tool_result, policy=policy)
        trace_path = str(trace.write_json(f"{trace_dir}/{trace.trace_id}.json"))
        return InputRunResult(
            trace_id=trace.trace_id,
            session_id=sid,
            person_id=person_id,
            rom=rom,
            tool_result=tool_result,
            policy=policy,
            trace_path=trace_path,
            renderer_ok=renderer_ok,
            renderer_status=renderer_status,
        )

    with trace.span("policy_checked", {"action": action.name}):
        policy = policy_gate.check(
            action,
            trace=trace,
            event_id=trace.trace_id,
            actor=input_event.source,
            person_id=person_id,
            auth_scope=None,
            safety_context=None,
        )
    _append("policy_decision", attrs={"allowed": policy.allowed, "reason": policy.reason}, payload={"action": action.name})

    if not policy.allowed:
        tool_result = ActionResult(action_id=action.action_id, ok=False, error=f"policy denied: {policy.reason}")
    else:
        # Populate person/session for VDI tasks.
        if action.kind == "vdi":
            action.args.setdefault("person_id", person_id)
            action.args.setdefault("session_id", sid)
        with trace.span("tool_started", {"tool": action.name, "kind": action.kind}):
            if action.kind == "vdi":
                if emitter:
                    emitter.emit(
                        trace_id=trace.trace_id,
                        session_id=sid,
                        person_id=person_id,
                        type="outcome.reflected",
                        payload={"text": f"Starting VDI task: {action.name}", "person_id": person_id, "session_id": sid},
                    )
                tool_result = (
                    vdi.execute(action=action, clients=clients, trace=trace)
                    if clients and clients.actuation
                    else ActionResult(action_id=action.action_id, ok=False, error="actuation client not configured")
                )
            else:
                tool_result = tools.execute(action)
        trace.emit_event("tool_ended", {"ok": tool_result.ok})
        if emitter and action.kind == "vdi":
            emitter.emit(
                trace_id=trace.trace_id,
                session_id=sid,
                person_id=person_id,
                type="outcome.reflected",
                payload={"text": f"VDI task {action.name} completed (ok={tool_result.ok})", "person_id": person_id, "session_id": sid},
            )
    _append("tool_result", attrs={"ok": tool_result.ok}, payload={"tool": action.name, "error": tool_result.error})

    with trace.span("rom_built"):
        rom = rom_builder.build(
            trace_id=trace.trace_id,
            session_id=sid,
            person_id=person_id or "unknown",
            tool_result=tool_result,
            policy=policy,
        )
    _append("rom_built")

    if emitter:
        with trace.span("renderer_emitted", {"renderer_url": emitter.renderer_url}):
            ok2, st2 = emitter.emit(
                trace_id=trace.trace_id,
                session_id=sid,
                person_id=person_id,
                type="rom.render",
                payload=rom.model_dump(mode="json"),
            )
            renderer_ok = renderer_ok and ok2 if renderer_ok else ok2
            renderer_status = st2
        trace.emit_event("renderer_emitted", {"ok": renderer_ok, "status": renderer_status})
        _append("renderer_emitted", attrs={"type": "rom.render", "ok": renderer_ok, "status": renderer_status})

    if clients is not None and person_id:
        with trace.span("context_write_queued"):
            batch = write_behind.enqueue_last_interaction(
                person_id=person_id, session_id=sid, trace_id=trace.trace_id, input_text=text
            )
        write_behind.flush_sync(clients=clients, batch=batch, trace=trace)
        _append("context_write_flushed", attrs={"batch_id": batch.batch_id})

    status = TraceSpanStatus.OK if tool_result.ok and (not emitter or renderer_ok) else TraceSpanStatus.ERROR
    trace.emit_event("completed", {"status": status.value})
    _append("completed", attrs={"status": status.value})

    trace_path = str(trace.write_json(f"{trace_dir}/{trace.trace_id}.json"))
    return InputRunResult(
        trace_id=trace.trace_id,
        session_id=sid,
        person_id=person_id,
        rom=rom,
        tool_result=tool_result,
        policy=policy,
        trace_path=trace_path,
        renderer_ok=renderer_ok,
        renderer_status=renderer_status,
    )
