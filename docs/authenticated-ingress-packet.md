# Authenticated ingress packet, October 2026

Status: implementation under review. This is a narrow software assurance packet,
not clearance for private health data or an operational runtime deployment.

`POST /input` is selected as the canonical intent ingress for the next runtime
integration packet. Its request identity comes from PrincipalBindingMiddleware.
Person-scoped event append and query use the same authenticated identity. A
workload needs a separately verified delegation design before using these
person-scoped routes; possession of a service token alone is insufficient.

Execution requires an available allowing policy decision. A decision requiring
confirmation produces an incomplete outcome and no tool invocation. This route
does not yet expose an exact-action approval/resume transport. Missing policy
configuration or policy outage denies execution. Raw input is omitted from this
runner's event diagnostics; this does not certify all other logs or persistence.

## Evidence and reproduction

Evidence class: local Windows Python 3.12 authenticated ASGI integration and unit
tests. Synthetic RSA keys and two synthetic people; loopback JWKS and session
introspection; real signature, expiry and audience checks; no auth dependency
override. No operational service, real account or personal record is used.

Run from component root with the pinned requirements and adjacent workspace
`unison-common` source:

```text
python -m pytest tests/test_authenticated_input_boundary.py tests/test_event_graph_store.py tests/test_input_runner_first_feedback.py tests/test_phase2_context_and_policy.py
```

The initial 15-case authenticated/boundary suite reproduced eight failures and
seven passes before repair. After repair, that suite plus six adjacent tests
passed (21 total). The expanded focused suite passed 23 tests. The wider legacy
suite produced 265 passes and one outdated demo expectation that assumed policy
absence permitted execution; that expectation now requires a traceable denial.
Final revision verification is recorded in the PR checks and local handoff.

Review follow-up: vendor JSON and case-variant JSON bodies now receive recursive
identity validation independent of MIME matching; persisted event identities and
speech attributes are stamped from the bound person. The legacy Phase 1 ingress
switch fails closed. Trace identifiers are constrained and diagnostic filenames
are generated exclusively by the server, preventing caller path selection or
replacement of an earlier trace. Production cache lookup follows authentication
and uses a principal-specific namespace; authenticated tests cover cache replay,
missing/revoked authority, and another person reusing the same key.

The follow-up full local suite passed 278 tests before the trace-file change;
eight focused trace/runner tests pass after that change. Hosted final-head checks
remain the merge gate. Cache isolation does not establish durable exactly-once
execution or atomic spending budgets.

## Remaining gates

This packet does not certify every legacy ingress, retrieval or output path.
Independent inference authority and fallback eligibility, exact execution
approval, encrypted derivatives, transactional private state, recovery and update
security remain required before the health-data pilot. The planner remains a
limited deterministic implementation. No real model or wearable integration is
claimed. Hardware and participatory validation remain separate.

Component changes require human review. Workspace pins advance only after the
component is pushed and validated. Detailed unresolved findings stay in local
working notes pending remediation.
