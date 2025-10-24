# unison-orchestrator

The orchestrator is the decision layer for the Unison system.

Responsibilities (future):
- Accept user intent from I/O modules.
- Query `unison-context` for current state.
- Call skills and generation providers.
- Enforce `unison-policy` for safety and consent.
- Drive `unison-io-*` to render responses (speech, canvas, etc.).

Current state:
- Minimal HTTP service with `/health` and `/ready`.
- Containerized for inclusion in `unison-devstack`.
