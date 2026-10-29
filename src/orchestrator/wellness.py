"""Deterministic synthetic wellness brief; no diagnosis or model dependency."""
from datetime import date, datetime, timedelta
from collections import Counter
from statistics import median
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from unison_common import ActionEnvelope


class WellnessDenied(ValueError):
    pass


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    synthetic: Literal[True]
    person_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    upstream_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    ingestion_source: str = Field(min_length=1)
    device: str = Field(min_length=1)
    method: str = Field(min_length=1)
    metric: Literal["sleep_duration", "steps"]
    kind: Literal["measurement", "vendor_score", "statement", "extraction", "inference"]
    value: float | None
    unit: Literal["minutes", "count"]
    source_day: date
    original_timezone: str
    measured_start: datetime
    measured_end: datetime
    ingested_at: datetime
    quality: Literal["valid", "missing", "invalid"]
    missing_reason: str | None = None
    consent_id: str = Field(min_length=1)
    transformation_version: Literal["synthetic-observation.v1"]
    revision: int = Field(ge=1)
    original_payload: dict[str, Any]

    @field_validator("value", mode="before")
    @classmethod
    def numeric_value(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("numeric observation required")
        return value

    @model_validator(mode="after")
    def coherent(self):
        try:
            ZoneInfo(self.original_timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("original IANA timezone required") from None
        if any(value.tzinfo is None or value.utcoffset() is None
               for value in (self.measured_start, self.measured_end, self.ingested_at)):
            raise ValueError("measurement and ingestion timestamps must be timezone aware")
        if self.measured_end < self.measured_start:
            raise ValueError("invalid measurement interval")
        if self.unit != {"sleep_duration": "minutes", "steps": "count"}[self.metric]:
            raise ValueError("incompatible metric unit")
        if self.value is not None and self.value < 0:
            raise ValueError("negative observation")
        if self.quality == "valid" and self.value is None:
            raise ValueError("valid observation requires a value")
        if self.quality != "valid" and (self.value is not None or not self.missing_reason):
            raise ValueError("unavailable observation requires a reason, not a numeric value")
        return self


class ObservationReader(Protocol):
    def read(self, person_id: str) -> list[dict]: ...


def _deduplicate(observations):
    selected = {}
    for observation in observations:
        key = (observation.source, observation.upstream_id, observation.metric)
        prior = selected.get(key)
        if prior and prior.revision == observation.revision:
            fields = ("value", "unit", "metric", "source_day", "device", "method", "quality", "original_timezone")
            if any(getattr(prior, field) != getattr(observation, field) for field in fields):
                raise WellnessDenied("Conflicting source revisions require reconciliation")
        if prior is None or observation.revision > prior.revision:
            selected[key] = observation
    return list(selected.values())


def build_brief(person_id: str, as_of: date, raw: list[dict]) -> dict:
    if len(raw) > 5000:
        raise WellnessDenied("Observation batch exceeds the synthetic journey limit")
    observations = [Observation.model_validate(item) for item in raw]
    if any(item.person_id != person_id for item in observations):
        raise WellnessDenied("Source identity does not match authenticated person")
    observations = _deduplicate(observations)
    entries = []
    for metric, label, unit in (("sleep_duration", "Sleep duration", "minutes"), ("steps", "Steps", "count")):
        available = [item for item in observations if item.metric == metric and item.kind == "measurement"]
        current = [item for item in available if item.source_day == as_of]
        entry = {"metric": metric, "label": label, "unit": unit, "status": "unknown",
                 "value": None, "baseline_median": None, "baseline_count": 0, "change": None,
                 "observation_ids": [], "source_ids": [], "sources": [], "uncertainty": None}
        if len(current) != 1:
            entry["uncertainty"] = "No measurement for the source day" if not current else "Multiple measurements need reconciliation"
        else:
            today = current[0]
            entry.update(observation_ids=[today.observation_id], source_ids=[today.source_record_id], sources=[today.source])
            if today.quality != "valid":
                entry["uncertainty"] = today.missing_reason
            else:
                entry["value"] = today.value
                baseline = [item for item in available if as_of - timedelta(days=30) <= item.source_day < as_of
                            and item.quality == "valid"]
                identity = lambda item: (item.source, item.device, item.method, item.unit, item.original_timezone)
                baseline = [item for item in baseline if identity(item) == identity(today)]
                # Multiple records on a baseline day are not added or averaged.
                counts = Counter(item.source_day for item in baseline)
                baseline = [item for item in baseline if counts[item.source_day] == 1]
                entry["baseline_count"] = len(baseline)
                if len(baseline) < 3:
                    entry["uncertainty"] = "Fewer than three comparable source days; device, method and timezone must match"
                else:
                    reference = median(item.value for item in baseline)
                    entry.update(baseline_median=reference, change=today.value-reference,
                                 status="unchanged" if today.value == reference else "changed")
                    entry["observation_ids"].extend(item.observation_id for item in baseline)
                    entry["source_ids"].extend(item.source_record_id for item in baseline)
        if entry["status"] == "unknown":
            text = f"{label}: comparison unavailable. {entry['uncertainty']}."
            if entry["value"] is not None:
                text = f"{label}: {entry['value']:g} {unit}. " + text
        else:
            text = (f"{label}: {entry['value']:g} {unit}; {entry['status']} from the comparable-day median "
                    f"of {entry['baseline_median']:g}, across {entry['baseline_count']} days.")
        entry["text"] = text
        entries.append(entry)
    return {"schema_version": "synthetic-wellness-brief.v1", "synthetic": True,
            "person_id": person_id, "source_day": as_of.isoformat(), "entries": entries,
            "text": " ".join(entry["text"] for entry in entries),
            "interpretation": "Descriptive comparison only; no health significance or treatment conclusion assessed.",
            "opportunities": [], "evidence_kind": "synthetic-observations",
            "retained_observations": len(observations)}


class SyntheticWellnessJourney:
    def __init__(self, *, reader: ObservationReader, policy):
        self.reader = reader
        self.policy = policy

    def run(self, *, principal, as_of: date, operation_id: str):
        if not {"wellness.read", "wellness.present"}.issubset(principal.scopes):
            raise WellnessDenied("Explicit read and presentation scopes required")
        def authorize(capability):
            action = ActionEnvelope(action_id=operation_id + ":" + capability, kind="tool", name=capability,
                                    args={"source_day": as_of.isoformat(), "recipient": principal.person_id},
                                    policy_context={"scopes": [capability], "synthetic_only": True})
            decision = self.policy.check(action, person_id=principal.person_id, actor=principal.principal_id,
                                         auth_scope=capability, event_id=operation_id)
            if not decision.allowed or decision.require_confirmation:
                raise WellnessDenied("Wellness policy denied this phase")
        authorize("wellness.read")
        brief = build_brief(principal.person_id, as_of, self.reader.read(principal.person_id))
        authorize("wellness.present")
        return brief
