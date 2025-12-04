from __future__ import annotations

import uuid
from typing import Any, Callable, Dict

from .clients import ServiceClients, ServiceHttpClient
from .companion import CompanionSessionManager, ToolRegistry, recall_workflow_from_dashboard, apply_workflow_design
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
    context_graph_url = os.getenv("UNISON_CONTEXT_GRAPH_URL") or os.getenv("UNISON_CONTEXT_GRAPH_BASE_URL")

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
    tool_registry.register_skill_tool(
        name="workflow.recall",
        description="Recall recent workflow-related views and cards for the current person",
        parameters={
            "type": "object",
            "properties": {
                "person_id": {"type": "string"},
                "query": {"type": "string"},
                "time_hint_days": {"type": "integer", "minimum": 1},
                "tags_hint": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["person_id"],
        },
    )
    tool_registry.register_skill_tool(
        name="workflow.design",
        description="Design or review a named workflow for the current person",
        parameters={
            "type": "object",
            "properties": {
                "person_id": {"type": "string"},
                "workflow_id": {"type": "string"},
                "project_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["design", "review"]},
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string"},
                            "title": {"type": "string"},
                            "position": {"type": "integer"},
                            "id": {"type": "string"},
                        },
                        "required": ["op"],
                    },
                },
            },
            "required": ["person_id", "workflow_id"],
        },
    )

    tool_registry.register_skill_tool(
        name="comms.check",
        description="Check for new/unread communications and produce priority cards",
        parameters={
            "type": "object",
            "properties": {
                "person_id": {"type": "string"},
                "channel": {"type": "string"},
            },
            "required": ["person_id"],
        },
    )
    tool_registry.register_skill_tool(
        name="comms.summarize",
        description="Summarize communications over a window or topic",
        parameters={
            "type": "object",
            "properties": {
                "person_id": {"type": "string"},
                "window": {"type": "string"},
            },
            "required": ["person_id"],
        },
    )
    tool_registry.register_skill_tool(
        name="comms.reply",
        description="Reply to an existing thread/message",
        parameters={
            "type": "object",
            "properties": {
                "person_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "message_id": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["person_id", "thread_id", "message_id", "body"],
        },
    )
    tool_registry.register_skill_tool(
        name="comms.compose",
        description="Compose and send a new communication",
        parameters={
            "type": "object",
            "properties": {
                "person_id": {"type": "string"},
                "channel": {"type": "string"},
                "recipients": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["person_id", "recipients", "subject", "body"],
        },
    )
    tool_registry.register_skill_tool(
        name="comms.join_meeting",
        description="Join a meeting by meeting id/link",
        parameters={
            "type": "object",
            "properties": {
                "person_id": {"type": "string"},
                "meeting_id": {"type": "string"},
                "join_url": {"type": "string"},
            },
            "required": ["person_id", "meeting_id"],
        },
    )
    tool_registry.register_skill_tool(
        name="comms.prepare_meeting",
        description="Prepare a meeting (agenda/participants)",
        parameters={
            "type": "object",
            "properties": {
                "person_id": {"type": "string"},
                "meeting_id": {"type": "string"},
            },
            "required": ["person_id", "meeting_id"],
        },
    )
    tool_registry.register_skill_tool(
        name="comms.debrief_meeting",
        description="Debrief after a meeting",
        parameters={
            "type": "object",
            "properties": {
                "person_id": {"type": "string"},
                "meeting_id": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["person_id", "meeting_id"],
        },
    )

    tool_registry.register_skill_tool(
        name="comms.check",
        description="Check for new/unread communications and produce priority cards",
        parameters={
            "type": "object",
            "properties": {
                "person_id": {"type": "string"},
                "channel": {"type": "string"},
            },
            "required": ["person_id"],
        },
    )
    tool_registry.register_skill_tool(
        name="comms.summarize",
        description="Summarize communications over a window or topic",
        parameters={
            "type": "object",
            "properties": {
                "person_id": {"type": "string"},
                "window": {"type": "string"},
            },
            "required": ["person_id"],
        },
    )
    tool_registry.register_skill_tool(
        name="comms.reply",
        description="Reply to an existing thread/message",
        parameters={
            "type": "object",
            "properties": {
                "person_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "message_id": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["person_id", "thread_id", "message_id", "body"],
        },
    )
    tool_registry.register_skill_tool(
        name="comms.compose",
        description="Compose and send a new communication",
        parameters={
            "type": "object",
            "properties": {
                "person_id": {"type": "string"},
                "channel": {"type": "string"},
                "recipients": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["person_id", "recipients", "subject", "body"],
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

    def handler_wakeword_update(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {}) or {}
        person_id = payload.get("person_id")
        wakeword = payload.get("wakeword") or payload.get("keyword") or ""
        if not isinstance(person_id, str) or not person_id:
            return {"ok": False, "error": "missing person_id"}
        wakeword = str(wakeword).strip()
        if not wakeword or len(wakeword.split()) > 3 or len(wakeword) > 24:
            return {"ok": False, "error": "invalid wakeword"}
        # Fetch existing profile to merge voice prefs
        profile_ok, _, profile_body = service_clients.context.get(f"/profile/{person_id}")
        profile = {}
        if profile_ok and isinstance(profile_body, dict):
            profile = profile_body.get("profile") or {}
        voice = profile.get("voice") or {}
        voice["wakeword"] = wakeword
        profile["voice"] = voice
        ok, status, _ = service_clients.context.post(f"/profile/{person_id}", {"profile": profile})
        if not ok:
            return {"ok": False, "error": f"context error {status}"}
        return {"ok": True, "person_id": person_id, "wakeword": wakeword}

    def _ensure_comms_client() -> ServiceHttpClient:
        if not service_clients.comms:
            raise RuntimeError("comms client not configured")
        return service_clients.comms

    def _log_comms_context(person_id: str, intent: str, cards: Any, extra: Dict[str, Any] | None = None) -> None:
        """Best-effort log of comms events into context-graph."""
        if not context_graph_url:
            return
        try:
            cards_for_log = cards if isinstance(cards, list) else []
            tag_set = set()
            for card in cards_for_log:
                if not isinstance(card, dict):
                    continue
                # Surface explicit unison channel tags and providers for downstream recall
                provider = card.get("provider")
                if provider:
                    tag_set.add(str(provider))
                for t in card.get("tags") or []:
                    if isinstance(t, str):
                        tag_set.add(t)
            tags = list(tag_set)
            created_at = time.time()
            body = {
                "user_id": person_id,
                "session_id": "",
                "dimensions": [
                    {
                        "name": "comms",
                        "value": {
                            "cards": cards_for_log,
                            "origin_intent": intent,
                            "tags": tags,
                            "created_at": created_at,
                            "extra": extra or {},
                        },
                    }
                ],
            }
            trace_body = {
                "user_id": person_id,
                "trace": [
                    {
                        "event": intent,
                        "metadata": {"tags": tags, "cards": cards_for_log, "created_at": created_at, "extra": extra or {}},
                    }
                ],
            }
            with httpx.Client(timeout=2.0) as client:
                client.post(f"{context_graph_url}/context/update", json=body)
                client.post(f"{context_graph_url}/traces/replay", json=trace_body)
        except Exception:
            # Never break comms flow on context-graph errors.
            pass

    def handler_comms_check(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {}) or {}
        person_id = payload.get("person_id") or "local-user"
        channel = payload.get("channel") or "email"
        if not isinstance(person_id, str) or not person_id:
            return {"ok": False, "error": "missing person_id"}
        comms_client = _ensure_comms_client()
        ok, status, body = comms_client.post("/comms/check", {"person_id": person_id, "channel": channel})
        if not ok or not isinstance(body, dict):
            return {"ok": False, "error": f"comms error {status}"}
        cards = body.get("cards")
        _log_comms_context(person_id, "comms.check", cards, {"channel": channel})
        return {"ok": True, "person_id": person_id, "channel": channel, "messages": body.get("messages"), "cards": cards}

    def handler_comms_summarize(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {}) or {}
        person_id = payload.get("person_id") or "local-user"
        window = payload.get("window") or "today"
        if not isinstance(person_id, str) or not person_id:
            return {"ok": False, "error": "missing person_id"}
        comms_client = _ensure_comms_client()
        ok, status, body = comms_client.post("/comms/summarize", {"person_id": person_id, "window": window})
        if not ok or not isinstance(body, dict):
            return {"ok": False, "error": f"comms error {status}"}
        cards = body.get("cards")
        _log_comms_context(person_id, "comms.summarize", cards, {"window": window})
        return {"ok": True, "person_id": person_id, "summary": body.get("summary"), "cards": cards}

    def handler_comms_reply(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {}) or {}
        person_id = payload.get("person_id") or "local-user"
        thread_id = payload.get("thread_id")
        message_id = payload.get("message_id")
        body_text = payload.get("body") or ""
        if not isinstance(person_id, str) or not person_id:
            return {"ok": False, "error": "missing person_id"}
        if not isinstance(thread_id, str) or not thread_id:
            return {"ok": False, "error": "missing thread_id"}
        if not isinstance(message_id, str) or not message_id:
            return {"ok": False, "error": "missing message_id"}
        comms_client = _ensure_comms_client()
        ok, status, body = comms_client.post(
            "/comms/reply",
            {"person_id": person_id, "thread_id": thread_id, "message_id": message_id, "body": body_text},
        )
        if not ok or not isinstance(body, dict):
            return {"ok": False, "error": f"comms error {status}"}
        _log_comms_context(person_id, "comms.reply", [], {"thread_id": thread_id, "message_id": message_id})
        return {"ok": True, "person_id": person_id, "thread_id": thread_id, "message_id": message_id, "response": body}

    def handler_comms_compose(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {}) or {}
        person_id = payload.get("person_id") or "local-user"
        channel = payload.get("channel") or "email"
        recipients = payload.get("recipients") or []
        subject = payload.get("subject") or ""
        body_text = payload.get("body") or ""
        if not isinstance(person_id, str) or not person_id:
            return {"ok": False, "error": "missing person_id"}
        if not recipients or not isinstance(recipients, list):
            return {"ok": False, "error": "recipients required"}
        if not subject:
            return {"ok": False, "error": "subject required"}
        comms_client = _ensure_comms_client()
        ok, status, body = comms_client.post(
            "/comms/compose",
            {"person_id": person_id, "channel": channel, "recipients": recipients, "subject": subject, "body": body_text},
        )
        if not ok or not isinstance(body, dict):
            return {"ok": False, "error": f"comms error {status}"}
        tags = body.get("tags") if isinstance(body, dict) else None
        _log_comms_context(person_id, "comms.compose", [], {"channel": channel, "tags": tags, "recipients": recipients})
        return {"ok": True, "person_id": person_id, "channel": channel, "response": body}

    def handler_comms_join_meeting(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {}) or {}
        person_id = payload.get("person_id") or "local-user"
        meeting_id = payload.get("meeting_id") or "meeting-1"
        comms_client = _ensure_comms_client()
        ok, status, body = comms_client.post("/comms/join_meeting", {"person_id": person_id, "meeting_id": meeting_id})
        if not ok or not isinstance(body, dict):
            return {"ok": False, "error": f"comms error {status}"}
        cards = body.get("cards")
        _log_comms_context(person_id, "comms.join_meeting", cards, {"meeting_id": meeting_id})
        return {"ok": True, "person_id": person_id, "cards": cards}

    def handler_comms_prepare_meeting(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {}) or {}
        person_id = payload.get("person_id") or "local-user"
        meeting_id = payload.get("meeting_id") or "meeting-1"
        comms_client = _ensure_comms_client()
        ok, status, body = comms_client.post("/comms/prepare_meeting", {"person_id": person_id, "meeting_id": meeting_id})
        if not ok or not isinstance(body, dict):
            return {"ok": False, "error": f"comms error {status}"}
        cards = body.get("cards")
        _log_comms_context(person_id, "comms.prepare_meeting", cards, {"meeting_id": meeting_id})
        return {"ok": True, "person_id": person_id, "cards": cards}

    def handler_comms_debrief_meeting(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {}) or {}
        person_id = payload.get("person_id") or "local-user"
        meeting_id = payload.get("meeting_id") or "meeting-1"
        comms_client = _ensure_comms_client()
        ok, status, body = comms_client.post("/comms/debrief_meeting", {"person_id": person_id, "meeting_id": meeting_id})
        if not ok or not isinstance(body, dict):
            return {"ok": False, "error": f"comms error {status}"}
        cards = body.get("cards")
        _log_comms_context(person_id, "comms.debrief_meeting", cards, {"meeting_id": meeting_id})
        return {"ok": True, "person_id": person_id, "cards": cards}

    def handler_dashboard_refresh(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload", {}) or {}
        person_id = payload.get("person_id")
        if not person_id:
            return {"ok": False, "error": "missing person_id"}
        # Fetch existing profile to respect preferences (optional)
        profile_ok, _, profile_body = service_clients.context.get(f"/profile/{person_id}")
        prefs: Dict[str, Any] = {}
        if profile_ok and isinstance(profile_body, dict):
            prefs = (profile_body.get("profile") or {}).get("dashboard", {}).get("preferences", {})
        # Build priority cards (stub + existing dashboard resume)
        existing_dashboard = dashboard_get(service_clients, person_id)
        existing_cards = existing_dashboard.get("cards") or []
        cards = payload.get("cards")
        # Pull comms cards from comms service (best effort)
        comms_cards: List[Dict[str, Any]] = []
        if service_clients.comms:
            for channel in ("email", "unison"):
                try:
                    ok, status, body = service_clients.comms.post(
                        "/comms/check", {"person_id": person_id, "channel": channel}
                    )
                    if ok and isinstance(body, dict) and isinstance(body.get("cards"), list):
                        comms_cards.extend([c for c in body.get("cards") if isinstance(c, dict)])
                except Exception:
                    continue
        if not cards:
            # Stub priority cards; replace with real data fetches later.
            # Tag these cards so they can participate in recall flows.
            cards = [
                {
                    "id": "dashboard-1",
                    "type": "summary",
                    "title": "Your morning briefing",
                    "body": "Schedule, comms, and tasks summarized.",
                    "tool_activity": "calendar.refresh",
                    "origin_intent": "dashboard.refresh",
                    "tags": ["dashboard", "briefing"],
                },
                {
                    "id": "dashboard-2",
                    "type": "comms",
                    "title": "Messages to respond to",
                    "body": "2 priority replies pending.",
                    "tool_activity": "comms.triage",
                    "origin_intent": "dashboard.refresh",
                    "tags": ["dashboard", "comms"],
                },
            ]
        merged_cards = cards + comms_cards + existing_cards
        dashboard_state = {
            "cards": merged_cards[:10],
            "preferences": prefs,
            "person_id": person_id,
            "updated_at": time.time(),
        }
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
                # Renderer emit failures are non-fatal
                pass
        # Best-effort logging of dashboard refresh into context-graph for later recall.
        context_graph_url = os.getenv("UNISON_CONTEXT_GRAPH_URL") or os.getenv("UNISON_CONTEXT_GRAPH_BASE_URL")
        if context_graph_url:
            try:
                created_at = dashboard_state.get("updated_at", time.time())
                cards_for_log = dashboard_state.get("cards") or []
                if not isinstance(cards_for_log, list):
                    cards_for_log = []
                tag_set = set()
                for card in cards_for_log:
                    if not isinstance(card, dict):
                        continue
                    for t in card.get("tags") or []:
                        if isinstance(t, str):
                            tag_set.add(t)
                tags = list(tag_set)
                body = {
                    "user_id": person_id,
                    "session_id": "",  # dashboard refresh is not tied to a single conversation session
                    "dimensions": [
                        {
                            "name": "dashboard",
                            "value": {
                                "cards": cards_for_log,
                                "origin_intent": "dashboard.refresh",
                                "tags": tags,
                                "created_at": created_at,
                            },
                        }
                    ],
                }
                with httpx.Client(timeout=2.0) as client:
                    client.post(f"{context_graph_url}/context/update", json=body)
                    trace_body = {
                        "user_id": person_id,
                        "trace": [
                            {
                                "event": "dashboard.refresh",
                                "metadata": {
                                    "cards": cards_for_log,
                                    "tags": tags,
                                    "created_at": created_at,
                                },
                            }
                        ],
                    }
                    client.post(f"{context_graph_url}/traces/replay", json=trace_body)
            except Exception:
                # Context-graph emit failures are non-fatal and should not break dashboard refresh.
                pass
        return {"ok": True, "person_id": person_id, "cards": dashboard_state["cards"]}

    def handler_workflow_design(envelope: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create or edit a workflow for a person and surface its current state as cards.

        This first implementation stores workflow documents in the context service's
        key–value store and emits a summary card into the dashboard and renderer.
        """
        payload = envelope.get("payload", {}) or {}
        person_id = payload.get("person_id") or "local-user"
        workflow_id = payload.get("workflow_id")
        project_id = payload.get("project_id")
        mode = payload.get("mode") or "design"
        changes = payload.get("changes") or []
        return apply_workflow_design(
            service_clients,
            person_id,
            workflow_id=workflow_id or "",
            project_id=project_id,
            mode=mode,
            changes=changes,
        )

    def handler_workflow_recall(envelope: Dict[str, Any]) -> Dict[str, Any]:
        """
        Recall recent workflow-related cards for a person and resurface them on the dashboard.

        This implementation focuses on dashboard state in unison-context; future versions may also
        consult context-graph traces for richer recall.
        """
        payload = envelope.get("payload", {}) or {}
        person_id = payload.get("person_id") or "local-user"
        query = payload.get("query") or ""
        time_hint_days = payload.get("time_hint_days") or 30
        tags_hint = payload.get("tags_hint")

        try:
            time_hint_int = int(time_hint_days)
        except Exception:
            time_hint_int = 30

        return recall_workflow_from_dashboard(
            service_clients,
            person_id,
            query=query,
            time_hint_days=time_hint_int,
            tags_hint=tags_hint if isinstance(tags_hint, list) else None,
        )

    def handler_caps_report(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload") or {}
        person_id = payload.get("person_id") or "local-user"
        caps = payload.get("caps") or {}
        if not isinstance(caps, dict):
            return {"ok": False, "error": "invalid-caps"}
        key = f"caps:{person_id}"
        ok, status, _ = service_clients.context.post("/kv/set", {"key": key, "value": caps})
        if not ok:
            return {"ok": False, "error": f"context error {status}", "person_id": person_id}
        return {"ok": True, "person_id": person_id, "key": key, "caps": caps}

    def _load_caps(person_id: str) -> Dict[str, Any]:
        caps = {}
        ok, _, body = service_clients.context.post("/kv/get", {"keys": [f"caps:{person_id}"]})
        if ok and isinstance(body, dict):
            caps = (body.get("values") or {}).get(f"caps:{person_id}") or {}
        if not isinstance(caps, dict):
            caps = {}
        return caps

    def _pick_locale(person_id: str, locale_hint: str | None = None) -> str:
        if locale_hint and isinstance(locale_hint, str) and locale_hint.strip():
            return locale_hint.strip()
        ok, _, body = service_clients.context.get(f"/profile/{person_id}")
        if ok and isinstance(body, dict):
            profile = body.get("profile") or {}
            locale = profile.get("locale") or profile.get("language")
            if isinstance(locale, str) and locale.strip():
                return locale.strip()
        env_locale = os.getenv("UNISON_LOCALE_HINT") or os.getenv("LANG") or ""
        return (env_locale or "en-US").split(".")[0]

    def handler_startup_prompt_plan(envelope: Dict[str, Any]) -> Dict[str, Any]:
        payload = envelope.get("payload") or {}
        person_id = payload.get("person_id") or "local-user"
        caps_payload = payload.get("caps") if isinstance(payload.get("caps"), dict) else None
        locale_hint = payload.get("locale_hint")
        caps = caps_payload or _load_caps(person_id)
        audio_present = bool(isinstance(caps.get("audio_in"), dict) and caps.get("audio_in", {}).get("present", False))
        audio_out = bool(isinstance(caps.get("audio_out"), dict) and caps.get("audio_out", {}).get("present", False))
        display_present = bool(isinstance(caps.get("display"), dict) and caps.get("display", {}).get("present", False))
        camera_present = bool(isinstance(caps.get("camera"), dict) and caps.get("camera", {}).get("present", False))
        mode = "display_voice" if display_present and audio_present and audio_out else "voice_only"
        locale = _pick_locale(person_id, locale_hint)
        inference_ready, _, inference_body = service_clients.inference.get("/ready")
        return {
            "ok": True,
            "person_id": person_id,
            "mode": mode,
            "locale": locale,
            "caps": caps,
            "camera_present": camera_present,
            "display_present": display_present,
            "audio_in_present": audio_present,
            "audio_out_present": audio_out,
            "prompts": {
                "voice": f"Hi, I'm Unison. I can help in {locale}. Which language do you prefer?",
                "display": "Welcome to Unison. Select your language or speak now." if display_present else None,
            },
            "inference_ready": bool(inference_ready and isinstance(inference_body, dict) and inference_body.get("ready", True)),
        }

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
        "workflow_design": handler_workflow_design,
        "workflow_recall": handler_workflow_recall,
        "wakeword_update": handler_wakeword_update,
        "comms_check": handler_comms_check,
        "comms_summarize": handler_comms_summarize,
        "comms_reply": handler_comms_reply,
        "comms_compose": handler_comms_compose,
        "comms_join_meeting": handler_comms_join_meeting,
        "comms_prepare_meeting": handler_comms_prepare_meeting,
        "comms_debrief_meeting": handler_comms_debrief_meeting,
        "caps_report": handler_caps_report,
        "startup_prompt_plan": handler_startup_prompt_plan,
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
        "workflow.design": handler_workflow_design,
        "workflow.recall": handler_workflow_recall,
        "wakeword.update": handler_wakeword_update,
        "comms.check": handler_comms_check,
        "comms.summarize": handler_comms_summarize,
        "comms.reply": handler_comms_reply,
        "comms.compose": handler_comms_compose,
        "comms.join_meeting": handler_comms_join_meeting,
        "comms.prepare_meeting": handler_comms_prepare_meeting,
        "comms.debrief_meeting": handler_comms_debrief_meeting,
        "caps.report": handler_caps_report,
        "startup.prompt.plan": handler_startup_prompt_plan,
    }

    return {"skills": skills, "handlers": handlers, "companion_manager": companion_manager}
