import pytest

from orchestrator.household_resources import HouseholdResourceScheduler, ResourceQuotaExceeded
from unison_common.household import AssistantResourceQuota


def scheduler():
    item = HouseholdResourceScheduler(total_concurrent_tasks=2)
    item.register(AssistantResourceQuota(
        assistant_instance_id="assistant-alice", max_concurrent_tasks=1, max_queued_tasks=3
    ))
    item.register(AssistantResourceQuota(
        assistant_instance_id="assistant-bob", max_concurrent_tasks=1, max_queued_tasks=2
    ))
    return item


def test_round_robin_and_per_assistant_concurrency_prevent_starvation():
    item = scheduler()
    item.submit("assistant-alice", "alice-1")
    item.submit("assistant-alice", "alice-2")
    item.submit("assistant-bob", "bob-1")
    first = item.dispatch_next()
    second = item.dispatch_next()
    assert {first.assistant_instance_id, second.assistant_instance_id} == {
        "assistant-alice", "assistant-bob"
    }
    assert item.dispatch_next() is None


def test_queue_exhaustion_is_scoped_to_one_assistant():
    item = scheduler()
    item.submit("assistant-bob", "bob-1")
    item.submit("assistant-bob", "bob-2")
    with pytest.raises(ResourceQuotaExceeded, match="queue"):
        item.submit("assistant-bob", "bob-3")
    item.submit("assistant-alice", "alice-1")
    assert item.dispatch_next().assistant_instance_id == "assistant-alice"


def test_restart_recovers_opaque_tasks_without_exposing_content():
    item = scheduler()
    item.submit("assistant-alice", "opaque-task-1")
    lease = item.dispatch_next()
    assert lease is not None
    item.restart()
    snapshot = item.operational_snapshot()
    assert snapshot["assistants"]["assistant-alice"]["active_count"] == 0
    assert snapshot["assistants"]["assistant-alice"]["queued_count"] == 1
    assert snapshot["contains_task_content"] is False
    assert "opaque-task-1" not in str(snapshot)
