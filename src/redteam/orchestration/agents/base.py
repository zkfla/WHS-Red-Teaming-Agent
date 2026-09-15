"""``AgentSpec``/``PlanStep``/``Planner`` and the frontier calculation.

An agent is a declared playbook plus a chooser: a planner picks from
``available_steps`` and cannot invent a capability, operation or tool id
that was never declared. Rationale: ``docs/agent/orchestration.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field, model_validator

from redteam.attack_graph.identity import node_id_for_target, target_for_node
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.patterns import NodePattern, resolve
from redteam.attack_graph.predicates import is_stale
from redteam.attack_graph.view import AttackGraphView
from redteam.core.contracts.target import AnyTarget, TargetRef
from redteam.core.ids import AgentId, EngagementId, RunId, ToolId
from redteam.core.ports.llm import LLMClient
from redteam.orchestration.expectations import Expectation

#: Cap on concrete steps produced from one playbook entry per turn, so a
#: graph with thousands of discovered nodes cannot blow up a prompt.
MAX_CANDIDATES_PER_STEP = 25


class HypothesisDraft(BaseModel):
    """The security question a step is asking, stated before it runs."""

    description: str
    confidence: float = Field(default=0.3, ge=0.0, le=1.0)
    #: Confirming this hypothesis means a security violation, not just a fact.
    violation_if_confirmed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlanStep(BaseModel):
    """One thing an agent may propose, with its preconditions and its test."""

    step_id: str
    capability: str
    operation: str
    tool_id: ToolId

    #: Either a fixed target...
    target: TargetRef | None = None
    #: ...or the name of a precondition bind to take the target from.
    target_bind: str = ""

    preconditions: list[NodePattern] = Field(default_factory=list)

    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str
    expected_observation: str | None = None

    hypothesis: HypothesisDraft | None = None
    expectation: Expectation | None = None

    #: Predicates to write onto the target node when the step succeeds.
    observed_attrs: dict[str, bool | str | int] = Field(default_factory=dict)

    #: ``False`` (the default) means: propose this at most once per target.
    repeatable: bool = False

    @model_validator(mode="after")
    def _has_exactly_one_target_source(self) -> PlanStep:
        if (self.target is None) == (not self.target_bind):
            raise ValueError(
                f"step {self.step_id!r} must set exactly one of target / target_bind"
            )
        if self.target_bind:
            bound = next(
                (p for p in self.preconditions if p.name == self.target_bind), None
            )
            if bound is None:
                raise ValueError(
                    f"step {self.step_id!r} binds its target to {self.target_bind!r}, "
                    f"but no precondition uses that bind"
                )
            # The target pattern must be pinnable to one chosen node, or
            # resolution would validate a different node than the one the
            # action actually runs against.
            if not bound.from_param:
                bound.from_param = self.target_bind
        return self

    @property
    def operation_key(self) -> str:
        """``"recon.example/ping"`` — how an attempt is recorded in the graph."""
        return f"{self.capability}/{self.operation}"

    def resolved_target(self) -> AnyTarget:
        if self.target is None:  # pragma: no cover - guarded by the validator
            raise ValueError(f"step {self.step_id!r} has no concrete target yet")
        return self.target

    def bound_to(self, target: AnyTarget) -> PlanStep:
        """A concrete copy of this step, pinned to one target."""
        return self.model_copy(update={"target": target, "target_bind": ""})


class AgentSpec(BaseModel):
    """One agent: who it is, what it may propose, and how it chooses."""

    agent_id: AgentId
    name: str
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    playbook: list[PlanStep] = Field(default_factory=list)

    #: ``"playbook"`` takes the first available step; ``"llm"`` asks the
    #: LLM gateway to choose among them (falling back to the first).
    planner_kind: str = "playbook"
    system_prompt: str = ""
    #: Stop offering this agent after this many executed actions.
    max_actions: int | None = Field(default=None, ge=1)


@dataclass(frozen=True)
class PlanningContext:
    """Everything a planner is allowed to see when it chooses."""

    agent: AgentSpec
    observation: GraphObservation
    candidates: list[PlanStep]
    run_id: RunId
    engagement_id: EngagementId
    step: int
    llm: LLMClient | None = None
    goal: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Planner(Protocol):
    """Chooses at most one of ``context.candidates``, or nothing."""

    async def plan(self, context: PlanningContext) -> PlanStep | None: ...


def _target_pattern(step: PlanStep) -> NodePattern:
    for pattern in step.preconditions:
        if pattern.name == step.target_bind:
            return pattern
    raise ValueError(  # pragma: no cover - guarded by the validator
        f"step {step.step_id!r} has no precondition bound as {step.target_bind!r}"
    )


async def available_steps(
    spec: AgentSpec,
    view: AttackGraphView,
    observation: GraphObservation,
    *,
    step: int = 0,
    limit: int = MAX_CANDIDATES_PER_STEP,
) -> list[PlanStep]:
    """The frontier: playbook entries whose preconditions resolve and
    that have not already been attempted against that target."""
    nodes = await view.list_nodes()
    edges = await view.list_edges()
    candidates: list[PlanStep] = []

    for entry in spec.playbook:
        if entry.target is not None:
            if entry.preconditions:
                result = resolve(
                    entry.preconditions, nodes=nodes, edges=edges, current_step=step
                )
                if not result.ok:
                    continue
            if _already_attempted(entry, node_id_for_target(entry.target), observation):
                continue
            candidates.append(entry)
            continue

        pattern = _target_pattern(entry)
        matches = [
            node
            for node in nodes
            if pattern.matches(node)
            and not is_stale(node.properties, step, max_age=pattern.max_age_steps)
        ]
        offered = 0
        for node in sorted(matches, key=lambda item: str(item.id)):
            if offered >= limit:
                break
            if _already_attempted(entry, node.id, observation):
                continue
            result = resolve(
                entry.preconditions,
                nodes=nodes,
                edges=edges,
                current_step=step,
                params={entry.target_bind: str(node.id)},
            )
            if not result.ok:
                continue
            candidates.append(entry.bound_to(target_for_node(node.id, node.type)))
            offered += 1

    return candidates


def _already_attempted(step: PlanStep, node_id: object, observation: GraphObservation) -> bool:
    if step.repeatable:
        return False
    return observation.has_attempted(f"{step.operation_key}@{node_id}")
