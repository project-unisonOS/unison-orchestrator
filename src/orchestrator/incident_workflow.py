"""Storage-backed shared incident lifecycle and governed model reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from unison_common.contracts.v1.shared_incident import HouseholdIncident, SensorObservation

from .shared_incident import IncidentEngineRejected, SharedIncidentEngine


class IncidentStore(Protocol):
    def create(self, person_id: str, incident: HouseholdIncident, spaces: list[str]) -> HouseholdIncident: ...
    def admit_observation(self, person_id: str, space_id: str, incident_id: str,
                          observation: SensorObservation, spaces: list[str], at: datetime) -> dict[str, Any]: ...
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
        self.store.create(person_id, incident, authorized_space_ids)
        self.store.admit_observation(person_id, space_id, incident.incident_id, observation,
                                     authorized_space_ids, at)
        return incident, checklist

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
