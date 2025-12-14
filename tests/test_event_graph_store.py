import json
from pathlib import Path

from orchestrator.event_graph.store import JsonlEventGraphStore, new_event
from unison_common import EventGraphAppend, EventGraphQuery


def test_jsonl_event_graph_store_append_and_query(tmp_path):
    path = tmp_path / "events.jsonl"
    store = JsonlEventGraphStore(path=path)
    evt1 = new_event(trace_id="t1", event_type="input_received", person_id="p1", session_id="s1", attrs={"a": 1})
    evt2 = new_event(trace_id="t1", event_type="rom_built", person_id="p1", session_id="s1", attrs={"b": 2}, causation_id=evt1.event_id)

    count = store.append(EventGraphAppend(trace_id="t1", session_id="s1", person_id="p1", events=[evt1, evt2]))
    assert count == 2
    assert path.exists()

    # File is JSONL; each line parses.
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    json.loads(lines[0])

    got = store.query(EventGraphQuery(trace_id="t1", limit=10))
    assert [e.event_type for e in got] == ["input_received", "rom_built"]


def test_jsonl_event_graph_redacts_sensitive_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("UNISON_REDACT_EVENT_GRAPH", "true")
    path = tmp_path / "events.jsonl"
    store = JsonlEventGraphStore(path=path)
    evt = new_event(
        trace_id="t2",
        event_type="input_received",
        person_id="p1",
        session_id="s1",
        attrs={"authorization": "Bearer abc.def.ghi"},
        payload={"email": "user@example.com"},
    )
    store.append(EventGraphAppend(trace_id="t2", session_id="s1", person_id="p1", events=[evt]))
    got = store.query(EventGraphQuery(trace_id="t2", limit=10))
    assert got[0].attrs["authorization"] == "[REDACTED]"
    assert got[0].payload["email"] == "[REDACTED_EMAIL]"
