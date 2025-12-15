from __future__ import annotations

import json

from dataclasses import dataclass
from typing import Any, Dict, Optional

from orchestrator.clients import ServiceClients


@dataclass(frozen=True)
class ToolExecutionResult:
    tool_call_id: str
    ok: bool
    result: Dict[str, Any]
    error: Optional[str] = None


@dataclass(frozen=True)
class Phase1ToolRuntime:
    """
    Executes Phase 1 ToolCallV1 objects.

    Policy gating is enforced by requiring `authorization.policy_decision == "allow"` to execute.
    """

    def execute(
        self,
        *,
        call: Dict[str, Any],
        clients: ServiceClients | None,
        person_id: Optional[str],
        trace_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> ToolExecutionResult:
        tool_call_id = str(call.get("tool_call_id") or "")
        tool_name = str(call.get("tool_name") or "")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        auth = call.get("authorization") if isinstance(call.get("authorization"), dict) else {}
        decision = auth.get("policy_decision")

        if decision != "allow":
            reason = auth.get("reason") if isinstance(auth.get("reason"), str) else None
            return ToolExecutionResult(
                tool_call_id=tool_call_id,
                ok=False,
                error="not_authorized",
                result={"tool_name": tool_name, "policy_decision": decision, "reason": reason},
            )

        if tool_name == "noop":
            return ToolExecutionResult(tool_call_id=tool_call_id, ok=True, result={"ok": True})

        if tool_name == "vdi.use_computer":
            if clients is None or clients.actuation is None:
                return ToolExecutionResult(
                    tool_call_id=tool_call_id,
                    ok=False,
                    error="not_available",
                    result={"tool_name": tool_name, "detail": "actuation service not configured"},
                )
            if not person_id:
                return ToolExecutionResult(
                    tool_call_id=tool_call_id,
                    ok=False,
                    error="missing_person_id",
                    result={"tool_name": tool_name},
                )
            action = args.get("action")
            url = args.get("url")
            if action != "open_url" or not isinstance(url, str) or not url.strip():
                return ToolExecutionResult(
                    tool_call_id=tool_call_id,
                    ok=False,
                    error="invalid_args",
                    result={"tool_name": tool_name, "expected": {"action": "open_url", "url": "https://..."}, "got": args},
                )
            payload: Dict[str, Any] = {
                "action_id": tool_call_id,
                "trace_id": trace_id,
                "person_id": person_id,
                "url": url.strip(),
                "session_id": session_id,
                "risk_level": "low",
                "actions": [],
            }
            ok, status, body = clients.actuation.post("/vdi/tasks/browse", payload)
            if not ok or status >= 400:
                body_obj = body if isinstance(body, dict) else {"body": body}
                detail = body_obj.get("detail") if isinstance(body_obj.get("detail"), (dict, str)) else None
                detail_text = json.dumps(detail) if isinstance(detail, dict) else (detail or "")
                # Phase 1 requirement: optional VDI must degrade gracefully.
                # Treat any non-successful actuation call as "not available" so the planner/tool interface remains stable.
                is_unavailable = (
                    (not ok)
                    or status in {404, 500, 501, 502, 503, 504}
                    or "vdi_unavailable" in str(body_obj).lower()
                    or "vdi_unavailable" in str(detail_text).lower()
                )
                return ToolExecutionResult(
                    tool_call_id=tool_call_id,
                    ok=False,
                    error="not_available" if is_unavailable else "vdi_failed",
                    result={"tool_name": tool_name, "status": status, "body": body_obj, "detail": detail},
                )
            return ToolExecutionResult(tool_call_id=tool_call_id, ok=True, result={"tool_name": tool_name, "status": status, "body": body or {}})

        if tool_name in {"system.open_url", "system.search", "context.query", "context.upsert"}:
            return ToolExecutionResult(
                tool_call_id=tool_call_id,
                ok=False,
                error="not_available",
                result={"tool_name": tool_name, "detail": "tool not implemented"},
            )

        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            ok=False,
            error="unknown_tool",
            result={"tool_name": tool_name},
        )


__all__ = ["Phase1ToolRuntime", "ToolExecutionResult"]
