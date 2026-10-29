import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.responses import JSONResponse
from unison_common import ModelTaskRequirement
from orchestrator.local_inference import LocalTextInferenceClient, LocalInferenceRejected
from test_authenticated_input_boundary import boundary  # noqa: F401


@pytest.fixture
def caller(boundary):
    captured = []
    result = {"ok": True, "provider": "ollama", "model": "synthetic-model", "result": "synthetic text",
              "event_id": "synthetic-operation", "fallback_used": False, "tool_calls": []}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            captured.append((self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    adapter = LocalTextInferenceClient(f"http://127.0.0.1:{server.server_port}")
    client, headers, revoked, _ = boundary
    @client.app.post("/synthetic-caller")
    def call():
        try:
            return asdict(adapter.request(operation_id="synthetic-operation", prompt="Synthetic fixture only",
                                          requirement=ModelTaskRequirement(task="synthesis")))
        except LocalInferenceRejected:
            return JSONResponse({"detail": "local inference denied"}, status_code=403)
    yield client, headers, revoked, captured, result
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def test_forwards_same_verified_authority_and_explicit_task(caller):
    client, headers, _, captured, _ = caller
    authorization = headers(aud=["orchestrator", "inference"], scopes=["inference.local"])
    response = client.post("/synthetic-caller", headers=authorization)
    assert response.status_code == 200
    assert response.json()["untrusted"] is True
    forwarded, body = captured[0]
    assert forwarded == authorization["Authorization"]
    assert body["person_id"] == "alice"
    assert body["task_requirement"]["task"] == "synthesis"
    assert body["task_requirement"]["max_cost"] == 0
    assert body["task_requirement"]["local_only"] is True
    assert "hardware" not in body and "trust_decision" not in body


@pytest.mark.parametrize("case", ["missing", "audience", "scope", "revoked", "expired"])
def test_no_egress_without_inference_authority(caller, case):
    client, headers, revoked, captured, _ = caller
    claims = {"aud": ["orchestrator", "inference"], "scopes": ["inference.local"]}
    if case == "audience": claims["aud"] = ["orchestrator"]
    if case == "scope": claims["scopes"] = []
    if case == "expired": claims["exp"] = 1
    if case == "revoked": revoked.add("session-alice")
    response = client.post("/synthetic-caller", headers={} if case == "missing" else headers(**claims))
    assert response.status_code in (401, 403)
    assert captured == []


@pytest.mark.parametrize("change", [{"provider": "openai"}, {"fallback_used": True}, {"tool_calls": [{}]},
                                    {"event_id": "different-operation"}, {"result": {"action": "execute"}}])
def test_provider_output_cannot_expand_contract(caller, change):
    client, headers, _, captured, result = caller
    result.update(change)
    response = client.post("/synthetic-caller", headers=headers(aud=["orchestrator", "inference"], scopes=["inference.local"]))
    assert response.status_code == 403
    assert len(captured) == 1


@pytest.mark.parametrize("url", ["http://inference:8080", "http://192.168.0.1", "https://example.com", "http://127.0.0.1@remote", "http://127.0.0.1/other"])
def test_transport_fails_closed_before_request(url):
    with pytest.raises(LocalInferenceRejected):
        LocalTextInferenceClient(url)
