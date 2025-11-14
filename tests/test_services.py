from types import SimpleNamespace
from unittest.mock import Mock

from src.orchestrator.services.health import fetch_core_health, fetch_policy_rules
from src.orchestrator.services.policy import readiness_allowed


def make_clients():
    return SimpleNamespace(
        context=Mock(),
        storage=Mock(),
        policy=Mock(),
        inference=Mock(),
    )


def test_readiness_allowed_true_and_false():
    clients = make_clients()
    clients.policy.post.return_value = (True, 200, {"decision": {"allowed": True}})

    assert readiness_allowed(clients, event_id="evt-1") is True
    clients.policy.post.assert_called_once_with(
        "/evaluate",
        {
            "capability_id": "test.ACTION",
            "context": {"actor": "local-user", "intent": "readiness-check"},
        },
        headers={"X-Event-ID": "evt-1"},
    )

    clients.policy.post.reset_mock()
    clients.policy.post.return_value = (True, 200, {"decision": {"allowed": False}})
    assert readiness_allowed(clients, event_id="evt-2") is False

    clients.policy.post.return_value = (False, 500, None)
    assert readiness_allowed(clients, event_id="evt-3") is False


def test_fetch_core_health_returns_mapping():
    clients = make_clients()
    clients.context.get.return_value = ("ctx", 200, {})
    clients.storage.get.return_value = ("stor", 200, {})
    clients.policy.get.return_value = ("pol", 200, {})
    clients.inference.get.return_value = ("inf", 200, {})

    result = fetch_core_health(clients, headers={"X": "1"})
    assert result["context"] == ("ctx", 200, {})
    assert result["storage"] == ("stor", 200, {})
    assert result["policy"] == ("pol", 200, {})
    assert result["inference"] == ("inf", 200, {})

    clients.context.get.assert_called_once_with("/health", headers={"X": "1"})
    clients.storage.get.assert_called_once_with("/health", headers={"X": "1"})
    clients.policy.get.assert_called_once_with("/health", headers={"X": "1"})
    clients.inference.get.assert_called_once_with("/health", headers={"X": "1"})


def test_fetch_policy_rules_targets_summary_endpoint():
    clients = make_clients()
    clients.policy.get.return_value = ("rules", 200, {"count": 5})

    result = fetch_policy_rules(clients, headers={"X": "2"})
    assert result == ("rules", 200, {"count": 5})
    clients.policy.get.assert_called_once_with("/rules/summary", headers={"X": "2"})
