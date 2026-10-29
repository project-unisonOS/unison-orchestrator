from orchestrator.dev_thin_slice import run_thin_slice


def test_run_thin_slice_writes_trace(tmp_path):
    result = run_thin_slice(text="hello", person_id="person:test", renderer_url=None, trace_dir=str(tmp_path))
    # An unconfigured demo is not a policy authority. It still returns a
    # traceable denied outcome without invoking a tool.
    assert result.tool_result.ok is False
    assert result.policy.allowed is False
    assert result.policy.reason == "policy_not_configured"
    assert result.trace_id
    assert tmp_path.joinpath(f"{result.trace_id}.json").exists()
