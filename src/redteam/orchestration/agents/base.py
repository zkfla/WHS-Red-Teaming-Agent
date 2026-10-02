"""Role, action and legacy reference-playbook contracts.

Production planning uses the LLM and the real tool catalog. Frontier
helpers produce reference material, never an execution pool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator
from pydantic_core import PydanticCustomError

from redteam.attack_graph.identity import host_node_id, node_id_for_target, target_for_node
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.patterns import NodePattern, resolve
from redteam.attack_graph.predicates import facts_of, is_stale
from redteam.attack_graph.view import AttackGraphView
from redteam.core.contracts.target import AnyTarget, TargetRef
from redteam.core.enums import RiskLevel
from redteam.core.ids import AgentId, EngagementId, HypothesisId, RunId, ToolId
from redteam.core.ports.llm import LLMClient
from redteam.orchestration.action_naming import (
    canonical_action_key,
    tool_matches_action_namespace,
)
from redteam.orchestration.authentication import AuthenticationCheck
from redteam.orchestration.expectations import Expectation

#: Cap on concrete steps produced from one playbook entry per turn, so a
#: graph with thousands of discovered nodes cannot blow up a prompt.
MAX_CANDIDATES_PER_STEP = 25

#: ``PlanStep.arguments`` values that ``bound_to`` replaces with a real
#: field off the bound target — lets one static playbook entry call a
#: real tool whose required arguments vary per discovered node, without
#: a specific host/service/path baked into the playbook. Deliberately
#: not a general template engine: this is the exact, small set of
#: fields real tools in this codebase need resolved from graph context
#: today (``docs/contracts/actions.md``'s canonical ``http_request``
#: shape). Add to this dict only when a real tool needs another one.
TARGET_REF_PLACEHOLDER = "$target_ref"
PATH_PLACEHOLDER = "$path"


def _target_ref_value(target: AnyTarget) -> str | None:
    for attribute in ("host", "domain", "service_name", "url"):
        value = getattr(target, attribute, None)
        if isinstance(value, str) and value:
            return value
    return str(target)


def _path_value(target: AnyTarget) -> str | None:
    """Only ``EndpointTarget`` carries a path — a ``ServiceTarget``/
    ``HostTarget`` genuinely has none, and this returns ``None`` rather
    than inventing ``"/"``: the caller drops that candidate instead of
    proposing an unresolved or guessed path."""
    value = getattr(target, "path", None)
    return value if isinstance(value, str) and value else None


#: Resolver per placeholder. ``_target_ref_value`` falls back to
#: ``str(target)`` so it never fails (every target kind has *some*
#: string identity); ``_path_value`` can genuinely return ``None`` when
#: the bound target has no path, which ``bound_to`` treats as "this
#: candidate cannot be resolved from what's known" rather than a guess.
_PLACEHOLDER_RESOLVERS: dict[str, Any] = {
    TARGET_REF_PLACEHOLDER: _target_ref_value,
    PATH_PLACEHOLDER: _path_value,
}


class HypothesisDraft(BaseModel):
    """The security question a step is asking, stated before it runs."""

    description: str
    confidence: float = Field(default=0.3, ge=0.0, le=1.0)
    #: Confirming this hypothesis means a security violation, not just a fact.
    violation_if_confirmed: bool = False
    #: Optional domain-provider impact assessment, declared before execution.
    severity: RiskLevel | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlanStep(BaseModel):
    """One thing an agent may propose, with its preconditions and its test."""

    step_id: str
    #: Runtime-assigned safe identity for cross-event lookup. Providers leave
    #: it blank; PLAN fills it only after compilation.
    candidate_key: str = ""
    path_id: str | None = None
    path_version: int | None = None
    path_step_id: str | None = None
    principal_ref: str | None = None
    operator_intent_id: str | None = None
    intent_index: int = 0
    availability_impact: bool = False
    capability: str
    operation: str
    tool_id: ToolId
    # Tool result contracts understood by this playbook; empty permits any compatible input.
    compatible_tool_ids: list[ToolId] = Field(default_factory=list)
    assessment_kind: Literal["observation", "security_check"] = "security_check"
    authentication_check: AuthenticationCheck | None = None

    #: Either a fixed target...
    target: TargetRef | None = None
    #: ...or the name of a precondition bind to take the target from.
    target_bind: str = ""

    preconditions: list[NodePattern] = Field(default_factory=list)

    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str
    expected_observation: str | None = None

    hypothesis: HypothesisDraft | None = None
    #: Reuse a question that an earlier observation stated before this
    #: targeted validation was planned. Agent-internal only: the Core action
    #: contract already carries the resulting hypothesis_id.
    existing_hypothesis_id: HypothesisId | None = None
    expectation: Expectation | None = None

    #: Legacy planning hints, never promoted as measured facts.
    observed_attrs: dict[str, bool | str | int] = Field(default_factory=dict)

    #: ``False`` (the default) means: propose this at most once per target.
    repeatable: bool = False

    @model_validator(mode="after")
    def _has_exactly_one_target_source(self) -> PlanStep:
        try:
            canonical_action_key(self.capability, self.operation)
        except ValueError as exc:
            raise PydanticCustomError(
                "unknown_operation", "Use a supported action meaning."
            ) from exc
        if not tool_matches_action_namespace(self.tool_id, self.capability):
            raise PydanticCustomError(
                "tool_outside_capability_namespace",
                "step {step_id} binds tool {tool_id} outside capability namespace {capability}",
                {
                    "step_id": repr(self.step_id),
                    "tool_id": repr(str(self.tool_id)),
                    "capability": repr(self.capability),
                },
            )
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
        """Canonical ROE/action key recorded in the graph."""
        return canonical_action_key(self.capability, self.operation)

    def resolved_target(self) -> AnyTarget:
        if self.target is None:  # pragma: no cover - guarded by the validator
            raise ValueError(f"step {self.step_id!r} has no concrete target yet")
        return self.target

    def bound_to(self, target: AnyTarget, *, target_ref: str | None = None) -> PlanStep | None:
        """A concrete copy of this step, pinned to one target, with every
        placeholder in :data:`_PLACEHOLDER_RESOLVERS` resolved from that
        target's real fields — lets a static playbook argument stay in
        sync with whichever concrete node it gets bound to, instead of a
        deployment-specific value (or a guessed one, e.g. a fixed path)
        baked into the playbook itself.

        Returns ``None`` when a placeholder this step actually uses
        cannot be resolved from ``target`` (e.g. ``$path`` against a
        ``ServiceTarget``, which has no path) — the caller
        (:func:`available_steps`) drops that candidate rather than
        proposing one with a dangling ``"$path"`` string, which would
        either fail tool-schema validation or, worse, reach a real tool
        as a literal argument value.
        """
        if self.unresolved_placeholder(target, target_ref=target_ref) is not None:
            return None
        arguments: dict[str, Any] = {}
        for key, value in self.arguments.items():
            resolver = _PLACEHOLDER_RESOLVERS.get(value) if isinstance(value, str) else None
            if resolver is None:
                arguments[key] = value
                continue
            resolved = (
                target_ref if value == TARGET_REF_PLACEHOLDER and target_ref else resolver(target)
            )
            arguments[key] = resolved
        return self.model_copy(
            update={"target": target, "target_bind": "", "arguments": arguments}
        )

    def unresolved_placeholder(
        self, target: AnyTarget, *, target_ref: str | None = None
    ) -> str | None:
        """The first argument key whose placeholder this target cannot fill.

        The reason a step is dropped used to be invisible: no draft existed,
        so no rejection could be reported. Naming the offending key lets the
        drop surface as a real graph_prerequisite_missing rejection instead
        of vanishing.
        """

        for key, value in self.arguments.items():
            resolver = _PLACEHOLDER_RESOLVERS.get(value) if isinstance(value, str) else None
            if resolver is None:
                continue
            if value == TARGET_REF_PLACEHOLDER and target_ref:
                continue
            if resolver(target) is None:
                return key
        return None


class AgentSpec(BaseModel):
    """One agent: who it is, what it may propose, and how it chooses."""

    agent_id: AgentId
    name: str
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    playbook: list[PlanStep] = Field(default_factory=list)

    #: One runtime mode: the model generates and reviews paths/actions.
    #: Playbook steps remain reference material for that reasoning.
    planner_kind: Literal["llm"] = "llm"
    system_prompt: str = ""
    #: Stop offering this agent after this many executed actions.
    max_actions: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _step_ids_are_unique(self) -> AgentSpec:
        step_ids = [step.step_id for step in self.playbook]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError(f"agent {self.agent_id!r} has duplicate step_id values")
        return self


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
    drops: list[tuple[str, str, str]] | None = None,
) -> list[PlanStep]:
    """The frontier: playbook entries whose preconditions resolve and
    that have not already been attempted against that target.

    A step whose bound target satisfies every precondition but cannot fill a
    placeholder argument is reported into drops as (step_id, node_id, field).
    It produces no draft, so without that record it would leave no trace.
    """
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
            target = target_for_node(
                node.id, node.type, observed=facts_of(node.properties).get("observed_target")
            )
            host = getattr(target, "host", None)
            host_node = await view.get_node(host_node_id(host)) if host else None
            target_ref = facts_of(host_node.properties).get("target_ref") if host_node else None
            bound = entry.bound_to(target, target_ref=target_ref)
            if bound is None:
                # A placeholder this step needs could not be resolved
                # from this particular node (section 7: never guess) --
                # skip it, do not count it against this turn's offer cap.
                if drops is not None:
                    drops.append(
                        (
                            entry.step_id,
                            str(node.id),
                            entry.unresolved_placeholder(target, target_ref=target_ref) or "",
                        )
                    )
                continue
            candidates.append(bound)
            offered += 1

    return candidates


def _already_attempted(step: PlanStep, node_id: object, observation: GraphObservation) -> bool:
    if step.repeatable:
        return False
    return observation.has_attempted(f"{step.operation_key}@{node_id}")
