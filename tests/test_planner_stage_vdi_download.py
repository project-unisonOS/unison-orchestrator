from orchestrator.interaction.planner_stage import PlannerStage
from unison_common import TraceRecorder


def test_planner_stage_maps_download_request_to_bounded_vdi_download():
    stage = PlannerStage()
    trace = TraceRecorder(service="test")

    out = stage.run(
        text="download https://example.com/report.pdf",
        trace=trace,
        context=None,
    )

    assert out.plan.intent.name == "vdi.download"
    assert len(out.plan.actions) == 1
    action = out.plan.actions[0]
    assert action.kind == "vdi"
    assert action.name == "vdi.download"
    assert action.args["url"] == "https://example.com/report.pdf"
    assert action.policy_context == {"scopes": ["vdi.download"]}
