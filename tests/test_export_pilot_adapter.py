import hashlib
import json

import httpx
import pytest
from test_authenticated_input_boundary import boundary, input_body  # noqa: F401
from test_synthetic_wellness import observation, DAY
from orchestrator.wellness import SyntheticWellnessJourney
from orchestrator.wellness_storage import ExportPilotReader


@pytest.mark.parametrize("case", ["valid", "hash", "policy", "outage", "redirect", "malformed", "consent"])
def test_pilot_adapter_checks_provenance_and_fails_closed(boundary, case):
    client, headers, _, _ = boundary
    facts = observation(consent_id="grant-test")
    digest = hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    record = {"person_id": "alice", "domain": "health", "shared": False, "record_type": "wearable-observation",
              "evidence_status": "observed", "source_ids": [facts["source_record_id"]], "facts": facts,
              "provenance": {"parser_version": "synthetic-export.v1", "fact_sha256": digest}}
    if case == "hash": record["provenance"]["fact_sha256"] = "changed"
    if case == "consent": facts["consent_id"] = "other-grant"
    calls = []
    def transport(request):
        calls.append(request.url.path)
        if case == "outage": raise httpx.ConnectError("synthetic outage", request=request)
        if case == "redirect": return httpx.Response(307, headers={"Location": "https://invalid.example"})
        if case == "malformed": return httpx.Response(200, json=[])
        if request.url.path.endswith("/authorize"):
            return httpx.Response(200, json={"allowed": True, "grant_id": "grant-test",
                                  "policy_version": "old" if case == "policy" else "synthetic-health-policy.v1"})
        return httpx.Response(200, json={"records": [record], "policy_version": "synthetic-health-policy.v1"})
    adapter = ExportPilotReader("http://127.0.0.1:19080", "grant-test", transport=httpx.MockTransport(transport))
    client.app.state.synthetic_wellness_journey = SyntheticWellnessJourney(reader=adapter, policy=adapter)
    response = client.post("/input", headers=headers(aud=["orchestrator", "storage"],
                           scopes=["wellness.read", "wellness.present", "storage.health.pilot.read"]),
                           json={**input_body(), "payload": {"intent": "wellness.daily", "source_day": DAY.isoformat()}})
    assert response.status_code == (200 if case == "valid" else 403), response.text
    assert all(path.startswith("/v1/health-pilot/") for path in calls)
    if case != "valid": assert "brief" not in response.json()
