from __future__ import annotations

import uuid
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException

from ..companion import CompanionSessionManager
from ..clients import ServiceClients
from .routes import _auth_dependency


def register_voice_routes(
    app,
    *,
    companion_manager: CompanionSessionManager,
    service_clients: ServiceClients,
    metrics: Dict[str, int],
) -> None:
    api = APIRouter()

    @api.post("/voice/ingest")
    def voice_ingest(
        body: Dict[str, Any] = Body(...),
        current_user: Dict[str, Any] = Depends(_auth_dependency()),
    ):
        """Ingest STT transcripts from io-speech and trigger a companion turn."""
        metrics["/voice/ingest"] += 1
        transcript = body.get("transcript") or body.get("text")
        if not isinstance(transcript, str) or not transcript.strip():
            raise HTTPException(status_code=400, detail="transcript is required")
        person_id = body.get("person_id") or "anonymous"
        session_id = body.get("session_id") or str(uuid.uuid4())
        wakeword_command = bool(body.get("wakeword_command"))
        envelope = {
            "intent": "companion.turn",
            "payload": {
                "person_id": person_id,
                "session_id": session_id,
                "messages": [{"role": "user", "content": transcript}],
                "text": transcript,
                "wakeword_command": wakeword_command,
            },
        }
        result = companion_manager.process_turn(envelope)
        return {"ok": True, "result": result, "person_id": person_id, "session_id": session_id}

    app.include_router(api)
