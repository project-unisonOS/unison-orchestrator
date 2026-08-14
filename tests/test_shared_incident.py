from datetime import datetime, timedelta, timezone

import pytest
from unison_common.contracts.v1.shared_incident import (
    KnowledgeProcedure,
    OfflineKnowledgePack,
    SensorObservation,
)

from orchestrator.shared_incident import IncidentEngineRejected, SharedIncidentEngine


NOW = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)


def pack():
    return OfflineKnowledgePack(
        pack_id="home-safety", version="1", region="US", language="en-US", authority="Unison",
        source_ids=["manual-1"], effective_at=NOW - timedelta(days=1), review_by=NOW + timedelta(days=30),
        hazards=["electricity near standing water"], stop_rules=["Stop if electricity may contact water"],
        procedures=[KnowledgeProcedure(procedure_id="water-1", purpose="water-leak",
                                       steps=["Locate the labeled manual shutoff", "Ask a household member to inspect it"])],
        digest="sha256:" + "a" * 64, signature_key_id="local-root", signature="fixture-signature",
    )


def observation(**updates):
    values = dict(observation_id="obs-1", sensor_id="water-1", source_sequence=1, observed_at=NOW,
                  received_at=NOW, state="wet", value=True, unit="boolean", confidence=.99,
                  fresh_until=NOW + timedelta(minutes=5), integrity_state="verified", device_health="healthy")
    values.update(updates)
    return SensorObservation(**values)


def test_deterministic_water_route_produces_manual_assignment_and_no_actuation():
    incident, checklist = SharedIncidentEngine(pack()).assess_water_leak(
        person_id="alice", assistant_instance_id="assistant-alice", space_id="shared:home",
        observation=observation(), at=NOW)
    assert [event.state.value for event in incident.timeline] == ["observed", "assessing", "action-needed"]
    assert incident.assignments[0].physical_actuation is False
    assert checklist[0].startswith("Stop")


def test_electrical_hazard_uses_deterministic_stop_rule_and_escalates():
    incident, _ = SharedIncidentEngine(pack()).assess_water_leak(
        person_id="alice", assistant_instance_id="assistant-alice", space_id="shared:home",
        observation=observation(), at=NOW, electrical_hazard=True)
    assert incident.state.value == "escalated"
    assert incident.timeline[-1].deterministic_rule == "water:electrical-hazard-stop"


def test_stale_or_untrusted_evidence_cannot_enter_deterministic_route():
    engine = SharedIncidentEngine(pack())
    with pytest.raises(IncidentEngineRejected, match="stale"):
        engine.assess_water_leak(person_id="alice", assistant_instance_id="a", space_id="shared:home",
                                 observation=observation(), at=NOW + timedelta(hours=1))
    with pytest.raises(IncidentEngineRejected, match="integrity"):
        engine.assess_water_leak(person_id="alice", assistant_instance_id="a", space_id="shared:home",
                                 observation=observation(integrity_state="unverified"), at=NOW)


def test_no_model_path_returns_useful_partial_result_and_recovery():
    attempt = SharedIncidentEngine(pack()).novel_request_attempt(
        person_id="alice", assistant_instance_id="assistant-alice", purpose="unfamiliar valve question",
        authorized_space_ids=["shared:home"], useful_artifact_id="offline-checklist-1", no_model_available=True)
    assert attempt.state == "partial"
    assert attempt.partial_artifact_ids == ["offline-checklist-1"]
    assert attempt.recovery
    assert attempt.structural_fingerprint.contains_person_content is False
