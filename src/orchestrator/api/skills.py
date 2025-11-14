from __future__ import annotations

import logging
from typing import Any, Callable, Dict, MutableMapping

from fastapi import APIRouter, Body, HTTPException

from unison_common.logging import log_json

SkillHandler = Callable[[Dict[str, Any]], Dict[str, Any]]


def register_skill_routes(
    app,
    *,
    skills: Dict[str, SkillHandler],
    handlers: Dict[str, SkillHandler],
    metrics: MutableMapping[str, int],
) -> None:
    router = APIRouter()

    @router.get("/skills")
    def list_skills():
        metrics["/skills"] += 1
        return {"skills": list(skills.keys()), "count": len(skills)}

    @router.post("/skills")
    def add_skill(skill: Dict[str, Any] = Body(...)):
        prefix = skill.get("intent_prefix")
        handler_name = skill.get("handler", "echo")
        if not isinstance(prefix, str) or not prefix:
            raise HTTPException(status_code=400, detail="invalid intent_prefix")
        if handler_name not in handlers:
            raise HTTPException(status_code=400, detail="unknown handler")

        context_keys = skill.get("context_keys")
        if context_keys is not None and not isinstance(context_keys, list):
            raise HTTPException(
                status_code=400, detail="context_keys must be a list if provided"
            )
        if prefix in skills:
            raise HTTPException(
                status_code=409, detail=f"intent_prefix already registered: {prefix}"
            )
        skills[prefix] = handlers[handler_name]
        entry = {"intent_prefix": prefix, "handler": handler_name}
        if context_keys:
            entry["context_keys"] = context_keys
        log_json(
            logging.INFO,
            "skill_added",
            service="unison-orchestrator",
            intent_prefix=prefix,
            handler=handler_name,
        )
        return {"ok": True, "skill": entry}

    app.include_router(router)
