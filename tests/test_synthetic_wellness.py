from copy import deepcopy
from datetime import date, timedelta

import pytest
from pydantic import ValidationError
from unison_common import PolicyDecision
from orchestrator.wellness import build_brief, Observation, SyntheticWellnessJourney, WellnessDenied
from test_authenticated_input_boundary import boundary, input_body  # noqa: F401


DAY = date(2026, 10, 28)


def observation(metric="sleep_duration", day=DAY, value=420, **changes):
    identifier = f"{metric}-{day}"
    data = dict(synthetic=True, person_id="alice", observation_id=identifier, source_record_id="source-"+identifier, upstream_id=identifier,
                source="synthetic-ring" if metric == "sleep_duration" else "synthetic-activity",
                ingestion_source="synthetic-export", device="device-a", method="daily-aggregate",
                metric=metric, kind="measurement", value=value, unit="minutes" if metric == "sleep_duration" else "count",
                source_day=day.isoformat(), original_timezone="UTC", measured_start=f"{day}T00:00:00+00:00",
                measured_end=f"{day}T23:59:00+00:00", ingested_at="2026-10-29T01:00:00+00:00",
                quality="valid", missing_reason=None, consent_id="synthetic-consent", revision=1,
                transformation_version="synthetic-observation.v1", original_payload={"fixture_value": value})
    data.update(changes)
    return data


def fixture_observations():
    return ([observation(day=DAY-timedelta(days=i), value=450 if i else 420) for i in range(4)]
            + [observation("steps", DAY-timedelta(days=i), 6000) for i in range(4)])


def test_changed_unchanged_and_traceable_brief():
    data = fixture_observations()
    before = deepcopy(data)
    result = build_brief("alice", DAY, data)
    sleep, steps = result["entries"]
    assert (sleep["status"], sleep["change"], sleep["baseline_median"]) == ("changed", -30, 450)
    assert steps["status"] == "unchanged"
    assert len(sleep["observation_ids"]) == 4
    assert sleep["sources"] == ["synthetic-ring"]
    assert sleep["source_ids"] == [item["source_record_id"] for item in data[:4]]
    assert result["opportunities"] == []
    assert data == before  # Reading and briefing do not change source payloads.
    assert all(entry["text"] in result["text"] for entry in result["entries"])


def test_missing_is_unknown_and_zero_is_a_measurement():
    data = fixture_observations()
    data[0].update(value=None, quality="missing", missing_reason="Device was not worn")
    result = build_brief("alice", DAY, data)
    assert result["entries"][0]["status"] == "unknown"
    assert result["entries"][0]["value"] is None
    assert "not worn" in result["text"]
    data[0].update(value=0, quality="valid", missing_reason=None)
    assert build_brief("alice", DAY, data)["entries"][0]["value"] == 0


@pytest.mark.parametrize("change", [{"device": "device-b"}, {"method": "another-method"}, {"original_timezone": "Etc/GMT+5"}])
def test_device_method_and_travel_changes_do_not_mix_baselines(change):
    data = fixture_observations()
    data[0].update(change)
    entry = build_brief("alice", DAY, data)["entries"][0]
    assert entry["status"] == "unknown" and entry["baseline_count"] == 0


def test_syndicated_duplicate_is_not_double_counted_and_correction_replaces():
    data = fixture_observations()
    data.append({**data[0], "ingestion_source": "synthetic-syndication", "observation_id": "copy"})
    assert build_brief("alice", DAY, data)["entries"][0]["status"] == "changed"
    data.append({**data[0], "revision": 2, "value": 450, "observation_id": "corrected"})
    entry = build_brief("alice", DAY, data)["entries"][0]
    assert entry["status"] == "unchanged" and entry["observation_ids"][0] == "corrected"


def test_conflicting_same_revision_is_not_silently_selected():
    data = fixture_observations()
    data.append({**data[0], "value": 999})
    with pytest.raises(WellnessDenied):
        build_brief("alice", DAY, data)


def test_multiple_metrics_in_one_upstream_record_remain_distinct():
    data = [observation(upstream_id="daily-source-record", source="synthetic-device"),
            observation("steps", value=6000, upstream_id="daily-source-record", source="synthetic-device")]
    assert build_brief("alice", DAY, data)["retained_observations"] == 2


@pytest.mark.parametrize("change", [{"value": float("nan")}, {"value": True}, {"unit": "count"},
                                    {"synthetic": False}, {"quality": "missing"}, {"original_timezone": "not-a-zone"}])
def test_bad_or_real_observations_rejected(change):
    with pytest.raises(ValidationError):
        Observation.model_validate(observation(**change))


def test_vendor_score_is_not_a_measurement_and_foreign_person_is_denied():
    data = fixture_observations()
    data[0]["kind"] = "vendor_score"
    assert build_brief("alice", DAY, data)["entries"][0]["status"] == "unknown"
    data[0]["person_id"] = "bob"
    with pytest.raises(WellnessDenied):
        build_brief("alice", DAY, data)


class ControlledReader:
    def __init__(self): self.calls = []
    def read(self, person_id):
        self.calls.append(person_id)
        return fixture_observations()


class ControlledPolicy:
    def __init__(self, denied=None): self.calls, self.denied = [], denied
    def check(self, action, **kwargs):
        self.calls.append((action.name, kwargs["person_id"]))
        return PolicyDecision(allowed=action.name != self.denied)


@pytest.mark.parametrize("phase", [None, "wellness.read", "wellness.present"])
def test_authenticated_canonical_input_checks_read_and_presentation(boundary, phase):
    client, headers, _, _ = boundary
    reader, policy = ControlledReader(), ControlledPolicy(phase)
    client.app.state.synthetic_wellness_journey = SyntheticWellnessJourney(reader=reader, policy=policy)
    body = {**input_body(), "payload": {"intent": "wellness.daily", "source_day": DAY.isoformat()}}
    response = client.post("/input", json=body, headers=headers(scopes=["wellness.read", "wellness.present"]))
    assert response.status_code == (200 if phase is None else 403)
    assert reader.calls == ([] if phase == "wellness.read" else ["alice"])
    if phase is None:
        assert policy.calls == [("wellness.read", "alice"), ("wellness.present", "alice")]
        assert response.json()["brief"]["synthetic"] is True
    else:
        assert "brief" not in response.json()


def test_unconfigured_journey_never_falls_back_to_echo(boundary):
    client, headers, _, _ = boundary
    body = {**input_body(), "payload": {"intent": "wellness.daily", "source_day": DAY.isoformat()}}
    assert client.post("/input", json=body, headers=headers()).status_code == 503


def test_missing_scope_or_revoked_session_never_reads(boundary):
    client, headers, revoked, _ = boundary
    reader = ControlledReader()
    client.app.state.synthetic_wellness_journey = SyntheticWellnessJourney(reader=reader, policy=ControlledPolicy())
    body = {**input_body(), "payload": {"intent": "wellness.daily", "source_day": DAY.isoformat()}}
    assert client.post("/input", json=body, headers=headers()).status_code == 403
    revoked.add("session-alice")
    assert client.post("/input", json=body, headers=headers(scopes=["wellness.read", "wellness.present"])).status_code == 403
    assert reader.calls == []
