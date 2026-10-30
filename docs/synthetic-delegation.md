# Synthetic delegation and multimodal intent reference

Status: bounded connected mock prototype, not a live account client.

`synthetic-delegation.v2` binds confirmation to the exact grant identity, person,
account, purpose, actions, recipient, terms, duration, creation/expiry and approval
deadline. Consequential confirmations additionally bind the exact task identity
and approval deadline. Expired or mutated terms fail closed. Earlier unmerged v1
state is deliberately incompatible; no automatic migration or authority expansion.

`SyntheticIntentInteraction` connects trusted state to visual and screenless
command/response flows through an optional renderer fixture bridge. The same
presented repair choice now requests a bound challenge; explicit simulated
verification completes it. Late or cross-task challenge responses fail. An
unchanged active delegation is reused; new authority is not requested per task.

Three simulated credential-use adapters have distinct lifecycle behavior:
OAuth rotates expired access under a valid refresh lease; session expiry requires
repair without automatic renewal; vault-use approval expires or locks and requires
renewed approval. All return opaque handles. No credentials, vault contents,
actual OAuth, MFA or 1Password integration are used. The protocol remains
replaceable, but only mock provider instances are qualified.

The renderer's `/static/intent-experience.html` is a connected test expression,
not a production destination. It posts structured choices to the default-disabled
`/synthetic-intent` bridge, which requires a bound person and an explicitly injected
mock coordinator. Visual controls and screenless typed commands drive the same
state transitions. Spoken text is a representation: no ASR/TTS/audio playback or
participant accessibility qualification is claimed. Unsupported nonvisual provider
verification explains its limit and preserves progress. No transport destination
is exposed as the person's goal.

Five tasks share one repair; paused, revoked and expired work does not resume
through repair. Pending attempts are stored before provider execution and verified
before continuation. Absence or uncertainty never automatically repeats an
existing attempt. Pause does not revoke authority or undo an external effect.
Deletion retains minimal terminal authority, not proof of physical erasure.

Evidence: focused delegation regressions, encrypted-store restart integration,
and authenticated renderer acceptance through actual visual controls and typed
screenless choices. Four new regression cases reproduced the review defects before
fixes. Mobile/desktop, forced colors, enlarged text, reload/mode persistence and
busy controls are exercised. No model text confers authority.

Remaining gates: production principal/transport deployment, secured broker
isolation, real provider and accessible device challenges, real speech/assistive
technology and participant qualification, full external-effect reconciliation,
ledger rollback resistance and replacement-device key custody. No real data,
financial effects, runtime deployment or public publication is authorized here.
