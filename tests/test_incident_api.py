from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.api.incidents import register_incident_routes
from test_shared_incident import observation, pack


class Client:
    def __init__(self, renderer=False):
        self.renderer = renderer
        self.calls = []

    def post(self, path, payload):
        self.calls.append((path, payload))
        if self.renderer:
            return True, 200, {"ok": True}
        return True, 201, {"incident": payload["incident"]}


def test_simulation_route_persists_then_delivers_semantic_incident():
    storage, renderer = Client(), Client(renderer=True)
    app = FastAPI()
    register_incident_routes(app, service_clients=SimpleNamespace(storage=storage, renderer=renderer))
    response = TestClient(app).post("/v1/incidents/simulations/water-leak", json={
        "person_id": "alice", "assistant_instance_id": "assistant-alice", "household_id": "home",
        "at": datetime(2026, 8, 14, 12, tzinfo=timezone.utc).isoformat(),
        "observation": observation().model_dump(mode="json"),
        "knowledge_pack": pack().model_dump(mode="json"),
    })
    assert response.status_code == 201
    assert response.json()["evidence_class"] == "simulation"
    assert storage.calls[0][0] == "/v1/incidents"
    assert renderer.calls[0][0] == "/events"


def test_renderer_loss_preserves_authoritative_incident_result():
    storage, renderer = Client(), Client(renderer=True)
    renderer.post = lambda path, payload: (False, 503, None)
    app = FastAPI()
    register_incident_routes(app, service_clients=SimpleNamespace(storage=storage, renderer=renderer))
    response = TestClient(app).post("/v1/incidents/simulations/water-leak", json={
        "person_id": "alice", "assistant_instance_id": "assistant-alice", "household_id": "home",
        "at": datetime(2026, 8, 14, 12, tzinfo=timezone.utc).isoformat(),
        "observation": observation().model_dump(mode="json"),
        "knowledge_pack": pack().model_dump(mode="json"),
    })
    assert response.status_code == 201
    assert response.json()["renderer_delivered"] is False
    assert response.json()["incident"]["state"] == "action-needed"
