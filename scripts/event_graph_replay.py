#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


def _ensure_src_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def main(argv: Optional[list[str]] = None) -> int:
    _ensure_src_on_path()
    from orchestrator.event_graph.store import JsonlEventGraphStore
    from unison_common import EventGraphQuery

    parser = argparse.ArgumentParser(description="Replay Event Graph events for a trace.")
    parser.add_argument("trace_id")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--json", action="store_true", help="Output raw JSON array.")
    args = parser.parse_args(argv)

    store = JsonlEventGraphStore.from_env()
    events = store.query(EventGraphQuery(trace_id=args.trace_id, limit=args.limit))

    if args.json:
        print(json.dumps([e.model_dump(mode="json") for e in events], indent=2))
        return 0

    for evt in events:
        t = evt.ts_unix_ms
        et = evt.event_type
        meta = evt.attrs or {}
        print(f"{t} {et} {meta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

