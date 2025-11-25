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


def ensure_conversation_endpoints(clients: ServiceClients) -> bool:
    """Lightweight probe to see if conversation endpoints exist; returns True if callable."""
    ok, status, _ = clients.context.get("/conversation/health")
    return bool(ok and status == 200)


def store_conversation_turn(
    clients: ServiceClients,
    person_id: str,
    session_id: str,
    messages: Any,
    response: Any,
    summary: str,
) -> Tuple[bool, int, Any]:
    payload = {
        "person_id": person_id,
        "session_id": session_id,
        "messages": messages,
        "response": response,
        "summary": summary,
    }
    return clients.context.post(f"/conversation/{person_id}/{session_id}", payload)


def load_conversation_messages(
    clients: ServiceClients, person_id: str, session_id: str
) -> List[Dict[str, Any]]:
    ok, _, body = clients.context.get(f"/conversation/{person_id}/{session_id}")
    if not ok or not isinstance(body, dict):
        return []
    messages = body.get("messages")
    return messages if isinstance(messages, list) else []


def dashboard_get(clients: ServiceClients, person_id: str) -> Dict[str, Any]:
    ok, _, body = clients.context.get(f"/dashboard/{person_id}")
    if not ok or not isinstance(body, dict):
        return {}
    return body.get("dashboard") or {}


def dashboard_put(clients: ServiceClients, person_id: str, dashboard: Dict[str, Any]) -> None:
    clients.context.post(f"/dashboard/{person_id}", {"dashboard": dashboard})
