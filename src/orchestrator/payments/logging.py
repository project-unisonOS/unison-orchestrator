from __future__ import annotations

import os
import httpx
import logging
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)


class PaymentEventLogger:
    """Best-effort logger to context-graph; avoids storing sensitive details."""

    def __init__(self, context_graph_url: str | None = None):
        self.context_graph_url = context_graph_url or os.getenv("UNISON_CONTEXT_GRAPH_URL")

    def log_event(
        self,
        *,
        event_type: str,
        subject_id: str,
        person_id: str,
        provider: str,
        status: str,
        amount: float | None = None,
        currency: str | None = None,
        counterparty: str | None = None,
        surface: str | None = None,
        instrument_kind: str | None = None,
    ) -> None:
        if not self.context_graph_url:
            return
        value: Dict[str, Any] = {
            "event_type": event_type,
            "payment_subject_id": subject_id,
            "provider": provider,
            "status": status,
            "timestamp": time.time(),
        }
        if amount is not None:
            value["amount_bucket"] = self._bucket_amount(amount)
        if currency:
            value["currency"] = currency
        if counterparty:
            value["counterparty"] = counterparty
        if surface:
            value["surface"] = surface
        if instrument_kind:
            value["instrument_kind"] = instrument_kind

        payload = {
            "user_id": person_id,
            "dimensions": [{"name": "payment", "value": value}],
        }
        try:
            with httpx.Client(timeout=2.0) as client:
                client.post(f"{self.context_graph_url}/context/update", json=payload)
        except Exception as exc:
            logger.debug("payment log emit failed: %s", exc)

    @staticmethod
    def _bucket_amount(amount: float) -> str:
        if amount < 10:
            return "<10"
        if amount < 50:
            return "10-50"
        if amount < 200:
            return "50-200"
        return "200+"
