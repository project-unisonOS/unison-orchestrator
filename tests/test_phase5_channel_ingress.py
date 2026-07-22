from orchestrator.channel_ingress import accept_channel_envelope


def envelope(**updates):
    value = {
        "version": "5.0", "event_id": "evt_1", "provider": "telegram",
        "provider_account_id": "bot-alex", "direction": "inbound", "provider_event_id": "7",
        "nonce": "long-random-nonce-value", "assurance": "low", "bound_person_id": "person-a",
        "bound_assistant_instance_id": "assistant-a", "privacy": {"provider": "telegram"},
        "capabilities": {"text": True}, "text": "summarize my day",
        "sensitive_action_requested": False, "recovery_action_requested": False,
    }
    value.update(updates)
    return value


def test_safe_remote_text_is_person_bound_but_external_action_stays_draft_first():
    result = accept_channel_envelope(envelope())
    assert result["status"] == "accepted"
    assert result["person_id"] == "person-a"
    assert result["external_action_allowed"] is False
    assert result["outbound_mode"] == "draft-first"
    assert result["confirmation_required"] is True


def test_sensitive_and_recovery_requests_do_not_reach_execution():
    for field in ("sensitive_action_requested", "recovery_action_requested"):
        result = accept_channel_envelope(envelope(**{field: True}))
        assert result["status"] == "step-up-required"
        assert result["step_up_required"] is True
        assert result["external_action_allowed"] is False


def test_unbound_invalid_or_wrong_profile_envelopes_fail_closed_without_oracle():
    for candidate in (
        envelope(bound_person_id=None), envelope(provider="unknown"), {"text": "hello"},
    ):
        result = accept_channel_envelope(candidate)
        assert result["external_action_allowed"] is False
        assert result["recovery_guidance"]
        assert "privacy_notice" in result
