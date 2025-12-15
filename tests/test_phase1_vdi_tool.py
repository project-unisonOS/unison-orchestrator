from __future__ import annotations

from dataclasses import dataclass

from orchestrator.phase1.planner import Phase1Planner
from orchestrator.phase1.schema import Phase1SchemaValidator
from orchestrator.phase1.tool_runtime import Phase1ToolRuntime


@dataclass
class _ActuationStub:
    ok: bool = True
    status: int = 200
    body: dict | None = None
    last_payload: dict | None = None

    def post(self, path: str, payload: dict, *, headers=None):
        _ = headers
        assert path == "/vdi/tasks/browse"
        self.last_payload = payload
        return self.ok, self.status, self.body or {"ok": True}


@dataclass
class _ClientsStub:
    actuation: _ActuationStub | None = None


def test_phase1_planner_vdi_allowlist_allows_example_com(monkeypatch):
    monkeypatch.setenv("UNISON_PHASE1_VDI_ALLOWLIST_DOMAINS", "example.com")
    validator = Phase1SchemaValidator.load()
    planner = Phase1Planner(validator=validator)
    profile = {"onboarding": {"completed": True}}
    intent, plan = planner.plan(raw_input="browse https://example.com", modality="text", profile=profile)
    assert plan["tool_calls"], "expected vdi tool call"
    call = plan["tool_calls"][0]
    assert call["tool_name"] == "vdi.use_computer"
    assert call["authorization"]["policy_decision"] == "allow"


def test_phase1_planner_vdi_non_allowlisted_requires_confirm(monkeypatch):
    monkeypatch.setenv("UNISON_PHASE1_VDI_ALLOWLIST_DOMAINS", "example.com")
    validator = Phase1SchemaValidator.load()
    planner = Phase1Planner(validator=validator)
    profile = {"onboarding": {"completed": True}}
    _, plan = planner.plan(raw_input="browse https://google.com", modality="text", profile=profile)
    call = plan["tool_calls"][0]
    assert call["authorization"]["policy_decision"] == "confirm"


def test_phase1_vdi_tool_executes_via_actuation_when_allowed():
    runtime = Phase1ToolRuntime()
    clients = _ClientsStub(actuation=_ActuationStub(ok=True, status=200, body={"task_id": "t1"}))
    call = {
        "tool_call_id": "toolcall_12345678",
        "tool_name": "vdi.use_computer",
        "args": {"action": "open_url", "url": "https://example.com"},
        "authorization": {"policy_decision": "allow"},
        "timeout_ms": 60000,
    }
    res = runtime.execute(call=call, clients=clients, person_id="local-person", trace_id="tr1", session_id="s1")
    assert res.ok is True
    assert clients.actuation is not None and clients.actuation.last_payload is not None
    assert clients.actuation.last_payload["person_id"] == "local-person"
    assert clients.actuation.last_payload["url"] == "https://example.com"
    assert clients.actuation.last_payload["trace_id"] == "tr1"


def test_phase1_vdi_tool_returns_not_available_without_actuation():
    runtime = Phase1ToolRuntime()
    call = {
        "tool_call_id": "toolcall_12345678",
        "tool_name": "vdi.use_computer",
        "args": {"action": "open_url", "url": "https://example.com"},
        "authorization": {"policy_decision": "allow"},
        "timeout_ms": 60000,
    }
    res = runtime.execute(call=call, clients=_ClientsStub(actuation=None), person_id="local-person", trace_id="tr1", session_id="s1")
    assert res.ok is False
    assert res.error == "not_available"


def test_phase1_vdi_tool_maps_vdi_unavailable_to_not_available():
    runtime = Phase1ToolRuntime()
    actuation = _ActuationStub(ok=False, status=502, body={"detail": {"error": "vdi_unavailable"}})
    clients = _ClientsStub(actuation=actuation)
    call = {
        "tool_call_id": "toolcall_12345678",
        "tool_name": "vdi.use_computer",
        "args": {"action": "open_url", "url": "https://example.com"},
        "authorization": {"policy_decision": "allow"},
        "timeout_ms": 60000,
    }
    res = runtime.execute(call=call, clients=clients, person_id="local-person", trace_id="tr1", session_id="s1")
    assert res.ok is False
    assert res.error == "not_available"


def test_phase1_vdi_tool_maps_internal_errors_to_not_available():
    runtime = Phase1ToolRuntime()
    actuation = _ActuationStub(ok=False, status=500, body={"detail": {"error": "internal_error"}})
    clients = _ClientsStub(actuation=actuation)
    call = {
        "tool_call_id": "toolcall_12345678",
        "tool_name": "vdi.use_computer",
        "args": {"action": "open_url", "url": "https://example.com"},
        "authorization": {"policy_decision": "allow"},
        "timeout_ms": 60000,
    }
    res = runtime.execute(call=call, clients=clients, person_id="local-person", trace_id="tr1", session_id="s1")
    assert res.ok is False
    assert res.error == "not_available"
