from orchestrator.clients import ServiceHttpClient
from unison_common.baton import set_current_baton


def test_baton_header_forwarded(monkeypatch):
    captured = {}

    def fake_get(host, port, path, headers=None, **kwargs):
        captured["headers"] = headers
        return True, 200, {}

    monkeypatch.setattr("orchestrator.clients.http_get_json_with_retry", fake_get)

    set_current_baton("test-token")
    client = ServiceHttpClient("example", "1234")
    ok, status, _ = client.get("/health")

    assert ok and status == 200
    assert captured["headers"].get("X-Context-Baton") == "test-token"
    # Reset baton to avoid leaking into other tests
    set_current_baton(None)
