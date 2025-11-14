from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.security import HTTPBearer
import asyncio
import json
import logging
import time
import uvicorn
from typing import Any, Callable, Dict, List, Optional, Tuple

from opentelemetry import trace

from orchestrator import OrchestratorSettings, ServiceClients, instrument_fastapi, setup_telemetry
from orchestrator.api import register_event_routes
from orchestrator.services import (
    evaluate_capability,
    fetch_core_health,
    fetch_policy_rules,
    readiness_allowed,
)
from unison_common import (
    AuthError,
    PermissionError,
    TracingMiddleware,
    add_security_headers,
    get_auth_cache,
    get_cors_config,
    get_performance_monitor,
    get_policy_cache,
    get_request_id,
    get_user_rate_limiter,
    require_role,
    require_roles,
    verify_consent_grant,
    verify_service_token,
    verify_token,
)
from unison_common.auth import rate_limit
from unison_common.idempotency import IdempotencyConfig, IdempotencyManager, get_idempotency_manager
from unison_common.idempotency_middleware import IdempotencyKeyRequiredMiddleware, IdempotencyMiddleware
from unison_common.logging import configure_logging, log_json
from unison_common.replay_store import ReplayConfig, get_replay_manager, initialize_replay
from router import RouteCandidate, Router, RoutingContext, RoutingStrategy
import uuid
from collections import defaultdict

# Initialize telemetry before the app boots
setup_telemetry()

app = FastAPI(
    title="unison-orchestrator",
    description="Orchestration service for Unison platform",
    version="1.0.0"
)

# M5.1: Instrument FastAPI with OpenTelemetry
instrument_fastapi(app)

logger = configure_logging("unison-orchestrator")
settings = OrchestratorSettings.from_env()
endpoints = settings.endpoints
service_clients = ServiceClients.from_endpoints(endpoints)
CONTEXT_HOST = endpoints.context_host
CONTEXT_PORT = endpoints.context_port
STORAGE_HOST = endpoints.storage_host
STORAGE_PORT = endpoints.storage_port
POLICY_HOST = endpoints.policy_host
POLICY_PORT = endpoints.policy_port
INFERENCE_HOST = endpoints.inference_host
INFERENCE_PORT = endpoints.inference_port
CONFIRM_TTL_SECONDS = settings.confirm_ttl_seconds
REQUIRE_CONSENT = settings.require_consent

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
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.allowed_hosts
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
router = Router(RoutingStrategy(settings.routing_strategy))

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
    ok, status, body = service_clients.context.post("/kv/get", {"keys": keys})
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
    ok, status, body = service_clients.storage.put(f"/kv/{key}", {"value": value})
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
        ok, status, body = service_clients.policy.get("/health")
        checks["policy"] = {"ready": ok, "status": status}
        if not ok:
            overall_ready = False
    except Exception as e:
        checks["policy"] = {"ready": False, "error": str(e)}
        overall_ready = False
    
    # Check context service
    try:
        ok, status, body = service_clients.context.get("/health")
        checks["context"] = {"ready": ok, "status": status}
        if not ok:
            overall_ready = False
    except Exception as e:
        checks["context"] = {"ready": False, "error": str(e)}
        overall_ready = False
    
    # Check storage service
    try:
        ok, status, body = service_clients.storage.get("/health")
        checks["storage"] = {"ready": ok, "status": status}
        if not ok:
            overall_ready = False
    except Exception as e:
        checks["storage"] = {"ready": False, "error": str(e)}
        overall_ready = False
    
    # Check inference service
    try:
        ok, status, body = service_clients.inference.get("/health")
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
    services_health = fetch_core_health(service_clients, headers=hdrs)
    context_ok, _, _ = services_health["context"]
    storage_ok, _, _ = services_health["storage"]
    inference_ok, _, _ = services_health["inference"]
    allowed = readiness_allowed(service_clients, event_id=rid)

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

*** End Patch  notificatio
    "echo": _handler_echo,
    "inference": _handler_inference,
    "summarize_doc": _handler_summarize_doc,
    "context_get": _handler_context_get,
    "storage_put": _handler_storage_put,
}

# Skills already registered above (lines 174-180)

register_event_routes(
    app,
    service_clients=service_clients,
    skills=_skills,
    metrics=_metrics,
    pending_confirms=_pending_confirms,
    confirm_ttl_seconds=CONFIRM_TTL_SECONDS,
    require_consent_flag=REQUIRE_CONSENT,
    prune_pending=_prune_pending,
    endpoints=endpoints,
)

# --- M4: Golden Path v1 - /ingest with authentication ---
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
