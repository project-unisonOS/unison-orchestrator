"""Read-only storage adapter for the isolated synthetic wellness journey."""
import ipaddress
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

    def read(self, person_id):
        principal, token = get_current_principal(), get_current_principal_token()
        if (principal is None or not token or principal.principal_kind != PrincipalKind.PERSON
                or principal.person_id != person_id or "storage" not in principal.audience
                or "wellness.read" not in principal.scopes):
            raise WellnessDenied("Storage audience and read authority required")
        try:
            with httpx.Client(timeout=10, trust_env=False, follow_redirects=False, transport=self.transport) as client:
                response = client.get(self.base_url + "/v1/domain/records", params={"domain": "health"},
                                      headers={"Authorization": "Bearer " + token})
            response.raise_for_status()
            records = response.json()["records"]
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
