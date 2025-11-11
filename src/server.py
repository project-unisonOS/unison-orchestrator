from fastapi import FastAPI, HTTPException, Body, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.security import HTTPBearer
import uvicorn
import os
import httpx
import json
from typing import Any, Dict, Tuple, List, Callable, Optional
import asyncio

# M5.1: OpenTelemetry imports
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
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
    PermissionError,
    # M5.2: Consent grant verification
    ConsentScopes,
    verify_consent_grant,
    check_consent_header,
    # M5.4: Performance optimization
    get_http_client,
    get_auth_cache,
    get_policy_cache,
    get_user_rate_limiter,
    get_endpoint_rate_limiter,
    get_performance_monitor,
    # P0.3: Tracing middleware
    TracingMiddleware,
    get_request_id,
    create_tracing_client,
)
from unison_common.logging import configure_logging, log_json
from unison_common.http_client import http_post_json_with_retry, http_get_json_with_retry, http_put_json_with_retry
from unison_common.auth import rate_limit
from unison_common.replay_store import initialize_replay, ReplayConfig, get_replay_manager
from unison_common.replay_endpoints import store_processing_envelope
from unison_common.idempotency_middleware import IdempotencyMiddleware, IdempotencyKeyRequiredMiddleware
from unison_common.idempotency import IdempotencyManager, IdempotencyConfig, get_idempotency_manager
from unison_common.consent import require_consent, ConsentScopes
from router import Router, RoutingStrategy, RoutingContext, RouteCandidate
import logging
import uuid
import time
from collections import defaultdict

# M5.1: Initialize OpenTelemetry
def setup_telemetry():
    """Configure OpenTelemetry tracing"""
    # Get configuration from environment
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318")
    service_name = os.getenv("OTEL_SERVICE_NAME", "unison-orchestrator")
    service_version = os.getenv("OTEL_SERVICE_VERSION", "1.0.0")
    
    # Create resource with service information
    resource = Resource(attributes={
        SERVICE_NAME: service_name,
        SERVICE_VERSION: service_version,
    })
    
    # Configure tracer provider
    provider = TracerProvider(resource=resource)
    
    # Add OTLP exporter with batch processor
    otlp_exporter = OTLPSpanExporter(
        endpoint=f"{otlp_endpoint}/v1/traces",
        timeout=10
    )
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    
    # Set as global tracer provider
    trace.set_tracer_provider(provider)
    
    # Instrument HTTPX for outgoing requests
    HTTPXClientInstrumentor().instrument()
    
    logging.info(f"OpenTelemetry configured: {service_name} -> {otlp_endpoint}")

# Initialize telemetry
setup_telemetry()

app = FastAPI(
    title="unison-orchestrator",
    description="Orchestration service for Unison platform",
    version="1.0.0"
)

# M5.1: Instrument FastAPI with OpenTelemetry
FastAPIInstrumentor.instrument_app(app)

logger = configure_logging("unison-orchestrator")

# Initialize replay store for M3 event storage
replay_config = ReplayConfig()
replay_config.default_retention_days = 30
replay_config.max_envelopes_per_trace = 1000
replay_config.max_stored_envelopes = 50000
initialize_replay(replay_config)
logger.info("Replay store initialized for M3 event storage")

# Security middleware configuration
cors_config = get_cors_config()

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    **cors_config
)

# P0.3: Add tracing middleware for x-request-id and traceparent propagation
app.add_middleware(TracingMiddleware, service_name="unison-orchestrator")
logger.info("P0.3: Tracing middleware enabled")

# M4: Initialize idempotency manager
idempotency_config = IdempotencyConfig()
idempotency_config.ttl_seconds = 24 * 60 * 60  # 24 hours
# Note: Using in-memory store for M4; Redis integration can be added later
app.add_middleware(IdempotencyMiddleware, ttl_seconds=idempotency_config.ttl_seconds)

# Add idempotency key requirement for critical endpoints (M4: re-enabled)
app.add_middleware(IdempotencyKeyRequiredMiddleware, required_paths=['/ingest'])

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

# Router configuration
ROUTING_STRATEGY = os.getenv("UNISON_ROUTING_STRATEGY", "rule_based")
router = Router(RoutingStrategy(ROUTING_STRATEGY))

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
_skills["summarize.doc"] = _handler_summarize_doc
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
    # Register handler function in the skills map (dict), keyed by intent prefix
    if prefix in _skills:
        raise HTTPException(status_code=409, detail=f"intent_prefix already registered: {prefix}")
    _skills[prefix] = _HANDLERS[handler_name]
    entry = {"intent_prefix": prefix, "handler": handler_name}
    if context_keys:
        entry["context_keys"] = context_keys
    log_json(logging.INFO, "skill_added", service="unison-orchestrator", intent_prefix=prefix, handler=handler_name)
    return {"ok": True, "skill": entry}

@app.get("/health")
def health():
    _metrics["/health"] += 1
    log_json(logging.INFO, "health", service="unison-orchestrator")
    return {"status": "ok", "service": "unison-orchestrator"}

@app.get("/readyz")
def readiness():
    """Readiness check that verifies all dependencies are ready"""
    _metrics["/readyz"] += 1
    
    checks = {}
    overall_ready = True
    
    # Check policy service
    try:
        ok, status, body = http_get_json(POLICY_HOST, POLICY_PORT, "/health")
        checks["policy"] = {"ready": ok, "status": status}
        if not ok:
            overall_ready = False
    except Exception as e:
        checks["policy"] = {"ready": False, "error": str(e)}
        overall_ready = False
    
    # Check context service
    try:
        ok, status, body = http_get_json(CONTEXT_HOST, CONTEXT_PORT, "/health")
        checks["context"] = {"ready": ok, "status": status}
        if not ok:
            overall_ready = False
    except Exception as e:
        checks["context"] = {"ready": False, "error": str(e)}
        overall_ready = False
    
    # Check storage service
    try:
        ok, status, body = http_get_json(STORAGE_HOST, STORAGE_PORT, "/health")
        checks["storage"] = {"ready": ok, "status": status}
        if not ok:
            overall_ready = False
    except Exception as e:
        checks["storage"] = {"ready": False, "error": str(e)}
        overall_ready = False
    
    # Check inference service
    try:
        ok, status, body = http_get_json(INFERENCE_HOST, INFERENCE_PORT, "/health")
        checks["inference"] = {"ready": ok, "status": status}
        if not ok:
            overall_ready = False
    except Exception as e:
        checks["inference"] = {"ready": False, "error": str(e)}
        overall_ready = False
    
    # Check if skills are registered
    checks["skills"] = {"ready": len(_skills) > 0, "count": len(_skills)}
    if len(_skills) == 0:
        overall_ready = False
    
    log_json(logging.INFO, "readiness", service="unison-orchestrator", 
             ready=overall_ready, checks=checks)
    
    if overall_ready:
        return {"ready": True, "checks": checks}
    else:
        return {"ready": False, "checks": checks}

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

# --- Router Management Endpoints ---

@app.get("/router/config")
def get_router_config():
    """Get current router configuration"""
    return {
        "strategy": router.get_strategy_name(),
        "metrics": router.get_metrics()
    }

@app.post("/router/strategy")
def set_routing_strategy(strategy: str = Body(..., embed=True)):
    """Change routing strategy"""
    try:
        routing_strategy = RoutingStrategy(strategy)
        router.set_strategy(routing_strategy)
        log_json(logging.INFO, "router_strategy_changed", 
                service="unison-orchestrator", 
                strategy=strategy)
        return {"ok": True, "strategy": strategy}
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid strategy: {strategy}")

@app.post("/router/rules")
def add_routing_rule(rule: Dict[str, Any] = Body(...)):
    """Add a routing rule (for rule-based and hybrid strategies)"""
    try:
        router.add_routing_rule(rule)
        log_json(logging.INFO, "routing_rule_added", 
                service="unison-orchestrator", 
                rule_id=rule.get('id'),
                intent_prefix=rule.get('intent_prefix'))
        return {"ok": True, "rule": rule}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/router/rules")
def get_routing_rules():
    """Get current routing rules (if available)"""
    if hasattr(router.router, 'rules'):
        return {"rules": router.router.rules}
    else:
        return {"rules": [], "message": "Current strategy does not support rules"}

@app.post("/router/test")
def test_routing(request_body: Dict[str, Any] = Body(...)):
    """Test routing without executing the skill"""
    intent = request_body.get("intent", "")
    payload = request_body.get("payload", {})
    user = request_body.get("user", {"username": "test", "roles": ["user"]})
    source = request_body.get("source", "test")
    
    # Create routing context
    context = RoutingContext(
        intent=intent,
        payload=payload,
        user=user,
        source=source,
        event_id=str(uuid.uuid4()),
        timestamp=time.time()
    )
    
    # Test routing
    candidate = router.route(context, _skills)
    
    if candidate:
        return {
            "routed": True,
            "skill_id": candidate.skill_id,
            "strategy_used": candidate.strategy_used,
            "score": candidate.score,
            "metadata": candidate.metadata
        }
    else:
        return {
            "routed": False,
            "message": "No matching skill found"
        }

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
            "confirmed": True,
        }
    except Exception as e:
        log_json(logging.ERROR, "confirm_handler_error", service="unison-orchestrator", event_id=event_id, intent=intent, error=str(e), token=token)
        raise HTTPException(status_code=500, detail=f"Handler error: {str(e)}")

# Handler mapping for dynamic skill registration
_HANDLERS = {
    "echo": _handler_echo,
    "inference": _handler_inference,
    "summarize_doc": _handler_summarize_doc,
    "context_get": _handler_context_get,
    "storage_put": _handler_storage_put,
}

# Skills already registered above (lines 174-180)

# --- M4: Golden Path v1 - /ingest with authentication ---
REQUIRE_CONSENT = os.getenv("UNISON_REQUIRE_CONSENT", "false").lower() == "true"

@app.post("/ingest")
async def ingest_m4(
    request: Request,
    body: Dict[str, Any] = Body(...),
    current_user: Dict[str, Any] = Depends(verify_token),
    consent_grant: Dict[str, Any] = Depends(require_consent([ConsentScopes.INGEST_WRITE])) if REQUIRE_CONSENT else None,
):
    """M5.2: Ingest with JWT authentication and consent verification.
    Request body example: {"intent": "echo", "payload": {"message": "hello"}}
    Requires: 
    - Authorization header with valid JWT token
    - X-Consent-Grant header with consent grant for ingest.write scope (optional for now)
    """
    # M5.1: Get tracer for custom spans
    tracer = trace.get_tracer(__name__)
    
    # M5.4: Rate limiting
    user_rate_limiter = get_user_rate_limiter()
    endpoint_rate_limiter = get_endpoint_rate_limiter()
    
    # Check user rate limit
    if not user_rate_limiter.is_allowed(current_user.get("username")):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please try again later.",
            headers={"Retry-After": "60"}
        )
    
    # Check endpoint rate limit
    if not endpoint_rate_limiter.is_allowed("/ingest"):
        raise HTTPException(
            status_code=429,
            detail="Service temporarily unavailable. Please try again later.",
            headers={"Retry-After": "60"}
        )
    
    _metrics["/ingest"] += 1
    
    # Track total processing time
    start_time = time.time()
    
    # Generate correlation ID for this request
    correlation_id = str(uuid.uuid4())
    trace_id = str(uuid.uuid4()).replace('-', '')[:32]
    
    # M5.1: Add span attributes for user context
    current_span = trace.get_current_span()
    current_span.set_attribute("user.id", current_user.get("username"))
    current_span.set_attribute("user.roles", ",".join(current_user.get("roles", [])))
    current_span.set_attribute("correlation.id", correlation_id)
    current_span.set_attribute("trace.id", trace_id)
    
    # M5.2: Consent grant (feature-flagged)
    if REQUIRE_CONSENT and consent_grant is not None:
        logger.info(f"Consent grant verified for user {current_user.get('username')}")
        current_span.set_attribute("consent.verified", "true")
        current_span.set_attribute("consent.scopes", ",".join(consent_grant.get("scopes", [])))
    
    # Extract intent and payload from body
    intent = body.get("intent", "")
    payload = body.get("payload", {})
    source = body.get("source", "test-client")
    
    # M5.1: Add intent to span
    current_span.set_attribute("intent.type", intent)
    current_span.set_attribute("request.source", source)
    
    # Validate required fields
    if not intent:
        raise HTTPException(status_code=400, detail="intent is required")
    
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    
    # M5.2: Store ingest request event with user and consent context
    envelope_data = {
        "intent": intent,
        "payload": payload,
        "source": source,
        "user": current_user.get("username"),
        "roles": current_user.get("roles", [])
    }
    
    # Add consent context if available
    if REQUIRE_CONSENT and consent_grant is not None:
        envelope_data["consent"] = {
            "subject": consent_grant.get("sub"),
            "scopes": consent_grant.get("scopes", []),
            "grant_id": consent_grant.get("jti"),
            "verified": True,
        }
    
    store_processing_envelope(
        envelope_data=envelope_data,
        trace_id=trace_id,
        correlation_id=correlation_id,
        event_type="ingest_request",
        source="orchestrator",
        user_id=current_user.get("username"),
        processing_time_ms=None,
        status_code=200,
        error_message=None
    )
    
    # Process based on intent
    if intent == "echo":
        # Echo skill processing
        message = payload.get("message", "")
        
        if not message:
            raise HTTPException(status_code=400, detail="message is required for echo intent")
        
        # M3: Store skill processing start event
        store_processing_envelope(
            envelope_data={"intent": intent, "message": message},
            trace_id=trace_id,
            correlation_id=correlation_id,
            event_type="skill_start",
            source="orchestrator",
            user_id=None
        )
        
        # Call echo skill handler
        try:
            # M5.1: Create custom span for skill execution
            with tracer.start_as_current_span("skill.echo") as skill_span:
                skill_span.set_attribute("skill.name", "echo")
                skill_span.set_attribute("skill.message", message)
                
                echo_result = _handler_echo({"payload": {"message": message}, "source": source})
                
                skill_span.set_attribute("skill.result", "success")
            
            # Calculate total duration
            total_duration = (time.time() - start_time) * 1000
            
            # M5.4: Record performance metrics
            perf_monitor = get_performance_monitor()
            perf_monitor.record("ingest_latency_ms", total_duration)
            perf_monitor.record("skill_execution_ms", total_duration)
            
            # M3: Store skill processing complete event
            store_processing_envelope(
                envelope_data={"intent": intent, "result": echo_result},
                trace_id=trace_id,
                correlation_id=correlation_id,
                event_type="skill_complete",
                source="orchestrator",
                user_id=None,
                processing_time_ms=total_duration,
                status_code=200
            )
            
            # Return response
            return {
                "status": "success",
                "trace_id": trace_id,
                "correlation_id": correlation_id,
                "result": {
                    "intent": intent,
                    "response": echo_result.get("echo", {}).get("message", message),
                    "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime())
                },
                "duration_ms": round(total_duration, 2)
            }
            
        except Exception as e:
            # M3: Store error event
            store_processing_envelope(
                envelope_data={"intent": intent, "error": str(e)},
                trace_id=trace_id,
                correlation_id=correlation_id,
                event_type="error",
                source="orchestrator",
                user_id=None,
                error_message=str(e),
                status_code=500
            )
            raise HTTPException(status_code=500, detail=f"Skill processing failed: {str(e)}")
    
    else:
        # Unsupported intent
        raise HTTPException(status_code=400, detail=f"Unsupported intent: {intent}. Only 'echo' is supported in M2.")


# --- M5.3: Enhanced Replay Endpoints (with filtering) ---
@app.get("/replay/traces")
async def list_traces_m5(
    limit: int = 50,
    offset: int = 0,
    user_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    status: Optional[str] = None,
    intent: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(verify_token)
):
    """
    List stored traces with filtering and pagination (M5.3)
    
    Query parameters:
    - limit: Maximum number of results (default: 50)
    - offset: Offset for pagination (default: 0)
    - user_id: Filter by user ID
    - start_date: Filter by start date (ISO format)
    - end_date: Filter by end date (ISO format)
    - status: Filter by status ("success" or "error")
    - intent: Filter by intent type
    """
    try:
        from datetime import datetime
        
        replay_manager = get_replay_manager()
        
        # Parse date filters
        start_dt = datetime.fromisoformat(start_date) if start_date else None
        end_dt = datetime.fromisoformat(end_date) if end_date else None
        
        # Apply filters
        filtered_ids, total_count = replay_manager.store.filter_traces(
            user_id=user_id,
            start_date=start_dt,
            end_date=end_dt,
            status=status,
            intent=intent,
            limit=limit,
            offset=offset
        )
        
        # Get summary for each trace
        traces = []
        for trace_id in filtered_ids:
            summary = replay_manager.get_trace_summary(trace_id)
            if summary.get("found"):
                traces.append(summary)
        
        return {
            "traces": traces,
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "filters": {
                "user_id": user_id,
                "start_date": start_date,
                "end_date": end_date,
                "status": status,
                "intent": intent
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {str(e)}")
    except Exception as e:
        logger.error(f"Error listing traces: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list traces: {str(e)}")

@app.get("/replay/{trace_id}/summary")
async def get_trace_summary_m4(
    trace_id: str,
    current_user: Dict[str, Any] = Depends(verify_token)
):
    """Get summary for a specific trace (requires authentication)"""
    try:
        replay_manager = get_replay_manager()
        summary = replay_manager.get_trace_summary(trace_id)
        
        if not summary.get("found", False):
            raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
        
        return summary
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting trace summary: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get trace summary: {str(e)}")

@app.post("/replay/{trace_id}")
async def replay_trace_m4(
    trace_id: str,
    current_user: Dict[str, Any] = Depends(verify_token)
):
    """Replay a trace by ID (requires authentication)"""
    try:
        replay_manager = get_replay_manager()
        
        # Check if trace exists
        summary = replay_manager.get_trace_summary(trace_id)
        if not summary.get("found", False):
            raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
        
        # Get all events for the trace
        envelopes = replay_manager.store.get_envelopes_by_trace(trace_id)
        
        # Convert envelopes to dict format
        events = [env.to_dict() for env in envelopes]
        
        # For M3, we'll do a simple replay by returning the events
        # Full re-execution can be added in a future iteration
        return {
            "trace_id": trace_id,
            "status": "replayed",
            "events_count": len(events),
            "events": events,
            "message": "Trace events retrieved successfully. Full re-execution coming in future iteration."
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error replaying trace: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to replay trace: {str(e)}")

@app.delete("/replay/{trace_id}")
async def delete_trace_m5(
    trace_id: str,
    current_user: Dict[str, Any] = Depends(require_roles(["admin", "operator"]))
):
    """
    Delete a trace by ID (M5.3)
    Requires admin or operator role
    """
    try:
        replay_manager = get_replay_manager()
        
        # Check if trace exists
        summary = replay_manager.get_trace_summary(trace_id)
        if not summary.get("found", False):
            raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
        
        # Delete the trace
        success = replay_manager.store.delete_trace(trace_id)
        
        if success:
            logger.info(f"Trace {trace_id} deleted by user {current_user.get('username')}")
            return {
                "trace_id": trace_id,
                "status": "deleted",
                "deleted_by": current_user.get("username"),
                "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime())
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to delete trace")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting trace: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete trace: {str(e)}")

@app.get("/replay/{trace_id}/export")
async def export_trace_m5(
    trace_id: str,
    format: str = "json",
    current_user: Dict[str, Any] = Depends(verify_token)
):
    """
    Export a trace in JSON or CSV format (M5.3)
    
    Query parameters:
    - format: Export format ("json" or "csv", default: "json")
    """
    from fastapi.responses import Response
    import csv
    from io import StringIO
    
    try:
        replay_manager = get_replay_manager()
        
        # Check if trace exists
        summary = replay_manager.get_trace_summary(trace_id)
        if not summary.get("found", False):
            raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
        
        # Get all events for the trace
        envelopes = replay_manager.store.get_envelopes_by_trace(trace_id)
        
        if format.lower() == "csv":
            # Export as CSV
            output = StringIO()
            if envelopes:
                # Get all possible fields
                fieldnames = [
                    "envelope_id", "trace_id", "correlation_id", "timestamp",
                    "event_type", "source", "user_id", "processing_time_ms",
                    "status_code", "error_message", "intent", "payload"
                ]
                
                writer = csv.DictWriter(output, fieldnames=fieldnames)
                writer.writeheader()
                
                for env in envelopes:
                    row = {
                        "envelope_id": env.envelope_id,
                        "trace_id": env.trace_id,
                        "correlation_id": env.correlation_id,
                        "timestamp": env.timestamp.isoformat(),
                        "event_type": env.event_type,
                        "source": env.source,
                        "user_id": env.user_id or "",
                        "processing_time_ms": env.processing_time_ms or "",
                        "status_code": env.status_code or "",
                        "error_message": env.error_message or "",
                        "intent": env.envelope_data.get("intent", ""),
                        "payload": json.dumps(env.envelope_data.get("payload", {}))
                    }
                    writer.writerow(row)
            
            csv_content = output.getvalue()
            return Response(
                content=csv_content,
                media_type="text/csv",
                headers={
                    "Content-Disposition": f"attachment; filename=trace_{trace_id}.csv"
                }
            )
        else:
            # Export as JSON (default)
            events = [env.to_dict() for env in envelopes]
            export_data = {
                "trace_id": trace_id,
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
                "exported_by": current_user.get("username"),
                "events_count": len(events),
                "events": events,
                "summary": summary
            }
            
            return Response(
                content=json.dumps(export_data, indent=2),
                media_type="application/json",
                headers={
                    "Content-Disposition": f"attachment; filename=trace_{trace_id}.json"
                }
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting trace: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to export trace: {str(e)}")

@app.get("/replay/statistics")
async def get_replay_statistics_m5(
    current_user: Dict[str, Any] = Depends(verify_token)
):
    """
    Get replay system statistics (M5.3)
    
    Returns statistics about stored traces and events
    """
    try:
        replay_manager = get_replay_manager()
        stats = replay_manager.store.get_statistics()
        
        # Add per-user statistics if admin
        user_roles = current_user.get("roles", [])
        if "admin" in user_roles:
            # Count traces per user
            user_counts = {}
            for trace_id in replay_manager.store.get_trace_ids():
                envelopes = replay_manager.store.get_envelopes_by_trace(trace_id)
                if envelopes and envelopes[0].user_id:
                    user_id = envelopes[0].user_id
                    user_counts[user_id] = user_counts.get(user_id, 0) + 1
            
            stats["traces_by_user"] = user_counts
        
        return {
            "statistics": stats,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime())
        }
        
    except Exception as e:
        logger.error(f"Error getting statistics: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get statistics: {str(e)}")

@app.get("/performance/metrics")
async def get_performance_metrics_m5(
    current_user: Dict[str, Any] = Depends(require_roles(["admin", "operator"]))
):
    """
    Get performance metrics (M5.4)
    Requires admin or operator role
    
    Returns:
    - Latency statistics (p50, p95, p99)
    - Cache hit rates
    - Rate limit statistics
    """
    try:
        perf_monitor = get_performance_monitor()
        auth_cache = get_auth_cache()
        policy_cache = get_policy_cache()
        user_rate_limiter = get_user_rate_limiter()
        
        return {
            "latency": perf_monitor.get_all_stats(),
            "caches": {
                "auth": auth_cache.get_stats(),
                "policy": policy_cache.get_stats()
            },
            "rate_limiting": {
                "user_limiter": {
                    "rate": user_rate_limiter.rate,
                    "per_seconds": user_rate_limiter.per
                }
            },
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime())
        }
    except Exception as e:
        logger.error(f"Error getting performance metrics: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get performance metrics: {str(e)}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080) 
