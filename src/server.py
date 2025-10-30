from fastapi import FastAPI, HTTPException, Body, Request
import uvicorn
import os
import httpx
import json
from typing import Any, Dict, Tuple, List, Callable
from unison_common import validate_event_envelope, EnvelopeValidationError
from unison_common.logging import configure_logging, log_json
from unison_common.http_client import http_post_json_with_retry, http_get_json_with_retry, http_put_json_with_retry
import logging
import uuid
import time
from collections import defaultdict

app = FastAPI(title="unison-orchestrator")

logger = configure_logging("unison-orchestrator")

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

logger = configure_logging("unison-orchestrator")

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

# Register built-in skills
_skills["echo"] = _handler_echo
_skills["summarize.doc"] = _handler_inference
_skills["analyze.code"] = _handler_inference
_skills["translate.text"] = _handler_inference
_skills["generate.idea"] = _handler_inference

# --- Skill registry endpoints ---
@app.get("/skills")
def list_skills(request: Request):
    event_id = request.headers.get("X-Event-ID")
    log_json(logging.INFO, "skills_list", service="unison-orchestrator", event_id=event_id, count=len(_skills), intents=list(_skills.keys()))
    return {"ok": True, "skills": list(_skills.keys()), "count": len(_skills)}

@app.post("/skills")
def register_skill(request: Request, body: Dict[str, Any] = Body(...)):
    event_id = request.headers.get("X-Event-ID")
    intent = body.get("intent")
    if not isinstance(intent, str) or not intent:
        raise HTTPException(status_code=400, detail="Invalid or missing 'intent'")
    # In a real implementation we would validate a callable reference; for MVP we only support built-in intents
    if intent in _skills:
        raise HTTPException(status_code=409, detail=f"Intent '{intent}' already registered")
    # MVP: only allow pre-defined built-in intents
    allowed_builtin = {"summarize.doc", "context.get", "storage.put"}
    if intent not in allowed_builtin:
        raise HTTPException(status_code=400, detail=f"Intent '{intent}' not supported in MVP")
    # Map to built-in handlers
    if intent == "summarize.doc":
        _skills[intent] = _handler_summarize_doc
    elif intent == "context.get":
        _skills[intent] = _handler_context_get
    elif intent == "storage.put":
        _skills[intent] = _handler_storage_put
    log_json(logging.INFO, "skill_registered", service="unison-orchestrator", event_id=event_id, intent=intent)
    return {"ok": True, "intent": intent}

# --- Built-in skill handlers ---
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
    namespace = payload.get("namespace")
    key = payload.get("key")
    value = payload.get("value")
    if not isinstance(namespace, str) or not isinstance(key, str):
        raise ValueError("storage.put requires 'namespace' and 'key' in payload")
    # Call Storage service KV PUT
    ok, status, body = http_put_json(STORAGE_HOST, STORAGE_PORT, f"/kv/{namespace}/{key}", {"value": value})
    if not ok or not isinstance(body, dict):
        raise RuntimeError(f"Storage service error: {status}")
    return body

# --- POL-002: Pending confirmations (in-memory) ---
_pending_confirms: Dict[str, Dict[str, Any]] = {}

def _prune_pending(now: float | None = None) -> None:
    now = now or time.time()
    if not _pending_confirms:
        return
    expired = [tok for tok, rec in _pending_confirms.items() if now - float(rec.get("created_at", now)) > CONFIRM_TTL_SECONDS]
    for tok in expired:
        _pending_confirms.pop(tok, None)

@app.get("/skills")
def list_skills():
    return {"skills": _skills}

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
def handle_event(envelope: dict = Body(...)):
    _metrics["/event"] += 1
    try:
        envelope = validate_event_envelope(envelope)
    except EnvelopeValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    capability_id = envelope["intent"]
    event_id = str(uuid.uuid4())
    log_json(logging.INFO, "event_received", service="unison-orchestrator", event_id=event_id, source=envelope.get("source"), intent=capability_id)
    steps: List[Dict[str, Any]] = []
    steps.append({"step": "validate", "ok": True})
    policy_context = {
        "actor": envelope.get("source", "unknown"),
        "payload_preview": envelope.get("payload", {}),
        "auth_scope": envelope.get("auth_scope", None),
        "safety_context": envelope.get("safety_context", None),
    }

    ok, status_code, policy_body = http_post_json(
        POLICY_HOST,
        POLICY_PORT,
        "/evaluate",
        {"capability_id": capability_id, "context": policy_context},
        headers={"X-Event-ID": event_id},
    )

    allowed = False
    require_confirmation = False
    reason = "no-decision"
    suggested_alternative: str | None = None

    if ok and isinstance(policy_body, dict):
        decision_block = policy_body.get("decision", {})
        allowed = bool(decision_block.get("allowed", False))
        require_confirmation = bool(decision_block.get("require_confirmation", False))
        reason = decision_block.get("reason", reason)
        suggested_alternative = decision_block.get("suggested_alternative")

    steps.append({"step": "policy", "ok": bool(allowed), "status": status_code, "reason": reason, "suggested_alternative": suggested_alternative})

    if not allowed:
        _prune_pending()
        # Require confirmation path
        if require_confirmation:
            token = str(uuid.uuid4())
            _pending_confirms[token] = {
                "envelope": envelope,
                "matched": None,
                "handler_name": None,
                "created_at": time.time(),
                "expires_at": time.time() + CONFIRM_TTL_SECONDS,
            }
            # Pre-compute matching so confirm can be deterministic
            if capability_id in _skills:
                _pending_confirms[token]["handler_name"] = capability_id
            else:
                _pending_confirms[token]["handler_name"] = "echo"

            # Persist to storage for durability
            rec = {
                "envelope": envelope,
                "matched": _pending_confirms[token].get("matched"),
                "handler_name": _pending_confirms[token].get("handler_name"),
                "created_at": _pending_confirms[token]["created_at"],
                "expires_at": _pending_confirms[token]["expires_at"],
                "used": False,
            }
            _ok, _st, _ = http_put_json(
                STORAGE_HOST,
                STORAGE_PORT,
                f"/kv/confirm/{token}",
                {"value": rec},
                headers={"X-Event-ID": event_id},
            )

            log_json(logging.INFO, "event_requires_confirmation", service="unison-orchestrator", event_id=event_id, intent=capability_id, reason=reason, token=token, suggested_alternative=suggested_alternative)
            return {
                "accepted": False,
                "reason": reason,
                "require_confirmation": True,
                "confirmation_token": token,
                "confirmation_ttl_seconds": CONFIRM_TTL_SECONDS,
                "policy_status": status_code,
                "policy_raw": policy_body,
                "policy_suggested_alternative": suggested_alternative,
                "event_id": event_id,
                "steps": steps,
            }

        # Hard deny path
        log_json(logging.INFO, "event_blocked", service="unison-orchestrator", event_id=event_id, intent=capability_id, reason=reason, require_confirmation=False, policy_status=status_code, suggested_alternative=suggested_alternative)
        return {
            "accepted": False,
            "reason": reason,
            "require_confirmation": False,
            "policy_status": status_code,
            "policy_raw": policy_body,
            "policy_suggested_alternative": suggested_alternative,
            "event_id": event_id,
            "steps": steps,
        }

    # Route to a registered skill by intent; default to echo
    handler_name = capability_id if capability_id in _skills else "echo"
    handler_fn = _skills.get(handler_name, _handler_echo)
    # Optional context fetch before handling (no matched metadata in this MVP)
    fetched_context: Dict[str, Any] | None = None
    # If source is io-speech or io-vision, we may want to attach transcript or image_url to outputs for downstream response
    source = envelope.get("source", "")
    if source == "io-speech" and "transcript" in envelope.get("payload", {}):
        outputs = handler_fn(envelope)
        if isinstance(outputs, dict):
            outputs["transcript"] = envelope["payload"]["transcript"]
    elif source == "io-vision" and "image_url" in envelope.get("payload", {}):
        outputs = handler_fn(envelope)
        if isinstance(outputs, dict):
            outputs["image_url"] = envelope["payload"]["image_url"]
    else:
        outputs = handler_fn(envelope)
    if fetched_context is not None and isinstance(outputs, dict):
        outputs.setdefault("context", fetched_context)

    steps.append({"step": "handler", "ok": True, "name": handler_name})
    log_json(logging.INFO, "event_accepted", service="unison-orchestrator", event_id=event_id, intent=capability_id, policy_status=status_code, require_confirmation=require_confirmation, handler=handler_name, suggested_alternative=suggested_alternative)
    return {
        "accepted": True,
        "routed_intent": capability_id,
        "payload": envelope["payload"],
        "policy_status": status_code,
        "policy_require_confirmation": require_confirmation,
        "policy_suggested_alternative": suggested_alternative,
        "explanation": "stub handler executed",
        "handled_by": handler_name,
        "outputs": outputs,
        "event_id": event_id,
        "steps": steps,
    }


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
            log_json(logging.INFO, "confirm_load_failed", service="unison-orchestrator", token=token, storage_ok=ok_s, status=st_s, body_type=type(body_s).__name__)
            raise HTTPException(status_code=400, detail="invalid or expired token")
        value = body_s.get("value") or {}
        # Validate token not used/expired
        now = time.time()
        try:
            exp = float(value.get("expires_at") or now)
        except Exception:
            exp = now
        if bool(value.get("used")) or now > exp:
            log_json(logging.INFO, "confirm_invalid", service="unison-orchestrator", token=token, used=bool(value.get("used")), now=now, exp=exp)
            raise HTTPException(status_code=400, detail="invalid or expired token")
        # Use storage-loaded record directly (no in-memory requirement)
        pending = {
            "envelope": value.get("envelope", {}),
            "matched": value.get("matched"),
            "handler_name": value.get("handler_name"),
            "created_at": value.get("created_at", now),
            "expires_at": exp,
        }
        log_json(logging.INFO, "confirm_loaded_from_storage", service="unison-orchestrator", token=token)
    else:
        # Use and remove in-memory pending
        pending = _pending_confirms.pop(token)
    envelope: Dict[str, Any] = pending.get("envelope", {})
    capability_id = envelope.get("intent")
    event_id = str(uuid.uuid4())
    steps: List[Dict[str, Any]] = [{"step": "confirm", "ok": True}]

    # Execute the same routing as /event but skip policy
    matched = pending.get("matched")
    handler_name = pending.get("handler_name") or "echo"
    handler_fn = _skills.get(handler_name, _handler_echo)

    # Optional context fetch per stored match
    fetched_context: Dict[str, Any] | None = None
    if matched and isinstance(matched.get("context_keys"), list) and matched["context_keys"]:
        ctx_ok, ctx_status, ctx_body = http_post_json(
            CONTEXT_HOST,
            CONTEXT_PORT,
            "/kv/get",
            {"keys": matched["context_keys"]},
            headers={"X-Event-ID": event_id},
        )
        if ctx_ok and isinstance(ctx_body, dict) and ctx_body.get("ok"):
            fetched_context = ctx_body.get("values", {})
            steps.append({"step": "context_get", "ok": True, "keys": len(matched["context_keys"])})
        else:
            steps.append({"step": "context_get", "ok": False, "status": ctx_status})

    outputs = handler_fn(envelope)
    if fetched_context is not None and isinstance(outputs, dict):
        outputs.setdefault("context", fetched_context)

    steps.append({"step": "handler", "ok": True, "name": handler_name})
    log_json(logging.INFO, "event_confirmed", service="unison-orchestrator", event_id=event_id, intent=capability_id, handled_by=handler_name)

    # Mark token as used in storage (best-effort)
    try:
        used_rec = {
            "envelope": envelope,
            "matched": matched,
            "handler_name": handler_name,
            "created_at": pending.get("created_at"),
            "expires_at": pending.get("expires_at"),
            "used": True,
            "used_at": time.time(),
        }
        http_put_json(STORAGE_HOST, STORAGE_PORT, f"/kv/confirm/{token}", {"value": used_rec}, headers={"X-Event-ID": event_id})
    except Exception:
        pass
    return {
        "accepted": True,
        "routed_intent": capability_id,
        "payload": envelope.get("payload"),
        "explanation": "confirmed and executed",
        "handled_by": handler_name,
        "outputs": outputs,
        "event_id": event_id,
        "steps": steps,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
