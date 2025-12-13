from orchestrator.dev_thin_slice import run_thin_slice


def test_run_thin_slice_writes_trace(tmp_path):
    result = run_thin_slice(text="hello", renderer_url=None, trace_dir=str(tmp_path))
    assert result.tool_result.ok is True
    assert result.trace_id
    assert tmp_path.joinpath(f"{result.trace_id}.json").exists()

