from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Dict, MutableMapping

import httpx
from fastapi import APIRouter, Body, HTTPException

from ..context_client import fetch_core_health
from ..policy_client import fetch_policy_rules, readiness_allowed
from ..skills import SkillHandler
from ..config import OrchestratorSettings
from ..clients import ServiceClients
from ..router import RoutingContext, Router
from unison_common.logging import log_json


def _live_renderer_check(renderer_url: str | None) -> Dict[str, Any]:
    if not renderer_url:
        return {"ready": False, "status": 503, "url": renderer_url}
    try:
        with httpx.Client(timeout=1.5) as client:
            resp = client.get(f"{renderer_url.rstrip('/')}/ready")
        if resp.status_code != 200:
            return {"ready": False, "status": resp.status_code, "url": renderer_url}
        body = resp.json() or {}
        return {
            "ready": bool(body.get("ready")),
            "status": 200 if bool(body.get("ready")) else 503,
            "url": renderer_url,
        }
    except Exception as exc:
        return {"ready": False, "status": 503, "error": str(exc), "url": renderer_url}


def _live_auth_check() -> Dict[str, Any]:
    auth_base = os.getenv("UNISON_AUTH_URL")
    if not auth_base:
        host = os.getenv("UNISON_AUTH_HOST")
        port = os.getenv("UNISON_AUTH_PORT")
        if host and port:
            auth_base = f"http://{host}:{port}"
    if not auth_base:
        auth_base = "http://auth:8083"

    url = f"{auth_base.rstrip('/')}/bootstrap/status"
    try:
        with httpx.Client(timeout=1.5) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return {"ready": False, "status": resp.status_code, "url": url}
        body = resp.json() or {}
        bootstrap_required = bool(body.get("bootstrap_required"))
        return {
            "ready": not bootstrap_required,
            "status": 200,
            "bootstrap_required": bootstrap_required,
            "enabled": bool(body.get("enabled")),
            "admin_exists": bool(body.get("admin_exists")),
            "url": url,
        }
    except Exception as exc:
        return {"ready": False, "status": 503, "error": str(exc), "url": url}


def register_admin_routes(
    app,
    *,
    service_clients: ServiceClients,
    metrics: MutableMapping[str, int],
    skills: Dict[str, SkillHandler],
    router: Router,
    start_time: float,
) -> None:
    router_api = APIRouter()

    @router_api.get("/health")
    def health():
        metrics["/health"] += 1
        log_json(logging.INFO, "health", service="unison-orchestrator")
        return {"status": "ok", "service": "unison-orchestrator"}

    @router_api.get("/readyz")
    def readiness():
        metrics["/readyz"] += 1
        checks: Dict[str, Dict[str, Any]] = {}
        overall_ready = True

        def _record(name: str, call):
            nonlocal overall_ready
            try:
                ok, status, _ = call
                checks[name] = {"ready": ok, "status": status}
                if not ok:
                    overall_ready = False
            except Exception as exc:  # pragma: no cover - defensive logging
                checks[name] = {"ready": False, "error": str(exc)}
                overall_ready = False

        _record("policy", service_clients.policy.get("/health"))
        _record("context", service_clients.context.get("/health"))
        _record("storage", service_clients.storage.get("/health"))
        _record("inference", service_clients.inference.get("/health"))

        checks["skills"] = {"ready": len(skills) > 0, "count": len(skills)}
        if not skills:
            overall_ready = False

        log_json(
            logging.INFO,
            "readiness",
            service="unison-orchestrator",
            ready=overall_ready,
            checks=checks,
        )
        return {"ready": overall_ready, "checks": checks}

    @router_api.get("/metrics")
    def metrics_endpoint():
        uptime = time.time() - start_time
        lines = [
            "# HELP unison_orchestrator_requests_total Total number of requests by endpoint",
            "# TYPE unison_orchestrator_requests_total counter",
        ]
        for key, value in metrics.items():
            lines.append(f'unison_orchestrator_requests_total{{endpoint="{key}"}} {value}')
        lines.extend(
            [
                "",
                "# HELP unison_orchestrator_uptime_seconds Service uptime in seconds",
                "# TYPE unison_orchestrator_uptime_seconds gauge",
                f"unison_orchestrator_uptime_seconds {uptime}",
                "",
                "# HELP unison_orchestrator_skills_registered Number of registered skills",
                "# TYPE unison_orchestrator_skills_registered gauge",
                f"unison_orchestrator_skills_registered {len(skills)}",
            ]
        )
        return "\n".join(lines)

    @router_api.get("/router/config")
    def get_router_config():
        return {
            "strategy": router.get_strategy_name(),
            "metrics": router.get_metrics(),
        }

    @router_api.post("/router/strategy")
    def set_routing_strategy(strategy: str = Body(..., embed=True)):
        try:
            router.set_strategy(router.strategy.__class__(strategy))
            log_json(
                logging.INFO,
                "router_strategy_changed",
                service="unison-orchestrator",
                strategy=strategy,
            )
            return {"ok": True, "strategy": strategy}
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid strategy: {strategy}")

    @router_api.post("/router/rules")
    def add_routing_rule(rule: Dict[str, Any] = Body(...)):
        try:
            router.add_routing_rule(rule)
            log_json(
                logging.INFO,
                "routing_rule_added",
                service="unison-orchestrator",
                rule_id=rule.get("id"),
                intent_prefix=rule.get("intent_prefix"),
            )
            return {"ok": True, "rule": rule}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router_api.get("/router/rules")
    def get_routing_rules():
        if hasattr(router.router, "rules"):
            return {"rules": router.router.rules}
        return {"rules": [], "message": "Current strategy does not support rules"}

    @router_api.post("/router/test")
    def test_routing(request_body: Dict[str, Any] = Body(...)):
        intent = request_body.get("intent", "")
        payload = request_body.get("payload", {})
        user = request_body.get("user", {"username": "test", "roles": ["user"]})
        source = request_body.get("source", "test")

        context = RoutingContext(
            intent=intent,
            payload=payload,
            user=user,
            source=source,
            event_id=str(uuid.uuid4()),
            timestamp=time.time(),
        )

        candidate = router.route(context, skills)
        if candidate:
            return {
                "routed": True,
                "skill_id": candidate.skill_id,
                "strategy_used": candidate.strategy_used,
                "score": candidate.score,
                "metadata": candidate.metadata,
            }
        return {"routed": False, "message": "No matching skill found"}

    @router_api.get("/ready")
    def ready():
        rid = str(uuid.uuid4())
        headers = {"X-Event-ID": rid}
        services_health = fetch_core_health(service_clients, headers=headers)
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
        log_json(
            logging.INFO,
            "readiness",
            service="unison-orchestrator",
            event_id=rid,
            context=context_ok,
            storage=storage_ok,
            inference=inference_ok,
            policy_allowed=allowed,
            ready=all_ok,
        )
        return resp

    @router_api.get("/startup/status")
    def startup_status():
        metrics["/startup/status"] += 1
        poweron = getattr(app.state, "poweron", None)
        poweron_error = getattr(app.state, "poweron_error", None)
        task = getattr(app.state, "poweron_task", None)

        if poweron is None:
            return {
                "ok": poweron_error is None,
                "state": "starting" if poweron_error is None and task is not None else "unavailable",
                "error": poweron_error,
            }

        checks = getattr(poweron, "checks", {}) or {}
        manifest = getattr(poweron, "manifest", {}) or {}
        speech = manifest.get("io", {}).get("speech", {}) if isinstance(manifest.get("io"), dict) else {}
        renderer_url = getattr(poweron, "renderer_url", None)
        renderer_check = _live_renderer_check(renderer_url)
        auth_check = _live_auth_check()
        checks["renderer"] = renderer_check
        checks["auth"] = auth_check

        renderer_ready = bool(renderer_check.get("ready"))
        core_ready = bool(getattr(poweron, "core_ready", False))
        speech_ready = bool(getattr(poweron, "speech_ready", False))
        bootstrap_required = bool(auth_check.get("bootstrap_required", False))
        auth_ready = bool(auth_check.get("ready", False))
        onboarding_required = bootstrap_required or not renderer_ready or not core_ready or not speech_ready

        stage = "READY_LISTENING"
        if bootstrap_required:
            stage = "AUTH_BOOTSTRAP_REQUIRED"
        elif not core_ready:
            stage = "CORE_SERVICES_DEGRADED"
        elif not speech_ready:
            stage = "SPEECH_UNAVAILABLE"
        elif not renderer_ready:
            stage = "RENDERER_UNAVAILABLE"

        return {
            "ok": not onboarding_required,
            "state": stage,
            "onboarding_required": onboarding_required,
            "bootstrap_required": bootstrap_required,
            "renderer_ready": renderer_ready,
            "core_ready": core_ready,
            "auth_ready": auth_ready,
            "speech_ready": speech_ready,
            "speech_reason": getattr(poweron, "speech_reason", None),
            "speech_ws_endpoint": speech.get("ws_endpoint") if isinstance(speech, dict) else None,
            "speech_endpointing": speech.get("endpointing") if isinstance(speech, dict) else None,
            "speech_asr_profile": speech.get("default_asr_profile") if isinstance(speech, dict) else None,
            "renderer_url": renderer_url,
            "checks": checks,
            "trace_id": getattr(poweron, "trace_id", None),
        }

    app.include_router(router_api)
