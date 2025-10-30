from fastapi.testclient import TestClient
from src.server import app

client = TestClient(app)


def test_list_skills_empty():
    r = client.get("/skills")
    assert r.status_code == 200
    j = r.json()
    assert isinstance(j, dict)
    assert j.get("ok") is True
    # Built-in echo should be present
    skills = j.get("skills", [])
    assert isinstance(skills, list)
    assert "echo" in skills


def test_register_skill_invalid_intent():
    r = client.post("/skills", json={"intent": ""})
    assert r.status_code == 400
    j = r.json()
    assert "Invalid or missing 'intent'" in str(j.get("detail", ""))


def test_register_skill_unsupported_intent():
    r = client.post("/skills", json={"intent": "unsupported.intent"})
    assert r.status_code == 400
    j = r.json()
    assert "not supported in MVP" in str(j.get("detail", ""))


def test_register_and_list_summarize_doc():
    # Register summarize.doc
    r = client.post("/skills", json={"intent": "summarize.doc"})
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is True
    assert j.get("intent") == "summarize.doc"

    # List includes it
    r = client.get("/skills")
    assert r.status_code == 200
    j = r.json()
    skills = j.get("skills", [])
    assert "summarize.doc" in skills


def test_register_duplicate_skill():
    # Register context.get twice
    r = client.post("/skills", json={"intent": "context.get"})
    assert r.status_code == 200
    r2 = client.post("/skills", json={"intent": "context.get"})
    assert r2.status_code == 409
    j = r2.json()
    assert "already registered" in str(j.get("detail", ""))


def test_event_routes_to_registered_skill():
    # Register storage.put
    r = client.post("/skills", json={"intent": "storage.put"})
    assert r.status_code == 200

    # Send an event for storage.put; should be accepted and routed
    env = {
        "timestamp": "2025-01-01T00:00:00Z",
        "source": "test",
        "intent": "storage.put",
        "payload": {"namespace": "test", "key": "a", "value": 1},
        "auth_scope": "person.local.explicit",
        "safety_context": {}
    }
    r = client.post("/event", json=env)
    assert r.status_code == 200
    j = r.json()
    assert j.get("accepted") is True
    assert j.get("handled_by") == "storage.put"
    # Outputs should contain the storage response (stub ok:true)
    outputs = j.get("outputs", {})
    assert outputs.get("ok") is True


def test_event_fallback_to_echo_for_unknown_intent():
    env = {
        "timestamp": "2025-01-01T00:00:00Z",
        "source": "test",
        "intent": "unknown.intent",
        "payload": {"msg": "hello"},
        "auth_scope": "person.local.explicit",
        "safety_context": {}
    }
    r = client.post("/event", json=env)
    assert r.status_code == 200
    j = r.json()
    assert j.get("accepted") is True
    assert j.get("handled_by") == "echo"
    outputs = j.get("outputs", {})
    assert "echo" in outputs
