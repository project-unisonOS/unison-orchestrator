from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _split_hosts(raw: str) -> List[str]:
    return [host.strip() for host in raw.split(",") if host.strip()]


@dataclass(frozen=True)
class ServiceEndpoints:
    """Downstream service endpoints used by the orchestrator."""

    context_host: str = "context"
    context_port: str = "8081"
    storage_host: str = "storage"
    storage_port: str = "8082"
    policy_host: str = "policy"
    policy_port: str = "8083"
    inference_host: str = "inference"
    inference_port: str = "8087"


@dataclass(frozen=True)
class OrchestratorSettings:
    """Typed configuration surface for the orchestrator service."""

    allowed_hosts: List[str] = field(default_factory=list)
    routing_strategy: str = "rule_based"
    confirm_ttl_seconds: int = 300
    require_consent: bool = False
    endpoints: ServiceEndpoints = field(default_factory=ServiceEndpoints)

    @classmethod
    def from_env(cls) -> "OrchestratorSettings":
        """Create settings instance by reading environment variables once."""
        allowed_hosts = _split_hosts(
            os.getenv("UNISON_ALLOWED_HOSTS", "localhost,127.0.0.1,orchestrator")
        )

        endpoints = ServiceEndpoints(
            context_host=os.getenv("UNISON_CONTEXT_HOST", "context"),
            context_port=os.getenv("UNISON_CONTEXT_PORT", "8081"),
            storage_host=os.getenv("UNISON_STORAGE_HOST", "storage"),
            storage_port=os.getenv("UNISON_STORAGE_PORT", "8082"),
            policy_host=os.getenv("UNISON_POLICY_HOST", "policy"),
            policy_port=os.getenv("UNISON_POLICY_PORT", "8083"),
            inference_host=os.getenv("UNISON_INFERENCE_HOST", "inference"),
            inference_port=os.getenv("UNISON_INFERENCE_PORT", "8087"),
        )

        return cls(
            allowed_hosts=allowed_hosts,
            routing_strategy=os.getenv("UNISON_ROUTING_STRATEGY", "rule_based"),
            confirm_ttl_seconds=int(os.getenv("UNISON_CONFIRM_TTL", "300")),
            require_consent=os.getenv("UNISON_REQUIRE_CONSENT", "false").lower() == "true",
            endpoints=endpoints,
        )
