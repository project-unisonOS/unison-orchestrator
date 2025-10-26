from fastapi import FastAPI, HTTPException, Body
import uvicorn
import os
import httpx
import json
from typing import Any, Dict, Tuple, List, Callable
from unison_common import validate_event_envelope, EnvelopeValidationError
import logging
import uuid
import time

app = FastAPI(title="unison-orchestrator")

CONTEXT_HOST = os.getenv("UNISON_CONTEXT_HOST", "context")
CONTEXT_PORT = os.getenv("UNISON_CONTEXT_PORT", "8081")
STORAGE_HOST = os.getenv("UNISON_STORAGE_HOST", "storage")
STORAGE_PORT = os.getenv("UNISON_STORAGE_PORT", "8082")
POLICY_HOST = os.getenv("UNISON_POLICY_HOST", "policy")
POLICY_PORT = os.getenv("UNISON_POLICY_PORT", "8083")
CONFIRM_TTL_SECONDS = int(os.getenv("UNISON_CONFIRM_TTL", "300"))

def http_get_json(host: str, port: str, path: str, headers: Dict[str, str] | None = None) -> Tuple[bool, int, dict | None]:
    try:
        url = f"http://{host}:{port}{path}"
        with httpx.Client(timeout=1.0) as client:
            resp = client.get(url, headers=headers or {})
        body = None
        try:
            body = resp.json()
        except Exception:
            body = None
        return (resp.status_code == 200, resp.status_code, body)
    except Exception:
        return (False, 0, None)


def http_post_json(host: str, port: str, path: str, payload: dict, headers: Dict[str, str] | None = None) -> Tuple[bool, int, dict | None]:
    try:
        url = f"http://{host}:{port}{path}"
        merged_headers = {"Accept": "application/json"}
        if headers:
            merged_headers.update(headers)
        with httpx.Client(timeout=1.0) as client:
            resp = client.post(url, json=payload, headers=merged_headers)
        parsed = None
        try:
            parsed = resp.json()
        except Exception:
            parsed = None
        return (resp.status_code >= 200 and resp.status_code < 300, resp.status_code, parsed)
    except Exception:
        return (False, 0, None)


def http_put_json(host: str, port: str, path: str, payload: dict, headers: Dict[str, str] | None = None) -> Tuple[bool, int, dict | None]:
    try:
        url = f"http://{host}:{port}{path}"
        merged_headers = {"Accept": "application/json"}
        if headers:
            merged_headers.update(headers)
        with httpx.Client(timeout=1.0) as client:
            resp = client.put(url, json=payload, headers=merged_headers)
        body = None
        try:
            body = resp.json()
        except Exception:
            body = None
        return (resp.status_code >= 200 and resp.status_code < 300, resp.status_code, body)
    except Exception:
        return (False, 0, None)


logger = logging.getLogger("unison-orchestrator")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

def log_json(level: int, message: str, **fields: Any) -> None:
    record = {"ts": time.time(), "service": "unison-orchestrator", "message": message}
    record.update(fields)
    logger.log(level, json.dumps(record, separators=(",", ":")))


# --- ORCH-001: Skill/Intent registry (in-memory) ---
Skill = Dict[str, Any]
_skills: List[Skill] = []

def _handler_echo(envelope: Dict[str, Any]) -> Dict[str, Any]:
    return {"echo": envelope.get("payload", {})}

_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "echo": _handler_echo,
}

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
    log_json(logging.INFO, "skill_added", intent_prefix=prefix, handler=handler_name)
    return {"ok": True, "skill": entry}


@app.get("/health")
def health():
    return {"status": "ok", "service": "unison-orchestrator"}


@app.get("/ready")
def ready():
    rid = str(uuid.uuid4())
    hdrs = {"X-Event-ID": rid}
    context_ok, _, _ = http_get_json(CONTEXT_HOST, CONTEXT_PORT, "/health", headers=hdrs)
    storage_ok, _, _ = http_get_json(STORAGE_HOST, STORAGE_PORT, "/health", headers=hdrs)

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

    all_ok = context_ok and storage_ok and allowed

    resp = {
        "ready": all_ok,
        "deps": {
            "context": context_ok,
            "storage": storage_ok,
            "policy_allowed_action": allowed,
        },
        "event_id": rid,
    }
    log_json(logging.INFO, "readiness", event_id=rid, context=context_ok, storage=storage_ok, policy_allowed=allowed, ready=all_ok)
    return resp


@app.post("/event")
def handle_event(envelope: dict = Body(...)):
    try:
        envelope = validate_event_envelope(envelope)
    except EnvelopeValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    capability_id = envelope["intent"]
    event_id = str(uuid.uuid4())
    log_json(logging.INFO, "event_received", event_id=event_id, source=envelope.get("source"), intent=capability_id)
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

    if ok and isinstance(policy_body, dict):
        decision_block = policy_body.get("decision", {})
        allowed = bool(decision_block.get("allowed", False))
        require_confirmation = bool(decision_block.get("require_confirmation", False))
        reason = decision_block.get("reason", reason)

    steps.append({"step": "policy", "ok": bool(allowed), "status": status_code, "reason": reason})

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
            for s in _skills:
                pref = s.get("intent_prefix", "")
                if isinstance(pref, str) and capability_id.startswith(pref):
                    _pending_confirms[token]["matched"] = s
                    _pending_confirms[token]["handler_name"] = s.get("handler", "echo")
                    break

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

            log_json(logging.INFO, "event_requires_confirmation", event_id=event_id, intent=capability_id, reason=reason, token=token)
            return {
                "accepted": False,
                "reason": reason,
                "require_confirmation": True,
                "confirmation_token": token,
                "confirmation_ttl_seconds": CONFIRM_TTL_SECONDS,
                "policy_status": status_code,
                "policy_raw": policy_body,
                "event_id": event_id,
                "steps": steps,
            }

        # Hard deny path
        log_json(logging.INFO, "event_blocked", event_id=event_id, intent=capability_id, reason=reason, require_confirmation=False, policy_status=status_code)
        return {
            "accepted": False,
            "reason": reason,
            "require_confirmation": False,
            "policy_status": status_code,
            "policy_raw": policy_body,
            "event_id": event_id,
            "steps": steps,
        }

    # Route to a registered handler by intent prefix; default to echo
    matched: Skill | None = None
    for s in _skills:
        pref = s.get("intent_prefix", "")
        if isinstance(pref, str) and capability_id.startswith(pref):
            matched = s
            break
    handler_name = (matched or {}).get("handler", "echo")
    handler_fn = _HANDLERS.get(handler_name, _handler_echo)
    # Optional context fetch before handling
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
    log_json(logging.INFO, "event_accepted", event_id=event_id, intent=capability_id, policy_status=status_code, require_confirmation=require_confirmation, handler=handler_name)
    return {
        "accepted": True,
        "routed_intent": capability_id,
        "payload": envelope["payload"],
        "policy_status": status_code,
        "policy_require_confirmation": require_confirmation,
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
        "skills": _skills,
        "policy_rules": {
            "ok": rules_ok,
            "status": rules_status,
            "summary": rules if isinstance(rules, dict) else None,
        },
    }
    log_json(
        logging.INFO,
        "introspect",
        event_id=eid,
        context_ok=ctx_ok,
        storage_ok=stor_ok,
        policy_ok=pol_ok,
        rules=rules.get("count") if isinstance(rules, dict) else None,
    )
    return result


@app.post("/event/confirm")
def confirm_event(body: Dict[str, Any] = Body(...)):
    _prune_pending()
    token = body.get("confirmation_token")
    log_json(logging.INFO, "confirm_request", token=str(token))
    if not isinstance(token, str) or token not in _pending_confirms:
        # Attempt to load from storage for durability across restarts
        ok_s, st_s, body_s = http_get_json(STORAGE_HOST, STORAGE_PORT, f"/kv/confirm/{token}")
        if not ok_s or not isinstance(body_s, dict) or not body_s.get("ok"):
            log_json(logging.INFO, "confirm_load_failed", token=token, storage_ok=ok_s, status=st_s, body_type=type(body_s).__name__)
            raise HTTPException(status_code=400, detail="invalid or expired token")
        value = body_s.get("value") or {}
        # Validate token not used/expired
        now = time.time()
        try:
            exp = float(value.get("expires_at") or now)
        except Exception:
            exp = now
        if bool(value.get("used")) or now > exp:
            log_json(logging.INFO, "confirm_invalid", token=token, used=bool(value.get("used")), now=now, exp=exp)
            raise HTTPException(status_code=400, detail="invalid or expired token")
        # Use storage-loaded record directly (no in-memory requirement)
        pending = {
            "envelope": value.get("envelope", {}),
            "matched": value.get("matched"),
            "handler_name": value.get("handler_name"),
            "created_at": value.get("created_at", now),
            "expires_at": exp,
        }
        log_json(logging.INFO, "confirm_loaded_from_storage", token=token)
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
    handler_fn = _HANDLERS.get(handler_name, _handler_echo)

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
    log_json(logging.INFO, "event_confirmed", event_id=event_id, intent=capability_id, handled_by=handler_name)

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
