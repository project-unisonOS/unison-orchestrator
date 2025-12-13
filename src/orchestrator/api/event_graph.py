from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from orchestrator.event_graph.store import JsonlEventGraphStore
from unison_common.auth import verify_token
from unison_common.contracts.v1 import EventGraphAppend, EventGraphQuery

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


def register_event_graph_routes(app) -> None:
    api = APIRouter()
    store = JsonlEventGraphStore.from_env()

    @api.post("/event-graph/append")
    async def append_event_graph(
        body: Dict[str, Any] = Body(...),
        _user: Dict[str, Any] = Depends(_optional_auth),
    ):
        batch = EventGraphAppend(**body)
        count = store.append(batch)
        return {"ok": True, "appended": count, "trace_id": batch.trace_id}

    @api.post("/event-graph/query")
    async def query_event_graph(
        body: Dict[str, Any] = Body(...),
        _user: Dict[str, Any] = Depends(_optional_auth),
    ):
        query = EventGraphQuery(**body)
        events = store.query(query)
        return {"ok": True, "count": len(events), "events": [e.model_dump(mode="json") for e in events]}

    app.include_router(api)

