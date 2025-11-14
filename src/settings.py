"""Typed configuration for the standalone unison-orchestrator repo."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _split_csv(raw: str) -> List[str]:
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TelemetrySettings:
    exporter_endpoint: str = ""
    service_name: str = "unison-orchestrator"
    service_version: str = "1.0.0"


@dataclass(frozen=True)
class ServiceEndpoints:
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
    allowed_hosts: List[str] = field(
        default_factory=lambda: ["localhost", "127.0.0.1", "orchestrator"]
    )
    routing_strategy: str = "rule_based"
    confirm_ttl_seconds: int = 300
    require_consent: bool = False
    endpoints: ServiceEndpoints = field(default_factory=ServiceEndpoints)
    telemetry: TelemetrySettings = field(default_factory=TelemetrySettings)

    @classmethod
    def from_env(cls) -> "OrchestratorSettings":
        allowed_hosts = _split_csv(
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

        telemetry = TelemetrySettings(
            exporter_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            service_name=os.getenv("OTEL_SERVICE_NAME", "unison-orchestrator"),
            service_version=os.getenv("OTEL_SERVICE_VERSION", "1.0.0"),
        )

        return cls(
            allowed_hosts=allowed_hosts,
            routing_strategy=os.getenv("UNISON_ROUTING_STRATEGY", "rule_based"),
            confirm_ttl_seconds=int(os.getenv("UNISON_CONFIRM_TTL", "300")),
            require_consent=_as_bool(os.getenv("UNISON_REQUIRE_CONSENT"), False),
            endpoints=endpoints,
            telemetry=telemetry,
        )


__all__ = [
    "OrchestratorSettings",
    "ServiceEndpoints",
    "TelemetrySettings",
]
