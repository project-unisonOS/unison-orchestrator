from __future__ import annotations

import uuid
from typing import Dict, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
import os
import logging

from ..payments import (
    PaymentInstrument,
    PaymentTransactionRequest,
    MockPaymentProvider,
    PaymentService,
    PaymentEventLogger,
    PaymentProvider,
)
from .routes import _auth_dependency
from ..policy_client import evaluate_capability
from unison_common.consent import require_consent, ConsentScopes

_require_payments_consent = os.getenv("UNISON_REQUIRE_PAYMENTS_CONSENT", "false").lower() in {"1", "true", "yes", "on"}
_require_payment_approval = os.getenv("UNISON_REQUIRE_PAYMENT_APPROVAL", "true").lower() in {"1", "true", "yes", "on"}


class PaymentInstrumentPayload(BaseModel):
    person_id: str = Field(..., description="Owner of the instrument")
    provider: str = Field(default="mock", description="Payment provider ID")
    kind: str = Field(default="mock", description="Instrument kind (card, bank, paypal, etc.)")
    display_name: str | None = None
    brand: str | None = None
    last4: str | None = None
    expiry: str | None = None
    handle: str | None = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    token: str | None = Field(default=None, description="Tokenized provider reference (no PAN/ACH)")


class PaymentTransactionPayload(BaseModel):
    person_id: str
    instrument_id: str
    amount: float
    currency: str = "USD"
    description: str | None = None
    counterparty: str | None = None
    authorization_context: Dict[str, Any] = Field(default_factory=dict)
    surface: str | None = Field(default=None, description="Requesting surface (voice, text, app)")


def register_payment_routes(app, *, metrics: Dict[str, int], service_clients) -> PaymentService:
    api = APIRouter()
    provider_name = os.getenv("UNISON_PAYMENTS_PROVIDER", "mock")
    provider: PaymentProvider
    webhook_secret = os.getenv("UNISON_PAYMENTS_WEBHOOK_SECRET")
    api_key = os.getenv("UNISON_PAYMENTS_API_KEY")
    if provider_name == "cardbank":
        from ..payments import CardBankProvider

        provider = CardBankProvider(api_key=api_key, webhook_secret=webhook_secret)
    else:
        provider = MockPaymentProvider()
        if provider_name != "mock":
            logging.getLogger(__name__).warning(
                "Unsupported provider '%s' configured; defaulting to mock. Implementations can be swapped later.",
                provider_name,
            )
    context_client = getattr(service_clients, "context", None)
    storage_client = getattr(service_clients, "storage", None)

    service = PaymentService(
        provider,
        PaymentEventLogger(),
        context_client=context_client,
        storage_client=storage_client,
    )

    @api.post("/payments/instruments")
    def register_instrument(
        payload: PaymentInstrumentPayload = Body(...),
        current_user: Dict[str, Any] = Depends(_auth_dependency()),
        consent=Depends(require_consent([ConsentScopes.INGEST_WRITE])) if _require_payments_consent else None,
    ):
        metrics["/payments/instruments"] += 1
        instrument = PaymentInstrument(
            instrument_id=str(uuid.uuid4()),
            person_id=payload.person_id,
            provider=payload.provider,
            kind=payload.kind,
            display_name=payload.display_name,
            brand=payload.brand,
            last4=payload.last4,
            expiry=payload.expiry,
            handle=payload.handle,
            metadata=payload.metadata,
        )
        registered = service.register_instrument(instrument, token=payload.token)
        return {"ok": True, "instrument": registered.__dict__}

    @api.post("/payments/transactions")
    def create_transaction(
        payload: PaymentTransactionPayload = Body(...),
        current_user: Dict[str, Any] = Depends(_auth_dependency()),
        consent=Depends(require_consent([ConsentScopes.INGEST_WRITE])) if _require_payments_consent else None,
    ):
        metrics["/payments/transactions"] += 1
        if _require_payment_approval and not payload.authorization_context.get("approved"):
            raise HTTPException(status_code=403, detail="payment requires explicit approval")
        policy_payload = {
            "capability_id": "unison.payments.transaction",
            "context": {
                "actor": current_user.get("username"),
                "roles": current_user.get("roles", []),
                "amount": payload.amount,
                "currency": payload.currency,
                "intent": "payment.transaction",
            },
        }
        try:
            ok, status, body = evaluate_capability(service_clients, policy_payload)
            decision = body.get("decision", {}) if isinstance(body, dict) else {}
            if not ok or status != 200 or not decision.get("allowed", False):
                raise HTTPException(status_code=403, detail="policy denied payment")
        except HTTPException:
            raise
        except Exception as exc:
            logging.getLogger(__name__).warning("policy evaluation failed, default deny: %s", exc)
            raise HTTPException(status_code=403, detail="policy denied payment")
        request = PaymentTransactionRequest(
            person_id=payload.person_id,
            instrument_id=payload.instrument_id,
            amount=payload.amount,
            currency=payload.currency,
            description=payload.description,
            counterparty=payload.counterparty,
            authorization_context=payload.authorization_context,
            surface=payload.surface,
        )
        txn = service.create_transaction(request)
        return {"ok": True, "transaction": txn.__dict__}

    @api.get("/payments/transactions/{txn_id}")
    def get_transaction_status(
        txn_id: str,
        current_user: Dict[str, Any] = Depends(_auth_dependency()),
    ):
        metrics["/payments/transactions"] += 1
        try:
            txn = service.get_transaction_status(txn_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="transaction not found")
        return {"ok": True, "transaction": txn.__dict__}

    @api.post("/payments/webhooks/{provider}")
    async def provider_webhook(
        provider: str,
        request: Request,
    ):
        raw_body = await request.body()
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        # Attach raw body for signature verification in provider
        if isinstance(payload, dict):
            payload["_raw_body"] = raw_body.decode("utf-8")
        signature = request.headers.get("X-Payments-Signature") or request.headers.get("X-Signature")
        payload["_signature"] = signature
        try:
            txn = service.process_webhook(provider, payload)
        except ValueError:
            raise HTTPException(status_code=404, detail="unknown provider")
        except KeyError:
            raise HTTPException(status_code=404, detail="transaction not found")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"webhook processing failed: {exc}")
        return {"ok": True, "transaction": txn.__dict__}

    app.include_router(api)
    return service
