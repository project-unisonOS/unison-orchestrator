# Synthetic export pilot adapter

`ExportPilotReader` in `src/orchestrator/wellness_storage.py` is an explicitly
configured adapter for the default-disabled storage export pilot. It never falls
back to manually posted domain facts. It uses the current signed person bearer,
requires the storage audience, and accepts only literal loopback transport with
redirects and environment proxies disabled. No remote-service topology is qualified.

Use the same instance as the `reader` and `policy` of `SyntheticWellnessJourney`
in the isolated integration harness. A configured consent ID is not itself
permission: storage rechecks durable owner/purpose/version/expiry/source policy
before reading and presenting. The model and request payload cannot grant authority.
Facts must match the versioned extraction receipt and current consent. Unknown
policy versions, malformed responses, hash mismatch and outages fail closed.

The workspace integration uses actual storage/ingress ASGI routes, encrypted source
state and synthetic exports. It verifies revocation between reading and presenting.
This is not an atomic remote-delivery protocol or protection from process compromise.
Production wiring, real data, external channels and inference remain unqualified.
