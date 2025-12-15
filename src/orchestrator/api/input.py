from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from orchestrator.interaction.input_runner import run_input_event
from unison_common import InputEventEnvelope
from unison_common.auth import verify_token
from unison_common.contracts.v1.speechio import TranscriptEvent

_security = HTTPBearer(auto_error=False)


async def _optional_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> Dict[str, Any]:
    test_mode = bool(os.getenv("PYTEST_CURRENT_TEST")) or os.getenv(
        "DISABLE_AUTH_FOR_TESTS", "false"
    ).lower() == "true"

    if credentials is None:
        if test_mode:
            return {"username": "test-user", "roles": ["admin"]}
        raise HTTPException(status_code=401, detail="Authorization required")

    return await verify_token(credentials)  # type: ignore[arg-type]


def register_input_routes(app) -> None:
    api = APIRouter()

    @api.post("/input")
    async def ingest_input(
        body: Dict[str, Any] = Body(...),
        _user: Dict[str, Any] = Depends(_optional_auth),
    ):
        input_event = InputEventEnvelope(**body)
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
                        adapter.ingest(evt)
                        return {"ok": True, "trace_id": input_event.trace_id, "streaming": True}
                    except Exception:
                        return {"ok": False, "trace_id": input_event.trace_id, "streaming": True, "error": "invalid_speechio_event"}

        clients = getattr(app.state, "service_clients", None)
        result = run_input_event(
            input_event=input_event,
            clients=clients,
            trace_dir=str(os.getenv("UNISON_TRACE_DIR", "traces")),
            renderer_url=os.getenv("UNISON_RENDERER_URL") or os.getenv("UNISON_EXPERIENCE_RENDERER_URL"),
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
