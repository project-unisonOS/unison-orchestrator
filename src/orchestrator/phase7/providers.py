"""Credential-free deterministic providers for Phase 7 acceptance and replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


class ProviderError(RuntimeError):
    pass


class ProviderTimeout(ProviderError):
    pass


@dataclass
class FakeProvider:
    kind: str
    provider_id: str = "fake-primary"
    fail_once: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    compensations: list[str] = field(default_factory=list)
    _receipts: dict[str, str] = field(default_factory=dict)

    def execute(self, *, action: str, payload: dict[str, Any], idempotency_key: str) -> str:
        if idempotency_key in self._receipts:
            return self._receipts[idempotency_key]
        if self.fail_once:
            failure = self.fail_once
            self.fail_once = None
            if failure == "timeout":
                raise ProviderTimeout(f"{self.provider_id} timed out")
            raise ProviderError(f"{self.provider_id} failed: {failure}")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        receipt = hashlib.sha256(
            f"{self.provider_id}:{self.kind}:{action}:{encoded}".encode()
        ).hexdigest()[:20]
        self.calls.append({"action": action, "payload": payload, "receipt": receipt})
        self._receipts[idempotency_key] = receipt
        return receipt

    def compensate(self, receipt: str) -> None:
        if receipt not in self.compensations:
            self.compensations.append(receipt)


def default_fake_providers() -> dict[str, FakeProvider]:
    return {
        kind: FakeProvider(kind=kind)
        for kind in ("calendar", "mail", "tasks", "household", "contacts", "research", "travel")
    }
