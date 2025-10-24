from fastapi import FastAPI
import uvicorn
import os
import http.client
import json

app = FastAPI(title="unison-orchestrator")

CONTEXT_HOST = os.getenv("UNISON_CONTEXT_HOST", "context")
CONTEXT_PORT = os.getenv("UNISON_CONTEXT_PORT", "8081")

STORAGE_HOST = os.getenv("UNISON_STORAGE_HOST", "storage")
STORAGE_PORT = os.getenv("UNISON_STORAGE_PORT", "8082")

POLICY_HOST = os.getenv("UNISON_POLICY_HOST", "policy")
POLICY_PORT = os.getenv("UNISON_POLICY_PORT", "8083")


def http_get_json(host: str, port: str, path: str):
    """
    Minimal internal GET.
    Returns (ok: bool, status_code: int, body: dict|None)
    """
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


def http_post_json(host: str, port: str, path: str, payload: dict):
    """
    Minimal internal POST with JSON body.
    Returns (ok: bool, status_code: int, body: dict|None)
    """
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
    # process is running
    return {"status": "ok", "service": "unison-orchestrator"}


@app.post("/authorize")
def authorize_stub():
    """
    Ask policy if a hypothetical action is allowed.
    This is how orchestrator will gate high-risk actions in the future.

    For now we send a hard-coded capability_id 'test.ACTION'
    and a dummy context payload.
    """
    payload = {
        "capability_id": "test.ACTION",
        "context": {
            "actor": "local-user",
            "intent": "demo",
        },
    }

    ok, status_code, body = http_post_json(
        POLICY_HOST,
        POLICY_PORT,
        "/evaluate",
        payload,
    )

    return {
        "policy_ok": ok,
        "status_code": status_code,
        "decision": body,
    }


@app.get("/ready")
def ready():
    """
    Orchestrator is 'ready' if:
    - context responds to /health
    - storage responds to /health
    - policy allows a sample action through /evaluate
    """

    context_ok, _, _ = http_get_json(CONTEXT_HOST, CONTEXT_PORT, "/health")
    storage_ok, _, _ = http_get_json(STORAGE_HOST, STORAGE_PORT, "/health")

    # call policy
    payload = {
        "capability_id": "test.ACTION",
        "context": {
            "actor": "local-user",
            "intent": "readiness-check",
        },
    }
    policy_ok, _, policy_body = http_post_json(
        POLICY_HOST,
        POLICY_PORT,
        "/evaluate",
        payload,
    )

    # consider allowed only if policy responded AND decision.allowed == True
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


if __name__ == "__main__":
    # listen on all interfaces for docker
    uvicorn.run(app, host="0.0.0.0", port=8080)
