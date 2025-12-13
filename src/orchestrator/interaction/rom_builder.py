from __future__ import annotations

from dataclasses import dataclass

from unison_common import ActionResult, PolicyDecision, ResponseObjectModel, RomText


@dataclass(frozen=True)
class RomBuilder:
    """Build a minimal ROM from tool results (Phase 1)."""

    def build(
        self,
        *,
        trace_id: str,
        session_id: str,
        person_id: str,
        tool_result: ActionResult,
        policy: PolicyDecision | None = None,
    ) -> ResponseObjectModel:
        if tool_result.ok:
            text = str((tool_result.result or {}).get("text", ""))
        else:
            text = f"Tool failed: {tool_result.error or 'unknown error'}"
            if policy and not policy.allowed and policy.reason:
                text = f"Policy denied: {policy.reason}"
        return ResponseObjectModel(
            trace_id=trace_id,
            session_id=session_id,
            person_id=person_id,
            blocks=[RomText(text=text)],
            meta={"origin": "thin_vertical_slice", "policy": (policy.model_dump(mode="json") if policy else None)},
        )
