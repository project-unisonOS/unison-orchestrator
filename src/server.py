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


def check_service(host: str, port: str, path: str = "/health"):
    """
    Minimal internal HTTP GET using stdlib http.client
    Returns tuple (ok: bool, body: dict|None)
    """
    try:
        conn = http.client.HTTPConnection(host, port, timeout=1.0)
        conn.request("GET", path)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()

        if resp.status != 200:
            return False, None

        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception:
            parsed = None

        return True, parsed
    except Exception:
        return False, None


@app.get("/health")
def health():
    # Process is up
    return {"status": "ok", "service": "unison-orchestrator"}


@app.get("/ready")
def ready():
    # Orchestrator is only "ready" if core deps are responding

    context_ok, _ = check_service(CONTEXT_HOST, CONTEXT_PORT, "/health")
    storage_ok, _ = check_service(STORAGE_HOST, STORAGE_PORT, "/health")

    all_ok = context_ok and storage_ok

    return {
        "ready": all_ok,
        "deps": {
            "context": context_ok,
            "storage": storage_ok,
        },
    }


if __name__ == "__main__":
    # 0.0.0.0 so Docker can expose it
    uvicorn.run(app, host="0.0.0.0", port=8080)
