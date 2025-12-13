from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from orchestrator.clients import ServiceClients
from unison_common import ContextWriteBehindBatch, TraceRecorder


def _now_unix_ms() -> int:
    return int(time.time() * 1000)


def _default_context_headers() -> Dict[str, str]:
    role = os.getenv("UNISON_CONTEXT_ROLE", "service")
    return {"x-test-role": role} if role else {}


@dataclass(frozen=True)
class ContextWriteBehindQueue:
    """
    Minimal write-behind queue that persists small context updates asynchronously.

    Phase 2: in-process queue + background worker thread; best-effort flushes.
    """

    maxsize: int = 1000

    def __post_init__(self) -> None:
        object.__setattr__(self, "_q", queue.Queue(maxsize=self.maxsize))
        object.__setattr__(self, "_worker", None)
        object.__setattr__(self, "_stop", threading.Event())

    def start(self, *, clients: ServiceClients, trace: Optional[TraceRecorder] = None) -> None:
        if self._worker is not None:
            return

        def _run():
            while not self._stop.is_set():
                try:
                    batch = self._q.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    self._flush_one(clients=clients, batch=batch, trace=trace)
                finally:
                    self._q.task_done()

        t = threading.Thread(target=_run, name="context-write-behind", daemon=True)
        object.__setattr__(self, "_worker", t)
        t.start()

    def stop(self) -> None:
        self._stop.set()

    def enqueue_last_interaction(
        self,
        *,
        person_id: str,
        session_id: str,
        trace_id: str,
        input_text: str,
    ) -> ContextWriteBehindBatch:
        batch = ContextWriteBehindBatch(
            batch_id=str(uuid.uuid4()),
            person_id=person_id,
            session_id=session_id,
            queued_at_unix_ms=_now_unix_ms(),
            updates=[
                {
                    "op": "kv.put",
                    "tier": "B",
                    "items": {
                        f"{person_id}:profile:last_interaction": {
                            "trace_id": trace_id,
                            "session_id": session_id,
                            "text": input_text[:500],
                            "ts_unix_ms": _now_unix_ms(),
                        }
                    },
                }
            ],
        )
        self.enqueue(batch)
        return batch

    def enqueue(self, batch: ContextWriteBehindBatch) -> None:
        try:
            self._q.put_nowait(batch)
        except queue.Full:
            # Drop on overload (best-effort).
            pass

    def flush_sync(self, *, clients: ServiceClients, batch: ContextWriteBehindBatch, trace: Optional[TraceRecorder] = None) -> Tuple[bool, Optional[str]]:
        return self._flush_one(clients=clients, batch=batch, trace=trace)

    def _flush_one(self, *, clients: ServiceClients, batch: ContextWriteBehindBatch, trace: Optional[TraceRecorder]) -> Tuple[bool, Optional[str]]:
        headers = _default_context_headers()
        ok_all = True
        last_error: Optional[str] = None
        span_ctx = {"batch_id": batch.batch_id, "person_id": batch.person_id, "updates": len(batch.updates)}
        def _do_flush():
            nonlocal ok_all, last_error
            for upd in batch.updates:
                if not isinstance(upd, dict) or upd.get("op") != "kv.put":
                    continue
                tier = upd.get("tier") or "B"
                items = upd.get("items") or {}
                if not isinstance(items, dict):
                    continue
                payload: Dict[str, Any] = {"person_id": batch.person_id, "tier": tier, "items": items}
                ok, status, body = clients.context.post("/kv/put", payload, headers=headers or None)
                if not ok or status >= 400 or not (isinstance(body, dict) and body.get("ok") is True):
                    ok_all = False
                    last_error = f"context kv.put failed status={status}"

        if trace:
            with trace.span("context_write_flushed", span_ctx):
                _do_flush()
            if not ok_all:
                trace.emit_event("context_write_failed", {"error": last_error})
            trace.emit_event("context_write_flushed", {"ok": ok_all, "error": last_error})
            return ok_all, last_error

        _do_flush()
        return ok_all, last_error
