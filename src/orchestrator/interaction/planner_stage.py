from __future__ import annotations

import uuid

from dataclasses import dataclass

from unison_common import ActionEnvelope, ContextSnapshot, Intent, Plan, PlannerOutput, TraceRecorder


@dataclass(frozen=True)
class PlannerStage:
    """
    Planner stage that produces a structured plan (Intent + ActionEnvelope[]).

    Phase 1: stub that always emits one deterministic tool action (echo).
    """

    def run(self, *, text: str, trace: TraceRecorder, context: ContextSnapshot | None = None) -> PlannerOutput:
        if context is not None:
            trace.emit_event(
                "planner_context",
                {
                    "has_profile": context.profile is not None,
                    "has_dashboard": context.dashboard is not None,
                },
            )
        normalized = (text or "").strip()
        if normalized.lower().startswith("browse ") and "://" in normalized:
            url = normalized.split(" ", 1)[1].strip()
            action = ActionEnvelope(
                action_id=str(uuid.uuid4()),
                kind="vdi",
                name="vdi.browse",
                args={"url": url},
                risk_level="medium",
                policy_context={"scopes": ["vdi.browse"]},
            )
            plan = Plan(intent=Intent(name="vdi.browse", goal="Browse a URL in a bounded VDI session"), actions=[action])
            trace.emit_event("planner_output", {"actions": 1, "intent": "vdi.browse"})
            return PlannerOutput(plan=plan, rationale="stub planner: browse URL")
        action = ActionEnvelope(
            action_id=str(uuid.uuid4()),
            kind="tool",
            name="tool.echo",
            args={"text": text},
            risk_level="low",
            policy_context={"scopes": ["tools.echo"]},
        )
        plan = Plan(intent=Intent(name="echo", goal="Echo the provided input"), actions=[action])
        trace.emit_event("planner_output", {"actions": 1, "intent": "echo"})
        return PlannerOutput(plan=plan, rationale="stub planner: always echo")
