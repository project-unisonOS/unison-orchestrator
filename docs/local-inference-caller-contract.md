# Proposed local text caller contract

Status: isolated follow-up module, not connected to a production route. The
reviewed PR47 head is unchanged. This adapter is intended for the next controlled
integration harness, not a bypass for legacy callers that lack authority.

`LocalTextInferenceClient.request` accepts a typed, zero-cost, local text task
requirement and a bounded prompt. It takes person identity and the token from
the authenticated request context. It requires the issuer-provided inference
audience and inference.local scope before any egress; it cannot mint, exchange
or expand a token. The inference service must independently authenticate and
introspect the forwarded token. Expiry/revocation failures stop the request.

The initial transport accepts literal loopback IPs, disables proxies/redirects,
and does not retry or choose fallback providers. This is a locality constraint,
not proof of service identity or isolation from malicious local processes. A
reviewed owned-runtime transport remains required before private deployment.

Results contain only text, model label and operation ID, marked untrusted. They
do not contain executable actions or grant retrieval, tool, delivery or disclosure
authority. A result claiming another provider, fallback, another operation or
tool calls is rejected. Independent policy enforcement is still required around
retrieval and presentation; capability scope alone is not that policy decision.

## Evidence

Sixteen local tests pass using ephemeral RSA/JWKS and revocation introspection
plus a controlled loopback HTTP inference service. They verify exact token
forwarding, explicit task fields, no egress on missing scope/audience/revocation,
transport rejection and untrusted response handling. No production inference
service or actual model ran. The legacy companion/skills/Phase1 callers remain
unmigrated and must not be advertised as working with the new inference contract.
