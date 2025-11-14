from __future__ import annotations

import logging
import os
import inspect

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
import httpx


def setup_telemetry(
    *, service_name: str | None = None, service_version: str | None = None
) -> None:
    """Configure OpenTelemetry tracing for the orchestrator service."""
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    resolved_name = service_name or os.getenv("OTEL_SERVICE_NAME", "unison-orchestrator")
    resolved_version = service_version or os.getenv("OTEL_SERVICE_VERSION", "1.0.0")

    resource = Resource(
        attributes={
            SERVICE_NAME: resolved_name,
            SERVICE_VERSION: resolved_version,
        }
    )

    provider = TracerProvider(resource=resource)
    if otlp_endpoint:
        otlp_exporter = OTLPSpanExporter(endpoint=f"{otlp_endpoint}/v1/traces", timeout=10)
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logging.getLogger(__name__).info(
            "OTLP exporter enabled for %s (%s)", resolved_name, otlp_endpoint
        )
    else:
        logging.getLogger(__name__).info(
            "OTLP exporter disabled for %s (no endpoint configured)", resolved_name
        )
    trace.set_tracer_provider(provider)

    if inspect.isclass(httpx.AsyncClient):
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    else:
        logging.getLogger(__name__).warning(
            "HTTPX instrumentation skipped: httpx.AsyncClient patched to non-class"
        )


def instrument_fastapi(app) -> None:
    """Wrap FastAPI instrumentation to keep a single import surface."""
    FastAPIInstrumentor.instrument_app(app)
