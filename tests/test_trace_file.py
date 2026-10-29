import json
from pathlib import Path
import pytest
from unison_common import TraceRecorder
from orchestrator.interaction.trace_file import write_input_trace


@pytest.mark.parametrize("identifier", ["../escape", "..\\escape", "/escape", "C:\\escape", "CON", "same-id"])
def test_trace_contents_cannot_choose_or_overwrite_file(tmp_path, identifier):
    trace = TraceRecorder(service="synthetic", trace_id=identifier)
    first = Path(write_input_trace(trace, str(tmp_path)))
    original = first.read_bytes()
    second = Path(write_input_trace(trace, str(tmp_path)))
    assert first != second
    assert first.parent == second.parent == tmp_path.resolve()
    assert first.read_bytes() == original
    assert json.loads(second.read_text())["trace_id"] == identifier
