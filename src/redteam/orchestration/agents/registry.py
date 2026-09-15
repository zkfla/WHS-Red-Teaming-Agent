"""Agent scheduling, and the recon agent that ships with the skeleton.

Registration order is the scheduling policy. The registry is stateless;
which agents are done lives in ``RuntimeState.finished_agents``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from redteam.attack_graph.patterns import NodePattern
from redteam.core.enums import GraphNodeType
from redteam.core.ids import AgentId, ToolId
from redteam.orchestration.agents.base import AgentSpec, HypothesisDraft, PlanStep
from redteam.orchestration.expectations import Expectation


class DuplicateAgentError(ValueError):
    pass


class AgentRegistry:
    """An ordered set of agents, addressable by id."""

    def __init__(self, specs: Iterable[AgentSpec] = ()) -> None:
        self._specs: list[AgentSpec] = []
        for spec in specs:
            self.register(spec)

    def register(self, spec: AgentSpec) -> AgentSpec:
        if any(existing.agent_id == spec.agent_id for existing in self._specs):
            raise DuplicateAgentError(f"agent {spec.agent_id!r} is already registered")
        self._specs.append(spec)
        return spec

    def get(self, agent_id: AgentId) -> AgentSpec | None:
        return next((spec for spec in self._specs if spec.agent_id == agent_id), None)

    def all(self) -> list[AgentSpec]:
        return list(self._specs)

    @property
    def agent_ids(self) -> list[AgentId]:
        return [spec.agent_id for spec in self._specs]

    def next_agent(
        self, *, current: AgentId | None, finished: Sequence[AgentId]
    ) -> AgentSpec | None:
        """The agent that should run now, or ``None`` when all are done."""
        done = set(finished)
        if current is not None and current not in done:
            spec = self.get(current)
            if spec is not None:
                return spec
        return next((spec for spec in self._specs if spec.agent_id not in done), None)

    def __len__(self) -> int:
        return len(self._specs)


# -- the agent that ships with the skeleton -------------------------------

RECON_AGENT_ID = AgentId("recon-agent")

#: Probes each in-scope host with the harmless example MCP tool. Its
#: target comes from the graph, not a hardcoded hostname, so adding a
#: host to the engagement YAML is enough to make it probe that host too.
RECON_AGENT = AgentSpec(
    agent_id=RECON_AGENT_ID,
    name="Recon Agent",
    description="Confirms which in-scope hosts respond, one probe per host.",
    capabilities=["recon.example"],
    planner_kind="llm",
    playbook=[
        PlanStep(
            step_id="probe-host",
            capability="recon.example",
            operation="ping",
            tool_id=ToolId("example.ping"),
            target_bind="host",
            preconditions=[
                NodePattern(
                    type=GraphNodeType.HOST,
                    attrs={"in_scope": True},
                    bind="host",
                )
            ],
            arguments={"message": "reachability probe"},
            reason="Confirm the host answers before anything else is planned against it.",
            expected_observation="the tool echoes the probe message back",
            hypothesis=HypothesisDraft(
                description="The host is reachable from the test harness.",
                confidence=0.4,
            ),
            expectation=Expectation(
                kind="output_field_present",
                field="echo",
                description="the example tool echoes its arguments back",
            ),
            observed_attrs={"reachable": True, "probed": True},
        )
    ],
)


def default_registry() -> AgentRegistry:
    """The registry used when nothing else is wired in ``app.bootstrap``.

    TODO(agent-team): add Web/API, Mobile and Reporting agents here as
    their playbooks land. No new contract is needed for that.
    """
    return AgentRegistry([RECON_AGENT])
