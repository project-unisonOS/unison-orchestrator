#!/usr/bin/env python3

from __future__ import annotations

import argparse
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
    from orchestrator.dev_thin_slice import run_thin_slice

    parser = argparse.ArgumentParser(description="UnisonOS thin vertical slice (dev).")
    parser.add_argument("text", help="Text input")
    parser.add_argument("--person-id", default="local-person")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--renderer-url", default=None, help="e.g. http://localhost:8092")
    parser.add_argument("--trace-dir", default="traces", help="Directory for trace artifacts")
    args = parser.parse_args(argv)

    result = run_thin_slice(
        text=args.text,
        person_id=args.person_id,
        session_id=args.session_id,
        renderer_url=args.renderer_url,
        trace_dir=args.trace_dir,
    )
    print(
        f"trace_id={result.trace_id} session_id={result.session_id} "
        f"renderer_ok={result.renderer_ok} trace_path={result.trace_path}"
    )
    return 0 if result.tool_result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

