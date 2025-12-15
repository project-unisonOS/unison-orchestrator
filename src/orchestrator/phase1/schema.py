from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import jsonschema
from jsonschema import RefResolver


def _candidate_schema_dirs(start: Path) -> list[Path]:
    candidates: list[Path] = []
    for parent in [start, *start.parents]:
        candidates.append(parent / "schemas" / "phase1")
    return candidates


def find_phase1_schema_dir() -> Path:
    override = os.getenv("UNISON_PHASE1_SCHEMA_DIR")
    if override:
        p = Path(override).expanduser().resolve()
        if (p / "plan.v1.schema.json").exists():
            return p
        raise FileNotFoundError(f"UNISON_PHASE1_SCHEMA_DIR does not contain plan.v1.schema.json: {p}")

    here = Path(__file__).resolve()
    for base in _candidate_schema_dirs(here):
        if (base / "plan.v1.schema.json").exists():
            return base

    # Default workspace layout: ../schemas/phase1 from unison-orchestrator repo root.
    repo_root = here.parents[3]
    fallback = (repo_root.parent / "schemas" / "phase1").resolve()
    if (fallback / "plan.v1.schema.json").exists():
        return fallback

    raise FileNotFoundError("Could not locate schemas/phase1/*.schema.json (set UNISON_PHASE1_SCHEMA_DIR).")


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Phase1SchemaValidator:
    schema_dir: Path
    store: Dict[str, Dict[str, Any]]

    @classmethod
    def load(cls, *, schema_dir: Optional[Path] = None) -> "Phase1SchemaValidator":
        base = (schema_dir or find_phase1_schema_dir()).resolve()
        store: Dict[str, Dict[str, Any]] = {}
        for schema_path in sorted(base.glob("*.schema.json")):
            schema = _load_json(schema_path)
            store[schema_path.name] = schema
            schema_id = schema.get("$id")
            if isinstance(schema_id, str) and schema_id:
                store[schema_id] = schema
        return cls(schema_dir=base, store=store)

    def _validator(self, schema_name: str) -> jsonschema.Draft202012Validator:
        schema = self.store.get(schema_name)
        if not schema:
            raise KeyError(f"missing schema: {schema_name} in {self.schema_dir}")
        resolver = RefResolver(base_uri=self.schema_dir.as_uri() + "/", referrer=schema, store=self.store)
        return jsonschema.Draft202012Validator(schema, resolver=resolver)

    def validate(self, schema_name: str, obj: Any) -> None:
        validator = self._validator(schema_name)
        errors = sorted(validator.iter_errors(obj), key=lambda e: e.path)
        if errors:
            msg = "; ".join([f"{'/'.join([str(p) for p in e.path]) or '$'}: {e.message}" for e in errors[:5]])
            raise ValueError(f"{schema_name} validation failed: {msg}")


__all__ = ["Phase1SchemaValidator", "find_phase1_schema_dir"]

