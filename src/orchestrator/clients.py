from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from .config import ServiceEndpoints
from unison_common.baton import get_current_baton
import os
from unison_common.http_client import (
    http_get_json_with_retry,
    http_post_json_with_retry,
    http_put_json_with_retry,
)

JsonDict = Dict[str, Any]
HttpResult = Tuple[bool, int, Optional[JsonDict]]

_CALL_DEFAULTS = dict(max_retries=3, base_delay=0.1, max_delay=2.0, timeout=2.0)


@dataclass
class ServiceHttpClient:
    host: str
    port: str
    timeout_seconds: float = 2.0
    default_headers: Dict[str, str] = field(default_factory=dict)

    def get(self, path: str, *, headers: Optional[Dict[str, str]] = None) -> HttpResult:
        merged_headers = {**self.default_headers, **dict(headers or {})}
        baton = get_current_baton()
        if baton:
            merged_headers.setdefault("X-Context-Baton", baton)
        return http_get_json_with_retry(
            self.host,
            self.port,
            path,
            headers=merged_headers or None,
            **{**_CALL_DEFAULTS, "timeout": float(self.timeout_seconds)},
        )

    def post(
        self,
        path: str,
        payload: JsonDict,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult:
        merged_headers = {**self.default_headers, **dict(headers or {})}
        baton = get_current_baton()
        if baton:
            merged_headers.setdefault("X-Context-Baton", baton)
        return http_post_json_with_retry(
            self.host,
            self.port,
            path,
            payload,
            headers=merged_headers or None,
            **{**_CALL_DEFAULTS, "timeout": float(self.timeout_seconds)},
        )

    def put(
        self,
        path: str,
        payload: JsonDict,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult:
        merged_headers = {**self.default_headers, **dict(headers or {})}
        baton = get_current_baton()
        if baton:
            merged_headers.setdefault("X-Context-Baton", baton)
        return http_put_json_with_retry(
            self.host,
            self.port,
            path,
            payload,
            headers=merged_headers or None,
            **{**_CALL_DEFAULTS, "timeout": float(self.timeout_seconds)},
        )


@dataclass
class ServiceClients:
    context: ServiceHttpClient
    storage: ServiceHttpClient
    policy: ServiceHttpClient
    inference: ServiceHttpClient
    capability: ServiceHttpClient | None = None
    comms: ServiceHttpClient | None = None
    actuation: ServiceHttpClient | None = None
    consent: ServiceHttpClient | None = None
    payments: ServiceHttpClient | None = None

    @classmethod
    def from_endpoints(cls, endpoints: ServiceEndpoints) -> "ServiceClients":
        # Inference requests can be slow (first-token latency, model warmup).
        # Keep this high by default and allow env override for tighter loops.
        inference_timeout = float(os.getenv("UNISON_INFERENCE_HTTP_TIMEOUT_SECONDS", "60.0"))
        payments_client = None
        if endpoints.payments_host and endpoints.payments_port:
            payments_client = ServiceHttpClient(endpoints.payments_host, endpoints.payments_port)
        comms_client = None
        if endpoints.comms_host and endpoints.comms_port:
            comms_client = ServiceHttpClient(endpoints.comms_host, endpoints.comms_port)
        capability_client = None
        if endpoints.capability_host and endpoints.capability_port:
            token = os.getenv("UNISON_CAPABILITY_BEARER_TOKEN") or os.getenv("UNISON_CAPABILITY_TOKEN")
            headers: Dict[str, str] = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            capability_client = ServiceHttpClient(endpoints.capability_host, endpoints.capability_port, default_headers=headers)
        actuation_client = None
        if endpoints.actuation_host and endpoints.actuation_port:
            actuation_client = ServiceHttpClient(endpoints.actuation_host, endpoints.actuation_port)
        consent_client = None
        if endpoints.consent_host and endpoints.consent_port:
            consent_client = ServiceHttpClient(endpoints.consent_host, endpoints.consent_port)
        return cls(
            context=ServiceHttpClient(endpoints.context_host, endpoints.context_port),
            storage=ServiceHttpClient(endpoints.storage_host, endpoints.storage_port),
            policy=ServiceHttpClient(endpoints.policy_host, endpoints.policy_port),
            inference=ServiceHttpClient(endpoints.inference_host, endpoints.inference_port, timeout_seconds=inference_timeout),
            capability=capability_client,
            comms=comms_client,
            actuation=actuation_client,
            consent=consent_client,
            payments=payments_client,
        )
