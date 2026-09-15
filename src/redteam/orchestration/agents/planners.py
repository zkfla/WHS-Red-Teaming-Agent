"""Planners: choose one step from the frontier.

``PlaybookPlanner`` is deterministic; ``LLMPlanner`` asks the LLM gateway
(``core.ports.llm.LLMClient``) and refuses any step it did not offer.
"""

from __future__ import annotations

import logging
from typing import Any

from redteam.orchestration.agents.base import PlanningContext, PlanStep

logger = logging.getLogger(__name__)

CHOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step_id": {
            "type": "string",
            "description": "step_id of the chosen option, copied exactly from the list",
        },
        "rationale": {
            "type": "string",
            "description": "one sentence on why this step is the best next move",
        },
    },
    "required": ["step_id"],
}

DEFAULT_SYSTEM_PROMPT = (
    "You plan the next step of an authorized security test. "
    "You are given the current state of the attack graph and a list of steps that are "
    "currently possible. Choose exactly one by copying its step_id. "
    "Do not invent steps, tools, or targets: anything not in the list is unavailable. "
    "Prefer a step that would resolve an open hypothesis or reveal new attack surface."
)


class PlaybookPlanner:
    """Takes the first available step. No LLM, fully deterministic."""

    async def plan(self, context: PlanningContext) -> PlanStep | None:
        return context.candidates[0] if context.candidates else None


class LLMPlanner:
    """Asks the LLM gateway to choose among the available steps."""

    def __init__(self, llm: Any, *, fallback: Any | None = None) -> None:
        self._llm = llm
        self._fallback = fallback or PlaybookPlanner()
        self.last_fallback_reason: str | None = None

    async def plan(self, context: PlanningContext) -> PlanStep | None:
        if not context.candidates:
            return None

        llm = context.llm or self._llm
        if llm is None:
            return await self._fall_back("no LLM client available", context)

        by_id = {step.step_id: step for step in context.candidates}
        try:
            choice = await llm.complete_structured(
                messages=[{"role": "user", "content": self._render(context)}],
                response_schema=CHOICE_SCHEMA,
                system=context.agent.system_prompt or DEFAULT_SYSTEM_PROMPT,
            )
        except Exception as exc:  # noqa: BLE001 - a planner must not abort the run
            logger.warning("LLM planning call failed: %s", exc)
            return await self._fall_back(f"llm call failed: {exc}", context)

        step_id = str(choice.get("step_id", "")) if isinstance(choice, dict) else ""
        if step_id not in by_id:
            return await self._fall_back(
                f"llm returned step_id {step_id!r}, which was not offered", context
            )

        self.last_fallback_reason = None
        return by_id[step_id]

    async def _fall_back(self, reason: str, context: PlanningContext) -> PlanStep | None:
        self.last_fallback_reason = reason
        logger.info("LLMPlanner falling back to %s: %s", type(self._fallback).__name__, reason)
        return await self._fallback.plan(context)

    @staticmethod
    def _render(context: PlanningContext) -> str:
        observation = context.observation
        lines = [
            f"Goal: {context.goal or 'establish what is reachable and where it is weak'}",
            f"Agent: {context.agent.name} — {context.agent.description}",
            f"Turn: {context.step}",
            "",
            "Attack graph right now:",
            f"  {observation.summary_line()}",
        ]
        for type_name, labels in sorted(observation.nodes_by_type.items()):
            lines.append(f"  {type_name}: {', '.join(labels)}")
        if observation.open_hypotheses:
            lines.append("Open hypotheses:")
            lines.extend(f"  - {item}" for item in observation.open_hypotheses)
        if observation.findings:
            lines.append("Findings so far:")
            lines.extend(f"  - {item}" for item in observation.findings)
        if observation.attempted:
            lines.append("Already attempted (do not repeat):")
            lines.extend(f"  - {item}" for item in observation.attempted)

        lines.append("")
        lines.append("Available steps:")
        for step in context.candidates:
            target = step.target.kind if step.target else step.target_bind
            detail = (
                f"  - step_id={step.step_id} capability={step.capability} "
                f"operation={step.operation} target={target}"
            )
            lines.append(detail)
            lines.append(f"      why: {step.reason}")
            if step.hypothesis is not None:
                lines.append(f"      tests: {step.hypothesis.description}")
        lines.append("")
        lines.append("Answer with the step_id of the single step to take next.")
        return "\n".join(lines)


def build_planner(planner_kind: str, llm: Any | None) -> Any:
    """Map ``AgentSpec.planner_kind`` to a planner instance."""
    if planner_kind == "llm":
        return LLMPlanner(llm)
    return PlaybookPlanner()
