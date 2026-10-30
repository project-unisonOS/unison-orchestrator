"""Read-only storage adapter for the isolated synthetic wellness journey."""
import ipaddress
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from unison_common.principal import PrincipalKind
from unison_common.principal_middleware import get_current_principal, get_current_principal_token
from orchestrator.wellness import Observation, WellnessDenied


class DomainObservationReader:
    def __init__(self, base_url, *, transport=None):
        parsed = urlsplit(base_url)
        try:
            local = ipaddress.ip_address(parsed.hostname or "").is_loopback
            port = parsed.port
        except ValueError:
            raise WellnessDenied("literal loopback storage transport required") from None
        if (not local or parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"} or port == 0):
            raise WellnessDenied("literal loopback storage transport required")
        self.base_url = base_url.rstrip("/")
        self.transport = transport  # Harness-owned ASGI transport; never a request field.

    def _records(self, client, token):
        response = client.get(self.base_url + "/v1/domain/records", params={"domain": "health"},
                              headers={"Authorization": "Bearer " + token})
        response.raise_for_status()
        return response.json()["records"]

    def read(self, person_id):
        principal, token = get_current_principal(), get_current_principal_token()
        if (principal is None or not token or principal.principal_kind != PrincipalKind.PERSON
                or principal.person_id != person_id or "storage" not in principal.audience
                or "wellness.read" not in principal.scopes):
            raise WellnessDenied("Storage audience and read authority required")
        try:
            with httpx.Client(timeout=10, trust_env=False, follow_redirects=False, transport=self.transport) as client:
                records = self._records(client, token)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise WellnessDenied("Storage read unavailable") from None
        if not isinstance(records, list) or len(records) > 5000:
            raise WellnessDenied("Invalid storage result")
        observations = []
        for record in records:
            if (not isinstance(record, dict) or record.get("person_id") != person_id
                    or record.get("domain") != "health" or record.get("shared") is not False):
                raise WellnessDenied("Storage record is outside private person scope")
            if record.get("record_type") != "wearable-observation":
                continue
            if record.get("evidence_status") != "observed":
                raise WellnessDenied("Derived interpretation is not a source measurement")
            observation = Observation.model_validate(record.get("facts"))
            if observation.person_id != person_id or observation.source_record_id not in record.get("source_ids", []):
                raise WellnessDenied("Observation provenance or person mismatch")
            observations.append(observation.model_dump(mode="json"))
        return observations


class ExportPilotReader(DomainObservationReader):
    """Explicitly configured synthetic route; never falls back to manual facts."""
    def __init__(self, base_url, grant_id, *, transport=None):
        super().__init__(base_url, transport=transport)
        self.grant_id = grant_id

    def _records(self, client, token):
        response = client.get(self.base_url + "/v1/health-pilot/records", params={"grant_id": self.grant_id},
                              headers={"Authorization": "Bearer " + token})
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("policy_version") != "synthetic-health-policy.v1":
            raise WellnessDenied("Unknown pilot policy")
        records = result["records"]
        if not isinstance(records, list) or len(records) > 5000:
            raise WellnessDenied("Invalid pilot records")
        for record in records:
            if not isinstance(record, dict):
                raise WellnessDenied("Invalid pilot record")
            provenance = record.get("provenance", {})
            facts = record.get("facts", {})
            if not isinstance(provenance, dict) or not isinstance(facts, dict):
                raise WellnessDenied("Invalid pilot provenance")
            checksum = hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            if (provenance.get("parser_version") != "synthetic-export.v1"
                    or provenance.get("fact_sha256") != checksum or facts.get("consent_id") != self.grant_id):
                raise WellnessDenied("Unverified pilot provenance")
        return records

    def check(self, action, *, person_id, actor, auth_scope, event_id):
        """Re-evaluate durable consent before read and presentation; never cache."""
        principal, token = get_current_principal(), get_current_principal_token()
        capability = {"wellness.read": "read", "wellness.present": "present"}.get(auth_scope)
        if (principal is None or principal.person_id != person_id or principal.principal_id != actor
                or not token or "storage" not in principal.audience or auth_scope not in principal.scopes
                or capability is None or action.name != auth_scope or action.args.get("recipient") != person_id):
            return SimpleNamespace(allowed=False, require_confirmation=False)
        try:
            with httpx.Client(timeout=10, trust_env=False, follow_redirects=False, transport=self.transport) as client:
                response = client.post(self.base_url + "/v1/health-pilot/authorize",
                                       headers={"Authorization": "Bearer " + token},
                                       json={"grant_id": self.grant_id, "action": capability})
            response.raise_for_status()
            result = response.json()
            allowed = (isinstance(result, dict) and result.get("allowed") is True and result.get("grant_id") == self.grant_id
                       and result.get("policy_version") == "synthetic-health-policy.v1")
        except (httpx.HTTPError, ValueError, TypeError):
            allowed = False
        return SimpleNamespace(allowed=allowed, require_confirmation=False)
