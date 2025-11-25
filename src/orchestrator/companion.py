from __future__ import annotations

import logging
import os
import uuid
import httpx
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .clients import ServiceClients
from .context_client import (
    load_conversation_messages,
    store_conversation_turn,
)

logger = logging.getLogger(__name__)

# Optional downstream emitters for renderer/speech (best-effort)
_RENDERER_URL = os.getenv("UNISON_RENDERER_URL")
_IO_SPEECH_URL = os.getenv("UNISON_IO_SPEECH_URL")

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
            tool_messages, tool_activity = self._execute_tool_calls(tool_calls)
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
                tool_activity.append({"tool": "inference_followup", "status": "error", "detail": f"status={status2}"})

        reply_text = final_body.get("result") or _first_assistant_content(final_body.get("messages"))
        self._remember_turn(person_id, session_id, messages, final_body, event_id)
        self._emit_downstream(reply_text, tool_activity)

        return {
            "text": reply_text,
            "tool_calls": tool_calls,
            "tool_activity": tool_activity,
            "provider": final_body.get("provider"),
            "model": final_body.get("model"),
            "event_id": event_id,
            "session_id": session_id,
            "person_id": person_id,
            "raw_response": final_body,
            "display_intent": {"text": reply_text, "images": []} if reply_text else None,
            "speak_intent": {"text": reply_text} if reply_text else None,
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

    def _execute_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
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
            result = self._execute_single_tool(name, args_json)
            tool_messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": str(result)})
            activity.append({"tool": name, "status": "ok" if "error" not in str(result).lower() else "error", "result": result})
        return tool_messages, activity

    def _execute_single_tool(self, name: Optional[str], arguments: Dict[str, Any]) -> Any:
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

    def _emit_downstream(self, text: Optional[str], tool_activity: List[Dict[str, Any]]) -> None:
        """Best-effort emit to renderer and IO speech if configured."""
        if not text:
            return
        payload = {"text": text, "tool_activity": tool_activity}
        headers = {}
        try:
            baton = None
            from unison_common.baton import get_current_baton  # type: ignore
            baton = get_current_baton()
            if baton:
                headers["X-Context-Baton"] = baton
        except Exception:
            pass

        if _RENDERER_URL:
            try:
                with httpx.Client(timeout=2.0) as client:
                    client.post(f"{_RENDERER_URL}/display", json=payload, headers=headers)
            except Exception as exc:
                logger.debug("renderer emit failed: %s", exc)
        if _IO_SPEECH_URL:
            try:
                with httpx.Client(timeout=2.0) as client:
                    client.post(f"{_IO_SPEECH_URL}/speech/tts", json={"text": text}, headers=headers)
            except Exception as exc:
                logger.debug("speech emit failed: %s", exc)


def _first_assistant_content(messages: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    if not messages:
        return None
    for msg in messages:
        if msg.get("role") == "assistant":
            return msg.get("content")
    return None
