# unison-orchestrator

Intent gateway and coordination layer for UnisonOS.

## Status
Core service (active). The implemented server is bootstrapped from `src/server.py` and most route groups live under `src/orchestrator/api/`.

## What is implemented
- Event ingress and intent routing via `/event`, `/ingest`, and `/input`.
- Voice ingestion route group in `src/orchestrator/api/voice.py`.
- Skills registry endpoints in `src/orchestrator/api/skills.py`.
- Payments proxy endpoints in `src/orchestrator/api/payments.py`.
- Replay and event-graph helpers in `src/orchestrator/replay.py` and `src/orchestrator/api/event_graph.py`.
- Router configuration/admin endpoints and health/metrics endpoints.
- Dashboard refresh, workflow recall/design, comms, payments, capability, and actuation flows covered by the current test suite.

## Important route groups
- `GET /health`, `GET /readyz`, `GET /ready`, `GET /metrics`
- `POST /event`
- `POST /event/confirm`
- `POST /ingest`
- `POST /input`
- `POST /voice/ingest`
- `GET /skills`
- `POST /skills`
- `GET /capabilities`
- `POST /payments/instruments`
- `POST /payments/transactions`
- `GET /payments/transactions/{txn_id}`
- `POST /payments/webhooks/{provider}`
- `GET /replay/traces`
- `POST /replay/{trace_id}`
- `POST /event-graph/append`
- `POST /event-graph/query`
- Admin/router endpoints under `/router/*`

## Local development
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -c ../constraints.txt -r requirements.txt
cp .env.example .env
python src/server.py
```

## Notable supporting files
- `routing_rules.yaml` — router strategy rules
- `scripts/thin_slice.py` — local vertical-slice exercise
- `scripts/event_graph_replay.py` — replay helper
- `event_graph/events.jsonl` — local event-graph artifact

## Tests
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -c ../constraints.txt -r requirements.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 OTEL_SDK_DISABLED=true python -m pytest
```

## Docs
- Public docs: https://project-unisonos.github.io
- Internal docs: `SETUP.md`, `SECURITY.md`
