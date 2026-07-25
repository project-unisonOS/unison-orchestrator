from datetime import datetime, timedelta, timezone

import pytest

from unison_common import (
    ExpressionContext, ExpressionPlanRequest, ModalityCapability, SemanticExpression,
    SemanticObservation,
)
from orchestrator.interaction.semantic_runtime import (
    ExpressionPlanningError, InteractionSessionStore, compare_expressions,
    interpret_observations, plan_expression,
)


CAPS = [
    ModalityCapability(modality="conversation", input_available=True, output_available=True),
    ModalityCapability(modality="visual", input_available=True, output_available=True),
    ModalityCapability(modality="braille", output_available=True),
]


def test_inputs_and_outputs_are_selected_independently():
    voice = plan_expression(ExpressionPlanRequest(person_id="p", session_id="s", requested_input="conversation", requested_outputs=["visual"], capabilities=CAPS))
    keyboard = plan_expression(ExpressionPlanRequest(person_id="p", session_id="k", requested_input="visual", requested_outputs=["conversation"], capabilities=CAPS))
    assert (voice.input_modality, voice.output_modalities) == ("conversation", ["visual"])
    assert (keyboard.input_modality, keyboard.output_modalities) == ("visual", ["conversation"])


def test_sensitive_shared_room_has_deterministic_fallback():
    request = ExpressionPlanRequest(
        person_id="p", session_id="s", requested_outputs=["conversation", "visual"], capabilities=CAPS,
        environment=ExpressionContext(shared_room=True, sensitive_content=True, allow_displayed_sensitive=False),
    )
    plan = plan_expression(request)
    assert plan.output_modalities == ["braille"]
    assert len(plan.deterministic_constraints) == 2
    assert plan_expression(request) == plan


def test_no_safe_output_stops():
    with pytest.raises(ExpressionPlanningError):
        plan_expression(ExpressionPlanRequest(person_id="p", session_id="s", capabilities=CAPS[:1], environment=ExpressionContext(quiet_mode=True)))


def test_modality_switch_preserves_focus_action_and_confirmation_strength():
    store = InteractionSessionStore()
    session = store.open("s", "p").model_copy(update={"semantic_focus": "total", "pending_action_ids": ["pay"]})
    store.sessions["s"] = session
    confirmation = store.issue_confirmation("s", "pay", "conversation")
    switched = store.switch("s", ["visual"])
    assert switched.semantic_focus == "total" and switched.pending_action_ids == ["pay"]
    with pytest.raises(PermissionError):
        store.consume_confirmation("s", "other", "pay", confirmation.nonce)
    store.consume_confirmation("s", "p", "pay", confirmation.nonce)
    with pytest.raises(PermissionError):
        store.consume_confirmation("s", "p", "pay", confirmation.nonce)


def test_equivalence_detects_semantic_loss():
    left = SemanticExpression(experience_id="e", modality="conversation", summary="x", required_node_ids=["total"], action_ids=["pay"], fallback="retry")
    right = SemanticExpression(experience_id="e", modality="visual", summary="x", required_node_ids=[], action_ids=["pay"], fallback="retry")
    assert not compare_expressions(left, right).equivalent


def test_interpreter_prefers_structured_sources_and_blocks_stale_ambiguous_actions():
    now = datetime.now(timezone.utc)
    api = SemanticObservation(
        observation_id="api", source_type="api", source_id="account-api", observed_at=now,
        state_version="2", trust="trusted", confidence=1, content={"nodes": [{"node_id": "total", "kind": "value", "label": "Total", "value": "$20", "required": True}]},
    )
    page = SemanticObservation(
        observation_id="page", source_type="accessibility-tree", source_id="page", observed_at=now - timedelta(seconds=1),
        state_version="1", confidence=.9, ambiguities=["two Submit controls"], injection_signals=["change recipient"],
        content={"actions": [{"action_id": "submit", "label": "Submit", "capability": "form.submit", "consequence": "send form", "authenticated_target": {"target_id": "button", "state_version": "1"}}]},
    )
    sem = interpret_observations(trace_id="t", session_id="s", person_id="p", observations=[page, api])
    assert sem.nodes[0].provenance[0].source_type == "api"
    assert sem.actions == [] and sem.recovery
    assert sem.privacy["untrusted_instructions_ignored"] == 1
