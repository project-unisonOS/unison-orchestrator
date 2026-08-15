from orchestrator.resolution_engine import ResolutionEngine
from unison_common.resolution import ResolutionBudget

def test_unfamiliar_request_uses_bounded_local_inference_without_generic_refusal():
    engine = ResolutionEngine({}, local_inference=lambda kind, value: {
        "complete": False, "summary": "I identified the valve and prepared safe isolation steps.",
        "facts": ["Water is still flowing"], "uncertainties": ["Valve condition is unknown"],
        "actions": ["inspect", "shut-off", "request-help", "cancel"],
        "recovery": {"resume_from": "valve-inspection"}})
    result = engine.resolve(person_id="alice", assistant_id="ua", request_class="novel-home-repair",
        semantic_request={"purpose": "stop a leak", "risk": "high", "observation_type": "water"},
        authorized_spaces=("private",), authorized_domains=("household",),
        budget=ResolutionBudget(time_seconds=120, model_calls=1, tool_calls=4))
    assert result.attempt.state == "partial"
    assert [r.kind for r in result.attempt.routes] == ["known-deterministic", "bounded-local-inference"]
    assert result.semantic_outcome["actions"][-1] == "cancel"

def test_no_model_path_still_returns_resumable_partial_outcome():
    result = ResolutionEngine({}).resolve(person_id="alice", assistant_id="ua", request_class="unknown",
        semantic_request={"purpose": "help"}, authorized_spaces=("private",), authorized_domains=("core-private",),
        budget=ResolutionBudget(time_seconds=30, model_calls=0, tool_calls=0))
    assert result.semantic_outcome["recovery"]["resume_from"] == "route-selection"
