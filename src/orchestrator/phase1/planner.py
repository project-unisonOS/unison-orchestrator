from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import os

from .schema import Phase1SchemaValidator
from unison_common import Phase1NdjsonTrace
from unison_common.prompt import compile_injected_system_prompt


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)


_PLAN_STEP_TYPES = {"respond", "clarify", "tool", "memory"}


def _coerce_enum(value: Any, *, allowed: set[str]) -> Any:
    if not isinstance(value, str):
        return value
    if value in allowed:
        return value
    if "|" in value:
        for part in (p.strip() for p in value.split("|")):
            if part in allowed:
                return part
    return value


def _normalize_plan_v1(out: Dict[str, Any]) -> Dict[str, Any]:
    """
    Best-effort normalization for common planner-model enum mistakes before schema validation.
    """
    steps = out.get("steps") if isinstance(out.get("steps"), list) else []
    for step in steps:
        if not isinstance(step, dict):
            continue
        step["type"] = _coerce_enum(step.get("type"), allowed=_PLAN_STEP_TYPES)
    return out


def _ensure_vdi_tool_call_for_actuation(*, out: Dict[str, Any], intent: Dict[str, Any], raw_input: str) -> None:
    """
    If the planner model misses an obvious actuation request, inject a deterministic vdi tool call.
    """
    if intent.get("category") != "actuation":
        return
    url = _extract_url(raw_input or "")
    if not url:
        return

    tool_calls = out.get("tool_calls") if isinstance(out.get("tool_calls"), list) else []
    if any(isinstance(tc, dict) and tc.get("tool_name") == "vdi.use_computer" for tc in tool_calls):
        return

    decision, reason = _policy_decision_for_url(url)
    tool_calls.append(
        {
            "tool_call_id": uuid.uuid4().hex,
            "tool_name": "vdi.use_computer",
            "args": {"action": "open_url", "url": url},
            "authorization": {"policy_decision": decision, "reason": reason},
            "timeout_ms": 60000,
        }
    )
    out["tool_calls"] = tool_calls

    steps = out.get("steps") if isinstance(out.get("steps"), list) else []
    if not any(isinstance(s, dict) and s.get("type") == "tool" for s in steps):
        steps.append({"step_id": "tool_1", "type": "tool", "summary": f"Open URL: {url}", "depends_on": []})
        out["steps"] = steps


def _extract_url(text: str) -> Optional[str]:
    match = _URL_RE.search(text or "")
    if not match:
        return None
    return match.group(1).rstrip(").,]}>\"'")


def _domain_allowlist() -> set[str]:
    raw = (os.getenv("UNISON_PHASE1_VDI_ALLOWLIST_DOMAINS") or "example.com").strip()
    if not raw:
        return set()
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return set(parts)


def _policy_decision_for_url(url: str) -> tuple[str, str]:
    """
    Returns (policy_decision, reason) for vdi.use_computer open_url requests.

    Phase 1 default: confirm unless the host is explicitly allowlisted.
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except Exception:
        host = ""
    allowlist = _domain_allowlist()
    if host and host in allowlist:
        return "allow", f"host allowlisted: {host}"
    return "confirm", "URL navigation requires confirmation"


def _default_renderer_directives(
    *,
    modality: str,
    profile: Optional[Dict[str, Any]],
    verbosity_override: Optional[str] = None,
    visual_density_override: Optional[str] = None,
) -> Dict[str, Any]:
    verbosity = "normal"
    visual_density = "balanced"
    presence = "calm"
    pacing_wpm = 160
    allow_motion = True

    if isinstance(profile, dict):
        prefs = profile.get("preferences")
        if isinstance(prefs, dict):
            v = prefs.get("verbosity")
            if v in {"minimal", "normal", "detailed"}:
                verbosity = v
            d = prefs.get("visual_density")
            if d in {"sparse", "balanced", "dense"}:
                visual_density = d
            p = prefs.get("presence")
            if p in {"calm", "neutral", "energetic"}:
                presence = p
            wpm = prefs.get("pacing_wpm")
            if isinstance(wpm, int) and 80 <= wpm <= 240:
                pacing_wpm = wpm
            am = prefs.get("allow_motion")
            if isinstance(am, bool):
                allow_motion = am

    if verbosity_override in {"minimal", "normal", "detailed"}:
        verbosity = verbosity_override
    if visual_density_override in {"sparse", "balanced", "dense"}:
        visual_density = visual_density_override

    return {
        "verbosity": verbosity,
        "visual_density": visual_density,
        "presence": presence,
        "modality": "voice" if modality == "voice" else "renderer",
        "pacing_wpm": pacing_wpm,
        "allow_motion": allow_motion,
        "accessibility_hints": {},
    }


def _parse_verbosity(text: str) -> Optional[str]:
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in {"minimal", "min", "brief", "short"}:
        return "minimal"
    if t in {"normal", "default", "standard"}:
        return "normal"
    if t in {"detailed", "detail", "verbose", "long"}:
        return "detailed"
    return None


def _parse_visual_density(text: str) -> Optional[str]:
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in {"sparse", "minimal", "low"}:
        return "sparse"
    if t in {"balanced", "normal", "default", "medium"}:
        return "balanced"
    if t in {"dense", "high", "compact"}:
        return "dense"
    return None


def _parse_skip(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"skip", "no", "none", "nah", "nope", "nothing"}


def _onboarding_state(profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(profile, dict):
        return {"completed": False, "stage": "name", "awaiting": None}
    onboarding = profile.get("onboarding") if isinstance(profile.get("onboarding"), dict) else {}
    completed = onboarding.get("completed") is True
    stage = onboarding.get("stage") if onboarding.get("stage") in {"name", "verbosity", "visual_density", "goals", "done"} else "name"
    awaiting = onboarding.get("awaiting") if onboarding.get("awaiting") in {"name", "verbosity", "visual_density", "goals"} else None
    return {"completed": completed, "stage": stage, "awaiting": awaiting}


def _memory_op(*, op: str, target: str, payload: Dict[str, Any], expected_effect: str) -> Dict[str, Any]:
    return {
        "op_id": uuid.uuid4().hex,
        "op": op,
        "target": target,
        "payload": payload,
        "expected_effect": expected_effect,
    }


@dataclass(frozen=True)
class Phase1Planner:
    """
    Phase 1 Planner (initial stub).

    Produces schema-valid `IntentV1` + `PlanV1` objects and nothing else.
    """

    validator: Phase1SchemaValidator
    planner_model: str = "stub"

    def _planner_mode(self) -> str:
        if (os.getenv("UNISON_PHASE1_DISABLE_INFERENCE") or "").lower() in {"1", "true", "yes", "on"}:
            return "stub"
        raw = (os.getenv("UNISON_PLANNER_MODE") or "llm").strip().lower()
        return raw if raw in {"llm", "stub"} else "llm"

    def _planner_fallback(self) -> str:
        raw = (os.getenv("UNISON_PLANNER_FALLBACK") or "error").strip().lower()
        return raw if raw in {"error", "stub"} else "error"

    def _planner_provider(self) -> str:
        return (os.getenv("UNISON_PLANNER_PROVIDER") or os.getenv("UNISON_INFERENCE_PROVIDER") or "ollama").strip()

    def _planner_model(self) -> str:
        # Phase 1.1 default: Qwen planner variant (configurable).
        return (os.getenv("UNISON_PLANNER_MODEL") or "qwen2.5").strip()

    def _planner_endpoint(self) -> str:
        return (os.getenv("UNISON_PLANNER_ENDPOINT") or "").strip()

    def _extract_json_object(self, text: str) -> Dict[str, Any]:
        raw = (text or "").strip()
        if not raw:
            raise ValueError("empty planner output")
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        # Best-effort: strip leading/trailing prose.
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            obj = json.loads(raw[start : end + 1])
            if isinstance(obj, dict):
                return obj
        raise ValueError("planner output is not a JSON object")

    def _plan_with_llm(
        self,
        *,
        raw_input: str,
        modality: str,
        profile: Optional[Dict[str, Any]],
        intent: Dict[str, Any],
        person_id: Optional[str],
        session_id: str,
        trace_id: str,
        phase1_trace: Optional[Phase1NdjsonTrace],
        clients: Any,
    ) -> Dict[str, Any]:
        if clients is None:
            raise RuntimeError("planner requires inference clients")

        inj = compile_injected_system_prompt(person_id=person_id, session_id=session_id, intent="phase1.planner.plan")
        if phase1_trace:
            phase1_trace.emit(
                trace_id=trace_id,
                source="planner_model",
                type="prompt.injection.applied",
                level="info",
                payload={"target": "planner_model", "config_path": inj.config_path, "config_hash": inj.config_hash},
                redactions=["prompt_content"],
            )

        directives = _default_renderer_directives(modality="voice" if modality == "voice" else "text", profile=profile)
        plan_shape_hint = {
            "plan_id": "string (min length 8, unique)",
            "intent_id": intent["intent_id"],
            "steps": [{"step_id": "step_1", "type": "respond", "summary": "string", "depends_on": []}],
            "tool_calls": [],
            "memory_ops": [],
            "renderer_directives": directives,
            "requires_confirmation": False,
            "confirmation_prompt": "string (only when requires_confirmation=true)",
        }

        system = (
            inj.system_prompt
            + "\n\n"
            + "You are the UnisonOS planner model.\n"
            + "Return ONLY a single JSON object (no markdown, no prose) that conforms to PlanV1.\n"
            + "Do not include user-facing language; planning output is internal.\n"
            + "Use ONLY tool_name values from the ToolCallV1 schema.\n"
            + "Use renderer_directives EXACTLY as provided.\n"
            + "For steps[].type, use exactly one of: respond, clarify, tool, memory.\n"
        )
        user = (
            "Create a PlanV1 for the following intent.\n\n"
            f"intent: {json.dumps(intent, ensure_ascii=False)}\n"
            f"profile: {json.dumps(profile or {}, ensure_ascii=False)}\n"
            f"required_renderer_directives: {json.dumps(directives, ensure_ascii=False)}\n"
            f"PlanV1 shape hint: {json.dumps(plan_shape_hint, ensure_ascii=False)}\n"
        )

        provider = self._planner_provider()
        model = self._planner_model()
        max_tokens = int(os.getenv("UNISON_PLANNER_MAX_TOKENS", "900"))
        temperature = float(os.getenv("UNISON_PLANNER_TEMPERATURE", "0.2"))

        ok, status, body = clients.inference.post(
            "/inference/request",
            {
                "intent": "phase1.planner.plan",
                "person_id": person_id or "anonymous",
                "session_id": session_id,
                "provider": provider,
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "tools": [],
                "tool_choice": "none",
                "response_format": "json_object",
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            headers={"X-Event-ID": trace_id, "X-Trace-ID": trace_id},
        )
        if not ok or not isinstance(body, dict) or status >= 400:
            raise RuntimeError(f"planner inference failed (status={status})")

        raw_result = body.get("result") if isinstance(body.get("result"), str) else ""

        def _parse_validate(raw: str) -> Dict[str, Any]:
            out = self._extract_json_object(raw)
            out["planner_model"] = f"{provider}:{model}"
            if out.get("intent_id") != intent["intent_id"]:
                raise ValueError("planner output intent_id mismatch")
            if out.get("renderer_directives") != directives:
                raise ValueError("planner output renderer_directives mismatch")

            # Policy hardening: vdi open_url authorization is enforced deterministically.
            tool_calls = out.get("tool_calls") if isinstance(out.get("tool_calls"), list) else []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                if tc.get("tool_name") != "vdi.use_computer":
                    continue
                args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                url = args.get("url")
                if isinstance(url, str) and url:
                    decision, reason = _policy_decision_for_url(url)
                    tc["authorization"] = {"policy_decision": decision, "reason": reason}

            out = _normalize_plan_v1(out)
            _ensure_vdi_tool_call_for_actuation(out=out, intent=intent, raw_input=raw_input)
            self.validator.validate("plan.v1.schema.json", out)
            return out

        try:
            return _parse_validate(raw_result)
        except ValueError as exc:
            # One retry: ask the model to correct schema issues deterministically.
            retry_system = (
                system
                + "\n\n"
                + "Your previous output failed schema validation. You MUST correct it.\n"
                + "Return ONLY a single PlanV1 JSON object.\n"
            )
            retry_user = (
                user
                + "\n\n"
                + f"Schema validation error: {str(exc)}\n"
                + "Previous output:\n"
                + raw_result
            )
            ok2, status2, body2 = clients.inference.post(
                "/inference/request",
                {
                    "intent": "phase1.planner.plan.retry",
                    "person_id": person_id or "anonymous",
                    "session_id": session_id,
                    "provider": provider,
                    "model": model,
                    "messages": [
                        {"role": "system", "content": retry_system},
                        {"role": "user", "content": retry_user},
                    ],
                    "tools": [],
                    "tool_choice": "none",
                    "response_format": "json_object",
                    "max_tokens": max_tokens,
                    "temperature": 0.0,
                },
                headers={"X-Event-ID": trace_id, "X-Trace-ID": trace_id},
            )
            if not ok2 or not isinstance(body2, dict) or status2 >= 400:
                raise RuntimeError(f"planner inference failed (status={status2})") from exc
            raw2 = body2.get("result") if isinstance(body2.get("result"), str) else ""
            return _parse_validate(raw2)

    def create_intent(self, *, raw_input: str, modality: str) -> Dict[str, Any]:
        normalized = (raw_input or "").strip()
        category = "qa"
        if normalized.lower().startswith(("browse ", "open ")):
            category = "actuation"
        if normalized.lower().startswith("remember") or "remember that" in normalized.lower():
            category = "memory"

        intent: Dict[str, Any] = {
            "intent_id": uuid.uuid4().hex,
            "timestamp": _iso_utc_now(),
            "modality": "voice" if modality == "voice" else "text",
            "raw_input": raw_input or "",
            "normalized_text": normalized,
            "language": "en",
            "category": category,
            "confidence": 0.72,
            "entities": [],
            "requires_clarification": False,
            "clarification_questions": [],
        }
        self.validator.validate("intent.v1.schema.json", intent)
        return intent

    def create_plan(
        self,
        *,
        intent: Dict[str, Any],
        profile: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        text = str(intent.get("normalized_text") or "")
        modality = "voice" if intent.get("modality") == "voice" else "text"

        tool_calls: list[Dict[str, Any]] = []
        memory_ops: list[Dict[str, Any]] = []
        steps: list[Dict[str, Any]] = []

        onboarding = _onboarding_state(profile)
        if onboarding.get("completed") is not True:
            stage = onboarding.get("stage") or "name"
            awaiting = onboarding.get("awaiting")

            def ask(next_stage: str) -> None:
                nonlocal stage, awaiting
                stage = next_stage
                awaiting = next_stage
                steps.append({"step_id": "step_1", "type": "clarify", "summary": f"Collect onboarding.{next_stage}", "depends_on": []})
                memory_ops.append(
                    _memory_op(
                        op="upsert",
                        target="profile",
                        payload={"onboarding": {"completed": False, "stage": next_stage, "awaiting": next_stage}},
                        expected_effect="write",
                    )
                )

            if awaiting in {"name", "verbosity", "visual_density", "goals"}:
                # Interpret this turn as the answer to the awaited stage.
                if awaiting == "name":
                    preferred_name = (text or "").strip()
                    if preferred_name:
                        memory_ops.append(
                            _memory_op(
                                op="upsert",
                                target="profile",
                                payload={"preferred_name": preferred_name, "onboarding": {"stage": "verbosity", "awaiting": "verbosity"}},
                                expected_effect="write",
                            )
                        )
                        steps.append({"step_id": "step_1", "type": "memory", "summary": "Persist preferred name", "depends_on": []})
                        steps.append({"step_id": "step_2", "type": "clarify", "summary": "Collect onboarding.verbosity", "depends_on": ["step_1"]})
                    else:
                        ask("name")
                elif awaiting == "verbosity":
                    verbosity_choice = _parse_verbosity(text)
                    if verbosity_choice:
                        memory_ops.append(
                            _memory_op(
                                op="upsert",
                                target="preferences",
                                payload={"verbosity": verbosity_choice},
                                expected_effect="write",
                            )
                        )
                        memory_ops.append(
                            _memory_op(
                                op="upsert",
                                target="profile",
                                payload={"onboarding": {"stage": "visual_density", "awaiting": "visual_density"}},
                                expected_effect="write",
                            )
                        )
                        steps.append({"step_id": "step_1", "type": "memory", "summary": "Persist verbosity preference", "depends_on": []})
                        steps.append({"step_id": "step_2", "type": "clarify", "summary": "Collect onboarding.visual_density", "depends_on": ["step_1"]})
                    else:
                        ask("verbosity")
                elif awaiting == "visual_density":
                    density_choice = _parse_visual_density(text)
                    if density_choice:
                        memory_ops.append(
                            _memory_op(
                                op="upsert",
                                target="preferences",
                                payload={"visual_density": density_choice},
                                expected_effect="write",
                            )
                        )
                        memory_ops.append(
                            _memory_op(
                                op="upsert",
                                target="profile",
                                payload={"onboarding": {"stage": "goals", "awaiting": "goals"}},
                                expected_effect="write",
                            )
                        )
                        steps.append({"step_id": "step_1", "type": "memory", "summary": "Persist visual density preference", "depends_on": []})
                        steps.append({"step_id": "step_2", "type": "clarify", "summary": "Collect onboarding.goals", "depends_on": ["step_1"]})
                    else:
                        ask("visual_density")
                else:
                    # goals
                    if _parse_skip(text):
                        memory_ops.append(
                            _memory_op(
                                op="upsert",
                                target="profile",
                                payload={"goals": "", "onboarding": {"completed": True, "stage": "done", "awaiting": None}},
                                expected_effect="write",
                            )
                        )
                        steps.append({"step_id": "step_1", "type": "memory", "summary": "Mark onboarding complete", "depends_on": []})
                        steps.append({"step_id": "step_2", "type": "respond", "summary": "Confirm onboarding completion", "depends_on": ["step_1"]})
                    else:
                        memory_ops.append(
                            _memory_op(
                                op="upsert",
                                target="profile",
                                payload={"goals": text, "onboarding": {"completed": True, "stage": "done", "awaiting": None}},
                                expected_effect="write",
                            )
                        )
                        steps.append({"step_id": "step_1", "type": "memory", "summary": "Persist goals and mark onboarding complete", "depends_on": []})
                        steps.append({"step_id": "step_2", "type": "respond", "summary": "Confirm onboarding completion", "depends_on": ["step_1"]})
            else:
                # Ask the current stage question.
                ask(stage if stage in {"name", "verbosity", "visual_density", "goals"} else "name")

            directives = _default_renderer_directives(modality=modality, profile=profile)
            plan: Dict[str, Any] = {
                "plan_id": uuid.uuid4().hex,
                "intent_id": intent["intent_id"],
                "planner_model": self.planner_model,
                "policy_summary": "onboarding",
                "steps": steps,
                "tool_calls": tool_calls,
                "memory_ops": memory_ops,
                "renderer_directives": directives,
            }
            self.validator.validate("plan.v1.schema.json", plan)
            return plan

        url = _extract_url(text)
        if url:
            decision, reason = _policy_decision_for_url(url)
            tool_calls.append(
                {
                    "tool_call_id": uuid.uuid4().hex,
                    "tool_name": "vdi.use_computer",
                    "args": {"action": "open_url", "url": url},
                    "authorization": {"policy_decision": decision, "reason": reason},
                    "timeout_ms": 60000,
                }
            )
            steps.append({"step_id": "step_1", "type": "tool", "summary": "Open the requested URL in a bounded computer session", "depends_on": []})
            steps.append({"step_id": "step_2", "type": "respond", "summary": "Confirm the result and offer next steps", "depends_on": ["step_1"]})
        else:
            steps.append({"step_id": "step_1", "type": "respond", "summary": "Answer the user", "depends_on": []})

        directives = _default_renderer_directives(modality=modality, profile=profile)

        plan: Dict[str, Any] = {
            "plan_id": uuid.uuid4().hex,
            "intent_id": intent["intent_id"],
            "planner_model": self.planner_model,
            "policy_summary": "stub",
            "steps": steps,
            "tool_calls": tool_calls,
            "memory_ops": memory_ops,
            "renderer_directives": directives,
            "requires_confirmation": any(
                tc.get("authorization", {}).get("policy_decision") == "confirm" for tc in tool_calls if isinstance(tc, dict)
            ),
            "confirmation_prompt": "I can do that. Do you want me to proceed?" if tool_calls else None,
        }
        # The schema does not require confirmation_prompt, but if present it must be a string.
        if not tool_calls:
            plan.pop("confirmation_prompt", None)

        self.validator.validate("plan.v1.schema.json", plan)
        return plan

    def plan(
        self,
        *,
        raw_input: str,
        modality: str,
        profile: Optional[Dict[str, Any]] = None,
        person_id: Optional[str] = None,
        session_id: str = "phase1",
        trace_id: str = "trace",
        phase1_trace: Optional[Phase1NdjsonTrace] = None,
        clients: Any = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        intent = self.create_intent(raw_input=raw_input, modality=modality)
        onboarding = _onboarding_state(profile)
        if onboarding.get("completed") is not True:
            plan = self.create_plan(intent=intent, profile=profile)
            return intent, plan

        mode = self._planner_mode()
        if mode == "stub":
            plan = self.create_plan(intent=intent, profile=profile)
            return intent, plan

        try:
            plan = self._plan_with_llm(
                raw_input=raw_input,
                modality=modality,
                profile=profile,
                intent=intent,
                person_id=person_id,
                session_id=session_id,
                trace_id=trace_id,
                phase1_trace=phase1_trace,
                clients=clients,
            )
        except Exception:
            if self._planner_fallback() == "stub":
                plan = self.create_plan(intent=intent, profile=profile)
            else:
                raise
        return intent, plan


__all__ = ["Phase1Planner"]
