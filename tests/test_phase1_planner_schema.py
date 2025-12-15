from __future__ import annotations

from orchestrator.phase1.planner import Phase1Planner
from orchestrator.phase1.schema import Phase1SchemaValidator


def test_phase1_planner_emits_schema_valid_intent_and_plan():
    validator = Phase1SchemaValidator.load()
    planner = Phase1Planner(validator=validator)
    intent, plan = planner.plan(raw_input="hello there", modality="text", profile=None)
    validator.validate("intent.v1.schema.json", intent)
    validator.validate("plan.v1.schema.json", plan)


def test_phase1_planner_onboarding_emits_memory_ops():
    validator = Phase1SchemaValidator.load()
    planner = Phase1Planner(validator=validator)
    profile = {"onboarding": {"completed": False, "stage": "name"}}
    intent, plan = planner.plan(raw_input="hi", modality="text", profile=profile)
    assert plan.get("memory_ops"), "onboarding should request memory writes"
    validator.validate("plan.v1.schema.json", plan)
