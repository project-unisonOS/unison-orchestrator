from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends

from orchestrator.event_graph.store import JsonlEventGraphStore
from unison_common.principal import PrincipalContext
from orchestrator.api.principal import require_bound_person
from unison_common.contracts.v1 import EventGraphAppend, EventGraphQuery


def register_event_graph_routes(app) -> None:
    api = APIRouter()
    store = JsonlEventGraphStore.from_env()

    @api.post("/event-graph/append")
    async def append_event_graph(
        body: Dict[str, Any] = Body(...),
        principal: PrincipalContext = Depends(require_bound_person),
    ):
        batch = EventGraphAppend(**{**body, "person_id": principal.person_id})
        count = store.append(batch)
        return {"ok": True, "appended": count, "trace_id": batch.trace_id}

    @api.post("/event-graph/query")
    async def query_event_graph(
        body: Dict[str, Any] = Body(...),
        principal: PrincipalContext = Depends(require_bound_person),
    ):
        query = EventGraphQuery(**{**body, "person_id": principal.person_id})
        events = store.query(query)
        return {"ok": True, "count": len(events), "events": [e.model_dump(mode="json") for e in events]}

    app.include_router(api)

