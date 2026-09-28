"""RC#1 prototype: LLM-driven candidate *generation*.

The existing :class:`PlaybookCandidateProvider` derives the frontier from
static playbooks and the :class:`LLMPlanner` then only *selects* one of
those pre-built steps -- it refuses anything the playbook did not already
enumerate. That caps the model at "pick from a menu": when a run leaves
the paths a playbook anticipated, there is no seam for the model to
propose a new investigation action.

``LLMCandidateProvider`` adds the missing generation seam without
loosening any of the execution guarantees. The model proposes drafts --
``{capability, operation, target_node_id, arguments, rationale}`` -- and
every draft is handed to the *same* ``CandidateCompiler`` ->
``ControlPlane`` -> Evidence pipeline as a playbook draft. A malformed,
unresolvable or out-of-scope proposal is dropped with a reason (surfaced
through the existing ``CandidateRejected``/``DraftDrop`` path), never
executed. The model gains "what to try next"; the code keeps "what may
actually run".

This module is deliberately self-contained (its own ``demo()`` self-check
runs under ``python -m``) and is not yet wired into the default planning
loop -- that wiring is the reviewed integration step.
"""

from __future__ import annotations

import logging
from typing import Any

from redteam.attack_graph.identity import split_node_id, target_for_node
from redteam.core.ids import AgentId, GraphNodeId, ToolId
from redteam.orchestration.agents.base import AgentSpec, PlanStep
from redteam.orchestration.agents.providers import (
    CandidateBatch,
    DraftDrop,
    GraphCandidateContext,
)
from redteam.orchestration.candidates.models import CandidateDraft

logger = logging.getLogger(__name__)

#: Max drafts accepted from one model turn -- a runaway model cannot flood
#: the compiler/queue. Extra drafts are ignored, not an error.
MAX_DRAFTS_PER_TURN = 8

ACTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "capability": {"type": "string"},
                    "operation": {"type": "string"},
                    "target_node_id": {
                        "type": "string",
                        "description": "exact node id from 'Known targets', copied verbatim",
                    },
                    "tool_id": {"type": "string"},
                    "arguments": {"type": "object"},
                    "rationale": {"type": "string"},
                },
                "required": ["capability", "operation", "target_node_id"],
            },
        }
    },
    "required": ["actions"],
}

SYSTEM_PROMPT = (
    "You plan the next investigation actions of an authorized security test. "
    "Propose concrete actions -- capability, operation, one target (copied "
    "from the Known targets list), and any arguments -- that would resolve an "
    "open question or reveal new attack surface. You are NOT limited to a "
    "fixed menu: propose the action you think is right. Every proposal is "
    "independently validated against scope, the tool catalog and policy before "
    "it can run, so an out-of-scope or unsupported proposal is simply dropped "
    "-- propose freely, but only against the targets listed."
)


class LLMCandidateProvider:
    """Generates candidate drafts from the model; the compiler still gates them."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    async def propose_graph(
        self, spec: AgentSpec, context: GraphCandidateContext
    ) -> CandidateBatch:
        if self._llm is None:
            return CandidateBatch(candidates=[])
        prompt = self._render(spec, context)
        try:
            reply = await self._llm.complete_structured(
                messages=[{"role": "user", "content": prompt}],
                response_schema=ACTIONS_SCHEMA,
                system=spec_system_prompt(spec),
            )
        except Exception as exc:  # noqa: BLE001 - generation must not abort a run
            # Provider messages may carry prompt fragments or target data; keep
            # only the exception class, mirroring LLMPlanner's failure handling.
            logger.warning("LLM candidate generation failed (%s)", type(exc).__name__)
            return CandidateBatch(candidates=[])

        actions = reply.get("actions", []) if isinstance(reply, dict) else []
        # The model may only act on targets that already exist in its
        # observation -- the generation analog of LLMPlanner refusing a
        # step_id it was not offered. A node id it invents is dropped here,
        # before any tool binding or scope check.
        known_targets = {
            node_id
            for node_ids in context.observation.node_ids_by_type.values()
            for node_id in node_ids
        }
        drafts: list[CandidateDraft] = []
        drops: list[DraftDrop] = []
        for index, action in enumerate(actions[:MAX_DRAFTS_PER_TURN]):
            draft, drop = self._to_draft(spec.agent_id, action, index, known_targets)
            if draft is not None:
                drafts.append(draft)
            elif drop is not None:
                drops.append(drop)
        return CandidateBatch(candidates=drafts, drops=drops)

    def _to_draft(
        self, agent_id: AgentId, action: Any, index: int, known_targets: set[str]
    ) -> tuple[CandidateDraft | None, DraftDrop | None]:
        step_id = f"llm-{index}"
        if not isinstance(action, dict):
            return None, DraftDrop(step_id=step_id, target_node_id="", reason="malformed_action")
        node_id_str = str(action.get("target_node_id", ""))
        # 1) The model must name a target that already exists as a graph node.
        #    A hallucinated target is dropped here, before any tool binding.
        if node_id_str not in known_targets:
            return None, DraftDrop(
                step_id=step_id, target_node_id=node_id_str, reason="unknown_target_node"
            )
        try:
            node_type, _ = split_node_id(GraphNodeId(node_id_str))
        except Exception:
            return None, DraftDrop(
                step_id=step_id, target_node_id=node_id_str, reason="unaddressable_target_node"
            )
        try:
            target = target_for_node(
                GraphNodeId(node_id_str), node_type, observed=action.get("observed")
            )
        except Exception:
            return None, DraftDrop(
                step_id=step_id, target_node_id=node_id_str, reason="target_not_addressable"
            )
        capability = str(action.get("capability", ""))
        operation = str(action.get("operation", ""))
        tool_id = str(action.get("tool_id") or f"{capability}.{operation}")
        arguments = action.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        rationale = str(action.get("rationale") or "model-proposed action")
        # 2) PlanStep's own validators (canonical operation, tool namespace,
        #    exactly-one-target) reject a structurally invalid proposal.
        try:
            plan_step = PlanStep(
                step_id=step_id,
                capability=capability,
                operation=operation,
                tool_id=ToolId(tool_id),
                target=target,
                arguments=arguments,
                reason=rationale,
            )
        except Exception as exc:  # noqa: BLE001 - invalid proposal, not a crash
            return None, DraftDrop(
                step_id=step_id,
                target_node_id=node_id_str,
                reason=f"invalid_step:{type(exc).__name__}",
            )
        draft = CandidateDraft(
            agent_id=agent_id,
            plan_step=plan_step,
            target_node_ids=[GraphNodeId(node_id_str)],
        )
        return draft, None

    @staticmethod
    def _render(spec: AgentSpec, context: GraphCandidateContext) -> str:
        observation = context.observation
        lines = [
            f"Agent: {spec.name} — {spec.description}",
            f"Turn: {context.step}",
            "",
            "Attack graph right now:",
            f"  {observation.summary_line()}",
        ]
        for type_name, labels in sorted(observation.nodes_by_type.items()):
            lines.append(f"  {type_name}: {', '.join(labels)}")
        if observation.open_hypotheses:
            lines.append("Open questions:")
            lines.extend(f"  - {item}" for item in observation.open_hypotheses)
        if context.available_operations:
            lines.append("Executable (capability, operation) pairs:")
            for capability, operation in sorted(context.available_operations):
                lines.append(f"  - {capability} / {operation}")
        if context.evidence_excerpts:
            # RC#2: the raw outcome per target (status/error), not just the
            # graph summary -- the material the model needs to adapt.
            lines.append("Recent results per target:")
            for item in context.evidence_excerpts:
                outcome = ""
                if item.get("status_code") is not None:
                    outcome += f" status={item['status_code']}"
                if item.get("error_code"):
                    outcome += f" error={item['error_code']}"
                lines.append(
                    f"  - {item.get('target_node_id', '')}{outcome} "
                    f"summary={item.get('summary')}"
                )
        lines.append("")
        lines.append("Known targets (use a target_node_id exactly as written):")
        for _type_name, node_ids in sorted(observation.node_ids_by_type.items()):
            for node_id in node_ids:
                lines.append(f"  - {node_id}")
        lines.append("")
        lines.append(
            "Propose the next investigation actions as JSON. Each action names a "
            "capability, an operation, one target_node_id from the list, and any "
            "arguments the operation needs."
        )
        return "\n".join(lines)


def spec_system_prompt(spec: AgentSpec) -> str:
    return getattr(spec, "system_prompt", "") or SYSTEM_PROMPT
