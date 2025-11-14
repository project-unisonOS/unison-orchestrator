from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..clients import ServiceClients


PolicyResponse = Tuple[bool, int, Optional[Dict[str, Any]]]


def evaluate_capability(
    clients: ServiceClients,
    payload: Dict[str, Any],
    *,
    event_id: Optional[str] = None,
) -> PolicyResponse:
    """Evaluate a capability request via the policy service."""
    headers = {"X-Event-ID": event_id} if event_id else None
    return clients.policy.post("/evaluate", payload, headers=headers)


def readiness_allowed(clients: ServiceClients, *, event_id: str) -> bool:
    """Verify a synthetic readiness capability via policy."""
    payload = {
        "capability_id": "test.ACTION",
        "context": {"actor": "local-user", "intent": "readiness-check"},
    }
    ok, _, body = evaluate_capability(clients, payload, event_id=event_id)
    if not ok or not isinstance(body, dict):
        return False

    decision = body.get("decision", {})
    return bool(decision.get("allowed"))
