from fastapi.testclient import TestClient
import os, importlib

# Allow TestClient host through TrustedHostMiddleware
os.environ["UNISON_ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1,orchestrator"

server = importlib.import_module("src.server")
server = importlib.reload(server)
app = server.app


def test_health():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json().get("service") == "unison-orchestrator"


def test_event_validation_missing_payload():
    client = TestClient(app)
    body = {
        "timestamp": "2025-10-25T00:00:00Z",
        "source": "test",
        "intent": "unit.test",
        # payload intentionally missing
    }
    resp = client.post("/event", json=body)
    assert resp.status_code == 400
