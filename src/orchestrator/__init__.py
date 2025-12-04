"""Internal orchestration helpers."""

from .clients import ServiceClients
from .config import OrchestratorSettings

try:
    from .telemetry import instrument_fastapi, setup_telemetry
except Exception:  # pragma: no cover
    # In minimal or test environments, telemetry dependencies may be absent.
    instrument_fastapi = None  # type: ignore
    setup_telemetry = None  # type: ignore

__all__ = [
    "OrchestratorSettings",
    "setup_telemetry",
    "instrument_fastapi",
    "ServiceClients",
]
