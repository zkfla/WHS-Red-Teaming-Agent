"""Planners: choose one step from the frontier.

``PlaybookPlanner`` is deterministic; ``LLMPlanner`` asks the LLM gateway
(``core.ports.llm.LLMClient``) and refuses any step it did not offer.
"""

from __future__ import annotations

import logging
from typing import Any

from redteam.core.contracts.target import target_display_name
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

#: The hybrid planner asks for a unique option id rather than a step_id: one
#: playbook step bound to several targets keeps a single PlanStep.step_id, so
#: an answer naming a step_id could not be mapped back to the candidate the
#: choice came from. The PlanStep itself is unchanged; this is only how the
#: ambiguity prompt addresses its options.
HYBRID_CHOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "option_id": {
            "type": "string",
            "description": "option_id of the chosen option, copied exactly from the list",
        },
        "rationale": {
            "type": "string",
            "description": "one sentence on why this option is the best next move",
        },
    },
    "required": ["option_id"],
}


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
        self.last_rationale: str | None = None

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
            return await self._fall_back("llm returned a step_id which was not offered", context)

        self.last_fallback_reason = None
        self.last_rationale = (
            str(choice.get("rationale", "")).strip()[:500]
            if isinstance(choice, dict) and choice.get("rationale")
            else None
        )
        return by_id[step_id]

    async def _fall_back(self, reason: str, context: PlanningContext) -> PlanStep | None:
        self.last_fallback_reason = reason
        self.last_rationale = None
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
                outcome = ""
                if item.get("status_code") is not None:
                    outcome += f" status={item['status_code']}"
                if item.get("error_code"):
                    outcome += f" error={item['error_code']}"
                lines.append(
                    "  - evidence_id={evidence_id} target={target_node_id} "
                    "source={source_id}{outcome} summary={summary}".format(outcome=outcome, **item)
                )
        _append_runtime_context(lines, context)

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
        #: The unique candidate identity that was chosen, when one was.
        self.last_option_id: str | None = None

    async def plan(self, context: PlanningContext) -> PlanStep | None:
        if not context.candidates:
            self.last_decision = None
            self.last_option_id = None
            return None
        ordered = self._ordered(context)
        if len(ordered) == 1:
            self.last_decision = "single"
            self.last_option_id = ordered[0][0]
            return ordered[0][1]
        if self._has_a_clear_winner(context, ordered):
            self.last_decision = "clear_margin"
            self.last_option_id = ordered[0][0]
            return ordered[0][1]
        return await self._rerank(context, ordered)

    def _option_ids(self, context: PlanningContext) -> list[str]:
        """The unique identity of each candidate, aligned with the candidates.

        One playbook step bound to several targets keeps a single
        PlanStep.step_id, so step_id cannot identify an option. The compiler's
        candidate_key can, and plan_node passes the aligned list. Older or
        direct callers that supply nothing fall back to step_id, which is
        unique for them.
        """

        keys = context.metadata.get("candidate_keys")
        if isinstance(keys, list) and len(keys) == len(context.candidates):
            return [str(item) for item in keys]
        return [step.step_id for step in context.candidates]

    def _ordered(self, context: PlanningContext) -> list[tuple[str, PlanStep]]:
        """(option_id, step) in score order, or the caller's own ranked order."""

        scores = context.metadata.get("candidate_scores") or {}
        paired = list(zip(self._option_ids(context), context.candidates, strict=False))
        if not isinstance(scores, dict) or not scores:
            return paired
        return sorted(
            paired,
            key=lambda item: (-float(scores.get(item[0], 0.0)), item[0]),
        )

    def _has_a_clear_winner(
        self, context: PlanningContext, ordered: list[tuple[str, PlanStep]]
    ) -> bool:
        scores = context.metadata.get("candidate_scores") or {}
        if not isinstance(scores, dict):
            return True
        top = scores.get(ordered[0][0])
        runner_up = scores.get(ordered[1][0])
        if top is None or runner_up is None:
            # No comparable numbers: the deterministic order stands rather
            # than being replaced by a guess about ambiguity.
            return True
        return float(top) - float(runner_up) >= self._score_margin

    async def _rerank(
        self, context: PlanningContext, ordered: list[tuple[str, PlanStep]]
    ) -> PlanStep | None:
        shortlist = ordered[: self._top_k]
        # Keyed by the unique option id, so two candidates that share a
        # step_id (same playbook entry, different targets) stay distinguishable
        # all the way through the model call.
        offered = {option_id: step for option_id, step in shortlist}
        llm = context.llm or self._llm
        if llm is None:
            return self._fall_back_to_top1("no LLM client available", ordered)
        try:
            choice = await llm.complete_structured(
                messages=[
                    {
                        "role": "user",
                        "content": self._render_options(context, shortlist),
                    }
                ],
                response_schema=HYBRID_CHOICE_SCHEMA,
                system=context.agent.system_prompt or DEFAULT_SYSTEM_PROMPT,
            )
        except Exception as exc:  # noqa: BLE001 - a planner must not abort the run
            # Provider messages may contain prompt fragments, target data or
            # credentials. Keep only the exception class.
            return self._fall_back_to_top1(f"llm call failed: {type(exc).__name__}", ordered)

        option_id = str(choice.get("option_id", "")) if isinstance(choice, dict) else ""
        if option_id not in offered:
            return self._fall_back_to_top1(
                "llm returned an option_id which was not offered", ordered
            )
        self.last_fallback_reason = None
        self.last_decision = "reranked"
        self.last_option_id = option_id
        self.last_rationale = (
            str(choice.get("rationale", "")).strip()[:500]
            if isinstance(choice, dict) and choice.get("rationale")
            else None
        )
        return offered[option_id]

    def _fall_back_to_top1(
        self, reason: str, ordered: list[tuple[str, PlanStep]]
    ) -> PlanStep | None:
        self.last_fallback_reason = reason
        self.last_rationale = None
        self.last_decision = "fallback"
        if ordered:
            self.last_option_id = ordered[0][0]
        logger.info("HybridPlanner keeping the deterministic top-1: %s", reason)
        return ordered[0][1] if ordered else None

    @staticmethod
    def _render_options(context: PlanningContext, shortlist: list[tuple[str, PlanStep]]) -> str:
        """The ambiguity prompt: unique option ids, nothing else to pick from.

        Rendered separately from the parent's prompt because the option id is
        what must come back -- asking for a step_id would be ambiguous
        whenever one playbook step is bound to several targets.

        It carries the same bounded context the parent prompt does (open
        hypotheses, findings, attempts, target-linked evidence excerpts) plus
        where each option would actually run, because choosing between two
        bindings of one step is impossible without seeing their targets.
        """

        observation = context.observation
        lines = [
            f"Goal: {context.goal or 'establish what is reachable and where it is weak'}",
            f"Agent: {context.agent.name} -- {context.agent.description}",
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
                outcome = ""
                if item.get("status_code") is not None:
                    outcome += f" status={item['status_code']}"
                if item.get("error_code"):
                    outcome += f" error={item['error_code']}"
                lines.append(
                    "  - evidence_id={evidence_id} target={target_node_id} "
                    "source={source_id}{outcome} summary={summary}".format(outcome=outcome, **item)
                )
        _append_runtime_context(lines, context)

        lines.append("")
        lines.append("These options are the only ones available; each option_id is unique:")
        for option_id, step in shortlist:
            lines.append(
                f"  - option_id={option_id} step_id={step.step_id} "
                f"capability={step.capability} operation={step.operation} "
                f"target={_target_label(step)}"
            )
            lines.append(f"      why: {step.reason}")
            if step.hypothesis is not None:
                lines.append(f"      tests: {step.hypothesis.description}")
        lines.append("")
        lines.append("Answer with the option_id of the single option to take next.")
        return "\n".join(lines)


def _append_runtime_context(lines: list[str], context: PlanningContext) -> None:
    """Append bounded non-graph feedback shared by both planner prompts."""

    observations = context.metadata.get("runtime_observations", [])
    if isinstance(observations, list) and observations:
        lines.append("Recent runtime observations (adapt to these outcomes):")
        for item in observations[-8:]:
            if isinstance(item, dict):
                lines.append(f"  - [{item.get('kind', 'runtime')}] {item.get('summary', '')}")
    guidance = context.metadata.get("operator_guidance", [])
    if isinstance(guidance, list) and guidance:
        lines.append("Latest operator guidance:")
        lines.extend(f"  - {str(item)[:500]}" for item in guidance[-3:])


def _target_label(step: PlanStep) -> str:
    """Where this option would actually run, from the target only.

    Deliberately built from the resolved target rather than from
    PlanStep.arguments: arguments can carry bodies or references that must
    never reach a prompt, while the target's own display name and path are
    exactly what distinguishes two bindings of the same playbook step.
    """

    if step.target is None:
        return step.target_bind or "unresolved"
    target = step.resolved_target()
    label = target_display_name(target)
    path = getattr(target, "path", None)
    if isinstance(path, str) and path:
        return f"{label}{path}"
    return label


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
