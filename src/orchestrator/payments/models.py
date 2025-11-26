from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, List


class PaymentTransactionStatus(str, enum.Enum):
    created = "created"
    pending = "pending"
    authorized = "authorized"
    succeeded = "succeeded"
    failed = "failed"
    refunded = "refunded"


@dataclass
class PaymentInstrument:
    """Tokenized payment instrument metadata (no PAN/ACH stored here)."""

    instrument_id: str
    person_id: str
    provider: str
    kind: str  # e.g., card, bank, paypal, venmo, zelle, crypto
    display_name: str | None = None
    brand: str | None = None
    last4: str | None = None
    expiry: str | None = None
    handle: str | None = None  # PayPal/Venmo/Zelle/crypto handle
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PaymentAuthorization:
    """User approval context attached to a transaction request."""

    approved: bool
    person_id: str
    surface: str | None = None  # e.g., voice, text, app
    consent_grant_id: str | None = None
    approved_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PaymentTransactionRequest:
    person_id: str
    instrument_id: str
    amount: float
    currency: str = "USD"
    description: str | None = None
    counterparty: str | None = None
    authorization_context: Dict[str, Any] = field(default_factory=dict)
    surface: str | None = None  # which modality requested this payment
    provider_token: str | None = None  # PSP token, if available


@dataclass
class PaymentTransaction:
    txn_id: str
    person_id: str
    instrument_id: str
    provider: str
    amount: float
    currency: str
    status: PaymentTransactionStatus
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    description: str | None = None
    counterparty: str | None = None
    provider_payload: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
