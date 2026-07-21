from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .clients import ServiceClients

HealthResult = Tuple[bool, int, Optional[dict]]


class ContextChoiceRequired(RuntimeError):
    """Raised before inference when no explicit governed context was selected."""


@dataclass(frozen=True)
class GovernedContextSnapshot:
    records: tuple[dict[str, Any], ...]
    privacy: dict[str, Any]


def governed_prompt_context(
    clients: ServiceClients,
    *,
    person_id: str,
    space_ids: List[str],
    query: str,
    purpose: str,
    headers: Optional[Dict[str, str]] = None,
) -> GovernedContextSnapshot:
    """Fetch only explicitly authorized spaces for model prompt construction."""
    if not space_ids:
        raise ContextChoiceRequired("an explicit context space is required before inference")
    ok, status, body = clients.context.post(
        "/v2/memory/prompt-context",
        {
            "person_id": person_id,
            "space_ids": list(space_ids),
            "query": query,
            "purpose": purpose,
        },
        headers=headers,
    )
    if not ok or status != 200 or not isinstance(body, dict):
        if status == 409:
            raise ContextChoiceRequired("context service requires an explicit boundary choice")
        raise RuntimeError("governed context unavailable")
    records = body.get("records")
    privacy = body.get("privacy")
    if not isinstance(records, list) or not isinstance(privacy, dict):
        raise RuntimeError("governed context response malformed")
    returned = tuple(str(item) for item in privacy.get("active_space_ids") or ())
    if returned != tuple(space_ids):
        raise RuntimeError("governed context response changed the requested boundary")
    return GovernedContextSnapshot(records=tuple(item for item in records if isinstance(item, dict)), privacy=privacy)


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
