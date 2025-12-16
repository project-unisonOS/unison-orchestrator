from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple


_GREETING_RE = re.compile(r"^(hi|hello|hey|yo|sup|howdy)\b", re.IGNORECASE)


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _norm(text: str) -> str:
    return re.sub(r"\\s+", " ", text.strip()).lower()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class OnboardingPlan:
    intent: Dict[str, Any]
    plan: Dict[str, Any]


def maybe_plan_onboarding(
    *,
    raw_input: str,
    modality: str,
    profile: Optional[Dict[str, Any]],
) -> Optional[OnboardingPlan]:
    """
    Deterministic onboarding state machine (Phase 1 contract) to avoid relying on the LLM
    for profile/prefs persistence, which is critical for product feel and smoketests.
    """
    prof = profile if isinstance(profile, dict) else {}
    onboarding = prof.get("onboarding") if isinstance(prof.get("onboarding"), dict) else {}
    completed = bool(onboarding.get("completed"))
    if completed:
        return None

    stage = str(onboarding.get("stage") or "name")
    text = raw_input.strip()
    if not text:
        return None

    normalized = _norm(text)
    intent_id = _new_id("intent")
    plan_id = _new_id("plan")

    directives_modality = "voice" if modality == "voice" else "renderer"
    renderer_directives = {
        "verbosity": "normal",
        "visual_density": "balanced",
        "presence": "neutral",
        "modality": directives_modality,
    }

    memory_ops = []
    steps = []

    def add_profile_patch(patch: Dict[str, Any], summary: str) -> None:
        op_id = _new_id("mem")
        memory_ops.append(
            {
                "op_id": op_id,
                "op": "upsert",
                "target": "profile",
                "payload": patch,
                "expected_effect": "write",
            }
        )
        steps.append({"step_id": op_id[-8:], "type": "memory", "summary": summary, "depends_on": []})

    def add_prefs_patch(patch: Dict[str, Any], summary: str) -> None:
        op_id = _new_id("mem")
        memory_ops.append(
            {
                "op_id": op_id,
                "op": "upsert",
                "target": "preferences",
                "payload": patch,
                "expected_effect": "write",
            }
        )
        steps.append({"step_id": op_id[-8:], "type": "memory", "summary": summary, "depends_on": []})

    # Global "skip" to finish onboarding.
    if normalized in {"skip", "done", "finish", "complete"}:
        add_profile_patch({"onboarding": {"completed": True, "stage": "done", "awaiting": None}}, "Complete onboarding")
    elif stage == "name":
        if _GREETING_RE.match(text) or normalized in {"ok", "okay", "yes", "yep", "no", "nah"}:
            return OnboardingPlan(
                intent={
                    "intent_id": intent_id,
                    "timestamp": _iso_now(),
                    "modality": modality,
                    "raw_input": raw_input,
                    "normalized_text": normalized,
                    "category": "clarification",
                    "confidence": 0.8,
                    "requires_clarification": True,
                    "clarification_questions": ["What name should I use for you?"],
                },
                plan={
                    "plan_id": plan_id,
                    "intent_id": intent_id,
                    "planner_model": "deterministic.onboarding",
                    "policy_summary": "Ask for preferred name to personalize responses.",
                    "steps": [{"step_id": "askname", "type": "clarify", "summary": "Ask for preferred name", "depends_on": []}],
                    "tool_calls": [],
                    "memory_ops": [],
                    "renderer_directives": renderer_directives,
                },
            )

        preferred_name = text.strip()
        add_profile_patch(
            {
                "preferred_name": preferred_name,
                "onboarding": {"completed": False, "stage": "verbosity", "awaiting": "verbosity"},
            },
            "Set preferred name",
        )
    elif stage == "verbosity":
        if normalized in {"minimal", "min"}:
            verbosity = "minimal"
        elif normalized in {"detailed", "detail", "verbose"}:
            verbosity = "detailed"
        elif normalized in {"normal", "balanced", "default"}:
            verbosity = "normal"
        else:
            return OnboardingPlan(
                intent={
                    "intent_id": intent_id,
                    "timestamp": _iso_now(),
                    "modality": modality,
                    "raw_input": raw_input,
                    "normalized_text": normalized,
                    "category": "clarification",
                    "confidence": 0.8,
                    "requires_clarification": True,
                    "clarification_questions": ["Verbosity preference: minimal, normal, or detailed?"],
                },
                plan={
                    "plan_id": plan_id,
                    "intent_id": intent_id,
                    "planner_model": "deterministic.onboarding",
                    "policy_summary": "Clarify verbosity preference.",
                    "steps": [{"step_id": "askverb", "type": "clarify", "summary": "Ask verbosity preference", "depends_on": []}],
                    "tool_calls": [],
                    "memory_ops": [],
                    "renderer_directives": renderer_directives,
                },
            )

        add_prefs_patch({"verbosity": verbosity}, "Set verbosity preference")
        add_profile_patch({"onboarding": {"completed": False, "stage": "density", "awaiting": "visual_density"}}, "Advance onboarding stage")
    elif stage == "density":
        if normalized in {"sparse", "low", "light"}:
            density = "sparse"
        elif normalized in {"balanced", "normal", "default"}:
            density = "balanced"
        elif normalized in {"dense", "high"}:
            density = "dense"
        else:
            return OnboardingPlan(
                intent={
                    "intent_id": intent_id,
                    "timestamp": _iso_now(),
                    "modality": modality,
                    "raw_input": raw_input,
                    "normalized_text": normalized,
                    "category": "clarification",
                    "confidence": 0.8,
                    "requires_clarification": True,
                    "clarification_questions": ["Visual density: sparse, balanced, or dense?"],
                },
                plan={
                    "plan_id": plan_id,
                    "intent_id": intent_id,
                    "planner_model": "deterministic.onboarding",
                    "policy_summary": "Clarify visual density preference.",
                    "steps": [{"step_id": "askdens", "type": "clarify", "summary": "Ask visual density preference", "depends_on": []}],
                    "tool_calls": [],
                    "memory_ops": [],
                    "renderer_directives": renderer_directives,
                },
            )

        add_prefs_patch({"visual_density": density}, "Set visual density preference")
        add_profile_patch({"onboarding": {"completed": False, "stage": "confirm", "awaiting": "confirm"}}, "Advance onboarding stage")
    else:
        # confirm / unknown stage: accept "skip"/"done" above, otherwise mark done once we have essentials.
        has_name = isinstance(prof.get("preferred_name"), str) and prof.get("preferred_name", "").strip()
        prefs = prof.get("preferences") if isinstance(prof.get("preferences"), dict) else {}
        has_prefs = bool(prefs.get("verbosity")) and bool(prefs.get("visual_density"))
        if has_name and has_prefs:
            add_profile_patch({"onboarding": {"completed": True, "stage": "done", "awaiting": None}}, "Complete onboarding")
        else:
            # Fall back to planner if onboarding state is inconsistent.
            return None

    if not steps:
        return None

    steps.append({"step_id": "respond", "type": "respond", "summary": "Respond to user", "depends_on": []})

    intent = {
        "intent_id": intent_id,
        "timestamp": _iso_now(),
        "modality": modality,
        "raw_input": raw_input,
        "normalized_text": normalized,
        "category": "memory",
        "confidence": 0.95,
        "entities": [],
        "requires_clarification": False,
        "clarification_questions": [],
    }
    plan = {
        "plan_id": plan_id,
        "intent_id": intent_id,
        "planner_model": "deterministic.onboarding",
        "policy_summary": "Apply onboarding memory updates.",
        "steps": steps,
        "tool_calls": [],
        "memory_ops": memory_ops,
        "renderer_directives": renderer_directives,
        "requires_confirmation": False,
    }
    return OnboardingPlan(intent=intent, plan=plan)


__all__ = ["maybe_plan_onboarding", "OnboardingPlan"]
