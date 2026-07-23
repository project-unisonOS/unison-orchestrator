"""Inspectable, cancellable, recoverable execution for bounded Phase 7 journeys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from unison_common.workflows import (
    ApprovalRecord,
    FailureRecovery,
    OutcomeEvidence,
    OutcomeMetrics,
    StepState,
    TaskPlan,
    WorkflowKind,
    WorkflowState,
    WorkflowStep,
    validate_ranking_signals,
)

from .providers import FakeProvider, ProviderError, ProviderTimeout, default_fake_providers


class WorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_id: str
    assistant_id: str
    kind: WorkflowKind
    purpose: str
    context_space_ids: tuple[str, ...]
    allowed_context_space_ids: tuple[str, ...]
    recipient_ids: tuple[str, ...] = ()
    allowed_recipient_ids: tuple[str, ...] = ()
    charter_constraints: tuple[str, ...] = ()
    commitment_ids: tuple[str, ...] = ()
    content: dict[str, Any] = Field(default_factory=dict)
    ranking_signals: dict[str, float] = Field(default_factory=lambda: {"time_returned": 1.0})
    idempotency_key: str


@dataclass
class _Execution:
    plan: TaskPlan
    request: WorkflowRequest
    receipts: list[tuple[str, str]]
    approvals: dict[str, ApprovalRecord]
    audit: list[str]
    recovery_count: int = 0
    cancelled: bool = False


_TEMPLATES: dict[WorkflowKind, tuple[str, str, int, bool]] = {
    WorkflowKind.CALENDAR_COORDINATION: ("calendar", "propose_then_create_event", 12, True),
    WorkflowKind.EMAIL_TRIAGE_DRAFT: ("mail", "triage_summarize_and_draft", 10, True),
    WorkflowKind.REMINDER_COMMITMENT_REVIEW: ("tasks", "review_and_schedule_reminder", 8, False),
    WorkflowKind.HOUSEHOLD_COORDINATION: ("household", "coordinate_shared_artifact", 9, True),
    WorkflowKind.CONTACT_RECALL: ("contacts", "recall_relationship_context", 4, False),
    WorkflowKind.DOCUMENT_WEB_RESEARCH: ("research", "retrieve_summarize_with_citations", 18, False),
    WorkflowKind.TRAVEL_PLANNING: ("travel", "compare_itinerary_options", 20, False),
}


class GovernedWorkflowEngine:
    def __init__(self, providers: dict[str, FakeProvider] | None = None) -> None:
        self.providers = providers or default_fake_providers()
        self._executions: dict[str, _Execution] = {}
        self._outcomes: dict[str, OutcomeEvidence] = {}

    def plan(self, request: WorkflowRequest) -> TaskPlan:
        validate_ranking_signals(request.ranking_signals)
        requested_spaces = set(request.context_space_ids)
        if not requested_spaces or not requested_spaces.issubset(request.allowed_context_space_ids):
            raise PermissionError("workflow requested an unauthorized context space")
        if not set(request.recipient_ids).issubset(request.allowed_recipient_ids):
            raise PermissionError("workflow requested a wrong-context recipient")
        provider_kind, action, _minutes, approval = _TEMPLATES[request.kind]
        external = provider_kind not in {"tasks", "contacts"}
        disclosed = self._minimal_fields(request.kind)
        step = WorkflowStep(
            step_id=f"{provider_kind}-1",
            capability=f"phase7.{provider_kind}",
            action=action,
            provider=self.providers[provider_kind].provider_id,
            requires_approval=approval,
            external_call=external,
            reversible=True,
            recipient_ids=request.recipient_ids,
            disclosed_fields=disclosed if external else (),
        )
        plan = TaskPlan(
            person_id=request.person_id,
            assistant_id=request.assistant_id,
            kind=request.kind,
            purpose=request.purpose,
            context_space_ids=request.context_space_ids,
            charter_constraints=request.charter_constraints,
            commitment_ids=request.commitment_ids,
            steps=(step,),
            state=WorkflowState.AWAITING_APPROVAL if approval else WorkflowState.PLANNED,
            idempotency_key=request.idempotency_key,
        )
        self._executions[plan.plan_id] = _Execution(
            plan=plan,
            request=request,
            receipts=[],
            approvals={},
            audit=["plan.created", "policy.context_allowed", "ranking.person_aligned"],
        )
        return plan

    def approve(
        self,
        plan_id: str,
        *,
        step_id: str,
        person_id: str,
        exact_action: str,
        exact_recipients: tuple[str, ...],
        approved: bool,
    ) -> ApprovalRecord:
        execution = self._executions[plan_id]
        step = self._step(execution.plan, step_id)
        if person_id != execution.plan.person_id:
            raise PermissionError("only the bound person may approve")
        if exact_action != step.action or exact_recipients != step.recipient_ids:
            raise PermissionError("approval does not match exact action and recipients")
        record = ApprovalRecord(
            plan_id=plan_id,
            step_id=step_id,
            person_id=person_id,
            exact_action=exact_action,
            exact_recipients=exact_recipients,
            approved=approved,
        )
        execution.approvals[step_id] = record
        execution.audit.append("approval.accepted" if approved else "approval.denied")
        return record

    def cancel(self, plan_id: str) -> OutcomeEvidence:
        execution = self._executions[plan_id]
        execution.cancelled = True
        for provider_kind, receipt in reversed(execution.receipts):
            self.providers[provider_kind].compensate(receipt)
        execution.audit.extend(("workflow.cancelled", "workflow.compensated"))
        return self._evidence(
            execution,
            WorkflowState.COMPENSATED if execution.receipts else WorkflowState.CANCELLED,
            recoveries=1 if execution.receipts else 0,
        )

    def execute(self, plan_id: str) -> OutcomeEvidence:
        if plan_id in self._outcomes:
            return self._outcomes[plan_id]
        execution = self._executions[plan_id]
        if execution.cancelled:
            return self.cancel(plan_id)
        for step in execution.plan.steps:
            if step.requires_approval:
                approval = execution.approvals.get(step.step_id)
                if not approval or not approval.approved:
                    execution.audit.append("execution.blocked_missing_approval")
                    return self._evidence(execution, WorkflowState.AWAITING_APPROVAL)
            provider_kind = step.capability.rsplit(".", 1)[-1]
            provider = self.providers[provider_kind]
            payload = self._minimized_payload(execution.request)
            execution.audit.extend(("step.running", "disclosure.minimized"))
            try:
                receipt = provider.execute(
                    action=step.action,
                    payload=payload,
                    idempotency_key=f"{execution.plan.idempotency_key}:{step.step_id}",
                )
            except (ProviderTimeout, ProviderError) as exc:
                execution.recovery_count += 1
                recovery = FailureRecovery(
                    failure_code="provider_timeout" if isinstance(exc, ProviderTimeout) else "provider_error",
                    failed_step_id=step.step_id,
                    safe_to_retry=True,
                    retry_count=step.attempt + 1,
                    compensation_actions=("cancel_completed_external_actions",),
                    user_message="The provider did not complete the step. Retry, replace the provider, or cancel.",
                )
                execution.audit.extend(("step.failed", "recovery.available"))
                return self._evidence(execution, WorkflowState.RECOVERABLE, recovery=recovery)
            execution.receipts.append((provider_kind, receipt))
            execution.audit.extend(("step.completed", "provider.receipt_recorded"))
        outcome = self._evidence(execution, WorkflowState.COMPLETED)
        self._outcomes[plan_id] = outcome
        return outcome

    def retry(self, plan_id: str) -> OutcomeEvidence:
        execution = self._executions[plan_id]
        execution.audit.append("recovery.retry")
        return self.execute(plan_id)

    def replace_provider(self, plan_id: str, *, provider: FakeProvider) -> OutcomeEvidence:
        execution = self._executions[plan_id]
        kind = execution.plan.steps[0].capability.rsplit(".", 1)[-1]
        if provider.kind != kind:
            raise ValueError("replacement provider kind must match")
        self.providers[kind] = provider
        execution.audit.append("recovery.provider_replaced")
        return self.execute(plan_id)

    @staticmethod
    def _minimal_fields(kind: WorkflowKind) -> tuple[str, ...]:
        return {
            WorkflowKind.CALENDAR_COORDINATION: ("availability", "attendees", "duration"),
            WorkflowKind.EMAIL_TRIAGE_DRAFT: ("sender", "subject", "body_excerpt", "recipient"),
            WorkflowKind.HOUSEHOLD_COORDINATION: ("shared_artifact", "members"),
            WorkflowKind.DOCUMENT_WEB_RESEARCH: ("query", "approved_document_excerpt"),
            WorkflowKind.TRAVEL_PLANNING: ("dates", "origin", "destination", "constraints"),
        }.get(kind, ())

    def _minimized_payload(self, request: WorkflowRequest) -> dict[str, Any]:
        allowed = set(self._minimal_fields(request.kind))
        payload = {key: request.content[key] for key in sorted(allowed) if key in request.content}
        if request.recipient_ids:
            payload["recipient_ids"] = list(request.recipient_ids)
        # External text remains tainted data. Provider instructions are never promoted to authority.
        payload.pop("instructions", None)
        payload.pop("sponsored", None)
        payload["purpose"] = request.purpose
        return payload

    def _evidence(
        self,
        execution: _Execution,
        state: WorkflowState,
        *,
        recovery: FailureRecovery | None = None,
        recoveries: int | None = None,
    ) -> OutcomeEvidence:
        _provider, _action, minutes, _approval = _TEMPLATES[execution.plan.kind]
        completed = len(execution.receipts) if state in {WorkflowState.COMPLETED, WorkflowState.COMPENSATED} else 0
        disclosures = sum(len(step.disclosed_fields) for step in execution.plan.steps if step.external_call)
        return OutcomeEvidence(
            plan_id=execution.plan.plan_id,
            person_id=execution.plan.person_id,
            kind=execution.plan.kind,
            state=state,
            completed_step_ids=tuple(step.step_id for step in execution.plan.steps[:completed]),
            provider_receipts=tuple(receipt for _, receipt in execution.receipts),
            recovery=recovery,
            metrics=OutcomeMetrics(
                administrative_tasks_completed=1 if state is WorkflowState.COMPLETED else 0,
                commitments_completed=1
                if state is WorkflowState.COMPLETED and execution.plan.commitment_ids
                else 0,
                interruptions_avoided=1 if state is WorkflowState.COMPLETED else 0,
                recoveries=execution.recovery_count if recoveries is None else recoveries,
                external_calls=len(execution.receipts),
                minimized_fields_disclosed=disclosures,
                estimated_minutes_returned=minutes if state is WorkflowState.COMPLETED else 0,
                boundary_incidents=0,
            ),
            audit_events=tuple(execution.audit),
        )

    @staticmethod
    def _step(plan: TaskPlan, step_id: str) -> WorkflowStep:
        for step in plan.steps:
            if step.step_id == step_id:
                return step
        raise KeyError(step_id)
