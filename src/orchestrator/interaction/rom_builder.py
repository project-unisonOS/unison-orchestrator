from __future__ import annotations

from dataclasses import dataclass

from unison_common import (
    ActionResult,
    PolicyDecision,
    ResponseObjectModel,
    RomText,
    SemanticAction,
    SemanticExperience,
    SemanticNode,
    SemanticNodeKind,
    SemanticProvenance,
)


@dataclass(frozen=True)
class RomBuilder:
    """Build canonical semantic output and a temporary ROM compatibility view."""

    def build_sem(
        self,
        *,
        trace_id: str,
        session_id: str,
        person_id: str,
        tool_result: ActionResult,
        policy: PolicyDecision | None = None,
    ) -> SemanticExperience:
        result = tool_result.result or {}
        if tool_result.ok:
            outcome = str(result.get("text") or result.get("outcome") or "Completed")
        else:
            outcome = f"Tool failed: {tool_result.error or 'unknown error'}"
            if policy and not policy.allowed and policy.reason:
                outcome = f"Policy denied: {policy.reason}"
        provenance = [SemanticProvenance(source_id=tool_result.action_id, source_type="capability-result")]
        nodes = [SemanticNode(node_id="outcome", kind=SemanticNodeKind.OUTCOME, label="Outcome", value=outcome, summary=outcome, required=True, provenance=provenance)]
        actions = []
        for raw in result.get("semantic_actions", []) if isinstance(result, dict) else []:
            actions.append(SemanticAction.model_validate(raw))
        return SemanticExperience(
            experience_id=f"sem:{trace_id}", trace_id=trace_id, session_id=session_id,
            person_id=person_id, purpose="complete requested intent", outcome=outcome,
            nodes=nodes, actions=actions,
            privacy={"policy_allowed": bool(policy.allowed) if policy else None},
            recovery=str(result.get("recovery")) if isinstance(result, dict) and result.get("recovery") else None,
        )

    def build(
        self,
        *,
        trace_id: str,
        session_id: str,
        person_id: str,
        tool_result: ActionResult,
        policy: PolicyDecision | None = None,
    ) -> ResponseObjectModel:
        sem = self.build_sem(trace_id=trace_id, session_id=session_id, person_id=person_id, tool_result=tool_result, policy=policy)
        return ResponseObjectModel(
            trace_id=trace_id,
            session_id=session_id,
            person_id=person_id,
            blocks=[RomText(text=sem.outcome)],
            meta={
                "origin": "semantic-rom-compatibility",
                "policy": (policy.model_dump(mode="json") if policy else None),
                "semantic_experience": sem.model_dump(mode="json"),
            },
        )
