from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict

from unison_common import ActionEnvelope, ActionResult

ToolExecutor = Callable[[ActionEnvelope], ActionResult]


def _echo_tool(action: ActionEnvelope) -> ActionResult:
    return ActionResult(action_id=action.action_id, ok=True, result={"text": action.args.get("text", "")})


@dataclass(frozen=True)
class ToolRegistry:
    """
    Deterministic tool registry/executor.

    Phase 1: minimal built-ins only.
    """

    tools: Dict[str, ToolExecutor]

    @classmethod
    def default(cls) -> "ToolRegistry":
        return cls(tools={"tool.echo": _echo_tool})

    def execute(self, action: ActionEnvelope) -> ActionResult:
        executor = self.tools.get(action.name)
        if executor is None:
            return ActionResult(action_id=action.action_id, ok=False, error=f"unknown tool: {action.name}")
        return executor(action)

