from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from orchestrator.clients import ServiceClients
from orchestrator.policy_client import evaluate_capability
from unison_common import ActionEnvelope, PolicyDecision, TraceRecorder


@dataclass(frozen=True)
class PolicyGate:
    """
    Policy gate for proposed actions.

    Phase 2: call `unison-policy` when clients are available; otherwise fall back to a stub.
    """

    clients: Optional[ServiceClients] = None

    def check(
        self,
        action: ActionEnvelope,
        *,
        trace: Optional[TraceRecorder] = None,
        event_id: Optional[str] = None,
        actor: Optional[str] = None,
        person_id: Optional[str] = None,
        auth_scope: Optional[str] = None,
        safety_context: Optional[Dict[str, Any]] = None,
    ) -> PolicyDecision:
        if self.clients is not None:
            payload: Dict[str, Any] = {
                "capability_id": action.name,
                "context": {
                    "actor": actor or "unknown",
                    "person_id": person_id,
                    "auth_scope": auth_scope,
                    "safety_context": safety_context or {},
                    "policy_context": action.policy_context or {},
                    "action_envelope": action.model_dump(mode="json"),
                },
            }
            ok, status, body = evaluate_capability(self.clients, payload, event_id=event_id)
            decision = {}
            allowed = False
            require_confirmation = False
            reason = "policy_unavailable"
            if ok and status < 400 and isinstance(body, dict):
                decision = body.get("decision") or {}
                allowed = decision.get("allowed", False) is True
                require_confirmation = decision.get("require_confirmation", False) is True
                reason = str(decision.get("reason") or "policy")
            else:
                fail_open = os.getenv("UNISON_POLICY_FAIL_OPEN", "false").lower() in {"1", "true", "yes", "on"}
                allowed = fail_open
                reason = "policy_unavailable_fail_open" if fail_open else "policy_unavailable_fail_closed"
            if trace:
                trace.emit_event(
                    "policy_decision",
                    {
                        "allowed": allowed,
                        "require_confirmation": require_confirmation,
                        "reason": reason,
                        "policy_http_ok": ok,
                        "policy_status": status,
                    },
                )
            return PolicyDecision(
                allowed=allowed,
                require_confirmation=require_confirmation,
                reason=reason,
                required_scopes=list((action.policy_context or {}).get("scopes") or []),
            )

        # Stub path (Phase 1 compatibility)
        text = str((action.args or {}).get("text", ""))
        if "deny:" in text.lower():
            return PolicyDecision(allowed=False, reason="stub deny rule matched", require_confirmation=False)
        return PolicyDecision(allowed=True)
