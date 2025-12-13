from __future__ import annotations

from dataclasses import dataclass

from unison_common import InputEventEnvelope, RouterOutput, TraceRecorder


@dataclass(frozen=True)
class RouterStage:
    """
    Fast router/classifier stage.

    Phase 1: rule-based stub for intent classification.
    """

    def run(self, input_event: InputEventEnvelope, trace: TraceRecorder) -> RouterOutput:
        text = str((input_event.payload or {}).get("text", "")).strip()
        classified = "echo"
        if text.endswith("?"):
            classified = "question"
        trace.emit_event("router_classified", {"classified_intent": classified})
        return RouterOutput(classified_intent=classified, planner_hint={"latency_budget_ms": 250})

