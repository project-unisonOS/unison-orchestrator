"""Storage-backed shared incident lifecycle and governed model reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from unison_common.contracts.v1.shared_incident import HouseholdIncident, SensorObservation

from .shared_incident import IncidentEngineRejected, SharedIncidentEngine


class IncidentStore(Protocol):
    def start(self, person_id: str, incident: HouseholdIncident, observation: SensorObservation,
              spaces: list[str], at: datetime) -> HouseholdIncident: ...
    def update_assignment(self, person_id: str, space_id: str, incident_id: str,
                          assignment_id: str, state: str, at: datetime, spaces: list[str],
                          members: list[str]) -> HouseholdIncident: ...


@dataclass(frozen=True)
class ReconciledProposal:
    explanation: str
    equipment_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    physical_actuation: bool = False


class SharedIncidentWorkflow:
    def __init__(self, engine: SharedIncidentEngine, store: IncidentStore):
        self.engine = engine
        self.store = store

    def start(self, *, person_id: str, assistant_instance_id: str, space_id: str,
              observation: SensorObservation, authorized_space_ids: list[str], at: datetime,
              electrical_hazard: bool = False) -> tuple[HouseholdIncident, list[str]]:
        incident, checklist = self.engine.assess_water_leak(
            person_id=person_id, assistant_instance_id=assistant_instance_id, space_id=space_id,
            observation=observation, at=at, electrical_hazard=electrical_hazard)
        stored = self.store.start(person_id, incident, observation, authorized_space_ids, at)
        return stored, checklist

    def assignment(self, *, person_id: str, space_id: str, incident_id: str, assignment_id: str,
                   state: str, at: datetime, authorized_space_ids: list[str],
                   household_member_ids: list[str]) -> HouseholdIncident:
        return self.store.update_assignment(person_id, space_id, incident_id, assignment_id, state, at,
                                            authorized_space_ids, household_member_ids)

    @staticmethod
    def reconcile_model_proposal(proposal: dict[str, Any], *, selected_equipment_ids: set[str],
                                 approved_source_ids: set[str], active_stop_rules: list[str]) -> ReconciledProposal:
        equipment = tuple(proposal.get("equipment_ids") or ())
        sources = tuple(proposal.get("source_ids") or ())
        if not equipment or not set(equipment) <= selected_equipment_ids:
            raise IncidentEngineRejected("model proposal references unselected equipment")
        if not sources or not set(sources) <= approved_source_ids:
            raise IncidentEngineRejected("model proposal lacks approved source reconciliation")
        if proposal.get("physical_actuation") is True:
            raise IncidentEngineRejected("model proposals cannot authorize physical actuation")
        if active_stop_rules and proposal.get("continue_despite_stop_rule") is True:
            raise IncidentEngineRejected("model proposal conflicts with an active deterministic stop rule")
        explanation = str(proposal.get("explanation") or "").strip()
        if not explanation:
            raise IncidentEngineRejected("model proposal requires an explanation")
        return ReconciledProposal(explanation, equipment, sources)


class IncidentStorageService:
    """IncidentStore adapter over the standard authenticated service client."""

    def __init__(self, client):
        self.client = client

    def start(self, person_id: str, incident: HouseholdIncident, observation: SensorObservation,
              spaces: list[str], at: datetime) -> HouseholdIncident:
        del at
        ok, status, body = self.client.post("/v1/incidents", {
            "person_id": person_id, "authorized_space_ids": spaces,
            "incident": incident.model_dump(mode="json"),
            "observation": observation.model_dump(mode="json"),
        })
        if not ok or status != 201 or not body:
            raise IncidentEngineRejected("incident storage service rejected start")
        return HouseholdIncident.model_validate(body["incident"])


class IncidentRendererService:
    """Publishes a semantic incident envelope; renderer failure never rolls back authority state."""

    def __init__(self, client):
        self.client = client

    def publish(self, incident: HouseholdIncident, checklist: list[str]) -> bool:
        ok, status, _ = self.client.post("/events", {
            "type": "household.incident.v1", "urgency": incident.severity,
            "payload": {"incident": incident.model_dump(mode="json"), "checklist": checklist},
        })
        return bool(ok and status == 200)

    def update_assignment(self, person_id: str, space_id: str, incident_id: str,
                          assignment_id: str, state: str, at: datetime, spaces: list[str],
                          members: list[str]) -> HouseholdIncident:
        ok, status, body = self.client.post(
            f"/v1/incidents/{incident_id}/assignments/{assignment_id}/state",
            {"person_id": person_id, "space_id": space_id, "state": state, "at": at.isoformat(),
             "authorized_space_ids": spaces, "household_member_ids": members})
        if not ok or status != 200 or not body:
            raise IncidentEngineRejected("incident storage service rejected assignment update")
        return HouseholdIncident.model_validate(body["incident"])
