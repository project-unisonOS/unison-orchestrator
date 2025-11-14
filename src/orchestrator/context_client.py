from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .clients import ServiceClients

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


def kv_get(clients: ServiceClients, keys: List[str]) -> Tuple[bool, int, Any]:
    """Proxy helper for Context service KV GET."""
    return clients.context.post("/kv/get", {"keys": keys})
