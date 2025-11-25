from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .config import ServiceEndpoints
from unison_common.baton import get_current_baton
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

    def get(self, path: str, *, headers: Optional[Dict[str, str]] = None) -> HttpResult:
        merged_headers = dict(headers or {})
        baton = get_current_baton()
        if baton:
            merged_headers.setdefault("X-Context-Baton", baton)
        return http_get_json_with_retry(
            self.host,
            self.port,
            path,
            headers=merged_headers or None,
            **_CALL_DEFAULTS,
        )

    def post(
        self,
        path: str,
        payload: JsonDict,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult:
        merged_headers = dict(headers or {})
        baton = get_current_baton()
        if baton:
            merged_headers.setdefault("X-Context-Baton", baton)
        return http_post_json_with_retry(
            self.host,
            self.port,
            path,
            payload,
            headers=merged_headers or None,
            **_CALL_DEFAULTS,
        )

    def put(
        self,
        path: str,
        payload: JsonDict,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult:
        merged_headers = dict(headers or {})
        baton = get_current_baton()
        if baton:
            merged_headers.setdefault("X-Context-Baton", baton)
        return http_put_json_with_retry(
            self.host,
            self.port,
            path,
            payload,
            headers=merged_headers or None,
            **_CALL_DEFAULTS,
        )


@dataclass
class ServiceClients:
    context: ServiceHttpClient
    storage: ServiceHttpClient
    policy: ServiceHttpClient
    inference: ServiceHttpClient

    @classmethod
    def from_endpoints(cls, endpoints: ServiceEndpoints) -> "ServiceClients":
        return cls(
            context=ServiceHttpClient(endpoints.context_host, endpoints.context_port),
            storage=ServiceHttpClient(endpoints.storage_host, endpoints.storage_port),
            policy=ServiceHttpClient(endpoints.policy_host, endpoints.policy_port),
            inference=ServiceHttpClient(endpoints.inference_host, endpoints.inference_port),
        )
