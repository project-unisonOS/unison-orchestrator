from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

from unison_common import (
    EquivalenceFinding, EquivalenceReport, ExpressionPlan, ExpressionPlanRequest,
    InteractionSession, PendingConfirmation, SemanticAction, SemanticExperience,
    SemanticExpression, SemanticNode, SemanticNodeKind, SemanticObservation,
    SemanticProvenance,
)


_ORDER = ("conversation", "visual", "braille", "sign", "switch-aac", "haptic")
_SOURCE_PRIORITY = {"api": 0, "document": 1, "accessibility-tree": 2, "computer-use": 3, "vision": 4}


class ExpressionPlanningError(ValueError):
    pass


def plan_expression(request: ExpressionPlanRequest) -> ExpressionPlan:
    """Produce a reproducible plan; privacy and safety exclusions are deterministic."""
    raw = request.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    available_in = {c.modality: c for c in request.capabilities if c.input_available and c.healthy}
    available_out = {c.modality: c for c in request.capabilities if c.output_available and c.healthy}
    unavailable = set(request.unavailable_modalities)
    explanation: list[str] = []
    constraints: list[str] = []

    def candidates(explicit, preferred, available):
        ordered = [explicit] if explicit else []
        ordered += list(preferred) + list(_ORDER)
        return [m for i, m in enumerate(ordered) if m and m not in ordered[:i] and m in available and m not in unavailable]

    input_choices = candidates(request.requested_input, request.preferred_inputs, available_in)
    if not input_choices:
        raise ExpressionPlanningError("no healthy permitted input modality")

    forbidden: set[str] = set()
    env = request.environment
    if env.quiet_mode:
        forbidden.add("conversation"); constraints.append("quiet mode forbids spoken output")
    if env.sensitive_content and (env.shared_room or env.bystanders) and not env.allow_spoken_sensitive:
        forbidden.add("conversation"); constraints.append("sensitive content cannot be spoken with others present")
    if env.sensitive_content and not env.allow_displayed_sensitive:
        forbidden.add("visual"); constraints.append("situational policy forbids displaying sensitive content")
    permitted_out = {m: c for m, c in available_out.items() if m not in forbidden and m not in unavailable}
    requested = request.requested_outputs or request.preferred_outputs
    output_choices = candidates(None, requested, permitted_out)
    if not output_choices:
        raise ExpressionPlanningError("no healthy output modality satisfies deterministic safety constraints")
    selected = [m for m in output_choices if m in request.requested_outputs][:2] or [output_choices[0]]
    if request.requested_outputs and selected != request.requested_outputs:
        explanation.append("Unavailable or unsafe requested outputs were replaced without changing the pending action")
    explanation.append(f"Input uses {input_choices[0]}; output uses {', '.join(selected)}")
    if env.offline:
        explanation.append("Plan uses locally reported capabilities because the system is offline")
    fallbacks = [m for m in output_choices if m not in selected]
    return ExpressionPlan(
        plan_id=f"expression:{request.session_id}:{digest[:12]}", person_id=request.person_id,
        session_id=request.session_id, input_modality=input_choices[0], output_modalities=selected,
        fallbacks=fallbacks, explanation=explanation, deterministic_constraints=constraints,
        recorded_inputs_sha256=digest,
    )


@dataclass
class InteractionSessionStore:
    sessions: dict[str, InteractionSession] = field(default_factory=dict)

    def open(self, session_id: str, person_id: str) -> InteractionSession:
        session = InteractionSession(session_id=session_id, person_id=person_id)
        self.sessions[session_id] = session
        return session

    def switch(self, session_id: str, modalities: list[str]) -> InteractionSession:
        current = self.sessions[session_id]
        updated = current.model_copy(update={"active_modalities": modalities, "revision": current.revision + 1})
        self.sessions[session_id] = updated
        return updated

    def issue_confirmation(self, session_id: str, action_id: str, modality: str) -> PendingConfirmation:
        current = self.sessions[session_id]
        confirmation = PendingConfirmation(
            confirmation_id=secrets.token_urlsafe(16), action_id=action_id,
            person_id=current.person_id, issued_for_modality=modality, nonce=secrets.token_urlsafe(24),
        )
        self.sessions[session_id] = current.model_copy(update={
            "confirmations": [*current.confirmations, confirmation], "revision": current.revision + 1,
        })
        return confirmation

    def consume_confirmation(self, session_id: str, person_id: str, action_id: str, nonce: str) -> None:
        current = self.sessions[session_id]
        match = next((c for c in current.confirmations if c.action_id == action_id and c.nonce == nonce), None)
        if not match or match.person_id != person_id or match.consumed:
            raise PermissionError("confirmation is invalid, belongs to another person, or was already used")
        confirmations = [c.model_copy(update={"consumed": True}) if c.confirmation_id == match.confirmation_id else c for c in current.confirmations]
        self.sessions[session_id] = current.model_copy(update={"confirmations": confirmations, "revision": current.revision + 1})


def compare_expressions(left: SemanticExpression, right: SemanticExpression) -> EquivalenceReport:
    findings: list[EquivalenceFinding] = []
    for code, lhs, rhs in (
        ("required-meaning", set(left.required_node_ids), set(right.required_node_ids)),
        ("available-actions", set(left.action_ids), set(right.action_ids)),
    ):
        if lhs != rhs:
            findings.append(EquivalenceFinding(severity="error", code=code, detail=f"Mismatch: {sorted(lhs ^ rhs)}"))
    if bool(left.fallback) != bool(right.fallback):
        findings.append(EquivalenceFinding(severity="error", code="recovery", detail="Recovery is unavailable in one expression"))
    return EquivalenceReport(
        experience_id=left.experience_id, left_modality=left.modality,
        right_modality=right.modality, equivalent=not findings, findings=findings,
    )


def interpret_observations(*, trace_id: str, session_id: str, person_id: str, observations: list[SemanticObservation]) -> SemanticExperience:
    """Reconcile observations into SEM, excluding untrusted instructions from authority."""
    if not observations:
        raise ValueError("at least one observation is required")
    ordered = sorted(observations, key=lambda item: (_SOURCE_PRIORITY[item.source_type], -item.confidence))
    newest = max(item.observed_at for item in ordered)
    stale = [item for item in ordered if item.observed_at < newest or item.content.get("stale")]
    ambiguities = [detail for item in ordered for detail in item.ambiguities]
    injections = [signal for item in ordered for signal in item.injection_signals]
    nodes: list[SemanticNode] = []
    seen: set[str] = set()
    for item in ordered:
        for raw in item.content.get("nodes", []):
            node_id = str(raw.get("node_id", ""))
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            nodes.append(SemanticNode(
                node_id=node_id, kind=SemanticNodeKind(raw.get("kind", "value")),
                label=str(raw.get("label") or node_id), value=raw.get("value"),
                summary=raw.get("summary"), required=bool(raw.get("required")),
                uncertainty="; ".join(item.ambiguities) or None,
                provenance=[SemanticProvenance(source_id=item.source_id, source_type=item.source_type, observed_at=item.observed_at, confidence=item.confidence)],
            ))
    stopped = bool(stale or ambiguities)
    recovery = None
    if stopped:
        recovery = "The source changed or the target is ambiguous. Refresh it and ask me to identify the target again."
    actions: list[SemanticAction] = []
    if not stopped:
        for item in ordered:
            for raw in item.content.get("actions", []):
                binding = raw.get("authenticated_target")
                if not binding or binding.get("state_version") != item.state_version:
                    continue
                actions.append(SemanticAction.model_validate({**raw, "target": binding, "provenance": [{"source_id": item.source_id, "source_type": item.source_type, "observed_at": item.observed_at, "confidence": item.confidence}]}))
    outcome = "I found the available meaning and actions."
    if injections:
        outcome += " I ignored instructions embedded in untrusted content."
    if stopped:
        outcome = "I stopped before acting because the source is stale or ambiguous."
    return SemanticExperience(
        experience_id=f"sem:{trace_id}", trace_id=trace_id, session_id=session_id,
        person_id=person_id, purpose="interpret an existing experience", outcome=outcome,
        nodes=nodes, actions=actions, recovery=recovery,
        privacy={"untrusted_instructions_ignored": len(injections), "source_priority": _SOURCE_PRIORITY},
    )
