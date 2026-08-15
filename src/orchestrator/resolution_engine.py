"""Bounded resolution ladder for unfamiliar requests."""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4
from unison_common.resolution import ResolutionAttempt, ResolutionBudget, ResolutionRoute

@dataclass(frozen=True)
class ResolutionResult:
    attempt: ResolutionAttempt
    semantic_outcome: dict

class ResolutionEngine:
    def __init__(self, deterministic_routes: dict[str, Callable[[dict], dict]],
                 local_inference: Callable[[str, dict], dict] | None = None):
        self.deterministic_routes = deterministic_routes
        self.local_inference = local_inference

    def resolve(self, *, person_id: str, assistant_id: str, request_class: str,
                semantic_request: dict, authorized_spaces: tuple[str, ...],
                authorized_domains: tuple[str, ...], budget: ResolutionBudget) -> ResolutionResult:
        fingerprint = hashlib.sha256(json.dumps({"request_class": request_class,
            "input_fields": sorted(semantic_request), "risk": semantic_request.get("risk", "medium")},
            sort_keys=True).encode()).hexdigest()
        routes: list[ResolutionRoute] = []
        handler = self.deterministic_routes.get(request_class)
        if handler:
            routes.append(ResolutionRoute(route_id="deterministic", kind="known-deterministic", state="running"))
            outcome = handler(semantic_request)
            routes[-1] = routes[-1].model_copy(update={"state": "succeeded"})
            state = "complete"
        else:
            routes.append(ResolutionRoute(route_id="deterministic", kind="known-deterministic", state="blocked",
                deterministic_rejection_reason="no reviewed route for this request shape"))
            if self.local_inference and budget.model_calls > 0:
                routes.append(ResolutionRoute(route_id="local-inference", kind="bounded-local-inference",
                    state="running", model_ids=("eligible-local",)))
                outcome = self.local_inference(request_class, semantic_request)
                routes[-1] = routes[-1].model_copy(update={"state": "succeeded"})
                state = "complete" if outcome.get("complete") else "partial"
            else:
                routes.append(ResolutionRoute(route_id="partial", kind="partial-outcome", state="succeeded"))
                outcome = {"complete": False, "summary": "Prepared a safe continuation plan.",
                    "actions": ["clarify", "resume", "cancel"], "uncertainties": ["No eligible local route is available"],
                    "recovery": {"resume_from": "route-selection"}}
                state = "partial"
        attempt = ResolutionAttempt(attempt_id=str(uuid4()), owner_person_id=person_id,
            assistant_instance_id=assistant_id, purpose=str(semantic_request.get("purpose", request_class)),
            risk=semantic_request.get("risk", "medium"), requested_result_class=request_class,
            authorized_space_ids=authorized_spaces, authorized_domain_ids=authorized_domains,
            budget=budget, routes=tuple(routes), state=state, structural_fingerprint=fingerprint,
            uncertainties=tuple(outcome.get("uncertainties", [])), recovery_state=outcome.get("recovery", {}))
        return ResolutionResult(attempt=attempt, semantic_outcome=outcome)
