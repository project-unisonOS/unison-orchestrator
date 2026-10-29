"""Narrow caller contract for reviewed local text inference.

This adapter does not qualify the model runtime or authorize retrieval, tools,
disclosure or delivery. Its result is untrusted text, never executable authority.
"""
from dataclasses import dataclass
import ipaddress
import time
from urllib.parse import urlsplit

import httpx
from unison_common import ModelTaskRequirement
from unison_common.principal import PrincipalKind
from unison_common.principal_middleware import get_current_principal, get_current_principal_token


class LocalInferenceRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class UntrustedTextResult:
    text: str
    model: str
    operation_id: str
    untrusted: bool = True


class LocalTextInferenceClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 30):
        url = urlsplit(base_url)
        try:
            local = ipaddress.ip_address(url.hostname or "").is_loopback
            port = url.port
        except ValueError:
            raise LocalInferenceRejected("literal loopback transport required") from None
        if (not local or url.scheme not in {"http", "https"} or url.username or url.password
                or url.path not in {"", "/"} or url.query or url.fragment or port == 0):
            raise LocalInferenceRejected("literal loopback transport required")
        if not 0 < timeout_seconds <= 120:
            raise LocalInferenceRejected("invalid inference timeout")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def request(self, *, operation_id: str, requirement: ModelTaskRequirement,
                prompt: str, max_tokens: int = 512) -> UntrustedTextResult:
        principal = get_current_principal()
        token = get_current_principal_token()
        if (principal is None or not token or principal.principal_kind != PrincipalKind.PERSON
                or not principal.person_id or "inference" not in principal.audience
                or "inference.local" not in principal.scopes or principal.expires_at <= int(time.time())):
            raise LocalInferenceRejected("active person authority for inference required")
        if (not isinstance(requirement, ModelTaskRequirement) or requirement.modality != "text"
                or not requirement.local_only or requirement.max_cost != 0):
            raise LocalInferenceRejected("only zero-cost local text requirements are supported")
        if not isinstance(prompt, str) or not prompt or len(prompt) > 32768:
            raise LocalInferenceRejected("bounded text prompt required")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 1024:
            raise LocalInferenceRejected("invalid token budget")
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
            raise LocalInferenceRejected("bounded operation identifier required")
        body = {
            "intent": "local.text", "operation_id": operation_id,
            "person_id": principal.person_id, "prompt": prompt,
            "task_requirement": requirement.model_dump(mode="json"),
            "provider": "ollama", "max_tokens": max_tokens,
        }
        try:
            # No retry/fallback: a transport failure must not silently repeat
            # model work, spend a second budget, or expand disclosure scope.
            with httpx.Client(timeout=self.timeout_seconds, trust_env=False, follow_redirects=False) as client:
                response = client.post(self.base_url + "/inference/request", json=body,
                                       headers={"Authorization": "Bearer " + token, "X-Event-ID": operation_id})
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise LocalInferenceRejected("local inference unavailable") from None
        if (not isinstance(data, dict) or data.get("ok") is not True
                or data.get("provider") != "ollama" or data.get("fallback_used") is not False
                or data.get("event_id") != operation_id or data.get("tool_calls")
                or not isinstance(data.get("result"), str) or not isinstance(data.get("model"), str)):
            raise LocalInferenceRejected("invalid local text result")
        return UntrustedTextResult(data["result"], data["model"], operation_id)
