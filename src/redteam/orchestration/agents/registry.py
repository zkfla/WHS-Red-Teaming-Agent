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
from redteam.orchestration.agents.builtin import MOBILE_AGENT, WEB_API_AGENT
from redteam.orchestration.agents.definition import (
    AgentDefinition,
    builtin_agent_definitions,
)
from redteam.orchestration.expectations import Expectation
from redteam.orchestration.progress import observation_fingerprint


class DuplicateAgentError(ValueError):
    pass


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
        self._specs.append(spec)
        return spec

    def get(self, agent_id: AgentId) -> AgentSpec | None:
        return next((spec for spec in self._specs if spec.agent_id == agent_id), None)

    def register_definition(self, definition: AgentDefinition) -> AgentDefinition:
        if definition.agent_id in self._definitions:
            raise DuplicateAgentError(
                f"agent definition {definition.agent_id!r} is already registered"
            )
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

    def revision_fingerprint(
        self, agent_id: AgentId, observation: GraphObservation
    ) -> str:
        """Meaningful graph revision visible to one role's context profile."""

        definition = self.definition(agent_id)
        if definition is None:
            return observation_fingerprint(observation)
        profile = definition.context_profile
        return observation_fingerprint(
            observation,
            include_node_types=profile.include_node_types or None,
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
        active = [
            spec
            for spec in self._specs
            if self._is_active(spec, observation, phase)
        ]
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

#: Probes each in-scope host. Its target comes from the graph, not a
#: hardcoded hostname, so adding a host to the engagement YAML is enough
#: to make it probe that host too. ``target_ref`` (via
#: ``PlanStep.bound_to``'s ``TARGET_REF_PLACEHOLDER`` substitution) is
#: the bound host's own address, so this one static step works both
#: against the harmless in-process ``ExampleMCPAdapter`` (zero MCP
#: servers configured -- its permissive ``ping`` schema accepts any
#: arguments and just echoes them) and against a real, configured
#: ``recon`` MCP server whose tool is keyed by that same address (e.g.
#: ``recon.http_probe(target_ref, path)`` -- see
#: ``deploy/mcp/README.md``/``.env.example``). Action-aware Tool
#: Selection (``orchestration.candidates.tool_selection``) decides which
#: concrete tool actually runs; this step never pins one.
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
                "message": "reachability probe",
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

    Analysis is injected as ``AnalysisVerifier`` and Reporting is a terminal
    read-only projector; Web/API and Mobile are executable when both their
    activation predicate and a matching Tool Catalog capability are present.
    """
    return AgentRegistry(
        [RECON_AGENT, WEB_API_AGENT, MOBILE_AGENT],
        definitions=builtin_agent_definitions(),
    )
