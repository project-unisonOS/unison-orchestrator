from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.incident_workflow import IncidentRendererService, IncidentStorageService, SharedIncidentWorkflow
from orchestrator.shared_incident import IncidentEngineRejected, SharedIncidentEngine
from test_shared_incident import observation, pack


NOW = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)


class MemoryStore:
    def __init__(self):
        self.incident = None
        self.observation = None

    def start(self, person_id, incident, item, spaces, at):
        assert incident.space_id in spaces
        self.incident = incident
        self.observation = item
        return incident

    def update_assignment(self, person_id, space_id, incident_id, assignment_id, state, at, spaces, members):
        assignment = self.incident.assignments[0].model_copy(update={"state": state,
            "acknowledged_at": at if state == "acknowledged" else None})
        self.incident = self.incident.model_copy(update={"assignments": [assignment]})
        return self.incident


def test_start_persists_incident_and_evidence_then_acknowledges_assignment():
    store = MemoryStore()
    workflow = SharedIncidentWorkflow(SharedIncidentEngine(pack()), store)
    incident, _ = workflow.start(person_id="alice", assistant_instance_id="assistant-alice",
                                 space_id="shared:home", observation=observation(),
                                 authorized_space_ids=["shared:home"], at=NOW)
    assert store.observation.observation_id == "obs-1"
    updated = workflow.assignment(person_id="alice", space_id="shared:home", incident_id=incident.incident_id,
                                  assignment_id=incident.assignments[0].assignment_id, state="acknowledged",
                                  at=NOW + timedelta(minutes=1), authorized_space_ids=["shared:home"],
                                  household_member_ids=["alice"])
    assert updated.assignments[0].state == "acknowledged"


def test_model_proposals_are_reconciled_and_cannot_override_authority():
    accepted = SharedIncidentWorkflow.reconcile_model_proposal(
        {"explanation": "The selected washer hose is adjacent to the sensor.",
         "equipment_ids": ["washer-1"], "source_ids": ["manual-1"]},
        selected_equipment_ids={"washer-1"}, approved_source_ids={"manual-1"}, active_stop_rules=[])
    assert accepted.physical_actuation is False
    with pytest.raises(IncidentEngineRejected, match="physical actuation"):
        SharedIncidentWorkflow.reconcile_model_proposal(
            {"explanation": "Close it", "equipment_ids": ["washer-1"], "source_ids": ["manual-1"],
             "physical_actuation": True}, selected_equipment_ids={"washer-1"},
            approved_source_ids={"manual-1"}, active_stop_rules=[])
    with pytest.raises(IncidentEngineRejected, match="stop rule"):
        SharedIncidentWorkflow.reconcile_model_proposal(
            {"explanation": "Continue", "equipment_ids": ["washer-1"], "source_ids": ["manual-1"],
             "continue_despite_stop_rule": True}, selected_equipment_ids={"washer-1"},
            approved_source_ids={"manual-1"}, active_stop_rules=["electrical hazard"])


def test_service_adapter_uses_versioned_storage_boundary_and_surfaces_failure():
    incident, _ = SharedIncidentEngine(pack()).assess_water_leak(
        person_id="alice", assistant_instance_id="a", space_id="shared:home",
        observation=observation(), at=NOW)

    class Client:
        def __init__(self, accepted=True):
            self.accepted = accepted
            self.calls = []

        def post(self, path, payload):
            self.calls.append((path, payload))
            if not self.accepted:
                return False, 503, None
            return True, 201, {"incident": incident.model_dump(mode="json")}

    client = Client()
    stored = IncidentStorageService(client).start("alice", incident, observation(), ["shared:home"], NOW)
    assert stored.incident_id == incident.incident_id
    assert client.calls[0][0] == "/v1/incidents"
    with pytest.raises(IncidentEngineRejected, match="storage service"):
        IncidentStorageService(Client(False)).start("alice", incident, observation(), ["shared:home"], NOW)


def test_renderer_delivery_is_semantic_and_failure_is_non_authoritative():
    incident, checklist = SharedIncidentEngine(pack()).assess_water_leak(
        person_id="alice", assistant_instance_id="a", space_id="shared:home",
        observation=observation(), at=NOW)

    class Client:
        def __init__(self, result):
            self.result = result
            self.payload = None

        def post(self, path, payload):
            self.payload = payload
            return self.result

    client = Client((True, 200, {"ok": True}))
    assert IncidentRendererService(client).publish(incident, checklist) is True
    assert client.payload["type"] == "household.incident.v1"
    assert client.payload["payload"]["incident"]["physical_actuation_allowed"] is False
    assert IncidentRendererService(Client((False, 503, None))).publish(incident, checklist) is False
