"""One observable LLM turn that may propose the next runtime action.

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
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from redteam.attack_graph.identity import split_node_id, target_for_node
from redteam.attack_graph.predicates import attrs_of, facts_of
from redteam.core.ids import AgentId, GraphNodeId, ToolId
from redteam.core.redaction import redact_sensitive_text
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

class AgentTurnAction(BaseModel):
    capability: str
    operation: str
    target_node_id: str
    tool_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = "model-proposed action"


class AgentTurnDecision(BaseModel):
    """Safe public trace plus machine-readable actions from one model call.

    ``response`` and ``decision_summary`` are not chain-of-thought. They are
    short conclusions intended for the operator and audit event stream.
    Defaults keep older action-only structured responses compatible.
    """

    intent: Literal["act", "finish"] = "act"
    response: str = Field(default="다음 실행 가능한 행동을 선택했습니다.", max_length=2000)
    decision_summary: str = Field(
        default="현재 관찰과 실행 가능한 도구를 기준으로 다음 행동을 선택했습니다.",
        max_length=1000,
    )
    assumptions: list[str] = Field(default_factory=list, max_length=5)
    actions: list[AgentTurnAction] = Field(default_factory=list, max_length=MAX_DRAFTS_PER_TURN)

    @field_validator("response", "decision_summary")
    @classmethod
    def _safe_text(cls, value: str) -> str:
        return redact_sensitive_text(value).strip()

    @field_validator("assumptions")
    @classmethod
    def _safe_assumptions(cls, value: list[str]) -> list[str]:
        return [
            redact_sensitive_text(str(item)).strip()[:500]
            for item in value
            if str(item).strip()
        ]


ACTIONS_SCHEMA: dict[str, Any] = AgentTurnDecision.model_json_schema()

SYSTEM_PROMPT = """You are the decision loop of an authorized security-testing agent.
Return one JSON AgentTurnDecision. Give a concise operator-facing response, a short decision
summary, explicit assumptions, and zero or more concrete actions. Never reveal private
chain-of-thought and never claim an action already ran. Choose actions from the live MCP action
space when possible: capability, operation, one exact known target_node_id, optional tool_id,
arguments, and a brief rationale. The runtime independently validates every proposal against the
tool catalog, engagement scope, approval rules and Control Plane. Control/tool outcomes from prior
turns are observations: adapt to their exact reason instead of treating every block as a generic
tool failure. Use intent=finish only when the assigned task is actually complete.

Language contract: write response, decision_summary, assumptions, and every action rationale in
the same language as the Operator Mission or latest operator guidance. If the operator writes in
Korean, these public explanation fields MUST be natural Korean. Keep protocol identifiers such as
capability, operation, tool_id, target_node_id, policy names, and error codes unchanged."""


class LLMCandidateProvider:
    """Generates candidate drafts from the model; the compiler still gates them."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm
        self.last_decision: AgentTurnDecision | None = None
        self.last_error: str | None = None

    async def propose_graph(
        self, spec: AgentSpec, context: GraphCandidateContext
    ) -> CandidateBatch:
        if self._llm is None:
            self.last_error = "llm_unavailable"
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
            self.last_error = type(exc).__name__
            return CandidateBatch(candidates=[])

        try:
            decision = AgentTurnDecision.model_validate(reply)
        except Exception as exc:  # noqa: BLE001 - malformed turn safely falls back
            logger.warning("LLM candidate response invalid (%s)", type(exc).__name__)
            self.last_error = type(exc).__name__
            return CandidateBatch(candidates=[])
        self.last_decision = decision
        self.last_error = None
        actions = [item.model_dump(mode="python") for item in decision.actions]
        # The model may only act on targets that already exist in its
        # observation -- the generation analog of LLMPlanner refusing a
        # step_id it was not offered. A node id it invents is dropped here,
        # before any tool binding or scope check.
        observed_targets = {
            node_id
            for node_ids in context.observation.node_ids_by_type.values()
            for node_id in node_ids
        }
        known_targets = (
            observed_targets & set(context.task_target_node_ids)
            if context.task_target_node_ids
            else observed_targets
        )
        drafts: list[CandidateDraft] = []
        drops: list[DraftDrop] = []
        for index, action in enumerate(actions[:MAX_DRAFTS_PER_TURN]):
            draft, drop = await self._to_draft(
                spec.agent_id, action, index, known_targets, context
            )
            if draft is not None:
                drafts.append(draft)
            elif drop is not None:
                drops.append(drop)
        return CandidateBatch(candidates=drafts, drops=drops)

    async def _to_draft(
        self,
        agent_id: AgentId,
        action: Any,
        index: int,
        known_targets: set[str],
        context: GraphCandidateContext,
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
        explicit_tool_id = action.get("tool_id")
        tool_id = str(explicit_tool_id or f"{capability}.{operation}")
        arguments = action.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        arguments = dict(arguments)
        # Model-supplied identity values cannot point the tool somewhere other
        # than the validated graph target. Prefer a graph-observed MCP alias
        # (target_ref) where one exists; otherwise use the typed target. This
        # normalization is intentionally confined to LLM drafts so established
        # graph providers can retain their already-resolved aliases.
        node = None
        if context.view is not None:
            try:
                node = await context.view.get_node(GraphNodeId(node_id_str))
            except (AttributeError, TypeError):
                node = None
        graph_properties = node.properties if node is not None else {}
        graph_facts = facts_of(graph_properties)
        graph_attrs = attrs_of(graph_properties)
        target_ref = graph_facts.get("target_ref") or graph_attrs.get("target_ref")
        if target_ref is None:
            target_ref = getattr(target, "host", None) or getattr(target, "domain", None)
            target_ref = target_ref or getattr(target, "url", None)
        selected_tool = next(
            (
                item
                for item in context.available_tools
                if str(item.get("tool_id")) == str(explicit_tool_id or "")
            ),
            None,
        )
        required = set(selected_tool.get("required_arguments", ())) if selected_tool else set()
        if target_ref is not None and ("target_ref" in arguments or "target_ref" in required):
            arguments["target_ref"] = target_ref
        typed_identity = {
            "host": getattr(target, "host", None) or getattr(target, "domain", None),
            "url": getattr(target, "url", None),
            "port": getattr(target, "port", None),
        }
        for name, value in typed_identity.items():
            if value is not None and (name in arguments or name in required):
                arguments[name] = value
        rationale = str(action.get("rationale") or "model-proposed action")
        # 2) PlanStep's own validators (canonical operation, tool namespace,
        #    exactly-one-target) reject a structurally invalid proposal.
        try:
            plan_step = PlanStep(
                step_id=step_id,
                capability=capability,
                operation=operation,
                tool_id=ToolId(tool_id),
                compatible_tool_ids=[ToolId(tool_id)] if explicit_tool_id else [],
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
        if context.mission_objective:
            lines.insert(2, f"Operator Mission: {context.mission_objective}")
        if context.task_objective:
            lines.insert(2, f"Assigned Task: {context.task_objective}")
        for type_name, labels in sorted(observation.nodes_by_type.items()):
            lines.append(f"  {type_name}: {', '.join(labels)}")
        if observation.open_hypotheses:
            lines.append("Open questions:")
            lines.extend(f"  - {item}" for item in observation.open_hypotheses)
        if context.available_operations:
            lines.append("Executable (capability, operation) pairs:")
            for capability, operation in sorted(context.available_operations):
                lines.append(f"  - {capability} / {operation}")
        if context.available_tools:
            lines.append("Live MCP action space:")
            for item in context.available_tools:
                lines.append(
                    "  - "
                    f"tool_id={item.get('tool_id')} server={item.get('server_id')} "
                    f"capability={item.get('capability')} "
                    f"operations={item.get('operations')} "
                    f"targets={item.get('target_types')} "
                    f"required_arguments={item.get('required_arguments')} "
                    f"risk={item.get('risk_level')} health={item.get('health_status')}"
                )
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
                    f"  - {item.get('target_node_id', '')}{outcome} summary={item.get('summary')}"
                )
        if context.runtime_observations:
            lines.append("Recent runtime observations (adapt to these outcomes):")
            for item in context.runtime_observations[-8:]:
                lines.append(f"  - [{item.get('kind', 'runtime')}] {item.get('summary', '')}")
        if context.operator_guidance:
            lines.append("Latest operator guidance:")
            lines.extend(f"  - {item}" for item in context.operator_guidance[-3:])
        lines.append("")
        lines.append("Known targets (use a target_node_id exactly as written):")
        scoped_targets = set(context.task_target_node_ids)
        for _type_name, node_ids in sorted(observation.node_ids_by_type.items()):
            for node_id in node_ids:
                if not scoped_targets or node_id in scoped_targets:
                    lines.append(f"  - {node_id}")
        lines.append("")
        lines.append(
            "Return one AgentTurnDecision JSON object. Explain the intended next step "
            "briefly for the operator, then name concrete actions using exact live "
            "capability/operation/target values and required arguments."
        )
        return "\n".join(lines)


def spec_system_prompt(spec: AgentSpec) -> str:
    role_prompt = getattr(spec, "system_prompt", "")
    if not role_prompt:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\nRole-specific instructions:\n{role_prompt}"
