"""Diagnostic files have server-generated names, never caller-selected targets."""
import json
import os
import tempfile
from pathlib import Path

from unison_common.redaction import redact_obj


def write_input_trace(trace, directory: str) -> str:
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    payload = trace.to_dict()
    if os.getenv("UNISON_REDACT_TRACE_ARTIFACTS", "true").lower() in {"1", "true", "yes", "on"}:
        payload = redact_obj(payload)
    # mkstemp uses exclusive creation and owner-only POSIX permissions. A
    # repeated trace ID cannot replace another person's diagnostic file.
    fd, name = tempfile.mkstemp(prefix="input-", suffix=".json", dir=root)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return name
