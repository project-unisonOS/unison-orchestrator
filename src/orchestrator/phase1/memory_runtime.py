from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from orchestrator.clients import ServiceClients


@dataclass(frozen=True)
class MemoryOpResult:
    op_id: str
    ok: bool
    result: Dict[str, Any]
    error: Optional[str] = None


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)  # type: ignore[arg-type]
        else:
            out[k] = v
    return out


@dataclass(frozen=True)
class Phase1MemoryRuntime:
    """
    Executes Phase 1 MemoryOpV1 objects against `unison-context` (best-effort).
    """

    def execute(
        self,
        *,
        op: Dict[str, Any],
        clients: ServiceClients | None,
        person_id: Optional[str],
    ) -> MemoryOpResult:
        op_id = str(op.get("op_id") or "")
        if clients is None:
            return MemoryOpResult(op_id=op_id, ok=False, error="clients_not_configured", result={})
        if not person_id:
            return MemoryOpResult(op_id=op_id, ok=False, error="missing_person_id", result={})

        operation = op.get("op")
        target = op.get("target")
        payload = op.get("payload") if isinstance(op.get("payload"), dict) else {}

        if target not in {"profile", "preferences"}:
            return MemoryOpResult(op_id=op_id, ok=False, error="not_available", result={"target": target})

        ok_p, status_p, body_p = clients.context.get(f"/profile/{person_id}", headers={"x-test-role": "service"})
        profile: Dict[str, Any] = {}
        if ok_p and isinstance(body_p, dict) and body_p.get("ok") is True and isinstance(body_p.get("profile"), dict):
            profile = body_p["profile"]

        if operation == "query":
            if target == "profile":
                return MemoryOpResult(op_id=op_id, ok=True, result={"profile": profile, "status": status_p})
            prefs = profile.get("preferences") if isinstance(profile.get("preferences"), dict) else {}
            return MemoryOpResult(op_id=op_id, ok=True, result={"preferences": prefs, "status": status_p})

        if operation == "upsert":
            if target == "profile":
                next_profile = _deep_merge(profile, payload)
            else:
                next_profile = dict(profile)
                next_profile["preferences"] = _deep_merge(
                    profile.get("preferences") if isinstance(profile.get("preferences"), dict) else {},
                    payload,
                )
            ok_w, status_w, body_w = clients.context.post(
                f"/profile/{person_id}",
                {"profile": next_profile},
                headers={"x-test-role": "service"},
            )
            return MemoryOpResult(
                op_id=op_id,
                ok=bool(ok_w and status_w < 400),
                result={"status": status_w, "body": body_w or {}, "target": target},
                error=None if ok_w and status_w < 400 else "write_failed",
            )

        if operation == "delete":
            keys = payload.get("keys")
            if not isinstance(keys, list):
                return MemoryOpResult(op_id=op_id, ok=False, error="invalid_payload", result={"expected": {"keys": ["..."]}})
            if target == "profile":
                next_profile = dict(profile)
                for k in keys:
                    if isinstance(k, str):
                        next_profile.pop(k, None)
            else:
                next_profile = dict(profile)
                prefs = profile.get("preferences") if isinstance(profile.get("preferences"), dict) else {}
                next_prefs = dict(prefs)
                for k in keys:
                    if isinstance(k, str):
                        next_prefs.pop(k, None)
                next_profile["preferences"] = next_prefs
            ok_w, status_w, body_w = clients.context.post(
                f"/profile/{person_id}",
                {"profile": next_profile},
                headers={"x-test-role": "service"},
            )
            return MemoryOpResult(
                op_id=op_id,
                ok=bool(ok_w and status_w < 400),
                result={"status": status_w, "body": body_w or {}, "target": target},
                error=None if ok_w and status_w < 400 else "write_failed",
            )

        return MemoryOpResult(op_id=op_id, ok=False, error="unknown_op", result={"op": operation})


__all__ = ["Phase1MemoryRuntime", "MemoryOpResult"]

