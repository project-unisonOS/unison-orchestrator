import json
import hmac
import hashlib

from orchestrator.payments import CardBankProvider, PaymentInstrument, PaymentTransactionRequest, PaymentTransactionStatus


def test_cardbank_webhook_signature_and_status_update():
    secret = "whsec_test"
    provider = CardBankProvider(api_key="key", webhook_secret=secret)

    instr = PaymentInstrument(
        instrument_id="i-webhook",
        person_id="p-webhook",
        provider=provider.name,
        kind="card",
    )
    provider.register_instrument(instr)
    req = PaymentTransactionRequest(
        person_id="p-webhook",
        instrument_id="i-webhook",
        amount=42.0,
        currency="USD",
        provider_token="tok_card",
    )
    txn = provider.create_transaction(req)
    assert txn.status == PaymentTransactionStatus.pending

    webhook_body = {
        "txn_id": txn.txn_id,
        "status": PaymentTransactionStatus.succeeded.value,
    }
    raw = json.dumps(webhook_body)
    sig = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    webhook_body["_raw_body"] = raw
    webhook_body["_signature"] = sig

    updated = provider.handle_webhook(webhook_body)
    assert updated.status == PaymentTransactionStatus.succeeded
    assert any(ev["status"] == PaymentTransactionStatus.succeeded.value for ev in updated.events)
