from __future__ import annotations

import logging
import time
import uuid
import os
from datetime import datetime
from typing import Any, Callable, Dict, MutableMapping, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from opentelemetry import trace

from ..clients import ServiceClients
from ..config import ServiceEndpoints
from ..services import (
    evaluate_capability,
    fetch_core_health,
    fetch_policy_rules,
    readiness_allowed,
)
from unison_common import (
    EnvelopeValidationError,
    get_endpoint_rate_limiter,
    get_performance_monitor,
    get_user_rate_limiter,
    require_consent,
    validate_event_envelope,
)
from unison_common.auth import verify_token
from unison_common.consent import ConsentScopes
from unison_common.logging import log_json
from unison_common.replay_endpoints import store_processing_envelope


Skill = Callable[[Dict[str, Any]], Dict[str, Any]]
SkillsRegistry = Dict[str, Skill]
PendingConfirms = MutableMapping[str, Dict[str, Any]]


def _auth_dependency():
    """Return a test user when auth is disabled, otherwise defer to verify_token."""
    if os.getenv("DISABLE_AUTH_FOR_TESTS", "false").lower() == "true" or os.getenv("PYTEST_CURRENT_TEST"):
        async def _test_user():
            return {"username": "test-user", "roles": ["admin"]}
        return _test_user
    return verify_token


def _ingest_consent_dependency(require_consent_flag: bool):
    if not require_consent_flag:
        async def _optional_consent():
            return None

        return Depends(_optional_consent)

    return Depends(require_consent([ConsentScopes.INGEST_WRITE]))


def register_event_routes(
    app,
    *,
    service_clients: ServiceClients,
    skills: SkillsRegistry,
    metrics: MutableMapping[str, int],
    pending_confirms: PendingConfirms,
    confirm_ttl_seconds: int,
    require_consent_flag: bool,
    prune_pending: Callable[[], None],
    endpoints: ServiceEndpoints,
) -> None:
    api = APIRouter()
    consent_dependency = _ingest_consent_dependency(require_consent_flag)

    @api.post("/event")
    async def handle_event(
        envelope: dict = Body(...),
        current_user: Dict[str, Any] = Depends(_auth_dependency()),
    ):
        metrics["/event"] += 1

        try:
            envelope = validate_event_envelope(envelope)
        except EnvelopeValidationError as e:
            log_json(
                logging.WARNING,
                "envelope_validation_failed",
                service="unison-orchestrator",
                error=str(e),
                user=current_user.get("username"),
                roles=current_user.get("roles", []),
            )
            raise HTTPException(status_code=400, detail=str(e))

        envelope["user"] = {
            "username": current_user.get("username"),
            "roles": current_user.get("roles", []),
            "authenticated": True,
        }

        event_id = str(uuid.uuid4())
        intent = envelope.get("intent", "")
        source = envelope.get("source", "")

        log_json(
            logging.INFO,
            "event_received",
            service="unison-orchestrator",
            event_id=event_id,
            intent=intent,
            source=source,
            user=current_user.get("username"),
            roles=current_user.get("roles", []),
        )

        handler = skills.get(intent)
        if not handler:
            log_json(
                logging.WARNING,
                "unknown_intent",
                service="unison-orchestrator",
                event_id=event_id,
                intent=intent,
                user=current_user.get("username"),
            )
            raise HTTPException(status_code=404, detail=f"Unknown intent: {intent}")

        eval_payload = {
            "capability_id": f"unison.{intent}",
            "context": {
                "actor": current_user.get("username"),
                "intent": intent,
                "source": source,
                "auth_scope": envelope.get("auth_scope"),
                "safety_context": envelope.get("safety_context"),
                "user_roles": current_user.get("roles", []),
            },
        }

        policy_ok, _, policy_body = evaluate_capability(
            service_clients, eval_payload, event_id=event_id
        )

        allowed = False
        decision: Dict[str, Any] = {}
        if policy_ok and isinstance(policy_body, dict):
            decision = policy_body.get("decision", {})
            allowed = decision.get("allowed", False) is True

        if not allowed:
            reason = decision.get("reason", "Policy denied")
            requires_confirmation = decision.get("require_confirmation", False)
            suggested_alternative = decision.get("suggested_alternative")
            if requires_confirmation:
                return {
                    "accepted": False,
                    "require_confirmation": True,
                    "policy_suggested_alternative": suggested_alternative,
                    "decision": decision,
                }
            log_json(
                logging.WARNING,
                "policy_denied",
                service="unison-orchestrator",
                event_id=event_id,
                intent=intent,
                reason=reason,
                user=current_user.get("username"),
                roles=current_user.get("roles", []),
            )
            raise HTTPException(status_code=403, detail=f"Policy denied: {reason}")

        try:
            result = handler(envelope)
            log_json(
                logging.INFO,
                "event_completed",
                service="unison-orchestrator",
                event_id=event_id,
                intent=intent,
                user=current_user.get("username"),
                success=True,
            )
            return {
                "ok": True,
                "event_id": event_id,
                "intent": intent,
                "result": result,
                "user": current_user.get("username"),
            }
        except Exception as exc:
            log_json(
                logging.ERROR,
                "handler_error",
                service="unison-orchestrator",
                event_id=event_id,
                intent=intent,
                error=str(exc),
                user=current_user.get("username"),
            )
            raise HTTPException(status_code=500, detail=f"Handler error: {exc}")

    @api.get("/introspect")
    def introspect():
        eid = str(uuid.uuid4())
        hdrs = {"X-Event-ID": eid}
        services_health = fetch_core_health(service_clients, headers=hdrs)
        ctx_ok, ctx_status, _ = services_health["context"]
        stor_ok, stor_status, _ = services_health["storage"]
        pol_ok, pol_status, _ = services_health["policy"]
        rules_ok, rules_status, rules = fetch_policy_rules(service_clients, headers=hdrs)

        result = {
            "event_id": eid,
            "services": {
                "context": {
                    "ok": ctx_ok,
                    "status": ctx_status,
                    "host": endpoints.context_host,
                    "port": endpoints.context_port,
                },
                "storage": {
                    "ok": stor_ok,
                    "status": stor_status,
                    "host": endpoints.storage_host,
                    "port": endpoints.storage_port,
                },
                "policy": {
                    "ok": pol_ok,
                    "status": pol_status,
                    "host": endpoints.policy_host,
                    "port": endpoints.policy_port,
                },
            },
            "skills": list(skills.keys()),
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

    @api.post("/event/confirm")
    def confirm_event(body: Dict[str, Any] = Body(...)):
        metrics["/event/confirm"] += 1
        prune_pending()
        token = body.get("confirmation_token")
        log_json(
            logging.INFO,
            "confirm_request",
            service="unison-orchestrator",
            token=str(token),
        )
        if not isinstance(token, str) or token not in pending_confirms:
            ok_s, st_s, body_s = service_clients.storage.get(f"/kv/confirm/{token}")
            if not ok_s or st_s >= 400 or not isinstance(body_s, dict) or not body_s.get("ok"):
                raise HTTPException(status_code=404, detail="Invalid or expired confirmation token")
            envelope = body_s.get("envelope")
            if not envelope:
                raise HTTPException(status_code=404, detail="Confirmation token corrupted")
            pending_confirms[token] = {
                "envelope": envelope,
                "matched": None,
                "handler_name": None,
                "created_at": time.time(),
                "expires_at": time.time() + confirm_ttl_seconds,
            }

        data = pending_confirms[token]
        envelope = data["envelope"]
        event_id = str(uuid.uuid4())
        intent = envelope.get("intent", "")

        handler = skills.get(intent)
        if not handler:
            raise HTTPException(status_code=404, detail=f"Intent {intent} not found")

        try:
            result = handler(envelope)
            pending_confirms.pop(token, None)
            service_clients.storage.post(f"/kv/delete/confirm/{token}", {})
            log_json(
                logging.INFO,
                "confirm_completed",
                service="unison-orchestrator",
                event_id=event_id,
                intent=intent,
                token=token,
            )
            return {
                "ok": True,
                "event_id": event_id,
                "intent": intent,
                "result": result,
                "confirmed": True,
            }
        except Exception as exc:
            log_json(
                logging.ERROR,
                "confirm_handler_error",
                service="unison-orchestrator",
                event_id=event_id,
                intent=intent,
                error=str(exc),
                token=token,
            )
            raise HTTPException(status_code=500, detail=f"Handler error: {exc}")

    @api.post("/ingest")
    async def ingest_m4(
        body: Dict[str, Any],
        current_user: Dict[str, Any] = Depends(verify_token),
        consent_grant: Optional[Dict[str, Any]] = consent_dependency,
    ):
        tracer = trace.get_tracer(__name__)
        user_rate_limiter = get_user_rate_limiter()
        endpoint_rate_limiter = get_endpoint_rate_limiter()

        if not user_rate_limiter.is_allowed(current_user.get("username")):
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Please try again later.",
                headers={"Retry-After": "60"},
            )

        if not endpoint_rate_limiter.is_allowed("/ingest"):
            raise HTTPException(
                status_code=429,
                detail="Service temporarily unavailable. Please try again later.",
                headers={"Retry-After": "60"},
            )

        metrics["/ingest"] += 1
        start_time = time.time()
        correlation_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4()).replace("-", "")[:32]

        current_span = trace.get_current_span()
        current_span.set_attribute("user.id", current_user.get("username"))
        current_span.set_attribute("user.roles", ",".join(current_user.get("roles", [])))
        current_span.set_attribute("correlation.id", correlation_id)
        current_span.set_attribute("trace.id", trace_id)

        if require_consent_flag and consent_grant is not None:
            log_json(
                logging.INFO,
                "consent_verified",
                service="unison-orchestrator",
                user=current_user.get("username"),
            )
            current_span.set_attribute("consent.verified", "true")
            current_span.set_attribute(
                "consent.scopes", ",".join(consent_grant.get("scopes", []))
            )

        intent = body.get("intent", "")
        payload = body.get("payload", {})
        source = body.get("source", "test-client")

        current_span.set_attribute("intent.type", intent)
        current_span.set_attribute("request.source", source)

        if not intent:
            raise HTTPException(status_code=400, detail="intent is required")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be an object")

        envelope_data = {
            "intent": intent,
            "payload": payload,
            "source": source,
            "user": current_user.get("username"),
            "roles": current_user.get("roles", []),
        }
        if require_consent_flag and consent_grant is not None:
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
            error_message=None,
        )

        if intent != "echo":
            raise HTTPException(
                status_code=400, detail=f"Unsupported intent: {intent}. Only 'echo' is supported in M2."
            )

        message = payload.get("message", "")
        if not message:
            raise HTTPException(status_code=400, detail="message is required for echo intent")

        store_processing_envelope(
            envelope_data={"intent": intent, "message": message},
            trace_id=trace_id,
            correlation_id=correlation_id,
            event_type="skill_start",
            source="orchestrator",
            user_id=None,
        )

        echo_skill = skills.get("echo")
        if echo_skill is None:
            raise HTTPException(status_code=500, detail="Echo skill is not registered")

        try:
            with tracer.start_as_current_span("skill.echo") as skill_span:
                skill_span.set_attribute("skill.name", "echo")
                skill_span.set_attribute("skill.message", message)
                echo_result = echo_skill({"payload": {"message": message}, "source": source})
                skill_span.set_attribute("skill.result", "success")

            total_duration = (time.time() - start_time) * 1000
            perf_monitor = get_performance_monitor()
            perf_monitor.record("ingest_latency_ms", total_duration)
            perf_monitor.record("skill_execution_ms", total_duration)

            store_processing_envelope(
                envelope_data={"intent": intent, "result": echo_result},
                trace_id=trace_id,
                correlation_id=correlation_id,
                event_type="skill_complete",
                source="orchestrator",
                user_id=None,
                processing_time_ms=total_duration,
                status_code=200,
            )

            return {
                "status": "success",
                "trace_id": trace_id,
                "correlation_id": correlation_id,
                "result": {
                    "intent": intent,
                    "response": echo_result.get("echo", {}).get("message", message),
                    "processed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                },
                "duration_ms": round(total_duration, 2),
            }
        except Exception as exc:
            store_processing_envelope(
                envelope_data={"intent": intent, "error": str(exc)},
                trace_id=trace_id,
                correlation_id=correlation_id,
                event_type="error",
                source="orchestrator",
                user_id=None,
                error_message=str(exc),
                status_code=500,
            )
            raise HTTPException(status_code=500, detail=f"Skill processing failed: {exc}")

    app.include_router(api)
