from orchestrator.companion import CompanionSessionManager, ToolRegistry
from orchestrator.clients import ServiceClients, ServiceHttpClient


class _StubClient(ServiceHttpClient):
    def __init__(self):
        super().__init__(host="stub", port="0")


class _DummyResponse:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _clients():
    return ServiceClients(
        context=_StubClient(),
        storage=_StubClient(),
        policy=_StubClient(),
        inference=_StubClient(),
    )


def test_updates_get_policy_uses_post(monkeypatch):
    requests = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            requests.append(("get", url, None))
            return _DummyResponse(200, {"capabilities": []})

        def post(self, url, json=None):
            requests.append(("post", url, json))
            return _DummyResponse(200, {"ok": True, "policy": {"auto_apply": "manual"}})

    monkeypatch.setattr("httpx.Client", FakeClient)
    monkeypatch.setenv("UNISON_UPDATES_URL", "http://updates.example:8089")

    manager = CompanionSessionManager(_clients(), ToolRegistry())
    result = manager._execute_single_tool("updates.get_policy", {}, "person-1", "event-1")

    assert result["ok"] is True
    assert requests[-1] == (
        "post",
        "http://updates.example:8089/v1/tools/updates.get_policy",
        {"arguments": {"person_id": "person-1"}},
    )


def test_updates_set_policy_uses_post_and_person_id(monkeypatch):
    requests = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            return _DummyResponse(200, {"capabilities": []})

        def post(self, url, json=None):
            requests.append(("post", url, json))
            return _DummyResponse(200, {"ok": True})

    monkeypatch.setattr("httpx.Client", FakeClient)
    monkeypatch.setenv("UNISON_UPDATES_URL", "http://updates.example:8089")

    manager = CompanionSessionManager(_clients(), ToolRegistry())
    result = manager._execute_single_tool("updates.set_policy", {"policy_patch": {"auto_apply": "security_only"}}, "person-1", "event-1")

    assert result["ok"] is True
    assert requests == [
        (
            "post",
            "http://updates.example:8089/v1/tools/updates.set_policy",
            {"arguments": {"policy_patch": {"auto_apply": "security_only"}, "person_id": "person-1"}},
        )
    ]


def test_updates_apply_uses_post(monkeypatch):
    requests = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            return _DummyResponse(200, {"capabilities": []})

        def post(self, url, json=None):
            requests.append(("post", url, json))
            return _DummyResponse(200, {"ok": True, "job_id": "job-1"})

    monkeypatch.setattr("httpx.Client", FakeClient)
    monkeypatch.setenv("UNISON_UPDATES_URL", "http://updates.example:8089")

    manager = CompanionSessionManager(_clients(), ToolRegistry())
    result = manager._execute_single_tool("updates.apply", {"plan_id": "plan-1"}, "person-1", "event-1")

    assert result["ok"] is True
    assert requests == [
        (
            "post",
            "http://updates.example:8089/v1/tools/updates.apply",
            {"arguments": {"plan_id": "plan-1", "person_id": "person-1"}},
        )
    ]


def test_updates_tool_reports_service_errors(monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            return _DummyResponse(200, {"capabilities": []})

        def post(self, url, json=None):
            return _DummyResponse(503, {"detail": "down"}, text="down")

    monkeypatch.setattr("httpx.Client", FakeClient)
    monkeypatch.setenv("UNISON_UPDATES_URL", "http://updates.example:8089")

    manager = CompanionSessionManager(_clients(), ToolRegistry())
    result = manager._execute_single_tool("updates.apply", {"plan_id": "plan-1"}, "person-1", "event-1")

    assert "update service error (503)" in result["error"]
