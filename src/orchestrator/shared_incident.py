"""Deterministic DJ-1 household incident orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from unison_common.contracts.v1.shared_incident import (
    EvidenceRecord,
    EvidenceState,
    HouseholdIncident,
    IncidentAssignment,
    IncidentState,
    IncidentTimelineEvent,
    OfflineKnowledgePack,
    ResolutionAttempt,
    ResolutionBudget,
    SensorObservation,
    StructuralFingerprint,
)


class IncidentEngineRejected(ValueError):
    pass


class SharedIncidentEngine:
    """Builds deterministic incident state; it never performs physical action."""

    def __init__(self, knowledge_pack: OfflineKnowledgePack):
        self.knowledge_pack = knowledge_pack

    def assess_water_leak(self, *, person_id: str, assistant_instance_id: str, space_id: str,
                          observation: SensorObservation, at: datetime | None = None,
                          electrical_hazard: bool = False) -> tuple[HouseholdIncident, list[str]]:
        now = at or datetime.now(timezone.utc)
        if not space_id.startswith("shared:"):
            raise IncidentEngineRejected("household incidents require an authorized shared space")
        if observation.integrity_state not in {"verified", "fixture"}:
            raise IncidentEngineRejected("sensor integrity is insufficient for deterministic routing")
        if observation.fresh_until < now:
            raise IncidentEngineRejected("sensor evidence is stale")
        if observation.state not in {"wet", "leak-detected"} or observation.value is not True:
            raise IncidentEngineRejected("observation does not establish a water leak")

        incident_id = f"inc_{uuid4().hex}"
        timeline = [self._event(IncidentState.OBSERVED, now, observation, "water:observed")]
        timeline.append(self._event(IncidentState.ASSESSING, now, observation, "water:assess"))
        if electrical_hazard:
            state, severity, rule = IncidentState.ESCALATED, "emergency", "water:electrical-hazard-stop"
        else:
            state, severity, rule = IncidentState.ACTION_NEEDED, "urgent", "water:confirmed-action-needed"
        timeline.append(self._event(state, now, observation, rule))
        fact = EvidenceRecord(evidence_id=f"ev_{uuid4().hex}", claim="Water is detected at the sensor",
                              state=EvidenceState.CONFIRMED, source_ids=[observation.observation_id],
                              observed_at=observation.observed_at, fresh_until=observation.fresh_until)
        assignment = IncidentAssignment(
            assignment_id=f"asg_{uuid4().hex}", incident_id=incident_id,
            workflow_step_id="manual-water-isolation", assignee_person_id=person_id,
            action="Inspect the labeled manual water shutoff and follow the cited procedure",
            created_at=now, source_ids=[observation.observation_id, self.knowledge_pack.pack_id],
        )
        incident = HouseholdIncident(
            incident_id=incident_id, space_id=space_id, kind="water-leak", state=state,
            severity=severity, source_ids=[observation.observation_id, self.knowledge_pack.pack_id],
            facts=[fact], assignments=[assignment], timeline=timeline,
            uncertainties=["Leak extent and source remain unconfirmed"],
        )
        return incident, self.offline_checklist("water-leak")

    def offline_checklist(self, purpose: str) -> list[str]:
        procedure = next((item for item in self.knowledge_pack.procedures if item.purpose == purpose), None)
        if not procedure:
            raise IncidentEngineRejected("offline knowledge has no applicable procedure")
        return [*self.knowledge_pack.stop_rules, *procedure.steps]

    def novel_request_attempt(self, *, person_id: str, assistant_instance_id: str, purpose: str,
                              authorized_space_ids: list[str], useful_artifact_id: str | None = None,
                              no_model_available: bool = False) -> ResolutionAttempt:
        routes = ["deterministic-skill", "offline-knowledge", "local-model"]
        rejections = {"deterministic-skill": "no exact skill matched"}
        state = "running"
        artifacts: list[str] = []
        recovery = None
        if no_model_available:
            rejections["local-model"] = "local model unavailable"
            if useful_artifact_id:
                state, artifacts = "partial", [useful_artifact_id]
                recovery = "Resume local inference when model capacity returns"
            else:
                state = "blocked"
                recovery = "Use the offline checklist or hand off to a household member"
        return ResolutionAttempt(
            attempt_id=f"attempt_{uuid4().hex}", outcome_id=f"outcome_{uuid4().hex}",
            person_id=person_id, assistant_instance_id=assistant_instance_id, purpose=purpose,
            risk="high", authorized_space_ids=authorized_space_ids, routes_considered=routes,
            rejection_reasons=rejections, uncertainties=["No deterministic route matched"],
            state=state, partial_artifact_ids=artifacts, recovery=recovery,
            budget=ResolutionBudget(max_seconds=30, max_model_calls=1, max_external_calls=0,
                                    max_cost=0, max_disclosed_fields=0),
            structural_fingerprint=StructuralFingerprint(
                contract_types=["resolution-attempt.v1"], tool_kinds=["offline-knowledge"],
                route_kinds=routes, error_classes=list(rejections), risk_class="high",
                outcome_shape="incident-guidance", contains_person_content=False,
            ),
        )

    @staticmethod
    def _event(state: IncidentState, at: datetime, observation: SensorObservation,
               rule: str) -> IncidentTimelineEvent:
        return IncidentTimelineEvent(event_id=f"evt_{uuid4().hex}", state=state, occurred_at=at,
                                     actor_id="unison", reason=state.value,
                                     source_ids=[observation.observation_id], deterministic_rule=rule)
