from orchestrator.payments import (
    PaymentInstrument,
    PaymentTransactionRequest,
    MockPaymentProvider,
    PaymentService,
    PaymentEventLogger,
    CardBankProvider,
    PaymentTransactionStatus,
)


class _NoopLogger(PaymentEventLogger):
    def __init__(self):
        super().__init__(context_graph_url=None)
        self.events = []

    def log_event(self, **kwargs):  # type: ignore[override]
        # Capture raw kwargs for assertions without network traffic.
        self.events.append(kwargs)


class _FakeClient:
    """Minimal fake for context/storage clients."""

    def __init__(self):
        self.data = {}
        self.calls = []

    def get(self, path, *, headers=None):
        self.calls.append(("get", path))
        if path.startswith("/profile/"):
            person_id = path.split("/")[-1]
            profile = self.data.get(("profile", person_id))
            return True, 200, {"profile": profile}
        if path.startswith("/kv/"):
            payload = self.data.get(("kv", path))
            if payload is None:
                return False, 404, {}
            return True, 200, payload
        return False, 404, {}

    def post(self, path, payload, *, headers=None):
        self.calls.append(("post", path, payload))
        if path.startswith("/profile/"):
            person_id = path.split("/")[-1]
            self.data[("profile", person_id)] = payload.get("profile")
            return True, 200, {"ok": True}
        return False, 404, {}

    def put(self, path, payload, *, headers=None):
        self.calls.append(("put", path, payload))
        self.data[("kv", path)] = payload
        return True, 200, {"ok": True}


def test_payment_service_register_and_create_txn():
    logger = _NoopLogger()
    provider = MockPaymentProvider()
    svc = PaymentService(provider, logger)

    instr = PaymentInstrument(
        instrument_id="i-1",
        person_id="p-1",
        provider="mock",
        kind="mock",
    )
    svc.register_instrument(instr)
    assert svc.get_instrument("i-1") is not None

    req = PaymentTransactionRequest(
        person_id="p-1", instrument_id="i-1", amount=12.5, currency="USD", description="test"
    )
    txn = svc.create_transaction(req)
    assert txn.status.value == "succeeded"
    assert len(logger.events) == 2  # instrument + txn
    assert logger.events[0]["event_type"] == "PaymentInstrumentRegistered"
    assert logger.events[1]["event_type"] == "PaymentTransactionSucceeded"
    assert logger.events[1]["amount"] == 12.5


def test_get_transaction_status_round_trip():
    logger = _NoopLogger()
    provider = MockPaymentProvider()
    svc = PaymentService(provider, logger)

    instr = PaymentInstrument(
        instrument_id="i-2",
        person_id="p-2",
        provider="mock",
        kind="mock",
    )
    svc.register_instrument(instr)
    txn = svc.create_transaction(
        PaymentTransactionRequest(person_id="p-2", instrument_id="i-2", amount=5.0)
    )

    fetched = svc.get_transaction_status(txn.txn_id)
    assert fetched.txn_id == txn.txn_id
    assert fetched.status == txn.status


def test_register_instrument_persists_profile_and_vault():
    logger = _NoopLogger()
    provider = MockPaymentProvider()
    fake = _FakeClient()
    svc = PaymentService(provider, logger, context_client=fake, storage_client=fake)

    instr = PaymentInstrument(
        instrument_id="i-3",
        person_id="p-3",
        provider="mock",
        kind="mock",
    )
    registered = svc.register_instrument(instr, token="tok_test_123")
    # Should annotate vault_key in metadata
    assert registered.metadata.get("vault_key")
    # Profile persisted with payments metadata
    profile = fake.data.get(("profile", "p-3"))
    assert profile and profile.get("payments", {}).get("instruments")
    # Vault payload stored
    vault_entries = [k for k in fake.data if k[0] == "kv"]
    assert vault_entries


def test_card_transaction_requires_token():
    logger = _NoopLogger()
    provider = CardBankProvider(api_key="test")
    fake = _FakeClient()
    svc = PaymentService(provider, logger, context_client=fake, storage_client=fake)
    instr = PaymentInstrument(
        instrument_id="i-card",
        person_id="p-card",
        provider=provider.name,
        kind="card",
    )
    svc.register_instrument(instr, token="tok_card")

    req = PaymentTransactionRequest(
        person_id="p-card", instrument_id="i-card", amount=10.0, currency="USD"
    )
    txn = svc.create_transaction(req)
    assert txn.status == PaymentTransactionStatus.pending
