from fastapi.testclient import TestClient
import src.server as srv

srv.app.dependency_overrides[srv.verify_token] = lambda: {
    "username": "test-user",
    "roles": ["tester"],
}
client = TestClient(srv.app)


def test_event_surfaces_suggested_alternative_and_confirmation(monkeypatch):
    # Patch policy call to require confirmation with suggested alternative
    def fake_http_post_json(host, port, path, payload, headers=None):
        body = {
            "decision": {
                "allowed": False,
                "require_confirmation": True,
                "reason": "needs-confirmation-for-confidential",
                "suggested_alternative": "Use internal summary mode or downgrade data classification.",
            }
        }
        return True, 200, body

    monkeypatch.setattr(srv, "http_post_json", fake_http_post_json)

    envelope = {
        "timestamp": "2025-10-28T00:00:00Z",
        "source": "unit-test",
        "intent": "summarize.doc",
        "payload": {},
        "safety_context": {"data_classification": "confidential"},
    }
    r = client.post("/event", json=envelope)
    assert r.status_code == 200
    j = r.json()
    assert j.get("accepted") is False
    assert j.get("require_confirmation") is True
    assert isinstance(j.get("policy_suggested_alternative"), str)
    assert j.get("policy_suggested_alternative").startswith("Use internal summary mode")
