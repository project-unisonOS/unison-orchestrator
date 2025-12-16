from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from orchestrator.clients import ServiceClients
from unison_common import ContextSnapshot, TraceRecorder


def _now_unix_ms() -> int:
    return int(time.time() * 1000)

_CACHE: dict[str, ContextSnapshot] = {}


def invalidate_context_cache(person_id: str) -> None:
    _CACHE.pop(person_id, None)


@dataclass(frozen=True)
class ContextReader:
    """
    Reads a best-effort context snapshot for routing/planning.

    Note: `unison-context` currently uses a minimal header-based guard in dev;
    we supply `x-test-role` when configured.
    """

    context_role: str = "service"

    @classmethod
    def from_env(cls) -> "ContextReader":
        return cls(context_role=os.getenv("UNISON_CONTEXT_ROLE", "service"))

    def read(self, *, clients: ServiceClients, person_id: str, trace: TraceRecorder) -> ContextSnapshot:
        ttl_ms = int(os.getenv("UNISON_CONTEXT_SNAPSHOT_CACHE_TTL_MS", "0"))
        if ttl_ms > 0:
            cached = _CACHE.get(person_id)
            if cached is not None and (_now_unix_ms() - cached.fetched_at_unix_ms) <= ttl_ms:
                trace.emit_event("context_cache_hit", {"person_id": person_id, "age_ms": _now_unix_ms() - cached.fetched_at_unix_ms})
                return cached

        headers = {"x-test-role": self.context_role} if self.context_role else {}
        profile: Optional[Dict[str, Any]] = None
        dashboard: Optional[Dict[str, Any]] = None

        with trace.span("context_read_started", {"person_id": person_id}):
            ok_p, _, body_p = clients.context.get(f"/profile/{person_id}", headers=headers or None)
            if ok_p and isinstance(body_p, dict) and body_p.get("ok") is True:
                p = body_p.get("profile")
                if isinstance(p, dict):
                    profile = p
            ok_d, _, body_d = clients.context.get(f"/dashboard/{person_id}", headers=headers or None)
            if ok_d and isinstance(body_d, dict) and body_d.get("ok") is True:
                d = body_d.get("dashboard")
                if isinstance(d, dict):
                    dashboard = d

        trace.emit_event(
            "context_read_ended",
            {"profile": profile is not None, "dashboard": dashboard is not None, "person_id": person_id},
        )
        snapshot = ContextSnapshot(
            person_id=person_id,
            profile=profile,
            dashboard=dashboard,
            fetched_at_unix_ms=_now_unix_ms(),
        )
        if ttl_ms > 0:
            _CACHE[person_id] = snapshot
        return snapshot
