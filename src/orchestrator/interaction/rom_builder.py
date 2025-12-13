from __future__ import annotations

from dataclasses import dataclass

from unison_common import ActionResult, ResponseObjectModel, RomText


@dataclass(frozen=True)
class RomBuilder:
    """Build a minimal ROM from tool results (Phase 1)."""

    def build(self, *, trace_id: str, session_id: str, person_id: str, tool_result: ActionResult) -> ResponseObjectModel:
        if tool_result.ok:
            text = str((tool_result.result or {}).get("text", ""))
        else:
            text = f"Tool failed: {tool_result.error or 'unknown error'}"
        return ResponseObjectModel(
            trace_id=trace_id,
            session_id=session_id,
            person_id=person_id,
            blocks=[RomText(text=text)],
            meta={"origin": "thin_vertical_slice"},
        )

