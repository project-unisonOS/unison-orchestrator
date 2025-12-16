from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from orchestrator.clients import ServiceClients
from orchestrator.interaction.context_reader import ContextReader
from orchestrator.interaction.input_runner import InputRunResult, RendererEmitter
from unison_common import (
    ActionResult,
    InputEventEnvelope,
    Phase1NdjsonTrace,
    PolicyDecision,
    ResponseObjectModel,
    RomText,
    TraceRecorder,
)
from unison_common.models import ModelPackInvalidError, ModelPackMissingError, ModelPackResolver
from unison_common.models.resolver import PackRef
from unison_common.prompt import compile_injected_system_prompt

from .interaction_model import Phase1InteractionModel
from .memory_runtime import Phase1MemoryRuntime
from .onboarding import maybe_plan_onboarding
from .planner import Phase1Planner
from .schema import Phase1SchemaValidator
from .tool_runtime import Phase1ToolRuntime


def _phase1_trace_enabled() -> bool:
    raw = os.getenv("UNISON_PHASE1_TRACE_ENABLED")
    if raw is None:
        return True
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Phase1RunConfig:
    trace_dir: str = "traces"
    renderer_url: Optional[str] = None


def run_phase1_input_event(
    *,
    input_event: InputEventEnvelope,
    clients: ServiceClients | None,
    cfg: Phase1RunConfig,
) -> InputRunResult:
    trace = TraceRecorder(service="unison-orchestrator.phase1", trace_id=input_event.trace_id or None)
    sid = input_event.session_id or f"session-{uuid.uuid4().hex[:8]}"
    person_id = input_event.person_id

    phase1_trace = Phase1NdjsonTrace.from_env() if _phase1_trace_enabled() else None
    if phase1_trace:
        phase1_trace.emit(
            trace_id=trace.trace_id,
            source="boot",
            type="boot.ready",
            level="debug",
            payload={"stage": "phase1.pipeline.start"},
        )

    # Model pack gate (Phase 1.1): best-effort enforce required packs before invoking any local models.
    required_raw = (os.getenv("UNISON_MODEL_PACK_REQUIRED") or "").strip()
    if required_raw:
        pack_id, pack_version = (required_raw.split("@", 1) + [""])[:2]
        if not pack_version:
            pack_version = (os.getenv("UNISON_MODEL_PACK_REQUIRED_VERSION") or "").strip()
        if not pack_id or not pack_version:
            raise RuntimeError("UNISON_MODEL_PACK_REQUIRED must be 'pack_id@version' (or set UNISON_MODEL_PACK_REQUIRED_VERSION)")

        resolver = ModelPackResolver.from_env()

        def _modelpack_emit(event_type: str, payload: dict[str, Any], level: str) -> None:
            if phase1_trace:
                phase1_trace.emit(trace_id=trace.trace_id, source="modelpack", type=event_type, level=level, payload=payload)

        try:
            resolver.ensure_required_pack(ref=PackRef(pack_id=pack_id, pack_version=pack_version), sink=_modelpack_emit)
        except (ModelPackMissingError, ModelPackInvalidError) as exc:
            msg = (
                "Required models are not installed.\n\n"
                f"- Model directory: {resolver.base_dir}\n"
                f"- Required pack: {pack_id}@{pack_version}\n\n"
                "Install offline:\n"
                "  unison-models install --path <path/to/model-pack.tgz>\n\n"
                "Install online:\n"
                "  unison-models install --fetch <url-or-alias>\n"
            )
            rom = ResponseObjectModel(
                trace_id=trace.trace_id,
                session_id=sid,
                person_id=person_id,
                blocks=[RomText(text=msg)],
                meta={"origin": "phase1", "error": "modelpack_missing_or_invalid", "detail": str(exc)},
            )
            return InputRunResult(
                trace_id=trace.trace_id,
                session_id=sid,
                person_id=person_id,
                rom=rom,
                tool_result=ActionResult(action_id="modelpack_gate", ok=False, error="modelpack_missing_or_invalid"),
                policy=PolicyDecision(allowed=False, reason="modelpack_missing_or_invalid", require_confirmation=False, required_scopes=[]),
                trace_path="",
                renderer_ok=False,
                renderer_status=None,
            )

    # Extract user text
    payload = input_event.payload if isinstance(input_event.payload, dict) else {}
    raw_text = str(payload.get("text") or payload.get("transcript") or payload.get("prompt") or "").strip()
    modality = "voice" if input_event.modality == "speech" else "text"

    profile: Optional[Dict[str, Any]] = None
    if clients is not None and person_id:
        try:
            snap = ContextReader.from_env().read(clients=clients, person_id=person_id, trace=trace)
            profile = snap.profile if isinstance(snap.profile, dict) else None
        except Exception:
            profile = None

    validator = Phase1SchemaValidator.load()
    onboarding = maybe_plan_onboarding(raw_input=raw_text, modality=modality, profile=profile)
    onboarding_stage: Optional[str] = None
    if onboarding is not None:
        intent_obj, plan_obj = onboarding.intent, onboarding.plan
        if isinstance(profile, dict) and isinstance(profile.get("onboarding"), dict):
            onboarding_stage = str(profile["onboarding"].get("stage") or "name")
        else:
            onboarding_stage = "name"
    else:
        planner = Phase1Planner(validator=validator)
        intent_obj, plan_obj = planner.plan(
            raw_input=raw_text,
            modality=modality,
            profile=profile,
            person_id=person_id,
            session_id=sid,
            trace_id=trace.trace_id,
            phase1_trace=phase1_trace,
            clients=clients,
        )

    if phase1_trace and isinstance(profile, dict) and isinstance(profile.get("preferred_name"), str) and profile["preferred_name"].strip():
        phase1_trace.emit(
            trace_id=trace.trace_id,
            source="context_store",
            type="memory.op.requested",
            level="debug",
            payload={"note": "preferred_name_present"},
        )

    if phase1_trace:
        phase1_trace.emit(
            trace_id=trace.trace_id,
            source="planner_model",
            type="intent.created",
            level="info",
            payload={"intent_id": intent_obj["intent_id"], "modality": intent_obj["modality"], "category": intent_obj["category"]},
        )
        phase1_trace.emit(
            trace_id=trace.trace_id,
            source="planner_model",
            type="planner.plan.created",
            level="info",
            payload={"plan_id": plan_obj["plan_id"], "intent_id": plan_obj["intent_id"], "tool_calls": len(plan_obj.get("tool_calls") or [])},
        )

    tool_runtime = Phase1ToolRuntime()
    memory_runtime = Phase1MemoryRuntime()
    tool_results: list[Dict[str, Any]] = []
    memory_results: list[Dict[str, Any]] = []

    for tc in plan_obj.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        if phase1_trace:
            phase1_trace.emit(
                trace_id=trace.trace_id,
                source="planner_model",
                type="planner.tool_call.requested",
                level="info",
                payload={"tool_call_id": tc.get("tool_call_id"), "tool_name": tc.get("tool_name")},
            )
            phase1_trace.emit(
                trace_id=trace.trace_id,
                source="tool_runtime",
                type="tool_call.started",
                level="info",
                payload={"tool_call_id": tc.get("tool_call_id"), "tool_name": tc.get("tool_name")},
            )
        result = tool_runtime.execute(call=tc, clients=clients, person_id=person_id, trace_id=trace.trace_id, session_id=sid)
        tool_results.append(
            {"tool_call_id": result.tool_call_id, "tool_name": tc.get("tool_name"), "status": "ok" if result.ok else "error", "result": result.result, "error": result.error}
        )
        if phase1_trace:
            phase1_trace.emit(
                trace_id=trace.trace_id,
                source="tool_runtime",
                type="tool_call.finished",
                level="info" if result.ok else "warn",
                payload={"tool_call_id": result.tool_call_id, "ok": result.ok, "error": result.error},
            )

    for mo in plan_obj.get("memory_ops") or []:
        if not isinstance(mo, dict):
            continue
        if phase1_trace:
            phase1_trace.emit(
                trace_id=trace.trace_id,
                source="context_store",
                type="memory.op.requested",
                level="info",
                payload={"op_id": mo.get("op_id"), "op": mo.get("op"), "target": mo.get("target")},
            )
        res = memory_runtime.execute(op=mo, clients=clients, person_id=person_id)
        memory_results.append(
            {"op_id": res.op_id, "target": mo.get("target"), "status": "ok" if res.ok else "error", "result": res.result, "error": res.error}
        )
        if phase1_trace:
            phase1_trace.emit(
                trace_id=trace.trace_id,
                source="context_store",
                type="memory.op.finished",
                level="info" if res.ok else "warn",
                payload={"op_id": res.op_id, "ok": res.ok, "error": res.error},
            )

    # Refresh profile after memory ops so preferences materially affect directives on subsequent turns.
    if clients is not None and person_id:
        try:
            snap2 = ContextReader.from_env().read(clients=clients, person_id=person_id, trace=trace)
            if isinstance(snap2.profile, dict):
                profile = snap2.profile
        except Exception:
            pass

    # Interaction response (LLM except for deterministic onboarding).
    response_ok = True
    response_provider = "deterministic"
    response_model = "deterministic"
    response_text: str
    if onboarding is not None:
        clarification = intent_obj.get("clarification_questions") if isinstance(intent_obj.get("clarification_questions"), list) else []
        if clarification:
            response_text = str(clarification[0])
        elif onboarding_stage == "name":
            response_text = "Thanks. Verbosity preference: minimal, normal, or detailed?"
        elif onboarding_stage == "verbosity":
            response_text = "Visual density: sparse, balanced, or dense?"
        elif onboarding_stage == "density":
            response_text = "Got it. Say 'skip' to finish setup."
        else:
            response_text = "Setup complete."
    else:
        inj = compile_injected_system_prompt(person_id=person_id, session_id=sid, intent="phase1.interaction.respond")
        system_prompt = inj.system_prompt

        if phase1_trace:
            phase1_trace.emit(
                trace_id=trace.trace_id,
                source="interaction_model",
                type="prompt.injection.applied",
                level="info",
                payload={"target": "interaction_model", "config_path": inj.config_path, "config_hash": inj.config_hash},
                redactions=["prompt_content"],
            )
            phase1_trace.emit(
                trace_id=trace.trace_id,
                source="interaction_model",
                type="interaction.response.requested",
                level="info",
                payload={"plan_id": plan_obj["plan_id"], "intent_id": plan_obj["intent_id"]},
            )

        interaction = Phase1InteractionModel()
        response = interaction.generate(
            clients=clients,
            event_id=trace.trace_id,
            trace_id=trace.trace_id,
            person_id=person_id,
            session_id=sid,
            system_prompt=system_prompt,
            user_text=raw_text,
            plan=plan_obj,
            tool_results=tool_results,
            memory_results=memory_results,
        )
        response_ok = response.ok
        response_provider = response.provider or "unknown"
        response_model = response.model or "unknown"
        response_text = response.text or ("I can do that. Do you want me to proceed?" if plan_obj.get("requires_confirmation") else "Done.")

    if phase1_trace:
        phase1_trace.emit(
            trace_id=trace.trace_id,
            source="interaction_model",
            type="interaction.response.generated",
            level="info" if response_ok else "warn",
            payload={"ok": response_ok, "provider": response_provider, "model": response_model, "text_len": len(response_text)},
        )

    rom = ResponseObjectModel(
        trace_id=trace.trace_id,
        session_id=sid,
        person_id=person_id,
        blocks=[RomText(text=response_text)],
        meta={
            "origin": "phase1",
            "intent": intent_obj,
            "plan": plan_obj,
            "tool_results": tool_results,
            "memory_results": memory_results,
            "renderer_directives": plan_obj.get("renderer_directives") or {},
        },
    )

    renderer_ok = False
    renderer_status: Optional[int] = None
    if cfg.renderer_url:
        emitter = RendererEmitter(cfg.renderer_url)
        renderer_ok, renderer_status = emitter.emit(
            trace_id=trace.trace_id,
            session_id=sid,
            person_id=person_id,
            type="rom.render",
            payload=rom.model_dump(mode="json"),
        )
        if phase1_trace:
            phase1_trace.emit(
                trace_id=trace.trace_id,
                source="renderer",
                type="renderer.frame.rendered",
                level="info" if renderer_ok else "warn",
                payload={"ok": renderer_ok, "status": renderer_status},
            )

    out_dir = cfg.trace_dir
    os.makedirs(out_dir, exist_ok=True)
    trace_path = str(trace.write_json(f"{out_dir}/{trace.trace_id}.json"))

    policy = PolicyDecision(allowed=not bool(plan_obj.get("requires_confirmation")), reason="phase1")
    tool_error = None
    for t in tool_results:
        if not isinstance(t, dict):
            continue
        if t.get("status") != "ok":
            tool_error = t.get("error") or "tool_error"
            # Treat `not_available` as a valid Phase 1 outcome for optional tools.
            if tool_error == "not_available":
                tool_error = None
            break
    tool_result = ActionResult(
        action_id="phase1",
        ok=True,
        error=tool_error,
        result={"tools": tool_results, "memory": memory_results, "requires_confirmation": bool(plan_obj.get("requires_confirmation"))},
    )

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


__all__ = ["Phase1RunConfig", "run_phase1_input_event"]
