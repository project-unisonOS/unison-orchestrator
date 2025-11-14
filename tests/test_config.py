import os
from types import SimpleNamespace

import pytest

from src.orchestrator.clients import ServiceHttpClient
from src.orchestrator.config import OrchestratorSettings


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    keys = [
        "UNISON_ALLOWED_HOSTS",
        "UNISON_ROUTING_STRATEGY",
        "UNISON_CONTEXT_HOST",
        "UNISON_CONTEXT_PORT",
        "UNISON_STORAGE_HOST",
        "UNISON_STORAGE_PORT",
        "UNISON_POLICY_HOST",
        "UNISON_POLICY_PORT",
        "UNISON_INFERENCE_HOST",
        "UNISON_INFERENCE_PORT",
        "UNISON_CONFIRM_TTL",
        "UNISON_REQUIRE_CONSENT",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    yield


def test_settings_defaults():
    settings = OrchestratorSettings.from_env()

    assert settings.allowed_hosts == ["localhost", "127.0.0.1", "orchestrator"]
    assert settings.routing_strategy == "rule_based"
    assert settings.confirm_ttl_seconds == 300
    assert settings.require_consent is False

    endpoints = settings.endpoints
    assert endpoints.context_host == "context"
    assert endpoints.context_port == "8081"
    assert endpoints.storage_host == "storage"
    assert endpoints.storage_port == "8082"
    assert endpoints.policy_host == "policy"
    assert endpoints.policy_port == "8083"
    assert endpoints.inference_host == "inference"
    assert endpoints.inference_port == "8087"


def test_settings_from_env_overrides(monkeypatch):
    monkeypatch.setenv("UNISON_ALLOWED_HOSTS", "api.example.com, 10.0.0.5")
    monkeypatch.setenv("UNISON_ROUTING_STRATEGY", "learning")
    monkeypatch.setenv("UNISON_CONTEXT_HOST", "ctx")
    monkeypatch.setenv("UNISON_CONTEXT_PORT", "9001")
    monkeypatch.setenv("UNISON_STORAGE_HOST", "stor")
    monkeypatch.setenv("UNISON_STORAGE_PORT", "9002")
    monkeypatch.setenv("UNISON_POLICY_HOST", "pol")
    monkeypatch.setenv("UNISON_POLICY_PORT", "9003")
    monkeypatch.setenv("UNISON_INFERENCE_HOST", "inf")
    monkeypatch.setenv("UNISON_INFERENCE_PORT", "9004")
    monkeypatch.setenv("UNISON_CONFIRM_TTL", "123")
    monkeypatch.setenv("UNISON_REQUIRE_CONSENT", "true")

    settings = OrchestratorSettings.from_env()

    assert settings.allowed_hosts == ["api.example.com", "10.0.0.5"]
    assert settings.routing_strategy == "learning"
    assert settings.confirm_ttl_seconds == 123
    assert settings.require_consent is True

    endpoints = settings.endpoints
    assert endpoints.context_host == "ctx"
    assert endpoints.context_port == "9001"
    assert endpoints.storage_host == "stor"
    assert endpoints.storage_port == "9002"
    assert endpoints.policy_host == "pol"
    assert endpoints.policy_port == "9003"
    assert endpoints.inference_host == "inf"
    assert endpoints.inference_port == "9004"


def test_service_http_client_uses_retry_defaults(monkeypatch):
    captured = {}

    def fake_get(host, port, path, headers=None, **kwargs):
        captured["args"] = (host, port, path, headers, kwargs)
        return True, 200, {"status": "ok"}

    monkeypatch.setattr(
        "src.orchestrator.clients.http_get_json_with_retry",
        fake_get,
    )

    client = ServiceHttpClient("svc", "8080")
    result = client.get("/health", headers={"X-Test": "1"})

    assert result == (True, 200, {"status": "ok"})
    host, port, path, headers, kwargs = captured["args"]
    assert host == "svc"
    assert port == "8080"
    assert path == "/health"
    assert headers == {"X-Test": "1"}
    assert kwargs == {"max_retries": 3, "base_delay": 0.1, "max_delay": 2.0, "timeout": 2.0}


def test_service_http_client_post_and_put(monkeypatch):
    post_calls = {}
    put_calls = {}

    def fake_post(host, port, path, payload, headers=None, **kwargs):
        post_calls["args"] = (host, port, path, payload, headers, kwargs)
        return False, 503, None

    def fake_put(host, port, path, payload, headers=None, **kwargs):
        put_calls["args"] = (host, port, path, payload, headers, kwargs)
        return True, 201, {"ok": True}

    monkeypatch.setattr("src.orchestrator.clients.http_post_json_with_retry", fake_post)
    monkeypatch.setattr("src.orchestrator.clients.http_put_json_with_retry", fake_put)

    client = ServiceHttpClient("svc", "9000")

    post_result = client.post("/ready", {"hello": "world"})
    assert post_result == (False, 503, None)
    host, port, path, payload, headers, kwargs = post_calls["args"]
    assert (host, port, path) == ("svc", "9000", "/ready")
    assert payload == {"hello": "world"}
    assert headers is None
    assert kwargs["max_retries"] == 3

    put_result = client.put("/kv/key", {"value": 1}, headers={"A": "b"})
    assert put_result == (True, 201, {"ok": True})
    host, port, path, payload, headers, kwargs = put_calls["args"]
    assert path == "/kv/key"
    assert headers == {"A": "b"}
    assert payload == {"value": 1}
