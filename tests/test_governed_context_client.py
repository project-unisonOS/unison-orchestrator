import pytest

from orchestrator.clients import ServiceClients
from orchestrator.context_client import ContextChoiceRequired, governed_prompt_context


class _Fake:
    def __init__(self, response=(False, 500, None)):
        self.response = response
        self.calls = []

    def get(self, path, *, headers=None):
        return False, 500, None

    def post(self, path, payload, *, headers=None):
        self.calls.append((path, payload, headers))
        return self.response


def _clients(context):
    unused = _Fake()
    return ServiceClients(context=context, storage=unused, policy=unused, inference=unused)


def test_prompt_context_never_guesses_a_space():
    context = _Fake()
    with pytest.raises(ContextChoiceRequired):
        governed_prompt_context(
            _clients(context), person_id="alice", space_ids=[], query="Sam", purpose="answer"
        )
    assert context.calls == []


def test_prompt_context_preserves_exact_boundary_and_privacy_state():
    context = _Fake((True, 200, {
        "records": [{"record_id": "r1", "content": {"fact": "allowed"}}],
        "privacy": {
            "active_space_ids": ["private-alice"],
            "space_kinds": ["private"],
            "purpose": "answer",
            "disclosure_allowed": False,
        },
    }))
    snapshot = governed_prompt_context(
        _clients(context), person_id="alice", space_ids=["private-alice"],
        query="allowed", purpose="answer", headers={"Authorization": "Bearer bound"},
    )
    assert snapshot.records[0]["content"] == {"fact": "allowed"}
    assert snapshot.privacy["disclosure_allowed"] is False
    assert context.calls[0][1]["space_ids"] == ["private-alice"]


def test_prompt_context_rejects_boundary_substitution():
    context = _Fake((True, 200, {
        "records": [],
        "privacy": {"active_space_ids": ["shared-other"]},
    }))
    with pytest.raises(RuntimeError, match="changed the requested boundary"):
        governed_prompt_context(
            _clients(context), person_id="alice", space_ids=["private-alice"],
            query="x", purpose="answer",
        )
