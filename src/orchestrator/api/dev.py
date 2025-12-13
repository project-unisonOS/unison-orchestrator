from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from orchestrator.dev_thin_slice import run_thin_slice
from unison_common.auth import verify_token

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


def register_dev_routes(app) -> None:
    api = APIRouter()

    @api.post("/dev/thin-slice")
    async def dev_thin_slice(
        body: Dict[str, Any] = Body(...),
        _user: Dict[str, Any] = Depends(_optional_auth),
    ):
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="text is required")

        result = run_thin_slice(
            text=text,
            person_id=str(body.get("person_id") or "local-person"),
            session_id=(str(body["session_id"]) if isinstance(body.get("session_id"), str) else None),
            renderer_url=(str(body["renderer_url"]) if isinstance(body.get("renderer_url"), str) else None),
            trace_dir=str(body.get("trace_dir") or "traces"),
        )
        return {
            "ok": result.tool_result.ok,
            "trace_id": result.trace_id,
            "session_id": result.session_id,
            "renderer_ok": result.renderer_ok,
            "renderer_status": result.renderer_status,
            "trace_path": result.trace_path,
            "rom": result.rom.model_dump(mode="json"),
        }

    app.include_router(api)

