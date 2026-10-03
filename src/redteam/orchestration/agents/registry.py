"""Agent scheduling, and the recon agent that ships with the skeleton.

Registration order is the scheduling policy. The registry is stateless;
which agents are done lives in ``RuntimeState.finished_agents``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.patterns import NodePattern
from redteam.core.enums import AgentPhase, GraphNodeType
from redteam.core.ids import AgentId, ToolId
from redteam.orchestration.agents.base import (
    TARGET_REF_PLACEHOLDER,
    AgentSpec,
    HypothesisDraft,
    PlanStep,
)
from redteam.orchestration.agents.definition import AgentDefinition
from redteam.orchestration.expectations import Expectation
from redteam.orchestration.progress import observation_fingerprint


class DuplicateAgentError(ValueError):
    pass


class AgentCapabilityDriftError(ValueError):
    """Raised when an ``AgentSpec`` and its ``AgentDefinition`` (same
    ``agent_id``) declare different ``capabilities``.

    Both models legitimately exist for different consumers (the Planner
    reads ``AgentSpec.capabilities``; scheduling/activation reads
    ``AgentDefinition.capabilities`` -- see ``mission_bootstrap.py``,
    ``nodes/observe.py``), so this is not itself the duplication to
    remove. What must never happen is the *value* diverging between them
    for the same agent -- that already happened once (web-api-agent was
    ``["web"]`` here and ``["web", "api"]`` there) with the drift
    completely silent, since ``GraphCandidateContext.available_capabilities``
    is populated from ``AgentSpec.capabilities`` but not read by anything
    (yet). Caught at registration time instead of trusting both call
    sites to be hand-kept in sync forever.
    """


def _check_capabilities_agree(
    agent_id: AgentId, spec_capabilities: list[str], definition_capabilities: list[str]
) -> None:
    if set(spec_capabilities) != set(definition_capabilities):
        raise AgentCapabilityDriftError(
            f"agent {agent_id!r}: AgentSpec.capabilities={spec_capabilities!r} does not "
            f"match AgentDefinition.capabilities={definition_capabilities!r}"
        )


class AgentRegistry:
    """An ordered set of agents, addressable by id."""

    def __init__(
        self,
        specs: Iterable[AgentSpec] = (),
        *,
        definitions: Iterable[AgentDefinition] = (),
    ) -> None:
        self._specs: list[AgentSpec] = []
        self._definitions: dict[AgentId, AgentDefinition] = {}
        for definition in definitions:
            self.register_definition(definition)
        for spec in specs:
            self.register(spec)

    def register(self, spec: AgentSpec) -> AgentSpec:
        if any(existing.agent_id == spec.agent_id for existing in self._specs):
            raise DuplicateAgentError(f"agent {spec.agent_id!r} is already registered")
        definition = self._definitions.get(spec.agent_id)
        if definition is not None:
            _check_capabilities_agree(spec.agent_id, spec.capabilities, definition.capabilities)
        self._specs.append(spec)
        return spec

    def get(self, agent_id: AgentId) -> AgentSpec | None:
        return next((spec for spec in self._specs if spec.agent_id == agent_id), None)

    def register_definition(self, definition: AgentDefinition) -> AgentDefinition:
        if definition.agent_id in self._definitions:
            raise DuplicateAgentError(
                f"agent definition {definition.agent_id!r} is already registered"
            )
        spec = self.get(definition.agent_id)
        if spec is not None:
            _check_capabilities_agree(spec.agent_id, spec.capabilities, definition.capabilities)
        self._definitions[definition.agent_id] = definition
        return definition

    def definition(self, agent_id: AgentId) -> AgentDefinition | None:
        return self._definitions.get(agent_id)

    def definitions(self) -> list[AgentDefinition]:
        return list(self._definitions.values())

    @property
    def definition_ids(self) -> list[AgentId]:
        return list(self._definitions)

    def all(self) -> list[AgentSpec]:
        return list(self._specs)

    @property
    def agent_ids(self) -> list[AgentId]:
        return [spec.agent_id for spec in self._specs]

    def next_agent(
        self,
        *,
        current: AgentId | None,
        finished: Sequence[AgentId],
        observation: GraphObservation | None = None,
        phase: AgentPhase = AgentPhase.OBSERVE,
        finished_revisions: dict[str, str] | None = None,
        rotate: bool = False,
    ) -> AgentSpec | None:
        """The agent that should run now, or ``None`` when all are done."""
        done = set(finished)
        if observation is not None and finished_revisions is not None:
            done = {
                agent_id
                for agent_id in done
                if finished_revisions.get(str(agent_id))
                == self.revision_fingerprint(agent_id, observation)
            }
        ordered = self._ordered_specs()
        eligible = [
            spec
            for spec in ordered
            if spec.agent_id not in done and self._is_active(spec, observation, phase)
        ]
        if not eligible:
            return None
        if rotate and current is not None:
            current_index = next(
                (index for index, spec in enumerate(eligible) if spec.agent_id == current),
                None,
            )
            if current_index is not None:
                return eligible[(current_index + 1) % len(eligible)]
        if current is not None and current not in done:
            spec = self.get(current)
            if spec is not None and self._is_active(spec, observation, phase):
                return spec
        return eligible[0]

    def revision_fingerprint(self, agent_id: AgentId, observation: GraphObservation) -> str:
        """Meaningful graph revision visible to one role's context profile."""

        definition = self.definition(agent_id)
        if definition is None:
            return observation_fingerprint(observation)
        profile = definition.context_profile
        return observation_fingerprint(
            observation,
            include_node_types=profile.include_node_types or None,
            include_edge_types=profile.include_edge_types,
            include_hypotheses=profile.include_hypotheses,
            include_findings=profile.include_findings,
        )

    def _ordered_specs(self) -> list[AgentSpec]:
        """Priority band first, registration order as the stable tie-break."""

        positions = {spec.agent_id: index for index, spec in enumerate(self._specs)}

        def priority(spec: AgentSpec) -> int:
            definition = self._definitions.get(spec.agent_id)
            return definition.priority_band if definition is not None else 100

        return sorted(
            self._specs,
            key=lambda spec: (
                priority(spec),
                positions[spec.agent_id],
            ),
        )

    def weighted_batch_limit(
        self,
        agent_id: AgentId,
        *,
        observation: GraphObservation,
        phase: AgentPhase,
        capacity: int,
    ) -> int:
        """Allocate this turn's fan-out share among currently active roles."""

        if capacity < 1:
            return 0
        active = [spec for spec in self._specs if self._is_active(spec, observation, phase)]
        if not active:
            return capacity

        def weight(spec: AgentSpec) -> float:
            definition = self._definitions.get(spec.agent_id)
            return definition.budget_weight if definition is not None else 1.0

        selected = next((spec for spec in active if spec.agent_id == agent_id), None)
        if selected is None:
            return 0
        total = sum(weight(spec) for spec in active)
        return max(1, min(capacity, math.ceil(capacity * weight(selected) / total)))

    def _is_active(
        self,
        spec: AgentSpec,
        observation: GraphObservation | None,
        phase: AgentPhase,
    ) -> bool:
        definition = self.definition(spec.agent_id)
        if definition is None:
            return True
        if observation is None:
            return False
        return definition.is_active(observation, phase)

    def __len__(self) -> int:
        return len(self._specs)


# -- the agent that ships with the skeleton -------------------------------

RECON_AGENT_ID = AgentId("recon-agent")

#: The HTTP-probe playbook supports the HTTP result contract and the
#: in-process demonstration tool. DNS/TLS discovery needs its own provider
#: and result mapper; sharing an operation name does not make it equivalent.
RECON_AGENT = AgentSpec(
    agent_id=RECON_AGENT_ID,
    name="Recon Agent",
    description="Confirms which in-scope hosts respond, one probe per host.",
    capabilities=["recon"],
    planner_kind="llm",
    playbook=[
        PlanStep(
            step_id="probe-host",
            capability="recon",
            operation="service_discovery",
            tool_id=ToolId("recon.probe"),
            compatible_tool_ids=[ToolId("recon.http_probe"), ToolId("recon.ping")],
            target_bind="host",
            preconditions=[
                NodePattern(
                    type=GraphNodeType.HOST,
                    attrs={"in_scope": True},
                    bind="host",
                )
            ],
            arguments={
                "target_ref": TARGET_REF_PLACEHOLDER,
                "path": "/",
            },
            reason="Confirm the host answers before anything else is planned against it.",
            expected_observation="the probe tool completes without a transport/tool error",
            hypothesis=HypothesisDraft(
                description="The host is reachable from the test harness.",
                confidence=0.4,
            ),
            expectation=Expectation(
                kind="tool_succeeded",
                description="the probe tool ran and returned a result for this host",
            ),
            observed_attrs={"reachable": True, "probed": True},
        )
    ],
)


def default_registry() -> AgentRegistry:
    """The registry used when nothing else is wired in ``app.bootstrap``.

    Assembled from the built-in role bundles so definition, playbook
    and provider link stay one unit. Reporting remains a terminal
    read-only projector. The other roles are executable when their
    activation predicate and a matching Tool Catalog capability are
    present.
    """
    from redteam.orchestration.agents.packages import builtin_role_packages

    registry = AgentRegistry()
    for package in builtin_role_packages():
        if not package.default_include:
            continue
        registry.register_definition(package.definition)
        if package.spec is not None:
            registry.register(package.spec)
    return registry
