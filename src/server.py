from fastapi import FastAPI, HTTPException, Body, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.security import HTTPBearer
import uvicorn
import os
import httpx
import json
from typing import Any, Dict, Tuple, List, Callable, Optional
from unison_common import (
    validate_event_envelope, 
    EnvelopeValidationError,
    verify_token,
    verify_service_token,
    require_roles,
    require_role,
    get_security_context,
    add_security_headers,
    get_cors_config,
    AuthError,
    PermissionError
)
from unison_common.logging import configure_logging, log_json
from unison_common.http_client import http_post_json_with_retry, http_get_json_with_retry, http_put_json_with_retry
from unison_common.auth import rate_limit
import logging
import uuid
import time
from collections import defaultdict

app = FastAPI(
    title="unison-orchestrator",
    description="Orchestration service for Unison platform",
    version="1.0.0"
)

logger = configure_logging("unison-orchestrator")

# Security middleware configuration
cors_config = get_cors_config()

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    **cors_config
)

# Trusted hosts middleware (prevents host header attacks)
allowed_hosts = os.getenv("UNISON_ALLOWED_HOSTS", "localhost,127.0.0.1,orchestrator").split(",")
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=allowed_hosts
)

# Security headers middleware
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    return add_security_headers(response)

# Rate limiting middleware
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Apply rate limiting based on client IP
    client_ip = request.client.host
    try:
        await rate_limit(f"ip:{client_ip}", limit=100, window=60)  # 100 requests per minute
    except HTTPException as e:
        return e
    
    return await call_next(request)

# Simple in-memory metrics
_metrics = defaultdict(int)
_start_time = time.time()

CONTEXT_HOST = os.getenv("UNISON_CONTEXT_HOST", "context")
CONTEXT_PORT = os.getenv("UNISON_CONTEXT_PORT", "8081")
STORAGE_HOST = os.getenv("UNISON_STORAGE_HOST", "storage")
STORAGE_PORT = os.getenv("UNISON_STORAGE_PORT", "8082")
POLICY_HOST = os.getenv("UNISON_POLICY_HOST", "policy")
POLICY_PORT = os.getenv("UNISON_POLICY_PORT", "8083")
INFERENCE_HOST = os.getenv("UNISON_INFERENCE_HOST", "inference")
INFERENCE_PORT = os.getenv("UNISON_INFERENCE_PORT", "8087")
CONFIRM_TTL_SECONDS = int(os.getenv("UNISON_CONFIRM_TTL", "300"))

def http_get_json(host: str, port: str, path: str, headers: Dict[str, str] | None = None) -> Tuple[bool, int, dict | None]:
    return http_get_json_with_retry(host, port, path, headers=headers, max_retries=3, base_delay=0.1, max_delay=2.0, timeout=2.0)

def http_post_json(host: str, port: str, path: str, payload: dict, headers: Dict[str, str] | None = None) -> Tuple[bool, int, dict | None]:
    return http_post_json_with_retry(host, port, path, payload, headers=headers, max_retries=3, base_delay=0.1, max_delay=2.0, timeout=2.0)

def http_put_json(host: str, port: str, path: str, payload: dict, headers: Dict[str, str] | None = None) -> Tuple[bool, int, dict | None]:
    return http_put_json_with_retry(host, port, path, payload, headers=headers, max_retries=3, base_delay=0.1, max_delay=2.0, timeout=2.0)

# --- ORCH-001: Skill/Intent registry (in-memory) ---
Skill = Dict[str, Any]
_skills: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}

def _handler_echo(envelope: Dict[str, Any]) -> Dict[str, Any]:
    return {"echo": envelope.get("payload", {})}

def _handler_inference(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Route inference intents to the inference service."""
    event_id = envelope.get("event_id", str(uuid.uuid4()))
    intent = envelope.get("intent", "")
    payload = envelope.get("payload", {})
    
    # Extract inference parameters
    prompt = payload.get("prompt", "")
    provider = payload.get("provider")
    model = payload.get("model")
    max_tokens = payload.get("max_tokens", 1000)
    temperature = payload.get("temperature", 0.7)
    
    if not prompt:
        return {"error": "Missing prompt for inference", "event_id": event_id}
    
    # Call inference service
    inference_payload = {
        "intent": intent,
        "prompt": prompt,
        "provider": provider,
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature
    }
    
    ok, status, body = http_post_json(
        INFERENCE_HOST, INFERENCE_PORT, "/inference/request", inference_payload,
        headers={"X-Event-ID": event_id}
    )
    
    if ok and body:
        return {
            "inference_result": body.get("result", ""),
            "provider": body.get("provider"),
            "model": body.get("model"),
            "event_id": event_id
        }
    else:
        return {"error": "Inference service unavailable", "event_id": event_id}

def _handler_summarize_doc(envelope: Dict[str, Any]) -> Dict[str, Any]:
    # MVP stub: return a canned summary; later integrate with inference service
    return {"summary": "This is a placeholder summary for summarize.doc."}

def _handler_context_get(envelope: Dict[str, Any]) -> Dict[str, Any]:
    payload = envelope.get("payload", {})
    keys = payload.get("keys")
    if not isinstance(keys, list):
        raise ValueError("context.get requires 'keys' list in payload")
    # Call Context service KV GET
    ok, status, body = http_post_json(CONTEXT_HOST, CONTEXT_PORT, "/kv/get", {"keys": keys})
    if not ok or not isinstance(body, dict):
        raise RuntimeError(f"Context service error: {status}")
    return body

def _handler_storage_put(envelope: Dict[str, Any]) -> Dict[str, Any]:
    payload = envelope.get("payload", {})
    key = payload.get("key")
    value = payload.get("value")
    if not isinstance(key, str) or not key:
        raise ValueError("storage.put requires 'key' string in payload")
    if value is None:
        raise ValueError("storage.put requires 'value' in payload")
    # Call Storage service KV PUT
    ok, status, body = http_put_json(STORAGE_HOST, STORAGE_PORT, f"/kv/{key}", {"value": value})
    if not ok or not isinstance(body, dict):
        raise RuntimeError(f"Storage service error: {status}")
    return body

# Register built-in skills
_skills["echo"] = _handler_echo
_skills["summarize.doc"] = _handler_inference
_skills["analyze.code"] = _handler_inference
_skills["translate.text"] = _handler_inference
_skills["generate.idea"] = _handler_inference
_skills["context.get"] = _handler_context_get
_skills["storage.put"] = _handler_storage_put

# --- Confirmation tracking ---
_pending_confirms: Dict[str, Dict[str, Any]] = {}

def _prune_pending():
    now = time.time()
    expired = [tok for tok, data in _pending_confirms.items() if data.get("expires_at", 0) < now]
    for tok in expired:
        _pending_confirms.pop(tok, None)

# --- API Endpoints ---

@app.get("/skills")
def list_skills():
    return {"skills": list(_skills.keys()), "count": len(_skills)}

@app.post("/skills")
def add_skill(skill: Dict[str, Any] = Body(...)):
    prefix = skill.get("intent_prefix")
    handler_name = skill.get("handler", "echo")
    if not isinstance(prefix, str) or not prefix:
        raise HTTPException(status_code=400, detail="invalid intent_prefix")
    if handler_name not in _HANDLERS:
        raise HTTPException(status_code=400, detail="unknown handler")
    context_keys = skill.get("context_keys")
    if context_keys is not None and not isinstance(context_keys, list):
        raise HTTPException(status_code=400, detail="context_keys must be a list if provided")
    entry = {"intent_prefix": prefix, "handler": handler_name}
    if context_keys:
        entry["context_keys"] = context_keys
    _skills.append(entry)
    log_json(logging.INFO, "skill_added", service="unison-orchestrator", intent_prefix=prefix, handler=handler_name)
    return {"ok": True, "skill": entry}

@app.get("/health")
def health():
    _metrics["/health"] += 1
    log_json(logging.INFO, "health", service="unison-orchestrator")
    return {"status": "ok", "service": "unison-orchestrator"}

@app.get("/metrics")
def metrics():
    """Prometheus text-format metrics."""
    uptime = time.time() - _start_time
    lines = [
        "# HELP unison_orchestrator_requests_total Total number of requests by endpoint",
        "# TYPE unison_orchestrator_requests_total counter",
    ]
    for k, v in _metrics.items():
        lines.append(f'unison_orchestrator_requests_total{{endpoint="{k}"}} {v}')
    lines.extend([
        "",
        "# HELP unison_orchestrator_uptime_seconds Service uptime in seconds",
        "# TYPE unison_orchestrator_uptime_seconds gauge",
        f"unison_orchestrator_uptime_seconds {uptime}",
        "",
        "# HELP unison_orchestrator_skills_registered Number of registered skills",
        "# TYPE unison_orchestrator_skills_registered gauge",
        f"unison_orchestrator_skills_registered {len(_skills)}",
    ])
    return "\n".join(lines)

@app.get("/ready")
def ready():
    rid = str(uuid.uuid4())
    hdrs = {"X-Event-ID": rid}
    context_ok, _, _ = http_get_json(CONTEXT_HOST, CONTEXT_PORT, "/health", headers=hdrs)
    storage_ok, _, _ = http_get_json(STORAGE_HOST, STORAGE_PORT, "/health", headers=hdrs)
    inference_ok, _, _ = http_get_json(INFERENCE_HOST, INFERENCE_PORT, "/health", headers=hdrs)

    eval_payload = {
        "capability_id": "test.ACTION",
        "context": {"actor": "local-user", "intent": "readiness-check"},
    }
    policy_ok, _, policy_body = http_post_json(
        POLICY_HOST,
        POLICY_PORT,
        "/evaluate",
        eval_payload,
        headers=hdrs,
    )

    allowed = False
    if policy_ok and isinstance(policy_body, dict):
        decision = policy_body.get("decision", {})
        allowed = decision.get("allowed", False) is True

    all_ok = context_ok and storage_ok and inference_ok and allowed

    resp = {
        "ready": all_ok,
        "deps": {
            "context": context_ok,
            "storage": storage_ok,
            "inference": inference_ok,
            "policy_allowed_action": allowed,
        },
        "event_id": rid,
    }
    log_json(logging.INFO, "readiness", service="unison-orchestrator", event_id=rid, context=context_ok, storage=storage_ok, inference=inference_ok, policy_allowed=allowed, ready=all_ok)
    return resp

@app.post("/event")
async def handle_event(
    envelope: dict = Body(...),
    current_user: Dict[str, Any] = Depends(verify_token)
):
    """Handle events with authentication and authorization"""
    _metrics["/event"] += 1
    
    # Create security context
    security_ctx = get_security_context(current_user)
    
    try:
        # Validate and sanitize envelope
        envelope = validate_event_envelope(envelope)
    except EnvelopeValidationError as e:
        log_json(
            logging.WARNING,
            "envelope_validation_failed",
            service="unison-orchestrator",
            error=str(e),
            user=current_user.get("username"),
            roles=current_user.get("roles", [])
        )
        raise HTTPException(status_code=400, detail=str(e))
    
    # Add user context to envelope for policy evaluation
    envelope["user"] = {
        "username": current_user.get("username"),
        "roles": current_user.get("roles", []),
        "authenticated": True
    }
    
    event_id = str(uuid.uuid4())
    intent = envelope.get("intent", "")
    source = envelope.get("source", "")
    payload = envelope.get("payload", {})
    
    log_json(
        logging.INFO,
        "event_received",
        service="unison-orchestrator",
        event_id=event_id,
        intent=intent,
        source=source,
        user=current_user.get("username"),
        roles=current_user.get("roles", [])
    )
    
    # Check if intent exists in skills registry
    handler = _skills.get(intent)
    if not handler:
        log_json(
            logging.WARNING,
            "unknown_intent",
            service="unison-orchestrator",
            event_id=event_id,
            intent=intent,
            user=current_user.get("username")
        )
        raise HTTPException(status_code=404, detail=f"Unknown intent: {intent}")
    
    # Policy evaluation - check if user is allowed to execute this intent
    eval_payload = {
        "capability_id": f"unison.{intent}",
        "context": {
            "actor": current_user.get("username"),
            "intent": intent,
            "source": source,
            "auth_scope": envelope.get("auth_scope"),
            "safety_context": envelope.get("safety_context"),
            "user_roles": current_user.get("roles", [])
        },
    }
    
    policy_ok, _, policy_body = http_post_json(
        POLICY_HOST, POLICY_PORT, "/evaluate", eval_payload,
        headers={"X-Event-ID": event_id}
    )
    
    allowed = False
    if policy_ok and isinstance(policy_body, dict):
        decision = policy_body.get("decision", {})
        allowed = decision.get("allowed", False) is True
    
    if not allowed:
        reason = decision.get("reason", "Policy denied")
        log_json(
            logging.WARNING,
            "policy_denied",
            service="unison-orchestrator",
            event_id=event_id,
            intent=intent,
            reason=reason,
            user=current_user.get("username"),
            roles=current_user.get("roles", [])
        )
        raise HTTPException(status_code=403, detail=f"Policy denied: {reason}")
    
    # Execute the skill handler
    try:
        result = handler(envelope)
        
        log_json(
            logging.INFO,
            "event_completed",
            service="unison-orchestrator",
            event_id=event_id,
            intent=intent,
            user=current_user.get("username"),
            success=True
        )
        
        return {
            "ok": True,
            "event_id": event_id,
            "intent": intent,
            "result": result,
            "user": current_user.get("username")
        }
        
    except Exception as e:
        log_json(
            logging.ERROR,
            "handler_error",
            service="unison-orchestrator",
            event_id=event_id,
            intent=intent,
            error=str(e),
            user=current_user.get("username")
        )
        raise HTTPException(status_code=500, detail=f"Handler error: {str(e)}")

@app.get("/introspect")
def introspect():
    eid = str(uuid.uuid4())
    hdrs = {"X-Event-ID": eid}
    ctx_ok, ctx_status, _ = http_get_json(CONTEXT_HOST, CONTEXT_PORT, "/health", headers=hdrs)
    stor_ok, stor_status, _ = http_get_json(STORAGE_HOST, STORAGE_PORT, "/health", headers=hdrs)
    pol_ok, pol_status, _ = http_get_json(POLICY_HOST, POLICY_PORT, "/health", headers=hdrs)
    rules_ok, rules_status, rules = http_get_json(POLICY_HOST, POLICY_PORT, "/rules/summary", headers=hdrs)

    result = {
        "event_id": eid,
        "services": {
            "context": {"ok": ctx_ok, "status": ctx_status, "host": CONTEXT_HOST, "port": CONTEXT_PORT},
            "storage": {"ok": stor_ok, "status": stor_status, "host": STORAGE_HOST, "port": STORAGE_PORT},
            "policy": {"ok": pol_ok, "status": pol_status, "host": POLICY_HOST, "port": POLICY_PORT},
        },
        "skills": list(_skills.keys()),
        "policy_rules": {
            "ok": rules_ok,
            "status": rules_status,
            "summary": rules if isinstance(rules, dict) else None,
        },
    }
    log_json(
        logging.INFO,
        "introspect",
        service="unison-orchestrator",
        event_id=eid,
        context_ok=ctx_ok,
        storage_ok=stor_ok,
        policy_ok=pol_ok,
        rules=rules.get("count") if isinstance(rules, dict) else None,
    )
    return result

@app.post("/event/confirm")
def confirm_event(body: Dict[str, Any] = Body(...)):
    _metrics["/event/confirm"] += 1
    _prune_pending()
    token = body.get("confirmation_token")
    log_json(logging.INFO, "confirm_request", service="unison-orchestrator", token=str(token))
    if not isinstance(token, str) or token not in _pending_confirms:
        # Attempt to load from storage for durability across restarts
        ok_s, st_s, body_s = http_get_json(STORAGE_HOST, STORAGE_PORT, f"/kv/confirm/{token}")
        if not ok_s or not isinstance(body_s, dict) or not body_s.get("ok"):
            raise HTTPException(status_code=404, detail="Invalid or expired confirmation token")
        envelope = body_s.get("envelope")
        if not envelope:
            raise HTTPException(status_code=404, detail="Confirmation token corrupted")
        _pending_confirms[token] = {
            "envelope": envelope,
            "matched": None,
            "handler_name": None,
            "created_at": time.time(),
            "expires_at": time.time() + CONFIRM_TTL_SECONDS,
        }
    
    data = _pending_confirms[token]
    envelope = data["envelope"]
    event_id = str(uuid.uuid4())
    intent = envelope.get("intent", "")
    
    # Dispatch to handler
    handler = _skills.get(intent)
    if not handler:
        raise HTTPException(status_code=404, detail=f"Intent {intent} not found")
    
    try:
        result = handler(envelope)
        # Clean up confirmation
        _pending_confirms.pop(token, None)
        # Remove from storage
        http_post_json(STORAGE_HOST, STORAGE_PORT, f"/kv/delete/confirm/{token}", {})
        log_json(logging.INFO, "confirm_completed", service="unison-orchestrator", event_id=event_id, intent=intent, token=token)
        return {
            "ok": True,
            "event_id": event_id,
            "intent": intent,
            "result": result,
            "confirmed": True
        }
    except Exception as e:
        log_json(logging.ERROR, "confirm_handler_error", service="unison-orchestrator", event_id=event_id, intent=intent, error=str(e), token=token)
        raise HTTPException(status_code=500, detail=f"Handler error: {str(e)}")

# Handler mapping for dynamic skill registration
_HANDLERS = {
    "echo": _handler_echo,
    "summarize.doc": _handler_summarize_doc,
    "context.get": _handler_context_get,
    "storage.put": _handler_storage_put,
    "inference": _handler_inference,
}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
