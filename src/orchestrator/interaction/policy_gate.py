from __future__ import annotations

from dataclasses import dataclass

from unison_common import ActionEnvelope, PolicyDecision


@dataclass(frozen=True)
class PolicyGate:
    """
    Policy gate for proposed actions.

    Phase 1: stub allow/deny based on input text.
    """

    def check(self, action: ActionEnvelope) -> PolicyDecision:
        text = str((action.args or {}).get("text", ""))
        if "deny:" in text.lower():
            return PolicyDecision(allowed=False, reason="stub deny rule matched", require_confirmation=False)
        return PolicyDecision(allowed=True)

