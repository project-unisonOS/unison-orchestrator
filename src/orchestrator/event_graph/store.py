from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from unison_common import EventGraphAppend, EventGraphEvent, EventGraphQuery
from unison_common.redaction import redact_obj


def _now_unix_ms() -> int:
    return int(time.time() * 1000)


def _safe_json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class EventGraphStore:
    def append(self, batch: EventGraphAppend) -> int:
        raise NotImplementedError

    def query(self, query: EventGraphQuery) -> List[EventGraphEvent]:
        raise NotImplementedError


@dataclass(frozen=True)
class JsonlEventGraphStore(EventGraphStore):
    """
    Append-only JSONL store.

    Each line is a single `EventGraphEvent` dict. Queries are scan-based (Phase 3).
    """

    path: Path

    @classmethod
    def from_env(cls) -> "JsonlEventGraphStore":
        directory = Path(os.getenv("UNISON_EVENT_GRAPH_DIR", "event_graph"))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / os.getenv("UNISON_EVENT_GRAPH_FILE", "events.jsonl")
        return cls(path=path)

    def append(self, batch: EventGraphAppend) -> int:
        events: List[EventGraphEvent] = []
        for raw in batch.events:
            if isinstance(raw, EventGraphEvent):
                evt = raw
            elif isinstance(raw, dict):
                # best-effort coercion
                evt = EventGraphEvent(**raw)
            else:
                continue
            if not evt.trace_id:
                evt.trace_id = batch.trace_id  # type: ignore[misc]
            if evt.person_id is None:
                evt.person_id = batch.person_id  # type: ignore[misc]
            if evt.session_id is None:
                evt.session_id = batch.session_id  # type: ignore[misc]
            events.append(evt)

        if not events:
            return 0

        redact = os.getenv("UNISON_REDACT_EVENT_GRAPH", "true").lower() in {"1", "true", "yes", "on"}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            for evt in events:
                payload = evt.model_dump(mode="json")
                if redact:
                    payload = redact_obj(payload)
                f.write(_safe_json(payload) + "\n")
        return len(events)

    def query(self, query: EventGraphQuery) -> List[EventGraphEvent]:
        if not self.path.exists():
            return []
        limit = max(1, min(int(query.limit or 500), 5000))
        out: List[EventGraphEvent] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                try:
                    evt = EventGraphEvent(**data)
                except Exception:
                    continue
                if query.trace_id and evt.trace_id != query.trace_id:
                    continue
                if query.session_id and evt.session_id != query.session_id:
                    continue
                if query.person_id and evt.person_id != query.person_id:
                    continue
                out.append(evt)
                if len(out) >= limit:
                    break

        out.sort(key=lambda e: (e.ts_unix_ms, e.ts_monotonic_ns or 0))
        return out


def new_event(
    *,
    trace_id: str,
    event_type: str,
    person_id: Optional[str] = None,
    session_id: Optional[str] = None,
    actor: Optional[str] = None,
    attrs: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    causation_id: Optional[str] = None,
    parent_event_id: Optional[str] = None,
) -> EventGraphEvent:
    return EventGraphEvent(
        event_id=str(uuid.uuid4()),
        trace_id=trace_id,
        ts_unix_ms=_now_unix_ms(),
        ts_monotonic_ns=time.perf_counter_ns(),
        event_type=event_type,
        actor=actor,
        person_id=person_id,
        session_id=session_id,
        attrs=dict(attrs or {}),
        payload=dict(payload or {}),
        causation_id=causation_id,
        parent_event_id=parent_event_id,
        tags=[],
    )
