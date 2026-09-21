"""Planners: choose one step from the frontier.

``PlaybookPlanner`` is deterministic; ``LLMPlanner`` asks the LLM gateway
(``core.ports.llm.LLMClient``) and refuses any step it did not offer.
"""

from __future__ import annotations

import logging
from dataclasses import replace
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
            # Provider messages may contain prompt fragments, target data or
            # credentials. Keep only the exception class in logs/runtime.
            error_type = type(exc).__name__
            logger.warning("LLM planning call failed (%s)", error_type)
            return await self._fall_back(f"llm call failed: {error_type}", context)

        step_id = str(choice.get("step_id", "")) if isinstance(choice, dict) else ""
        if step_id not in by_id:
            return await self._fall_back(
                "llm returned a step_id which was not offered", context
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
        evidence_excerpts = context.metadata.get("evidence_excerpts", [])
        if evidence_excerpts:
            lines.append("Target-linked evidence excerpts:")
            for item in evidence_excerpts:
                lines.append(
                    "  - evidence_id={evidence_id} target={target_node_id} "
                    "source={source_id} summary={summary}".format(**item)
                )

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


class HybridPlanner(LLMPlanner):
    """Deterministic unless the ranking is genuinely ambiguous.

    Scoring candidates already produces a total order, so most turns do not
    need a model call at all. The LLM is only asked when the top two scores
    are closer than the configured margin, and it may only choose among a
    bounded top-K slice. It never sees a tool, a target or a step that the
    CandidateCompiler did not already produce and validate.

    Every failure path -- no client, transport error, a step_id outside the
    offered slice -- resolves to the deterministic top-1, so planning can
    only get less clever, never less safe.
    """

    def __init__(
        self,
        llm: Any,
        *,
        fallback: Any | None = None,
        score_margin: float = 0.15,
        top_k: int = 3,
    ) -> None:
        super().__init__(llm, fallback=fallback)
        self._score_margin = score_margin
        self._top_k = max(2, top_k)
        #: Why the last decision was made: "single", "clear_margin",
        #: "reranked" or "fallback". Observable for tests and diagnostics.
        self.last_decision: str | None = None

    async def plan(self, context: PlanningContext) -> PlanStep | None:
        if not context.candidates:
            self.last_decision = None
            return None
        ordered = self._ordered(context)
        if len(ordered) == 1:
            self.last_decision = "single"
            return ordered[0]
        if self._has_a_clear_winner(context, ordered):
            self.last_decision = "clear_margin"
            return ordered[0]
        return await self._rerank(context, ordered)

    def _ordered(self, context: PlanningContext) -> list[PlanStep]:
        """Score order, falling back to the caller's own (already ranked) order."""

        scores = context.metadata.get("candidate_scores") or {}
        if not isinstance(scores, dict) or not scores:
            return list(context.candidates)
        return sorted(
            context.candidates,
            key=lambda step: (-float(scores.get(step.step_id, 0.0)), step.step_id),
        )

    def _has_a_clear_winner(
        self, context: PlanningContext, ordered: list[PlanStep]
    ) -> bool:
        scores = context.metadata.get("candidate_scores") or {}
        if not isinstance(scores, dict):
            return True
        top = scores.get(ordered[0].step_id)
        runner_up = scores.get(ordered[1].step_id)
        if top is None or runner_up is None:
            # No comparable numbers: the deterministic order stands rather
            # than being replaced by a guess about ambiguity.
            return True
        return float(top) - float(runner_up) >= self._score_margin

    async def _rerank(
        self, context: PlanningContext, ordered: list[PlanStep]
    ) -> PlanStep | None:
        shortlist = ordered[: self._top_k]
        offered = {step.step_id: step for step in shortlist}
        llm = context.llm or self._llm
        if llm is None:
            return self._fall_back_to_top1("no LLM client available", ordered)
        try:
            choice = await llm.complete_structured(
                messages=[
                    {
                        "role": "user",
                        "content": self._render(replace(context, candidates=shortlist)),
                    }
                ],
                response_schema=CHOICE_SCHEMA,
                system=context.agent.system_prompt or DEFAULT_SYSTEM_PROMPT,
            )
        except Exception as exc:  # noqa: BLE001 - a planner must not abort the run
            # Provider messages may contain prompt fragments, target data or
            # credentials. Keep only the exception class.
            return self._fall_back_to_top1(
                f"llm call failed: {type(exc).__name__}", ordered
            )

        step_id = str(choice.get("step_id", "")) if isinstance(choice, dict) else ""
        if step_id not in offered:
            return self._fall_back_to_top1(
                "llm returned a step_id which was not offered", ordered
            )
        self.last_fallback_reason = None
        self.last_decision = "reranked"
        return offered[step_id]

    def _fall_back_to_top1(
        self, reason: str, ordered: list[PlanStep]
    ) -> PlanStep | None:
        self.last_fallback_reason = reason
        self.last_decision = "fallback"
        logger.info("HybridPlanner keeping the deterministic top-1: %s", reason)
        return ordered[0] if ordered else None


def build_planner(
    planner_kind: str,
    llm: Any | None,
    *,
    score_margin: float = 0.15,
    top_k: int = 3,
) -> Any:
    """Map AgentSpec.planner_kind to a planner instance.

    "llm" means "the LLM may assist", not "the LLM is always asked": the
    returned planner keeps the deterministic choice whenever scoring already
    decided the turn.
    """
    if planner_kind == "llm":
        return HybridPlanner(llm, score_margin=score_margin, top_k=top_k)
    return PlaybookPlanner()
