from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from orchestrator.clients import ServiceClients
from unison_common import ActionEnvelope, ActionResult, TraceRecorder


@dataclass(frozen=True)
class VdiExecutor:
    """
    Executes bounded VDI tasks via the actuation service VDI proxy.

    Phase 5: calls `unison-actuation` endpoints:
    - POST /vdi/tasks/browse
    - POST /vdi/tasks/form-submit
    - POST /vdi/tasks/download
    """

    def execute(self, *, action: ActionEnvelope, clients: ServiceClients, trace: Optional[TraceRecorder] = None) -> ActionResult:
        if not clients.actuation:
            return ActionResult(action_id=action.action_id, ok=False, error="actuation client not configured")

        if action.name not in {"vdi.browse", "vdi.form_submit", "vdi.download"}:
            return ActionResult(action_id=action.action_id, ok=False, error=f"unknown vdi action: {action.name}")

        person_id = (action.args or {}).get("person_id")
        url = (action.args or {}).get("url")
        session_id = (action.args or {}).get("session_id")
        wait_for = (action.args or {}).get("wait_for")

        if not isinstance(person_id, str) or not person_id.strip():
            return ActionResult(action_id=action.action_id, ok=False, error="missing person_id for vdi task")
        if not isinstance(url, str) or not url.strip():
            return ActionResult(action_id=action.action_id, ok=False, error="missing url for vdi task")

        payload: Dict[str, Any] = {
            "action_id": action.action_id,
            "person_id": person_id,
            "url": url,
            "session_id": session_id,
            "wait_for": wait_for,
            "headers": (action.args or {}).get("headers"),
            "risk_level": action.risk_level,
        }
        if trace:
            payload["trace_id"] = trace.trace_id
        # Optional structured args
        if action.name == "vdi.browse":
            payload["actions"] = (action.args or {}).get("actions") or []
        elif action.name == "vdi.form_submit":
            payload["form"] = (action.args or {}).get("form") or []
            payload["submit_selector"] = (action.args or {}).get("submit_selector")
        elif action.name == "vdi.download":
            payload["target_path"] = (action.args or {}).get("target_path")
            payload["filename"] = (action.args or {}).get("filename")

        path = {
            "vdi.browse": "/vdi/tasks/browse",
            "vdi.form_submit": "/vdi/tasks/form-submit",
            "vdi.download": "/vdi/tasks/download",
        }[action.name]

        if trace:
            trace.emit_event("vdi_task_started", {"action": action.name, "url": url})
        ok, status, body = clients.actuation.post(path, payload)
        if not ok or status >= 400:
            err = f"actuation vdi call failed status={status}"
            if trace:
                trace.emit_event("vdi_task_failed", {"action": action.name, "status": status})
            return ActionResult(action_id=action.action_id, ok=False, error=err, result={"status": status, "body": body})

        if trace:
            trace.emit_event("vdi_task_completed", {"action": action.name})
        return ActionResult(action_id=action.action_id, ok=True, result={"status": status, "body": body or {}})
