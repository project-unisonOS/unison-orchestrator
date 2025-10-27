"""
Vendored minimal unison_common for orchestrator.
Provides envelope validation and error class used by orchestrator.
"""
from typing import Any, Dict


class EnvelopeValidationError(Exception):
    pass


_ALLOWED_TOP_LEVEL_KEYS = {
    "timestamp",
    "source",
    "intent",
    "payload",
    "auth_scope",
    "safety_context",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EnvelopeValidationError(message)


def validate_event_envelope(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validates the minimal EventEnvelope contract used by orchestrator.
    Ensures:
    - envelope is a dict
    - required fields exist: timestamp, source, intent, payload
    - types: timestamp/source/intent are strings; payload is dict
    - optional: auth_scope is string if present; safety_context is dict if present
    - no unknown top-level fields
    Returns the envelope (possibly the same object) on success, raises EnvelopeValidationError otherwise.
    """
    if not isinstance(envelope, dict):
        raise EnvelopeValidationError("Envelope must be an object")

    # Unknown keys guard
    unknown = set(envelope.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    _require(len(unknown) == 0, f"Unknown top-level fields: {sorted(list(unknown))}")

    # Required fields
    for key in ("timestamp", "source", "intent", "payload"):
        _require(key in envelope, f"Missing required field '{key}'")

    # Types
    _require(isinstance(envelope.get("timestamp"), str), "Field 'timestamp' must be a string")
    _require(isinstance(envelope.get("source"), str), "Field 'source' must be a string")
    _require(isinstance(envelope.get("intent"), str), "Field 'intent' must be a string")
    _require(isinstance(envelope.get("payload"), dict), "Field 'payload' must be an object")

    # Optional fields
    if "auth_scope" in envelope:
        _require(isinstance(envelope.get("auth_scope"), str), "Field 'auth_scope' must be a string if present")
    if "safety_context" in envelope:
        _require(isinstance(envelope.get("safety_context"), dict), "Field 'safety_context' must be an object if present")

    return envelope
