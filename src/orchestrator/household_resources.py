"""Fair, bounded resource scheduling for assistants sharing one appliance."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import RLock
from typing import Any
from uuid import uuid4

from unison_common.household import AssistantResourceQuota


class ResourceQuotaExceeded(RuntimeError):
    pass


class UnknownAssistant(RuntimeError):
    pass


@dataclass(frozen=True)
class ResourceLease:
    lease_id: str
    assistant_instance_id: str
    task_id: str
    cpu_units: int
    memory_mb: int


class HouseholdResourceScheduler:
    """Round-robin scheduler with per-assistant queues and hard quotas.

    Task payloads are intentionally not stored here. The scheduler operates on
    opaque task identifiers so resource administration cannot reveal prompts.
    """

    def __init__(self, *, total_concurrent_tasks: int):
        if total_concurrent_tasks < 1:
            raise ValueError("total_concurrent_tasks must be positive")
        self.total_concurrent_tasks = total_concurrent_tasks
        self._quotas: dict[str, AssistantResourceQuota] = {}
        self._queues: dict[str, deque[str]] = {}
        self._active: dict[str, dict[str, ResourceLease]] = {}
        self._rotation: deque[str] = deque()
        self._lock = RLock()

    def register(self, quota: AssistantResourceQuota) -> None:
        with self._lock:
            assistant = quota.assistant_instance_id
            if assistant not in self._quotas:
                self._rotation.append(assistant)
                self._queues[assistant] = deque()
                self._active[assistant] = {}
            self._quotas[assistant] = quota

    def submit(self, assistant_instance_id: str, task_id: str) -> None:
        with self._lock:
            quota = self._require(assistant_instance_id)
            queue = self._queues[assistant_instance_id]
            if len(queue) >= quota.max_queued_tasks:
                raise ResourceQuotaExceeded("assistant queue quota exhausted")
            if task_id in queue or task_id in self._active[assistant_instance_id]:
                raise ResourceQuotaExceeded("task is already scheduled")
            queue.append(task_id)

    def dispatch_next(self) -> ResourceLease | None:
        with self._lock:
            if self.active_count >= self.total_concurrent_tasks or not self._rotation:
                return None
            for _ in range(len(self._rotation)):
                assistant = self._rotation[0]
                self._rotation.rotate(-1)
                quota = self._quotas[assistant]
                if not self._queues[assistant]:
                    continue
                if len(self._active[assistant]) >= quota.max_concurrent_tasks:
                    continue
                task_id = self._queues[assistant].popleft()
                lease = ResourceLease(
                    lease_id=f"lease_{uuid4().hex}",
                    assistant_instance_id=assistant,
                    task_id=task_id,
                    cpu_units=quota.cpu_units,
                    memory_mb=quota.memory_mb,
                )
                self._active[assistant][lease.lease_id] = lease
                return lease
            return None

    def complete(self, lease_id: str) -> bool:
        with self._lock:
            for active in self._active.values():
                if lease_id in active:
                    del active[lease_id]
                    return True
        return False

    @property
    def active_count(self) -> int:
        return sum(len(items) for items in self._active.values())

    def operational_snapshot(self) -> dict[str, Any]:
        """Return capacity facts only; task identifiers and payloads are excluded."""
        with self._lock:
            return {
                "total_concurrent_tasks": self.total_concurrent_tasks,
                "assistants": {
                    assistant: {
                        "active_count": len(self._active[assistant]),
                        "queued_count": len(self._queues[assistant]),
                        "max_concurrent_tasks": quota.max_concurrent_tasks,
                        "max_queued_tasks": quota.max_queued_tasks,
                        "cpu_units": quota.cpu_units,
                        "memory_mb": quota.memory_mb,
                    }
                    for assistant, quota in sorted(self._quotas.items())
                },
                "contains_task_content": False,
            }

    def restart(self) -> None:
        """Recover interrupted opaque tasks at the front of each owner's queue."""
        with self._lock:
            for assistant, active in self._active.items():
                for lease in reversed(list(active.values())):
                    self._queues[assistant].appendleft(lease.task_id)
                active.clear()

    def _require(self, assistant_instance_id: str) -> AssistantResourceQuota:
        try:
            return self._quotas[assistant_instance_id]
        except KeyError as exc:
            raise UnknownAssistant("assistant is not registered") from exc

