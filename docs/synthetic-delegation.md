# Synthetic delegation and multimodal intent reference

Status: bounded mock library, not connected to production ingress or a credential vault.

`orchestrator.synthetic_delegation` keeps four decisions separate: verified
Unison identity/unlock, source authentication, an account/purpose/action/recipient
and terms-bound delegation, and confirmation of a consequential mock action.
The host must supply a verified `PrincipalContext` and a transactional encrypted
state store. No HTTP route constructs authority from model text. The constructor
is disabled by default and accepts only explicit mock provider instances.

The replaceable `CredentialUseAdapter` protocol refreshes or brokers credential
use through opaque handles and verifies action outcomes. The mock models OAuth
refresh, session reuse and a password-manager broker without network, secrets,
real MFA, vault reading or live provider effects. It is not a 1Password integration
and grants no rights to a provider vault. Production adapters remain gated.

Five dependent tasks share one connection repair. Paused, revoked or expired
work does not resume through repair. Delegation confirmations and task responses
are bound to exact revisions and terms; consequential confirmations expire.
Pending attempts are persisted before provider execution. Existing attempts are
verified before any continuation and are never automatically re-executed merely
because verification returns absent/unknown. Such ambiguity stays pending for
future qualified reconciliation. Pause is not revocation; deletion does not undo
external effects. Terminal tombstones retain minimal authority state.

`intent-experience.v1` derives a semantic visual control tree and a screenless
spoken-text representation from the same trusted state. The static semantic HTML
helper is a fallback representation, not an installed interactive UI. Choices
are returned to the engine as explicit structured responses with revisions.
No browser destination is presented as the person's goal. Unsupported nonvisual
provider verification explains the limit and permits pausing; it invents no
alternative and does not bypass human presence. Mode loss does not grant authority.

Evidence: `pytest tests/test_synthetic_delegation.py` (unit/simulation), plus the
workspace encrypted-store restart acceptance test. No actual speech recognition,
TTS, assistive-device verification, biometrics, participant usability, live OAuth
or browser session was exercised. The three mock mechanism labels exercise the
same protocol; they are not three qualified real integrations.

All content is synthetic, including a fixed account/purpose/recipient vocabulary.
Provider handles are omitted from generated visual/spoken representations and
no credentials are accepted or logged. Store/key custody, rollback-resistant
revocation of this delegation ledger, complete external-effect reconciliation,
production authenticated ingress, cancellation of real remote effects, a secured
broker process and real accessible challenge support remain release gates.
