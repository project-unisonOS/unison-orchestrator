from __future__ import annotations

import uuid
from typing import Any, Callable, Dict

from .clients import ServiceClients

SkillHandler = Callable[[Dict[str, Any]], Dict[str, Any]]


def build_skill_state(
    service_clients: ServiceClients,
) -> Dict[str, Dict[str, SkillHandler]]:
    """Return default skills and handler registry backed by downstream clients."""

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

    handlers: Dict[str, SkillHandler] = {
        "echo": handler_echo,
        "inference": handler_inference,
        "summarize_doc": handler_summarize_doc,
        "context_get": handler_context_get,
        "storage_put": handler_storage_put,
    }

    skills: Dict[str, SkillHandler] = {
        "echo": handler_echo,
        "summarize.doc": handler_summarize_doc,
        "analyze.code": handler_inference,
        "translate.text": handler_inference,
        "generate.idea": handler_inference,
        "context.get": handler_context_get,
        "storage.put": handler_storage_put,
    }

    return {"skills": skills, "handlers": handlers}
