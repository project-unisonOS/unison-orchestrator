from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import os

from .clients import ServiceClients

PolicyResponse = Tuple[bool, int, Optional[Dict[str, Any]]]


def evaluate_capability(
    clients: ServiceClients,
    payload: Dict[str, Any],
    *,
    event_id: Optional[str] = None,
) -> PolicyResponse:
    """Evaluate a capability request via the policy service."""
    # Test-friendly stub: allow monkeypatching src.server.http_post_json to avoid network calls
    if os.getenv("DISABLE_AUTH_FOR_TESTS", "false").lower() == "true":
        try:
            import src.server as srv  # type: ignore
            if hasattr(srv, "http_post_json"):
                return srv.http_post_json(
                    clients.policy.host,
                    clients.policy.port,
                    "/evaluate",
                    payload,
                    headers={"X-Event-ID": event_id} if event_id else None,
                )
        except Exception:
            # Fall back to normal path on any issues
            pass
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


def fetch_policy_rules(
    clients: ServiceClients, *, headers: Optional[Dict[str, str]] = None
) -> PolicyResponse:
    """Fetch policy rule summary for introspection."""
    return clients.policy.get("/rules/summary", headers=headers)
