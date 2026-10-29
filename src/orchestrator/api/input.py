from __future__ import annotations

import os
import re
from datetime import date
from pydantic import ValidationError
from orchestrator.wellness import WellnessDenied
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException

from orchestrator.interaction.input_runner import run_input_event
from unison_common import InputEventEnvelope
from unison_common.principal import PrincipalContext
from orchestrator.api.principal import require_bound_person
from unison_common.contracts.v1.speechio import TranscriptEvent


def register_input_routes(app) -> None:
    api = APIRouter()

    @api.post("/input")
    async def ingest_input(
        body: Dict[str, Any] = Body(...),
        principal: PrincipalContext = Depends(require_bound_person),
    ):
        trace_id = body.get("trace_id")
        if (not isinstance(trace_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", trace_id)
                or trace_id.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}):
            raise HTTPException(status_code=400, detail="Invalid trace identifier")
        if os.getenv("UNISON_PHASE1_PIPELINE", "false").lower() in {"1", "true", "yes", "on"}:
            raise HTTPException(status_code=503, detail="Legacy phase1 ingress is disabled pending authority convergence")
        input_event = InputEventEnvelope(**{**body, "person_id": principal.person_id})
        if input_event.payload.get("intent") == "wellness.daily":
            journey = getattr(app.state, "synthetic_wellness_journey", None)
            if journey is None:
                raise HTTPException(status_code=503, detail="Synthetic wellness journey is not configured")
            try:
                day = date.fromisoformat(input_event.payload["source_day"])
                brief = journey.run(principal=principal, as_of=day, operation_id=input_event.event_id)
            except WellnessDenied:
                raise HTTPException(status_code=403, detail="Wellness request denied") from None
            except (KeyError, TypeError, ValueError, ValidationError):
                raise HTTPException(status_code=400, detail="Invalid synthetic wellness data or source day") from None
            return {"ok": True, "person_id": principal.person_id, "trace_id": input_event.trace_id, "brief": brief}
        # Speech streaming events are ingested into the SpeechIO adapter (if enabled)
        # and handled asynchronously by the voice loop.
        if input_event.modality == "speech":
            payload = input_event.payload or {}
            speechio_payload = payload.get("speechio") if isinstance(payload, dict) else None
            if isinstance(speechio_payload, dict) and isinstance(speechio_payload.get("type"), str):
                adapter = getattr(app.state, "speechio", None)
                if adapter is not None:
                    try:
                        evt = TranscriptEvent(**speechio_payload)
                        # Preserve identity/session for the voice loop (streaming SpeechIO ingress).
                        if isinstance(evt.attrs, dict):
                            if isinstance(input_event.person_id, str) and input_event.person_id:
                                evt.attrs["person_id"] = principal.person_id
                            if isinstance(input_event.session_id, str) and input_event.session_id:
                                evt.attrs.setdefault("session_id", input_event.session_id)
                        adapter.ingest(evt)
                        return {"ok": True, "trace_id": input_event.trace_id, "streaming": True}
                    except Exception:
                        return {"ok": False, "trace_id": input_event.trace_id, "streaming": True, "error": "invalid_speechio_event"}

        clients = getattr(app.state, "service_clients", None)
        trace_dir = str(os.getenv("UNISON_TRACE_DIR", "traces"))
        renderer_url = os.getenv("UNISON_RENDERER_URL") or os.getenv("UNISON_EXPERIENCE_RENDERER_URL")
        if not renderer_url:
            host = os.getenv("UNISON_EXPERIENCE_RENDERER_HOST") or os.getenv("EXPERIENCE_RENDERER_HOST")
            port = os.getenv("UNISON_EXPERIENCE_RENDERER_PORT") or os.getenv("EXPERIENCE_RENDERER_PORT")
            if host and port:
                renderer_url = f"http://{host}:{port}"
        if not renderer_url:
            renderer_url = os.getenv("EXPERIENCE_RENDERER_URL") or os.getenv("RENDERER_URL")
        result = run_input_event(
                input_event=input_event,
                clients=clients,
                trace_dir=trace_dir,
                renderer_url=renderer_url,
        )
        return {
            "ok": result.tool_result.ok,
            "trace_id": result.trace_id,
            "session_id": result.session_id,
            "person_id": result.person_id,
            "renderer_ok": result.renderer_ok,
            "renderer_status": result.renderer_status,
            "trace_path": result.trace_path,
            "rom": result.rom.model_dump(mode="json"),
        }

    app.include_router(api)
