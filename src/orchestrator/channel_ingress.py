"""Fail-closed orchestration boundary for normalized remote-channel events."""

from __future__ import annotations

from typing import Any


def accept_channel_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    required = {
        "version", "event_id", "provider", "provider_account_id", "direction",
        "provider_event_id", "nonce", "assurance", "bound_person_id",
        "bound_assistant_instance_id", "privacy", "capabilities",
    }
    if required - envelope.keys() or envelope.get("direction") != "inbound":
        return _denied("invalid-channel-envelope")
    if envelope.get("provider") != "telegram" or envelope.get("assurance") != "low":
        return _denied("unsupported-channel-profile")
    if not envelope.get("bound_person_id") or not envelope.get("bound_assistant_instance_id"):
        return _denied("unbound-channel")
    if envelope.get("sensitive_action_requested") or envelope.get("recovery_action_requested"):
        return {
            **_denied("step-up-required"),
            "step_up_required": True,
            "confirmation_required": True,
            "concise_text": "Continue on your trusted local device.",
            "simplified_text": "Use your home device to confirm this safely.",
        }
    return {
        "status": "accepted",
        "person_id": envelope["bound_person_id"],
        "assistant_instance_id": envelope["bound_assistant_instance_id"],
        "intent_input": envelope.get("text", ""),
        "channel_assurance": "low",
        "external_action_allowed": False,
        "outbound_mode": "draft-first",
        "confirmation_required": True,
        "privacy_notice": "Telegram can process this message; do not send secrets.",
        "can_cancel": True,
    }


def _denied(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "external_action_allowed": False,
        "outbound_mode": "none",
        "confirmation_required": False,
        "step_up_required": False,
        "privacy_notice": "Telegram can process this message; do not send secrets.",
        "recovery_guidance": "Use a trusted local UnisonOS device or revoke the channel.",
        "can_cancel": True,
    }
