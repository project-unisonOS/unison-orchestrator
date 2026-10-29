"""Authenticated ASGI regression tests with a loopback RSA/JWKS auth service.

Synthetic identities only. No dependency override or principal/auth bypass.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.api.input import register_input_routes
from orchestrator.api.event_graph import register_event_graph_routes
from orchestrator.event_graph.store import JsonlEventGraphStore, new_event
from unison_common import EventGraphAppend, PolicyDecision, ActionEnvelope, ActionResult
from unison_common import auth
from unison_common.principal_middleware import PrincipalBindingMiddleware


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="synthetic-test-key", alg="RS256", use="sig")
    revoked = set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self):
            self.respond(200, {"keys": [jwk]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                claims = jwt.decode(body["token"], key.public_key(), algorithms=["RS256"], options={"verify_aud": False})
                assert claims["jti"] not in revoked
                self.respond(200, {"valid": True, "claims": claims})
            except Exception:
                self.respond(401, {"valid": False})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(auth, "AUTH_SERVICE_URL", url)
    monkeypatch.setattr(auth, "JWKS_URL", url + "/.well-known/jwks.json")
    monkeypatch.setattr(auth, "_jwks_cache", {"keys": None, "expires": 0, "etag": None})
    monkeypatch.setenv("UNISON_EVENT_GRAPH_DIR", str(tmp_path / "events"))
    monkeypatch.setenv("UNISON_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("UNISON_PHASE1_PIPELINE", "false")
    for name in ("UNISON_PRINCIPAL_BINDING_TEST_BYPASS", "DISABLE_AUTH_FOR_TESTS", "UNISON_RENDERER_URL", "UNISON_EXPERIENCE_RENDERER_URL"):
        monkeypatch.delenv(name, raising=False)
    app = FastAPI()
    app.add_middleware(PrincipalBindingMiddleware, service_name="orchestrator")
    register_input_routes(app)
    register_event_graph_routes(app)

    def headers(person="alice", **overrides):
        now = int(time.time())
        claims = dict(sub=person, principal_id=person, principal_kind="person", person_id=person,
                      assistant_instance_id=f"assistant-{person}", household_id="household", membership_id=f"member-{person}",
                      key_handle=f"key-{person}", credential_namespace=person, data_namespace=person,
                      cache_namespace=person, index_namespace=person, aud=["orchestrator"], iat=now, exp=now+300,
                      jti=f"session-{person}", auth_method="test-key", assurance="high")
        claims.update(overrides)
        return {"Authorization": "Bearer " + jwt.encode(claims, key, algorithm="RS256", headers={"kid": jwk["kid"]})}

    with TestClient(app) as client:
        yield client, headers, revoked, tmp_path
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def input_body():
    return dict(event_id="test-event", trace_id="test-trace", ts_unix_ms=1, source="test", modality="text",
                payload={"text": "SYNTHETIC_PRIVATE_WELLNESS_DETAIL"}, session_id="test-session", auth={})


def test_input_binds_omitted_person(boundary):
    client, headers, _, _ = boundary
    response = client.post("/input", json=input_body(), headers=headers())
    assert response.status_code == 200
    assert response.json()["person_id"] == "alice"


@pytest.mark.parametrize("body", [{}, {"person_id": None}, {"person_id": "alice"}])
def test_query_always_scopes_to_authenticated_person(boundary, body):
    client, headers, _, _ = boundary
    store = JsonlEventGraphStore.from_env()
    for person in ("alice", "bob"):
        event = new_event(trace_id="shared-trace", event_type="fixture", person_id=person)
        store.append(EventGraphAppend(trace_id="shared-trace", person_id=person, events=[event]))
    response = client.post("/event-graph/query", json=body, headers=headers())
    assert response.status_code == 200
    assert [event["person_id"] for event in response.json()["events"]] == ["alice"]


def test_append_stamps_omitted_identity(boundary):
    client, headers, _, _ = boundary
    event = new_event(trace_id="append-trace", event_type="fixture")
    response = client.post("/event-graph/append", json={"trace_id": "append-trace", "events": [event.model_dump(mode="json")]}, headers=headers())
    assert response.status_code == 200
    result = client.post("/event-graph/query", json={"trace_id": "append-trace", "person_id": "alice"}, headers=headers())
    assert result.json()["count"] == 1


@pytest.mark.parametrize("mutation", ["missing", "expired", "revoked", "audience", "forged", "nested", "workload"])
def test_denied_authority_cannot_read_events(boundary, mutation):
    client, headers, revoked, _ = boundary
    body = {}
    authorization = headers()
    if mutation == "missing": authorization = {}
    if mutation == "expired": authorization = headers(exp=int(time.time())-1)
    if mutation == "revoked": revoked.add("session-alice")
    if mutation == "audience": authorization = headers(aud=["storage"])
    if mutation == "forged": body = {"person_id": "bob"}
    if mutation == "nested": body = {"payload": {"person_id": "bob"}}
    if mutation == "workload": authorization = headers(principal_kind="workload", person_id=None)
    response = client.post("/event-graph/query", json=body, headers=authorization)
    assert response.status_code in (401, 403)
    assert "events" not in response.json()


def test_confirmation_required_never_executes_tool(boundary, monkeypatch):
    from orchestrator.interaction.policy_gate import PolicyGate
    from orchestrator.interaction.tools import ToolRegistry
    monkeypatch.setattr(PolicyGate, "check", lambda *args, **kwargs: PolicyDecision(allowed=True, require_confirmation=True))
    executions = []
    def execute(_self, action):
        executions.append(action)
        return ActionResult(action_id=action.action_id, ok=True, result={"text": "synthetic effect"})
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    client, headers, _, _ = boundary
    response = client.post("/input", json=input_body(), headers=headers())
    assert executions == []
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "confirmation" in json.dumps(response.json()).lower()


def test_raw_input_is_not_written_to_event_diagnostics(boundary):
    client, headers, _, tmp_path = boundary
    assert client.post("/input", json=input_body(), headers=headers()).status_code == 200
    assert "SYNTHETIC_PRIVATE_WELLNESS_DETAIL" not in (tmp_path / "events" / "events.jsonl").read_text()


def test_missing_policy_service_denies():
    from orchestrator.interaction.policy_gate import PolicyGate
    action = ActionEnvelope(action_id="synthetic-action", kind="tool", name="echo", args={"text": "fixture"})
    assert PolicyGate(clients=None).check(action).allowed is False


def test_policy_outage_cannot_be_overridden_by_environment(monkeypatch):
    from orchestrator.interaction import policy_gate
    monkeypatch.setenv("UNISON_POLICY_FAIL_OPEN", "true")
    monkeypatch.setattr(policy_gate, "evaluate_capability", lambda *a, **kw: (False, 503, None))
    action = ActionEnvelope(action_id="synthetic-action", kind="tool", name="echo", args={})
    assert policy_gate.PolicyGate(clients=object()).check(action).allowed is False


def test_routes_fail_closed_without_authentication_middleware(monkeypatch, tmp_path):
    # The old environment switches must not grant route-level authority.
    monkeypatch.setenv("DISABLE_AUTH_FOR_TESTS", "true")
    monkeypatch.setenv("UNISON_EVENT_GRAPH_DIR", str(tmp_path))
    app = FastAPI()
    register_input_routes(app)
    register_event_graph_routes(app)
    with TestClient(app) as client:
        assert client.post("/input", json=input_body()).status_code == 403
        assert client.post("/event-graph/query", json={}).status_code == 403
