from __future__ import annotations

import os

from dataclasses import dataclass
from typing import Any, Dict, Optional

from orchestrator.clients import ServiceClients


@dataclass(frozen=True)
class InteractionModelResult:
    ok: bool
    text: str
    provider: Optional[str] = None
    model: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class Phase1InteractionModel:
    """
    Phase 1 interaction model wrapper.

    Hard boundary: never provides tool schemas to the model, and ignores any tool calls returned.
    """

    def generate(
        self,
        *,
        clients: ServiceClients | None,
        event_id: str,
        trace_id: str,
        person_id: Optional[str],
        session_id: str,
        system_prompt: str,
        user_text: str,
        plan: Dict[str, Any],
        tool_results: list[Dict[str, Any]],
        memory_results: list[Dict[str, Any]],
    ) -> InteractionModelResult:
        if os.getenv("UNISON_PHASE1_DISABLE_INFERENCE", "false").lower() in {"1", "true", "yes", "on"}:
            directives = plan.get("renderer_directives") if isinstance(plan.get("renderer_directives"), dict) else {}
            verbosity = directives.get("verbosity") if isinstance(directives.get("verbosity"), str) else "normal"
            if plan.get("requires_confirmation") and isinstance(plan.get("confirmation_prompt"), str):
                return InteractionModelResult(ok=True, text=plan["confirmation_prompt"])
            if verbosity == "minimal":
                return InteractionModelResult(ok=True, text="Done.")
            if verbosity == "detailed":
                return InteractionModelResult(ok=True, text="Done. I can also adjust your preferences or proceed with a tool action if you ask.")
            return InteractionModelResult(ok=True, text="Done.")

        # Deterministic fallback for onboarding questions (keeps onboarding reliable even if inference is down).
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        if any(isinstance(s, dict) and s.get("type") == "clarify" for s in steps):
            summaries = " ".join([str(s.get("summary") or "") for s in steps if isinstance(s, dict)])
            if "onboarding.name" in summaries:
                return InteractionModelResult(ok=True, text="What name should I use to address you?")
            if "onboarding.verbosity" in summaries:
                return InteractionModelResult(ok=True, text="How verbose should I be: minimal, normal, or detailed?")
            if "onboarding.visual_density" in summaries:
                return InteractionModelResult(ok=True, text="For the fullscreen experience, do you prefer sparse, balanced, or dense visuals?")
            if "onboarding.goals" in summaries:
                return InteractionModelResult(ok=True, text="Any goals you want me to keep in mind? You can say “skip”.")
            return InteractionModelResult(ok=True, text="I have a quick question before we continue.")

        if clients is None:
            return InteractionModelResult(ok=False, text="", error="clients_not_configured")

        context_lines: list[str] = []
        directives = plan.get("renderer_directives") if isinstance(plan.get("renderer_directives"), dict) else {}
        verbosity = directives.get("verbosity") if isinstance(directives.get("verbosity"), str) else "normal"
        max_tokens = 500 if verbosity == "normal" else 180 if verbosity == "minimal" else 900

        if tool_results:
            context_lines.append("Tool results:")
            for tr in tool_results[:5]:
                context_lines.append(f"- {tr.get('tool_name')}: {tr.get('status')}")
        if memory_results:
            context_lines.append("Memory results:")
            for mr in memory_results[:5]:
                context_lines.append(f"- {mr.get('target')}: {mr.get('status')}")
        if plan.get("requires_confirmation"):
            context_lines.append("The plan requires confirmation before executing tools.")

        user_payload = user_text
        if context_lines:
            user_payload = f"{user_text}\n\n" + "\n".join(context_lines)

        ok, status, body = clients.inference.post(
            "/inference/request",
            {
                "intent": "phase1.interaction.respond",
                "person_id": person_id or "anonymous",
                "session_id": session_id,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
                # Enforce: no tools provided.
                "tools": [],
                "tool_choice": "none",
                "response_format": "text",
                "max_tokens": max_tokens,
                "temperature": 0.4,
            },
            headers={"X-Event-ID": event_id, "X-Trace-ID": trace_id},
        )
        if not ok or not isinstance(body, dict) or status >= 400:
            return InteractionModelResult(ok=False, text="", error=f"inference_failed_status_{status}")

        # Enforce boundary: ignore tool calls even if provider returns them.
        tool_calls = body.get("tool_calls") if isinstance(body, dict) else None
        if tool_calls:
            # Treat this as a protocol violation; still return text.
            pass

        text = body.get("result") if isinstance(body.get("result"), str) else ""
        return InteractionModelResult(
            ok=True,
            text=text,
            provider=body.get("provider") if isinstance(body.get("provider"), str) else None,
            model=body.get("model") if isinstance(body.get("model"), str) else None,
        )


__all__ = ["Phase1InteractionModel", "InteractionModelResult"]
