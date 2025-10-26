from fastapi.testclient import TestClient
from src.server import app


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
