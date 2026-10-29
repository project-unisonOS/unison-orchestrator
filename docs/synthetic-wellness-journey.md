# First synthetic read-only wellness journey

Status: experimental implementation for controlled tests. Production does not
configure this journey; requesting it returns unavailable. No actual wearable,
health record, genetic data, clinician workflow or model was connected.

The canonical POST /input accepts a wellness.daily intent with a source day.
The authenticated person needs separate read and presentation scopes. An
independent policy check precedes retrieval and another precedes returning the
brief. Denial or required confirmation yields no brief. Storage reads forward
the verified token only with storage audience, and the storage API independently
authenticates it. Responses stay in the requesting person's private scope.

The deterministic brief reports sleep duration and steps as changed, unchanged
or unknown. Comparisons use up to 30 prior source days with at least three
comparable days; these are descriptive fixture rules, not clinical thresholds.
Source/device/method/unit/timezone must match. Missing observations remain unknown,
zero remains a numeric observation, syndicated upstream records are deduplicated,
later revisions replace earlier ones, and conflicting equal revisions fail closed.
Vendor scores and inferred statements are not treated as measurements.

Each observation retains its original payload, source record and upstream IDs,
device/method, units, measurement interval, original IANA timezone and source day,
ingestion time, quality/missingness, consent reference, transformation version and
revision. Facts and inferences remain distinct. The consent reference is
provenance, not proof of an independently verified consent grant.

One text rendering contains the same descriptive entries as the structured
brief. Source/observation references accompany entries. There are no treatment
recommendations or inferred wellness opportunities, and no side-effect tools.
This lets the first data/provenance journey proceed independently of unqualified
model execution.

## Evidence and limits

Component tests exercise deterministic normalization plus signed-identity ingress
with controlled reader/policy fixtures. The workspace integration test exercises
real storage import, admission, domain record, read and delete routes, actual
encrypted source/domain databases, restart, cross-person isolation and revocation.
Storage transport is in-process ASGI; JWKS/introspection is a loopback service;
policy decisions are controlled fixtures. This is not a full deployed runtime or
an independent policy-engine qualification. No fixture is presented as a real
wearable API or model result.

Source/domain deletion still spans two commits; the test covers ordinary cascade,
not crash atomicity or simultaneous ingest/delete/correction. Migration, backup
integration, consent-grant lifecycle, cache invalidation across every route,
all derived logs/stores and real-device qualification remain release blockers.
Only the test harness sets app.state.synthetic_wellness_journey. Do not enable a
real-data runtime based on these tests or a caller-supplied synthetic flag.
