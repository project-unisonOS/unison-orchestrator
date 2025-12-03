from __future__ import annotations

import logging
import os
import uuid
import httpx
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .clients import ServiceClients
from .services import evaluate_capability
from .context_client import (
    load_conversation_messages,
    store_conversation_turn,
    dashboard_get,
    dashboard_put,
)

logger = logging.getLogger(__name__)

# Optional downstream emitters for renderer/speech (best-effort)
_RENDERER_URL = os.getenv("UNISON_RENDERER_URL")
_IO_SPEECH_URL = os.getenv("UNISON_IO_SPEECH_URL")
_CONTEXT_GRAPH_URL = os.getenv("UNISON_CONTEXT_GRAPH_URL")

@dataclass
class ToolDescriptor:
    name: str
    description: str
    parameters: Dict[str, Any]
    source: str = "skill"
    scope: Optional[str] = None
    mcp_server: Optional[str] = None

    def as_llm_tool(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """Registry for MCP tools and native orchestrator skills that can be exposed to the model."""

    def __init__(self, mcp_discovery_url: Optional[str] = None):
        self._tools: Dict[str, ToolDescriptor] = {}
        self._last_published: List[str] = []
        self._mcp_discovery_url = mcp_discovery_url or os.getenv("UNISON_MCP_REGISTRY_URL")

    def register_skill_tool(self, name: str, description: str, parameters: Dict[str, Any]) -> None:
        if name not in self._tools:
            self._tools[name] = ToolDescriptor(name=name, description=description, parameters=parameters, source="skill")

    def register_mcp_tools(self, server_id: str, tools: List[Dict[str, Any]]) -> None:
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            description = tool.get("description", "")
            parameters = tool.get("parameters", {"type": "object", "properties": {}})
            if not name:
                continue
            self._tools[name] = ToolDescriptor(
                name=name,
                description=description,
                parameters=parameters,
                source="mcp",
                mcp_server=server_id,
            )

    def list_llm_tools(self) -> List[Dict[str, Any]]:
        return [tool.as_llm_tool() for tool in self._tools.values()]

    def list_tools(self) -> List[ToolDescriptor]:
        return list(self._tools.values())

    def refresh_from_context_graph(self, clients: ServiceClients) -> None:
        """Pull capability descriptors from context-graph and hydrate registry (best-effort)."""
        ok, status, body = clients.context.get("/capabilities")
        if not ok or not isinstance(body, dict):
            logger.debug("context-graph capabilities fetch failed: status=%s body=%s", status, body)
            return
        items = body.get("capabilities") or body.get("items") or []
        for cap in items:
            if not isinstance(cap, dict):
                continue
            name = cap.get("name") or cap.get("id")
            params = cap.get("parameters") or cap.get("schema") or {"type": "object", "properties": {}}
            desc = cap.get("description", "")
            source = cap.get("source", "mcp")
            server_id = cap.get("server_id") or cap.get("mcp_server")
            if name:
                self._tools[name] = ToolDescriptor(
                    name=name,
                    description=desc,
                    parameters=params,
                    source=source,
                    mcp_server=server_id,
                )

    def refresh_from_mcp(self) -> None:
        """Pull tool list from an MCP registry endpoint (best-effort)."""
        if not self._mcp_discovery_url:
            return
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(self._mcp_discovery_url)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.debug("MCP discovery failed: %s", exc)
            return
        servers = data if isinstance(data, list) else data.get("servers", []) if isinstance(data, dict) else []
        for server in servers:
            server_id = server.get("id") or server.get("name")
            tools = server.get("tools") or []
            self.register_mcp_tools(server_id or "mcp", tools)

    def publish_to_context_graph(self, clients: ServiceClients) -> None:
        """Publish current tool descriptors to context-graph for other services."""
        manifest = []
        for tool in self._tools.values():
            manifest.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "source": tool.source,
                    "mcp_server": tool.mcp_server,
                    "scope": tool.scope or "global",
                }
            )
        tool_names = sorted(t.name for t in self._tools.values())
        if tool_names == self._last_published:
            return
        ok, status, _ = clients.context.post("/capabilities", {"capabilities": manifest})
        if not ok:
            logger.debug("Failed to publish tools to context-graph: status=%s", status)
            return
        self._last_published = tool_names


class CompanionSessionManager:
    """
    Minimal conversational loop scaffold.
    - Builds inference payload with messages/attachments + tools.
    - Executes tool calls through MCP/native skills (placeholder for now).
    - Persists short-term memory in process until context service wiring lands.
    """

    def __init__(self, service_clients: ServiceClients, tool_registry: Optional[ToolRegistry] = None):
        self._clients = service_clients
        self._registry = tool_registry or ToolRegistry()
        self._memory: Dict[str, List[Dict[str, Any]]] = {}
        self._registry.refresh_from_mcp()
        self._registry.refresh_from_context_graph(self._clients)
        self._registry.publish_to_context_graph(self._clients)

    def process_turn(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        event_id = envelope.get("event_id", str(uuid.uuid4()))
        payload = envelope.get("payload", {}) or {}
        person_id = payload.get("person_id") or payload.get("user_id") or "anonymous"
        session_id = payload.get("session_id") or str(uuid.uuid4())

        # Pull latest capabilities from MCP + context-graph (best-effort) and publish current registry.
        self._registry.refresh_from_mcp()
        self._registry.refresh_from_context_graph(self._clients)
        self._registry.publish_to_context_graph(self._clients)

        messages = payload.get("messages") or []
        text = payload.get("text") or payload.get("transcript") or payload.get("prompt")
        attachments = payload.get("attachments") or payload.get("images") or []

        if not messages and text:
            messages = [{"role": "user", "content": text}]
        if not messages:
            return {"error": "Missing messages or text for companion turn", "event_id": event_id}

        prior_turns = self._load_memory(person_id, session_id)

        inference_payload = {
            "intent": "companion.turn",
            "person_id": person_id,
            "session_id": session_id,
            "messages": prior_turns + messages,
            "attachments": attachments,
            "tools": self._registry.list_llm_tools(),
            "tool_choice": payload.get("tool_choice", "auto"),
            "response_format": payload.get("response_format", "text-and-tools"),
        }

        ok, status, body = self._clients.inference.post(
            "/inference/request",
            inference_payload,
            headers={"X-Event-ID": event_id},
        )
        if not ok or not isinstance(body, dict):
            logger.warning("Companion inference failed: status=%s body=%s", status, body)
            return {"error": f"Inference service unavailable ({status})", "event_id": event_id}

        tool_calls = body.get("tool_calls") or []
        tool_activity: List[Dict[str, Any]] = []
        final_body = body
        if tool_calls:
            tool_messages, tool_activity = self._execute_tool_calls(tool_calls, person_id, event_id)
            followup_messages = list(prior_turns + messages)
            if body.get("messages"):
                followup_messages.extend(body["messages"])
            followup_messages.extend(tool_messages)
            ok2, status2, body2 = self._clients.inference.post(
                "/inference/request",
                {
                    "intent": "companion.turn",
                    "person_id": person_id,
                    "session_id": session_id,
                    "messages": followup_messages,
                    "tools": self._registry.list_llm_tools(),
                    "tool_choice": "auto",
                },
                headers={"X-Event-ID": event_id},
            )
            if ok2 and isinstance(body2, dict):
                final_body = body2
            else:
                tool_activity.append(
                    {"tool": "inference_followup", "status": "error", "detail": f"status={status2}"}
                )

        extra_tool_activity = final_body.get("tool_activity") if isinstance(final_body, dict) else None
        if isinstance(extra_tool_activity, list):
            tool_activity.extend(extra_tool_activity)
        reply_text = final_body.get("result") or _first_assistant_content(final_body.get("messages"))
        self._remember_turn(person_id, session_id, messages, final_body, event_id)
        cards = final_body.get("cards") if isinstance(final_body, dict) else None

        # Derive simple metadata for downstream surfaces and context-graph.
        origin_intent = envelope.get("intent") or "companion.turn"
        tag_candidates: List[str] = []
        if isinstance(origin_intent, str) and origin_intent:
            tag_candidates.append(origin_intent)
        for ta in tool_activity:
            name = ta.get("tool") or ta.get("name")
            if isinstance(name, str) and name:
                tag_candidates.append(name)
        seen: set[str] = set()
        tags: List[str] = []
        for t in tag_candidates:
            if t not in seen:
                seen.add(t)
                tags.append(t)

        created_at = time.time()
        # Best-effort emit to downstream surfaces; support both the full signature
        # and older test doubles that only accept the original parameters.
        emit = self._emit_downstream
        try:
            import inspect

            sig = inspect.signature(emit)
            if len(sig.parameters) <= 5:
                emit(reply_text, tool_activity, person_id, session_id, cards)
            else:
                emit(
                    reply_text,
                    tool_activity,
                    person_id,
                    session_id,
                    cards,
                    origin_intent,
                    tags,
                    created_at,
                )
        except Exception:
            emit(reply_text, tool_activity, person_id, session_id, cards)
        self._log_context_graph(
            person_id,
            session_id,
            reply_text,
            tool_activity,
            cards,
            origin_intent,
            tags,
            created_at,
        )

        return {
            "text": reply_text,
            "tool_calls": tool_calls,
            "tool_activity": tool_activity,
            "provider": final_body.get("provider"),
            "model": final_body.get("model"),
            "event_id": event_id,
            "session_id": session_id,
            "person_id": person_id,
            "origin_intent": origin_intent,
            "tags": tags,
            "created_at": created_at,
            "raw_response": final_body,
            "display_intent": {"text": reply_text, "images": []} if reply_text else None,
            "speak_intent": {"text": reply_text} if reply_text else None,
            "wakeword": reply_text if payload.get("wakeword_command") else None,
        }

    def _remember_turn(
        self,
        person_id: str,
        session_id: str,
        messages: List[Dict[str, Any]],
        response: Dict[str, Any],
        event_id: str,
    ) -> None:
        """In-process short-term memory stub; hook to context service later."""
        key = f"{person_id}:{session_id}"
        memory = self._memory.setdefault(key, [])
        memory.append({"messages": messages, "response": response, "event_id": event_id})
        # Keep last 25 turns to avoid unbounded growth.
        if len(memory) > 25:
            self._memory[key] = memory[-25:]
        try:
            summary = response.get("result") or _first_assistant_content(response.get("messages")) or ""
            store_conversation_turn(self._clients, person_id, session_id, messages, response, summary)
        except Exception as exc:
            logger.debug("context persistence failed: %s", exc)

    def _load_memory(self, person_id: str, session_id: str) -> List[Dict[str, Any]]:
        key = f"{person_id}:{session_id}"
        memory = self._memory.get(key, [])
        if memory:
            return [msg for turn in memory for msg in turn.get("messages", [])]
        try:
            history = load_conversation_messages(self._clients, person_id, session_id)
            if history:
                return history
        except Exception as exc:
            logger.debug("context load failed: %s", exc)
        return []

    def _execute_tool_calls(
        self, tool_calls: List[Dict[str, Any]], person_id: str, event_id: str
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Execute tool calls; return tool result messages and activity metadata."""
        tool_messages: List[Dict[str, Any]] = []
        activity: List[Dict[str, Any]] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            func = call.get("function", {}) if isinstance(call.get("function"), dict) else {}
            name = func.get("name") or call.get("name")
            arguments = func.get("arguments") or "{}"
            try:
                args_json = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
            except Exception:
                args_json = {}
            allowed = self._policy_allows_tool(name or "unknown", person_id, event_id)
            if not allowed:
                result = {"error": "policy denied"}
            else:
                result = self._execute_single_tool(name, args_json, person_id, event_id)
            tool_messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": str(result)})
            activity.append({"tool": name, "status": "ok" if "error" not in str(result).lower() else "error", "result": result})
        return tool_messages, activity

    def _execute_single_tool(self, name: Optional[str], arguments: Dict[str, Any], person_id: str, event_id: str) -> Any:
        """Very small executor for built-in tools; MCP tools are stubbed for now."""
        if not name:
            return {"error": "missing tool name"}
        if name == "context.get":
            keys = arguments.get("keys", [])
            if not isinstance(keys, list):
                return {"error": "keys must be list"}
            ok, status, body = self._clients.context.post("/kv/get", {"keys": keys})
            if not ok:
                return {"error": f"context service error ({status})"}
            return body
        if name == "storage.put":
            key = arguments.get("key")
            value = arguments.get("value")
            if not key or value is None:
                return {"error": "key and value required"}
            ok, status, body = self._clients.storage.put(f"/kv/{key}", {"value": value})
            if not ok:
                return {"error": f"storage service error ({status})"}
            return body
        if name == "workflow.recall":
            # Allow the tool payload to override person_id if specified.
            target_person = arguments.get("person_id") or person_id
            query = arguments.get("query") or ""
            time_hint = arguments.get("time_hint_days") or 30
            try:
                time_hint_int = int(time_hint)
            except Exception:
                time_hint_int = 30
            tags_hint = arguments.get("tags_hint")
            if not isinstance(tags_hint, list):
                tags_hint = None
            return recall_workflow_from_dashboard(
                self._clients,
                target_person,
                query=query,
                time_hint_days=time_hint_int,
                tags_hint=tags_hint,
            )
        if name == "workflow.design":
            target_person = arguments.get("person_id") or person_id
            workflow_id = arguments.get("workflow_id") or ""
            project_id = arguments.get("project_id")
            mode = arguments.get("mode") or "design"
            changes = arguments.get("changes") or []
            return apply_workflow_design(
                self._clients,
                target_person,
                workflow_id=workflow_id,
                project_id=project_id,
                mode=mode,
                changes=changes,
            )
        # MCP execution via discovery URL (best-effort)
        if name in self._registry._tools and self._registry._tools[name].source == "mcp":
            return self._execute_mcp_tool(name, arguments)
        return {"error": f"tool {name} not supported yet"}

    def _execute_mcp_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        if not self._registry._mcp_discovery_url:
            return {"error": "no MCP registry configured"}
        # Expect registry response shape to include a base url per server
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(self._registry._mcp_discovery_url)
            resp.raise_for_status()
            registry = resp.json()
            servers = registry if isinstance(registry, list) else registry.get("servers", []) if isinstance(registry, dict) else []
            for server in servers:
                tools = server.get("tools") or []
                base = server.get("base_url") or server.get("url")
                if not base:
                    continue
                for tool in tools:
                    if tool.get("name") == name:
                        try:
                            with httpx.Client(timeout=8.0) as client:
                                call_resp = client.post(f"{base}/tools/{name}", json={"arguments": arguments})
                            call_resp.raise_for_status()
                            return call_resp.json()
                        except Exception as exc:
                            return {"error": f"mcp tool call failed: {exc}"}
        except Exception as exc:
            return {"error": f"mcp discovery failed: {exc}"}
        return {"error": f"mcp tool {name} not found"}


def apply_workflow_design(
    service_clients: ServiceClients,
    person_id: str,
    workflow_id: str,
    project_id: Optional[str] = None,
    mode: str = "design",
    changes: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Shared implementation for workflow design based on context KV and dashboard state.

    Used both by the workflow_design skill handler and by the companion tool
    execution path so that LLM tool calls and explicit skill invocations behave
    consistently.
    """
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        return {"ok": False, "error": "invalid-workflow-id"}
    workflow_id = workflow_id.strip()
    if mode not in ("design", "review"):
        mode = "design"
    changes = changes or []
    if not isinstance(changes, list):
        changes = []

    key = f"workflow:{person_id}:{workflow_id}"
    ok_get, _, body_get = service_clients.context.post("/kv/get", {"keys": [key]})
    workflow_doc: Dict[str, Any] = {}
    if ok_get and isinstance(body_get, dict):
        existing = (body_get.get("values") or {}).get(key) or {}
        if isinstance(existing, dict):
            workflow_doc = existing
    steps = workflow_doc.get("steps") or []
    if not isinstance(steps, list):
        steps = []

    changed = False
    for change in changes:
        if not isinstance(change, dict):
            continue
        op = change.get("op") or ""
        if op == "add_step":
            title = (change.get("title") or "").strip()
            if not title:
                continue
            position = change.get("position")
            step = {
                "id": change.get("id") or str(uuid.uuid4()),
                "title": title,
            }
            if isinstance(position, int) and 0 <= position < len(steps):
                steps.insert(position, step)
            else:
                steps.append(step)
            changed = True

    # Renumber steps for display.
    for idx, step in enumerate(steps):
        if isinstance(step, dict):
            step["order"] = idx + 1

    workflow_doc["workflow_id"] = workflow_id
    workflow_doc["person_id"] = person_id
    if isinstance(project_id, str) and project_id.strip():
        workflow_doc["project_id"] = project_id.strip()
    workflow_doc["steps"] = steps
    workflow_doc["updated_at"] = time.time()

    # Persist when designing or when changes are present.
    if mode != "review" or changed:
        ok_set, status_set, _ = service_clients.context.post("/kv/set", {"key": key, "value": workflow_doc})
        if not ok_set:
            return {
                "ok": False,
                "error": f"context error {status_set}",
                "person_id": person_id,
                "workflow_id": workflow_id,
            }

    now = time.time()
    base_tags = ["workflow", "planning", "workflow.design"]
    base_tags.append("review" if mode == "review" else "draft")
    base_tags.append(f"workflow:{workflow_id}")
    if isinstance(project_id, str) and project_id.strip():
        base_tags.append(f"project:{project_id.strip()}")
    seen_tags: set[str] = set()
    tags: list[str] = []
    for t in base_tags:
        if t and t not in seen_tags:
            seen_tags.add(t)
            tags.append(t)

    summary_card: Dict[str, Any] = {
        "id": f"workflow-{workflow_id}-summary",
        "type": "workflow.summary",
        "title": f"Workflow: {workflow_id}",
        "body": f"{len(steps)} step(s) in this workflow.",
        "tool_activity": "workflow.design",
        "origin_intent": "workflow.design",
        "tags": tags,
        "workflow_id": workflow_id,
        "project_id": project_id,
        "created_at": now,
        "steps": [{"order": s.get("order"), "title": s.get("title")} for s in steps if isinstance(s, dict)],
    }
    cards = [summary_card]

    # Persist to dashboard.
    dashboard_state = {
        "cards": cards,
        "preferences": {},
        "person_id": person_id,
        "updated_at": now,
    }
    dashboard_put(service_clients, person_id, dashboard_state)

    # Emit to renderer experiences if configured.
    renderer_url = os.getenv("UNISON_RENDERER_URL")
    if renderer_url:
        try:
            with httpx.Client(timeout=2.0) as client:
                for card in cards:
                    exp = dict(card)
                    exp.setdefault("person_id", person_id)
                    exp.setdefault("ts", now)
                    client.post(f"{renderer_url}/experiences", json=exp)
        except Exception:
            # Renderer emit failures are non-fatal
            pass

    return {
        "ok": True,
        "person_id": person_id,
        "workflow_id": workflow_id,
        "project_id": project_id,
        "mode": mode,
        "workflow": workflow_doc,
        "cards": cards,
    }


    def _emit_downstream(
        self,
        text: Optional[str],
        tool_activity: List[Dict[str, Any]],
        person_id: str,
        session_id: str,
        cards: Optional[List[Dict[str, Any]]] = None,
        origin_intent: Optional[str] = None,
        tags: Optional[List[str]] = None,
        created_at: Optional[float] = None,
    ) -> None:
        """Best-effort emit to renderer and IO speech if configured."""
        if not text:
            return
        ts = created_at or time.time()
        payload: Dict[str, Any] = {
            "text": text,
            "tool_activity": tool_activity,
            "person_id": person_id,
            "session_id": session_id,
            "ts": ts,
        }
        if cards:
            payload["cards"] = cards
        if origin_intent:
            payload["origin_intent"] = origin_intent
        if tags:
            payload["tags"] = tags
        headers = {}
        try:
            baton = None
            from unison_common.baton import get_current_baton  # type: ignore
            baton = get_current_baton()
            if baton:
                headers["X-Context-Baton"] = baton
        except Exception:
            pass

        audio_url = None
        if _IO_SPEECH_URL:
            try:
                with httpx.Client(timeout=3.0) as client:
                    resp = client.post(
                        f"{_IO_SPEECH_URL}/speech/tts",
                        json={"text": text, "person_id": person_id, "session_id": session_id},
                        headers=headers or None,
                    )
                resp.raise_for_status()
                data = resp.json()
                audio_url = data.get("audio_url")
            except Exception as exc:
                logger.debug("speech emit failed: %s", exc)
        if audio_url:
            payload["audio_url"] = audio_url

        if _RENDERER_URL:
            try:
                with httpx.Client(timeout=2.0) as client:
                    client.post(f"{_RENDERER_URL}/experiences", json=payload, headers=headers or None)
            except Exception as exc:
                logger.debug("renderer emit failed: %s", exc)

    def _log_context_graph(
        self,
        person_id: str,
        session_id: str,
        transcript: Optional[str],
        tool_activity: List[Dict[str, Any]],
        cards: Optional[List[Dict[str, Any]]] = None,
        origin_intent: Optional[str] = None,
        tags: Optional[List[str]] = None,
        created_at: Optional[float] = None,
    ) -> None:
        """Best-effort logging of turns/tool calls into context-graph."""
        if not _CONTEXT_GRAPH_URL:
            return
        body: Dict[str, Any] = {
            "user_id": person_id,
            "session_id": session_id,
            "dimensions": [
                {
                    "name": "conversation",
                    "value": {
                        "transcript": transcript or "",
                        "tool_activity": tool_activity or [],
                        "cards": cards or [],
                    },
                }
            ],
        }
        value = body["dimensions"][0]["value"]
        if origin_intent:
            value["origin_intent"] = origin_intent
        if tags:
            value["tags"] = tags
        if created_at is not None:
            value["created_at"] = created_at
        try:
            with httpx.Client(timeout=2.0) as client:
                # Maintain existing state-style update.
                client.post(f"{_CONTEXT_GRAPH_URL}/context/update", json=body)
                # Also record a trace event for future search.
                event_meta = dict(value)
                event_meta.setdefault("session_id", session_id)
                trace_body = {
                    "user_id": person_id,
                    "trace": [
                        {
                            "event": origin_intent or "conversation.turn",
                            "metadata": event_meta,
                        }
                    ],
                }
                client.post(f"{_CONTEXT_GRAPH_URL}/traces/replay", json=trace_body)
        except Exception as exc:
            logger.debug("context-graph emit failed: %s", exc)

def recall_workflow_from_dashboard(
    service_clients: ServiceClients,
    person_id: str,
    query: str = "",
    time_hint_days: int = 30,
    tags_hint: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Shared implementation for workflow recall based on dashboard state.

    Used both by the workflow_recall skill handler and by the companion tool
    execution path so that LLM tool calls and explicit skill invocations behave
    consistently.
    """
    # Derive tags from hint or a very simple heuristic.
    tags = tags_hint if isinstance(tags_hint, list) else None
    if not tags:
        lowered = str(query).lower()
        inferred: List[str] = []
        if "workflow" in lowered:
            inferred.append("workflow")
        if "design" in lowered:
            inferred.append("design")
        tags = inferred or ["workflow"]

    now = time.time()
    cutoff = now - float(time_hint_days) * 24 * 60 * 60

    # Fetch current dashboard state from context.
    state = dashboard_get(service_clients, person_id) or {}
    cards = state.get("cards") or []
    if not isinstance(cards, list):
        cards = []

    matched: List[Dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        card_tags = card.get("tags") or []
        if not isinstance(card_tags, list):
            card_tags = []
        if tags and not any(t in card_tags for t in tags):
            continue
        created_at = card.get("created_at") or state.get("updated_at")
        if isinstance(created_at, (int, float)) and created_at < cutoff:
            continue
        matched.append(card)

    # Optionally enrich matches with context-graph traces when available.
    if _CONTEXT_GRAPH_URL and tags:
        try:
            since_iso = datetime.fromtimestamp(cutoff).isoformat()
            search_body: Dict[str, Any] = {
                "user_id": person_id,
                "tags": tags,
                "since": since_iso,
                "limit": 20,
            }
            with httpx.Client(timeout=2.0) as client:
                resp = client.post(f"{_CONTEXT_GRAPH_URL}/traces/search", json=search_body)
            resp.raise_for_status()
            data = resp.json() or {}
            traces = data.get("traces") or []
            for trace in traces:
                meta = trace.get("metadata") or {}
                for card in meta.get("cards") or []:
                    if isinstance(card, dict):
                        matched.append(card)
        except Exception:
            # Search enrichment is best-effort; ignore failures.
            pass

    # If nothing matches at all, fall back to the most recent cards.
    if not matched:
        matched = cards

    recap_cards: List[Dict[str, Any]] = []
    for base in matched[:3]:
        summary = dict(base)
        summary.setdefault("origin_intent", "workflow.recall")
        summary_tags = summary.get("tags") or []
        if not isinstance(summary_tags, list):
            summary_tags = []
        if "workflow" not in summary_tags:
            summary_tags.append("workflow")
        summary["tags"] = summary_tags
        recap_cards.append(summary)

    dashboard_state = {
        "cards": recap_cards,
        "preferences": state.get("preferences") or {},
        "person_id": person_id,
        "updated_at": now,
    }
    dashboard_put(service_clients, person_id, dashboard_state)

    # Emit recap cards to renderer experiences if configured.
    renderer_url = os.getenv("UNISON_RENDERER_URL")
    if renderer_url:
        try:
            with httpx.Client(timeout=2.0) as client:
                for card in recap_cards:
                    exp = dict(card)
                    exp.setdefault("person_id", person_id)
                    exp.setdefault("origin_intent", "workflow.recall")
                    exp.setdefault("tags", card.get("tags") or ["workflow"])
                    exp.setdefault("ts", time.time())
                    client.post(f"{renderer_url}/experiences", json=exp)
        except Exception:
            # Renderer emit failures are non-fatal for recall.
            pass

    summary_text = (
        "I’ve resurfaced your most recent workflow cards on the dashboard."
        if recap_cards
        else "I couldn’t find any workflow cards to recall yet."
    )
    return {"ok": True, "person_id": person_id, "cards": recap_cards, "summary_text": summary_text}


def _first_assistant_content(messages: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    if not messages:
        return None
    for msg in messages:
        if msg.get("role") == "assistant":
            return msg.get("content")
    return None


def _policy_allows_tool_impl(self, name: str, person_id: str, event_id: str) -> bool:
    """Consult policy service; allow on failure to avoid hard-stop but log.

    This implementation is attached to CompanionSessionManager at import time
    to ensure the attribute exists even if earlier refactors moved helpers
    around. It mirrors the original behavior.
    """
    payload: Dict[str, Any] = {
        "capability_id": f"tool.{name}",
        "context": {
            "actor": person_id,
            "intent": "companion.tool",
            "tool": name,
        },
    }
    try:
        ok, status, body = evaluate_capability(self._clients, payload, event_id=event_id)
        if ok and isinstance(body, dict):
            decision = body.get("decision", {})
            return bool(decision.get("allowed", True))
    except Exception as exc:
        logger.debug("policy check failed: %s", exc)
    return True


def _emit_downstream_impl(
    self,
    text: Optional[str],
    tool_activity: List[Dict[str, Any]],
    person_id: str,
    session_id: str,
    cards: Optional[List[Dict[str, Any]]] = None,
    origin_intent: Optional[str] = None,
    tags: Optional[List[str]] = None,
    created_at: Optional[float] = None,
) -> None:
    """Best-effort emit to renderer and IO speech if configured."""
    if not text:
        return
    ts = created_at or time.time()
    payload: Dict[str, Any] = {
        "text": text,
        "tool_activity": tool_activity,
        "person_id": person_id,
        "session_id": session_id,
        "ts": ts,
    }
    if cards:
        payload["cards"] = cards
    if origin_intent:
        payload["origin_intent"] = origin_intent
    if tags:
        payload["tags"] = tags
    headers: Dict[str, str] = {}
    try:
        baton = None
        from unison_common.baton import get_current_baton  # type: ignore

        baton = get_current_baton()
        if baton:
            headers["X-Context-Baton"] = baton
    except Exception:
        pass

    audio_url = None
    if _IO_SPEECH_URL:
        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.post(
                    f"{_IO_SPEECH_URL}/speech/tts",
                    json={"text": text, "person_id": person_id, "session_id": session_id},
                    headers=headers or None,
                )
            resp.raise_for_status()
            data = resp.json()
            audio_url = data.get("audio_url")
        except Exception as exc:
            logger.debug("speech emit failed: %s", exc)
    if audio_url:
        payload["audio_url"] = audio_url

    if _RENDERER_URL:
        try:
            with httpx.Client(timeout=2.0) as client:
                client.post(f"{_RENDERER_URL}/experiences", json=payload, headers=headers or None)
        except Exception as exc:
            logger.debug("renderer emit failed: %s", exc)


def _log_context_graph_impl(
    self,
    person_id: str,
    session_id: str,
    transcript: Optional[str],
    tool_activity: List[Dict[str, Any]],
    cards: Optional[List[Dict[str, Any]]] = None,
    origin_intent: Optional[str] = None,
    tags: Optional[List[str]] = None,
    created_at: Optional[float] = None,
) -> None:
    """Best-effort logging of turns/tool calls into context-graph."""
    if not _CONTEXT_GRAPH_URL:
        return
    body: Dict[str, Any] = {
        "user_id": person_id,
        "session_id": session_id,
        "dimensions": [
            {
                "name": "conversation",
                "value": {
                    "transcript": transcript or "",
                    "tool_activity": tool_activity or [],
                    "cards": cards or [],
                },
            }
        ],
    }
    value = body["dimensions"][0]["value"]
    if origin_intent:
        value["origin_intent"] = origin_intent
    if tags:
        value["tags"] = tags
    if created_at is not None:
        value["created_at"] = created_at
    try:
        with httpx.Client(timeout=2.0) as client:
            # Maintain existing state-style update.
            client.post(f"{_CONTEXT_GRAPH_URL}/context/update", json=body)
            # Also record a trace event for future search.
            event_meta = dict(value)
            event_meta.setdefault("session_id", session_id)
            trace_body = {
                "user_id": person_id,
                "trace": [
                    {
                        "event": origin_intent or "conversation.turn",
                        "metadata": event_meta,
                    }
                ],
            }
            client.post(f"{_CONTEXT_GRAPH_URL}/traces/replay", json=trace_body)
    except Exception as exc:
        logger.debug("context-graph emit failed: %s", exc)


# Attach helpers to the CompanionSessionManager class so attributes are present
# even if earlier refactors moved the original method definitions.
CompanionSessionManager._policy_allows_tool = _policy_allows_tool_impl  # type: ignore[attr-defined]
CompanionSessionManager._emit_downstream = _emit_downstream_impl  # type: ignore[attr-defined]
CompanionSessionManager._log_context_graph = _log_context_graph_impl  # type: ignore[attr-defined]
