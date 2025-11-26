from __future__ import annotations

import logging
from typing import Dict, Any

from .models import PaymentInstrument, PaymentTransaction, PaymentTransactionRequest
from .providers import PaymentProvider
from .logging import PaymentEventLogger

logger = logging.getLogger(__name__)


class PaymentService:
    """Coordinates provider calls and emits redacted events."""

    def __init__(
        self,
        provider: PaymentProvider,
        logger: PaymentEventLogger | None = None,
        context_client: Any | None = None,
        storage_client: Any | None = None,
    ):
        self.provider = provider
        self.logger = logger or PaymentEventLogger()
        self.context_client = context_client
        self.storage_client = storage_client
        # In-memory instrument registry for mocks; real deployment should use context profile storage.
        self._instruments: Dict[str, PaymentInstrument] = {}
        self._transactions: Dict[str, PaymentTransaction] = {}

    def register_instrument(self, instrument: PaymentInstrument, token: str | None = None) -> PaymentInstrument:
        registered = self.provider.register_instrument(instrument)
        vault_key = self._store_instrument_secret(registered, token)
        if vault_key:
            registered.metadata["vault_key"] = vault_key
        self._instruments[registered.instrument_id] = registered
        self._persist_instrument_metadata(registered)
        self.logger.log_event(
            event_type="PaymentInstrumentRegistered",
            subject_id=registered.instrument_id,
            person_id=registered.person_id,
            provider=registered.provider,
            status="registered",
            instrument_kind=registered.kind,
        )
        return registered

    def create_transaction(self, request: PaymentTransactionRequest) -> PaymentTransaction:
        instrument = self.get_instrument(request.instrument_id)
        if not request.provider_token:
            token = self._load_instrument_secret(instrument)
            request.provider_token = token
        txn = self.provider.create_transaction(request)
        self._transactions[txn.txn_id] = txn
        self.logger.log_event(
            event_type=self._event_type_for_status(txn.status),
            subject_id=txn.txn_id,
            person_id=txn.person_id,
            provider=txn.provider,
            status=txn.status.value,
            amount=txn.amount,
            currency=txn.currency,
            counterparty=txn.counterparty,
            surface=request.surface,
            instrument_kind=instrument.kind if instrument else None,
        )
        return txn

    def get_instrument(self, instrument_id: str) -> PaymentInstrument | None:
        return self._instruments.get(instrument_id)

    def get_transaction_status(self, txn_id: str) -> PaymentTransaction:
        if txn_id in self._transactions:
            return self._transactions[txn_id]
        # Delegate to provider if it owns the txn state.
        return self.provider.get_status(txn_id)

    def process_webhook(self, provider_name: str, payload: Dict[str, Any]) -> PaymentTransaction:
        if provider_name != getattr(self.provider, "name", ""):
            raise ValueError("unknown provider")
        txn = self.provider.handle_webhook(payload)
        self._transactions[txn.txn_id] = txn
        self.logger.log_event(
            event_type=self._event_type_for_status(txn.status),
            subject_id=txn.txn_id,
            person_id=txn.person_id,
            provider=txn.provider,
            status=txn.status.value,
            amount=txn.amount,
            currency=txn.currency,
            counterparty=txn.counterparty,
            instrument_kind=self._instruments.get(txn.instrument_id).kind if txn.instrument_id in self._instruments else None,
        )
        return txn

    @staticmethod
    def _event_type_for_status(status) -> str:
        if getattr(status, "value", None):
            status_value = status.value
        else:
            status_value = str(status)
        if status_value == "succeeded":
            return "PaymentTransactionSucceeded"
        if status_value == "failed":
            return "PaymentTransactionFailed"
        if status_value == "authorized":
            return "PaymentTransactionAuthorized"
        return "PaymentTransactionCreated"

    def _persist_instrument_metadata(self, instrument: PaymentInstrument) -> None:
        """Store non-sensitive instrument metadata in the context profile."""
        if not self.context_client:
            return
        try:
            ok, status, body = self.context_client.get(f"/profile/{instrument.person_id}")
            profile = {}
            if ok and status == 200 and isinstance(body, dict):
                profile = body.get("profile") or {}
            payments = profile.get("payments") if isinstance(profile.get("payments"), dict) else {}
            instruments = payments.get("instruments") if isinstance(payments.get("instruments"), list) else []
            instruments = [i for i in instruments if i.get("instrument_id") != instrument.instrument_id]
            instruments.append(
                {
                    "instrument_id": instrument.instrument_id,
                    "provider": instrument.provider,
                    "kind": instrument.kind,
                    "display_name": instrument.display_name,
                    "brand": instrument.brand,
                    "last4": instrument.last4,
                    "expiry": instrument.expiry,
                    "handle": instrument.handle,
                    "vault_key": instrument.metadata.get("vault_key"),
                    "created_at": instrument.created_at,
                }
            )
            payments["instruments"] = instruments
            profile["payments"] = payments
            self.context_client.post(f"/profile/{instrument.person_id}", {"profile": profile})
        except Exception as exc:  # best-effort
            logger.debug("payment instrument metadata persistence failed: %s", exc)

    def _store_instrument_secret(self, instrument: PaymentInstrument, token: str | None) -> str | None:
        """Persist a provider-issued token/handle to storage vault namespace."""
        if not token or not self.storage_client:
            return None
        vault_key = f"payment:{instrument.person_id}:{instrument.instrument_id}"
        payload = {"provider": instrument.provider, "kind": instrument.kind, "token": token}
        try:
            ok, status, _ = self.storage_client.put(f"/kv/vault/{vault_key}", {"value": payload})
            if ok and status in {200, 201}:
                return vault_key
            logger.debug("vault store failed for %s: status=%s ok=%s", vault_key, status, ok)
        except Exception as exc:
            logger.debug("vault store exception for %s: %s", vault_key, exc)
        return None

    def _load_instrument_secret(self, instrument: PaymentInstrument | None) -> str | None:
        """Fetch stored token from storage vault using metadata key."""
        if not instrument or not self.storage_client:
            return None
        vault_key = instrument.metadata.get("vault_key")
        if not vault_key:
            return None
        try:
            ok, status, body = self.storage_client.get(f"/kv/vault/{vault_key}")
            if ok and status == 200 and isinstance(body, dict):
                value = body.get("value") or {}
                return value.get("token")
        except Exception as exc:
            logger.debug("vault fetch failed for %s: %s", vault_key, exc)
        return None
