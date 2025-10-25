from fastapi import FastAPI, HTTPException, Body
import uvicorn
import os
import http.client
import json
from typing import Any, Dict, Tuple
from unison_common import validate_event_envelope, EnvelopeValidationError

app = FastAPI(title="unison-orchestrator")

CONTEXT_HOST = os.getenv("UNISON_CONTEXT_HOST", "context")
CONTEXT_PORT = os.getenv("UNISON_CONTEXT_PORT", "8081")

STORAGE_HOST = os.getenv("UNISON_STORAGE_HOST", "storage")
STORAGE_PORT = os.getenv("UNISON_STORAGE_PORT", "8082")

POLICY_HOST = os.getenv("UNISON_POLICY_HOST", "policy")
POLICY_PORT = os.getenv("UNISON_POLICY_PORT", "8083")


def http_get_json(host: str, port: str, path: str) -> Tuple[bool, int, dict | None]:
    try:
        conn = http.client.HTTPConnection(host, port, timeout=1.0)
        conn.request("GET", path)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        body = None
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = None
        return (resp.status == 200, resp.status, body)
    except Exception:
        return (False, 0, None)


def http_post_json(host: str, port: str, path: str, payload: dict) -> Tuple[bool, int, dict | None]:
    try:
        body_str = json.dumps(payload)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        conn = http.client.HTTPConnection(host, port, timeout=1.0)
        conn.request("POST", path, body_str, headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        parsed = None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = None
        return (resp.status == 200, resp.status, parsed)
    except Exception:
        return (False, 0, None)


@app.get("/health")
def health():
    return {"status": "ok", "service": "unison-orchestrator"}


@app.get("/ready")
def ready():
    context_ok, _, _ = http_get_json(CONTEXT_HOST, CONTEXT_PORT, "/health")
    storage_ok, _, _ = http_get_json(STORAGE_HOST, STORAGE_PORT, "/health")

    eval_payload = {
        "capability_id": "test.ACTION",
        "context": {"actor": "local-user", "intent": "readiness-check"},
    }
    policy_ok, _, policy_body = http_post_json(
        POLICY_HOST,
        POLICY_PORT,
        "/evaluate",
        eval_payload,
    )

    allowed = False
    if policy_ok and isinstance(policy_body, dict):
        decision = policy_body.get("decision", {})
        allowed = decision.get("allowed", False) is True

    all_ok = context_ok and storage_ok and allowed

    return {
        "ready": all_ok,
        "deps": {
            "context": context_ok,
            "storage": storage_ok,
            "policy_allowed_action": allowed,
        },
    }


@app.post("/event")
def handle_event(envelope: dict = Body(...)):
    try:
        envelope = validate_event_envelope(envelope)
    except EnvelopeValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    capability_id = envelope["intent"]
    policy_context = {
        "actor": envelope.get("source", "unknown"),
        "payload_preview": envelope.get("payload", {}),
        "auth_scope": envelope.get("auth_scope", None),
    }

    ok, status_code, policy_body = http_post_json(
        POLICY_HOST,
        POLICY_PORT,
        "/evaluate",
        {"capability_id": capability_id, "context": policy_context},
    )

    allowed = False
    require_confirmation = False
    reason = "no-decision"

    if ok and isinstance(policy_body, dict):
        decision_block = policy_body.get("decision", {})
        allowed = bool(decision_block.get("allowed", False))
        require_confirmation = bool(decision_block.get("require_confirmation", False))
        reason = decision_block.get("reason", reason)

    if not allowed:
        return {
            "accepted": False,
            "reason": reason,
            "require_confirmation": require_confirmation,
            "policy_status": status_code,
            "policy_raw": policy_body,
        }

    return {
        "accepted": True,
        "routed_intent": capability_id,
        "payload": envelope["payload"],
        "policy_status": status_code,
        "policy_require_confirmation": require_confirmation,
        "explanation": "stub dispatch not yet implemented",
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
