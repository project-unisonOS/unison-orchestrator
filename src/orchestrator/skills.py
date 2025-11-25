from __future__ import annotations

import uuid
from typing import Any, Callable, Dict

from .clients import ServiceClients
from .companion import CompanionSessionManager, ToolRegistry
from .context_client import dashboard_get, dashboard_put
import os
import httpx
import time

SkillHandler = Callable[[Dict[str, Any]], Dict[str, Any]]


def build_skill_state(
    service_clients: ServiceClients,
) -> Dict[str, Dict[str, SkillHandler]]:
    """Return default skills and handler registry backed by downstream clients."""
    tool_registry = ToolRegistry()
    companion_manager = CompanionSessionManager(service_clients, tool_registry)

    def handler_echo(envelope: Dict[str, Any]) -> Dict[str, Any]:
        return {"echo": envelope.get("payload", {})}

    def handler_inference(envelope: Dict[str, Any]) -> Dict[str, Any]:
        event_id = envelope.get("event_id", str(uuid.uuid4()))
        intent = envelope.get("intent", "")
        payload = envelope.get("payload", {})

        prompt = payload.get("prompt", "")
        provider = payload.get("provider")
        model = payload.get("model")
        max_tokens = payload.get("max_tokens", 1000)
        temperature = payload.get("temperature", 0.7)

        if not prompt:
            return {"error": "Missing prompt for inference", "event_id": event_id}

        inference_payload = {
            "intent": intent,
            "prompt": prompt,
            "provider": provider,
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        ok, status, body = service_clients.inference.post(
            "/inference/request",
            inference_payload,
            headers={"X-Event-ID": event_id},
        )

        if ok and body:
            return {
                "inference_result": body.get("result", ""),
                "provider": body.get("provider"),
                "model": body.get("model"),
                "event_id": event_id,
            }
        return {"error": f"Inference service unavailable ({status})", "event_id": event_id}

    def handler_summarize_doc(_: Dict[str, Any]) -> Dict[str, Any]:
        return {"summary": "This is a placeholder summary for summarize.doc."}

    def handler_context_get(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {})
        keys = payload.get("keys")
        if not isinstance(keys, list):
            raise ValueError("context.get requires 'keys' list in payload")
        ok, status, body = service_clients.context.post("/kv/get", {"keys": keys})
        if not ok or not isinstance(body, dict):
            raise RuntimeError(f"Context service error: {status}")
        return body

    def handler_storage_put(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {})
        key = payload.get("key")
        value = payload.get("value")
        if not isinstance(key, str) or not key:
            raise ValueError("storage.put requires 'key' string in payload")
        if value is None:
            raise ValueError("storage.put requires 'value' in payload")
        ok, status, body = service_clients.storage.put(f"/kv/{key}", {"value": value})
        if not ok or not isinstance(body, dict):
            raise RuntimeError(f"Storage service error: {status}")
        return body

    def handler_companion_turn(envelope: Dict[str, Any]) -> Dict[str, Any]:
        return companion_manager.process_turn(envelope)

    # Register a few orchestrator-native tools for the companion loop.
    tool_registry.register_skill_tool(
        name="context.get",
        description="Fetch one or more context keys for the active person",
        parameters={
            "type": "object",
            "properties": {"keys": {"type": "array", "items": {"type": "string"}}},
            "required": ["keys"],
        },
    )
    tool_registry.register_skill_tool(
        name="storage.put",
        description="Store a value by key in the storage service",
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    )

    def handler_person_enroll(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {})
        person_id = payload.get("person_id")
        profile = payload.get("profile")
        if not isinstance(person_id, str) or not person_id:
            return {"ok": False, "error": "invalid-person-id"}
        if not isinstance(profile, dict):
            return {"ok": False, "error": "invalid-profile"}
        ok, status, body = service_clients.context.post(f"/profile/{person_id}", {"profile": profile})
        if ok:
            return {"ok": True, "person_id": person_id}
        return {"ok": False, "error": f"context error {status}", "body": body}

    def handler_person_verify(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {})
        person_id = payload.get("person_id")
        verification_token = payload.get("verification_token")
        if not isinstance(person_id, str) or not person_id:
            return {"ok": False, "error": "invalid-person-id"}
        ok, status, body = service_clients.context.get(f"/profile/{person_id}")
        if not ok or not isinstance(body, dict):
            return {"ok": False, "error": f"context error {status}"}
        profile = body.get("profile") or {}
        pin = None
        try:
            pin = profile.get("auth", {}).get("pin")
        except Exception:
            pin = None
        if pin and verification_token == pin:
            return {"ok": True, "person_id": person_id, "verified": True}
        return {"ok": False, "verified": False, "error": "verification_failed"}

    def handler_person_update_prefs(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {})
        person_id = payload.get("person_id")
        updates = payload.get("profile_updates")
        if not isinstance(person_id, str) or not person_id:
            return {"ok": False, "error": "invalid-person-id"}
        if not isinstance(updates, dict):
            return {"ok": False, "error": "invalid-updates"}
        ok, status, body = service_clients.context.get(f"/profile/{person_id}")
        if not ok or not isinstance(body, dict):
            return {"ok": False, "error": f"context error {status}"}
        profile = body.get("profile") or {}
        merged = {**profile, **updates}
        ok2, status2, _ = service_clients.context.post(f"/profile/{person_id}", {"profile": merged})
        if ok2:
            return {"ok": True, "person_id": person_id}
        return {"ok": False, "error": f"context error {status2}"}

    def handler_dashboard_refresh(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {}) or {}
        person_id = payload.get("person_id")
        if not person_id:
            return {"ok": False, "error": "missing person_id"}
        # Fetch existing profile to respect preferences (optional)
        profile_ok, _, profile_body = service_clients.context.get(f"/profile/{person_id}")
        prefs = {}
        if profile_ok and isinstance(profile_body, dict):
            prefs = (profile_body.get("profile") or {}).get("dashboard", {}).get("preferences", {})
        # Build priority cards (stub + existing dashboard resume)
        existing_dashboard = dashboard_get(service_clients, person_id)
        existing_cards = existing_dashboard.get("cards") or []
        cards = payload.get("cards")
        if not cards:
            # Stub priority cards; replace with real data fetches later
            cards = [
                {
                    "id": "dashboard-1",
                    "type": "summary",
                    "title": "Your morning briefing",
                    "body": "Schedule, comms, and tasks summarized.",
                    "tool_activity": "calendar.refresh",
                },
                {
                    "id": "dashboard-2",
                    "type": "comms",
                    "title": "Messages to respond to",
                    "body": "2 priority replies pending.",
                    "tool_activity": "comms.triage",
                },
            ]
        merged_cards = cards + existing_cards
        dashboard_state = {"cards": merged_cards[:10], "preferences": prefs, "person_id": person_id, "updated_at": time.time()}
        # Persist to context
        dashboard_put(service_clients, person_id, dashboard_state)
        # Emit to renderer experiences if configured
        renderer_url = os.getenv("UNISON_RENDERER_URL")
        if renderer_url:
            try:
                with httpx.Client(timeout=2.0) as client:
                    for card in cards:
                        exp = dict(card)
                        exp.setdefault("person_id", person_id)
                        exp.setdefault("ts", time.time())
                        client.post(f"{renderer_url}/experiences", json=exp)
            except Exception:
                pass
        return {"ok": True, "person_id": person_id, "cards": dashboard_state["cards"]}

    handlers: Dict[str, SkillHandler] = {
        "echo": handler_echo,
        "inference": handler_inference,
        "summarize_doc": handler_summarize_doc,
        "context_get": handler_context_get,
        "storage_put": handler_storage_put,
        "companion_turn": handler_companion_turn,
        "person_enroll": handler_person_enroll,
        "person_verify": handler_person_verify,
        "person_update_prefs": handler_person_update_prefs,
        "dashboard_refresh": handler_dashboard_refresh,
    }

    skills: Dict[str, SkillHandler] = {
        "echo": handler_echo,
        "summarize.doc": handler_summarize_doc,
        "analyze.code": handler_inference,
        "translate.text": handler_inference,
        "generate.idea": handler_inference,
        "context.get": handler_context_get,
        "storage.put": handler_storage_put,
        "companion.turn": handler_companion_turn,
        "person.enroll": handler_person_enroll,
        "person.verify": handler_person_verify,
        "person.update_prefs": handler_person_update_prefs,
        "dashboard.refresh": handler_dashboard_refresh,
    }

    return {"skills": skills, "handlers": handlers}
