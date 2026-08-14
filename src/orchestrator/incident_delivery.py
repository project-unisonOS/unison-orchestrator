"""Restart-safe delivery outbox for non-authoritative incident projections."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


class IncidentDeliveryOutbox:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(os.getenv("UNISON_INCIDENT_OUTBOX_ROOT", "/data/incident-outbox"))

    def enqueue(self, incident_id: str, envelope: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{incident_id}.json"
        with NamedTemporaryFile("w", dir=self.root, delete=False, encoding="utf-8") as handle:
            json.dump(envelope, handle, sort_keys=True)
            handle.flush()
            temporary = Path(handle.name)
        temporary.replace(target)

    def pending(self) -> list[tuple[Path, dict[str, Any]]]:
        if not self.root.exists():
            return []
        records = []
        for path in sorted(self.root.glob("*.json")):
            records.append((path, json.loads(path.read_text(encoding="utf-8"))))
        return records

    def acknowledge(self, path: Path) -> None:
        path.unlink(missing_ok=True)
