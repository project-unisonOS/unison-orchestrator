from orchestrator.interaction.rom_builder import RomBuilder
from unison_common import ActionResult, PolicyDecision, SemanticExperience


def test_builder_creates_sem_and_compatible_rom_without_presentation_authority():
    builder = RomBuilder()
    result = ActionResult(action_id="calendar.read", ok=True, result={"text": "You have two conflicts"})
    policy = PolicyDecision(allowed=True, reason="authorized")
    sem = builder.build_sem(trace_id="trace", session_id="session", person_id="alice", tool_result=result, policy=policy)
    rom = builder.build(trace_id="trace", session_id="session", person_id="alice", tool_result=result, policy=policy)
    restored = SemanticExperience.model_validate(rom.meta["semantic_experience"])
    assert sem.outcome == "You have two conflicts"
    assert restored.nodes[0].required is True
    assert rom.blocks[0].text == sem.outcome
    assert rom.meta["origin"] == "semantic-rom-compatibility"


def test_policy_denial_is_required_semantic_outcome():
    sem = RomBuilder().build_sem(
        trace_id="trace", session_id="session", person_id="alice",
        tool_result=ActionResult(action_id="payment.send", ok=False, error="blocked"),
        policy=PolicyDecision(allowed=False, reason="confirmation required"),
    )
    assert sem.outcome == "Policy denied: confirmation required"
    assert sem.nodes[0].required is True
