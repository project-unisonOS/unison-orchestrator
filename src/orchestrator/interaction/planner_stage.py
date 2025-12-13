from __future__ import annotations

import uuid

from dataclasses import dataclass

from unison_common import ActionEnvelope, Intent, Plan, PlannerOutput, TraceRecorder


@dataclass(frozen=True)
class PlannerStage:
    """
    Planner stage that produces a structured plan (Intent + ActionEnvelope[]).

    Phase 1: stub that always emits one deterministic tool action (echo).
    """

    def run(self, *, text: str, trace: TraceRecorder) -> PlannerOutput:
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

