from pathlib import Path

from orchestrator.dev_thin_slice import run_thin_slice


def test_thin_slice_appends_event_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("UNISON_EVENT_GRAPH_DIR", str(tmp_path))
    monkeypatch.setenv("UNISON_EVENT_GRAPH_FILE", "events.jsonl")
    monkeypatch.setenv("UNISON_EVENT_GRAPH_ENABLED", "true")

    result = run_thin_slice(text="hello", renderer_url=None, trace_dir=str(tmp_path / "traces"))
    assert result.trace_id

    jsonl = tmp_path / "events.jsonl"
    assert jsonl.exists()
    lines = jsonl.read_text(encoding="utf-8").splitlines()
    assert any("\"event_type\":\"input_received\"" in line for line in lines)
    assert any("\"event_type\":\"rom_built\"" in line for line in lines)

