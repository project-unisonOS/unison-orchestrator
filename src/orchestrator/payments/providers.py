from __future__ import annotations

import uuid
import time
import hmac
import hashlib
from typing import Protocol, Any, Dict
from .models import PaymentInstrument, PaymentTransaction, PaymentTransactionRequest, PaymentTransactionStatus


class PaymentProvider(Protocol):
    """Provider interface; implementations should be PCI-aware and avoid handling raw PAN here."""

    name: str

    def register_instrument(self, instrument: PaymentInstrument) -> PaymentInstrument:
        ...

    def create_transaction(self, request: PaymentTransactionRequest) -> PaymentTransaction:
        ...

    def get_status(self, txn_id: str) -> PaymentTransaction:
        ...

    def handle_webhook(self, payload: Dict[str, Any]) -> PaymentTransaction:
        ...


class MockPaymentProvider:
    """In-memory mock provider for dev/testing; never touches real payment rails."""

    name = "mock"

    def __init__(self):
        self._txns: dict[str, PaymentTransaction] = {}

    def register_instrument(self, instrument: PaymentInstrument) -> PaymentInstrument:
        # No-op validation; return as-is
        return instrument

    def create_transaction(self, request: PaymentTransactionRequest) -> PaymentTransaction:
        txn = PaymentTransaction(
            txn_id=str(uuid.uuid4()),
            person_id=request.person_id,
            instrument_id=request.instrument_id,
            provider=self.name,
            amount=request.amount,
            currency=request.currency,
            status=PaymentTransactionStatus.succeeded,
            description=request.description,
            counterparty=request.counterparty,
            provider_payload={"mock": True},
            events=[{"status": PaymentTransactionStatus.succeeded.value, "timestamp": time.time()}],
        )
        self._txns[txn.txn_id] = txn
        return txn

    def get_status(self, txn_id: str) -> PaymentTransaction:
        return self._txns[txn_id]

    def handle_webhook(self, payload: Dict[str, Any]) -> PaymentTransaction:
        txn_id = payload.get("txn_id")
        status = payload.get("status", PaymentTransactionStatus.succeeded.value)
        if txn_id in self._txns:
            txn = self._txns[txn_id]
            txn.status = PaymentTransactionStatus(status)
            txn.events.append({"status": status, "timestamp": time.time()})
            self._txns[txn_id] = txn
            return txn
        # If unknown, create a synthetic txn for testing.
        txn = PaymentTransaction(
            txn_id=txn_id or str(uuid.uuid4()),
            person_id=payload.get("person_id", ""),
            instrument_id=payload.get("instrument_id", ""),
            provider=self.name,
            amount=payload.get("amount", 0.0),
            currency=payload.get("currency", "USD"),
            status=PaymentTransactionStatus(status),
            description=payload.get("description"),
            counterparty=payload.get("counterparty"),
            provider_payload={"mock": True},
            events=[{"status": status, "timestamp": time.time()}],
        )
        self._txns[txn.txn_id] = txn
        return txn


class CardBankProvider:
    """Scaffold for a real PSP-backed provider (cards/ACH)."""

    name = "cardbank"

    def __init__(self, *, api_key: str | None = None, webhook_secret: str | None = None, sandbox: bool = True):
        # Real implementation will call external PSP; for now, reuse mock behavior with provider name set.
        self.api_key = api_key
        self.webhook_secret = webhook_secret or ""
        self.sandbox = sandbox
        self._mock = MockPaymentProvider()
        self._mock.name = self.name

    def register_instrument(self, instrument: PaymentInstrument) -> PaymentInstrument:
        # TODO: exchange provided token for PSP token; store minimal metadata only.
        return self._mock.register_instrument(instrument)

    def create_transaction(self, request: PaymentTransactionRequest) -> PaymentTransaction:
        # TODO: call PSP charge API using stored token keyed by instrument_id.
        if not request.provider_token:
            raise ValueError("provider token required for card/ACH transaction")
        txn = self._mock.create_transaction(request)
        txn.status = PaymentTransactionStatus.pending
        txn.events.append({"status": PaymentTransactionStatus.pending.value, "timestamp": time.time()})
        self._mock._txns[txn.txn_id] = txn
        return txn

    def get_status(self, txn_id: str) -> PaymentTransaction:
        # TODO: call PSP status API.
        return self._mock.get_status(txn_id)

    def handle_webhook(self, payload: Dict[str, Any]) -> PaymentTransaction:
        """Verify webhook signature and apply status update."""
        signature = payload.pop("_signature", None)
        raw_body = payload.pop("_raw_body", "")
        if not self._verify_signature(raw_body, signature):
            raise ValueError("invalid webhook signature")
        txn_id = payload.get("txn_id")
        status = payload.get("status", PaymentTransactionStatus.failed.value)
        if txn_id not in self._mock._txns:
            raise KeyError("unknown transaction")
        txn = self._mock._txns[txn_id]
        txn.status = PaymentTransactionStatus(status)
        txn.events.append({"status": status, "timestamp": time.time(), "webhook": True})
        self._mock._txns[txn_id] = txn
        return txn

    def _verify_signature(self, raw_body: str, signature: str | None) -> bool:
        if not self.webhook_secret:
            return True  # best-effort in dev
        if not signature:
            return False
        computed = hmac.new(self.webhook_secret.encode("utf-8"), raw_body.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, signature)
