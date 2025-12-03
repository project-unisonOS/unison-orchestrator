from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
import logging
import time
import uvicorn
import os
from datetime import datetime
from typing import Any, Dict

from orchestrator import OrchestratorSettings, ServiceClients, instrument_fastapi, setup_telemetry
from orchestrator.api import register_event_routes
from orchestrator.api.admin import register_admin_routes
from orchestrator.api.skills import register_skill_routes
from orchestrator.api.voice import register_voice_routes
from orchestrator.api.payments import register_payment_routes
from orchestrator.replay import configure_replay_store, register_replay_routes
from orchestrator.skills import build_skill_state
from unison_common import (
    AuthError,
    PermissionError,
    BatonMiddleware,
    BatonService,
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
)
from unison_common.auth import rate_limit
from unison_common.idempotency import IdempotencyConfig, IdempotencyManager, get_idempotency_manager
from unison_common.idempotency_middleware import IdempotencyKeyRequiredMiddleware, IdempotencyMiddleware
from unison_common.logging import configure_logging
from router import Router, RoutingStrategy
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
# Allow FastAPI's TestClient default host to pass TrustedHostMiddleware checks
if "testserver" not in settings.allowed_hosts:
    settings.allowed_hosts.append("testserver")
endpoints = settings.endpoints
service_clients = ServiceClients.from_endpoints(endpoints)
app.state.service_clients = service_clients
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
configure_replay_store()

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

# Context baton validation (optional; only enforces signature/expiry if header is present)
app.add_middleware(BatonMiddleware)
logger.info("Context baton middleware enabled (optional validation)")

# Multimodal capabilities
try:
    from unison_common.multimodal import CapabilityClient
    _capabilities = CapabilityClient.from_env()
    _capabilities.refresh()
    publish_capabilities_to_context()
except Exception:
    _capabilities = None

# Push manifest into context-graph at startup
def publish_capabilities_to_context():
    if not _capabilities:
        return
    try:
        manifest = _capabilities.manifest or {}
        ok, status, _ = service_clients.context.post("/capabilities", manifest)
        if not ok:
            logger.warning("Failed to publish capabilities to context-graph: status=%s", status)
    except Exception as exc:
        logger.warning("Error publishing capabilities to context-graph: %s", exc)

# M4: Initialize idempotency manager
idempotency_config = IdempotencyConfig()
idempotency_config.ttl_seconds = 24 * 60 * 60  # 24 hours
# Note: Using in-memory store for M4; Redis integration can be added later
app.add_middleware(IdempotencyMiddleware, ttl_seconds=idempotency_config.ttl_seconds)

# Add idempotency key requirement for critical endpoints (optional)
if os.getenv("ENABLE_IDEMPOTENCY_REQUIRED", "false").lower() == "true":
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

skill_state = build_skill_state(service_clients)
_skills: Dict[str, Any] = skill_state["skills"]
_skill_handlers = skill_state["handlers"]
_companion_manager = skill_state.get("companion_manager")

# Legacy helper expected in tests; routes use ServiceClients for HTTP calls.
def http_post_json(host, port, path, payload, headers=None):
    from unison_common.http_client import http_post_json_with_retry

    return http_post_json_with_retry(host, port, path, payload, headers=headers)

# --- Confirmation tracking ---
_pending_confirms: Dict[str, Dict[str, Any]] = {}

def _prune_pending():
    now = time.time()
    expired = [tok for tok, data in _pending_confirms.items() if data.get("expires_at", 0) < now]
    for tok in expired:
        _pending_confirms.pop(tok, None)

register_skill_routes(
    app,
    skills=_skills,
    handlers=_skill_handlers,
    metrics=_metrics,
)

register_admin_routes(
    app,
    service_clients=service_clients,
    metrics=_metrics,
    skills=_skills,
    router=router,
    start_time=_start_time,
)

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
if _companion_manager:
    register_voice_routes(
        app,
        companion_manager=_companion_manager,
        service_clients=service_clients,
        metrics=_metrics,
    )

# Payments API (Phase 1 mock provider)
_payment_service = register_payment_routes(app, metrics=_metrics, service_clients=service_clients)

register_replay_routes(app)

@app.get("/capabilities")
async def capabilities():
    if not _capabilities:
        raise HTTPException(status_code=503, detail="capabilities unavailable")
    manifest = _capabilities.manifest or {}
    return {"manifest": manifest, "displays": _capabilities.modality_count("displays")}


def publish_capabilities_to_context():
    if not _capabilities:
        return
    try:
        manifest = _capabilities.manifest or {}
        ok, status, _ = service_clients.context.post("/capabilities", manifest)
        if not ok:
            logger.warning("Failed to publish capabilities to context-graph: status=%s", status)
    except Exception as exc:
        logger.warning("Error publishing capabilities to context-graph: %s", exc)


@app.on_event("startup")
def _publish_manifest_startup():
    publish_capabilities_to_context()

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
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        }
    except Exception as e:
        logger.error(f"Error getting performance metrics: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get performance metrics: {str(e)}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080) 
