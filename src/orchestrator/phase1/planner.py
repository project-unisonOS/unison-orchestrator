from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import os

from .schema import Phase1SchemaValidator


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)


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
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        intent = self.create_intent(raw_input=raw_input, modality=modality)
        plan = self.create_plan(intent=intent, profile=profile)
        return intent, plan


__all__ = ["Phase1Planner"]
