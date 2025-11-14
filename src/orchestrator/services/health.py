from __future__ import annotations

from typing import Dict, Optional, Tuple

from ..clients import ServiceClients

HealthResult = Tuple[bool, int, Optional[dict]]


def fetch_core_health(
    clients: ServiceClients, *, headers: Optional[Dict[str, str]] = None
) -> Dict[str, HealthResult]:
    """Ping core downstream services and return their health responses."""
    return {
        "context": clients.context.get("/health", headers=headers),
        "storage": clients.storage.get("/health", headers=headers),
        "inference": clients.inference.get("/health", headers=headers),
        "policy": clients.policy.get("/health", headers=headers),
    }


def fetch_policy_rules(
    clients: ServiceClients, *, headers: Optional[Dict[str, str]] = None
) -> HealthResult:
    """Fetch policy rule summary for introspection."""
    return clients.policy.get("/rules/summary", headers=headers)
