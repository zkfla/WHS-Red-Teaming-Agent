"""Agents: the frontier, scheduling, and planner choice."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.patterns import NodePattern
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import AgentPhase, GraphEdgeType, GraphNodeType
from redteam.core.ids import AgentId, EngagementId, RunId, ToolId
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.agents import (
    ActivationRule,
    AgentDefinition,
    AgentRegistry,
    AgentSpec,
    ContextProfile,
    DuplicateAgentError,
    LLMPlanner,
    PlanningContext,
    PlanStep,
    PlaybookPlanner,
    available_steps,
    build_planner,
    default_registry,
)
from redteam.orchestration.progress import observation_fingerprint

HOST_PATTERN = NodePattern(type=GraphNodeType.HOST, attrs={"in_scope": True}, bind="host")


def probe_step(step_id: str = "probe", repeatable: bool = False) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        capability="recon",
        operation="service_discovery",
        tool_id=ToolId("recon.ping"),
        target_bind="host",
        preconditions=[HOST_PATTERN.model_copy(deep=True)],
        reason="probe the host",
        repeatable=repeatable,
    )


def spec(*steps: PlanStep, agent_id: str = "recon", planner_kind: str = "playbook") -> AgentSpec:
    return AgentSpec(
        agent_id=AgentId(agent_id),
        name=agent_id,
        playbook=list(steps),
        planner_kind=planner_kind,
    )


def context(agent: AgentSpec, candidates: list[PlanStep], observation, llm=None) -> PlanningContext:
    return PlanningContext(
        agent=agent,
        observation=observation,
        candidates=candidates,
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        step=1,
        llm=llm,
    )


def host_of(step: PlanStep) -> str:
    """The step's target host — every candidate in these tests is a host."""
    target = step.target
    assert isinstance(target, HostTarget)
    return target.host


def chosen(step: PlanStep | None) -> PlanStep:
    assert step is not None
    return step


def scheduled(spec_or_none: AgentSpec | None) -> AgentSpec:
    assert spec_or_none is not None
    return spec_or_none


async def seeded_service(*hosts: str) -> AttackGraphService:
    service = AttackGraphService(InMemoryAttackGraphStore())
    for host in hosts:
        await service.upsert_node(GraphNodeType.HOST, key=host, attrs={"in_scope": True}, step=0)
    return service


# -- PlanStep shape -------------------------------------------------------


def test_a_step_needs_exactly_one_target_source() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        PlanStep(
            step_id="s",
            capability="recon",
            operation="service_discovery",
            tool_id=ToolId("recon.ping"),
            reason="r",
        )
    with pytest.raises(ValidationError, match="exactly one"):
        PlanStep(
            step_id="s",
            capability="recon",
            operation="service_discovery",
            tool_id=ToolId("recon.ping"),
            reason="r",
            target=HostTarget(host="a.local"),
            target_bind="host",
            preconditions=[HOST_PATTERN.model_copy(deep=True)],
        )


def test_target_bind_must_name_a_declared_precondition() -> None:
    with pytest.raises(ValidationError, match="no precondition uses that bind"):
        PlanStep(
            step_id="s",
            capability="recon",
            operation="service_discovery",
            tool_id=ToolId("recon.ping"),
            reason="r",
            target_bind="nope",
            preconditions=[HOST_PATTERN.model_copy(deep=True)],
        )


def test_the_target_pattern_is_pinned_to_the_chosen_node() -> None:
    # Otherwise resolution could validate a different node than the one
    # the action actually runs against.
    step = probe_step()
    assert step.preconditions[0].from_param == "host"


# -- frontier -------------------------------------------------------------


async def test_one_candidate_per_matching_node() -> None:
    service = await seeded_service("a.local", "b.local")
    observation = await service.observe(step=1)

    candidates = await available_steps(spec(probe_step()), service.view(), observation, step=1)

    assert [host_of(c) for c in candidates] == ["a.local", "b.local"]
    assert all(c.target_bind == "" for c in candidates)


async def test_an_attempted_step_is_not_offered_again() -> None:
    # This is what stops a planner proposing the same action forever, and
    # it is derived from the graph rather than a conversation buffer.
    service = await seeded_service("a.local", "b.local")
    await service.record_attempt(
        (await service.query(node_type=GraphNodeType.HOST))[0].id, "recon.service_discovery", step=1
    )
    observation = await service.observe(step=2)

    candidates = await available_steps(spec(probe_step()), service.view(), observation, step=2)
    assert [host_of(c) for c in candidates] == ["b.local"]


async def test_a_repeatable_step_is_always_offered() -> None:
    service = await seeded_service("a.local")
    node = (await service.query(node_type=GraphNodeType.HOST))[0]
    await service.record_attempt(node.id, "recon.service_discovery", step=1)
    observation = await service.observe(step=2)

    candidates = await available_steps(
        spec(probe_step(repeatable=True)), service.view(), observation, step=2
    )
    assert len(candidates) == 1


async def test_nodes_that_fail_the_precondition_are_not_offered() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    await service.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"in_scope": False})
    observation = await service.observe(step=1)

    assert await available_steps(spec(probe_step()), service.view(), observation, step=1) == []


async def test_a_fixed_target_step_respects_its_preconditions() -> None:
    step = PlanStep(
        step_id="fixed",
        capability="recon",
        operation="service_discovery",
        tool_id=ToolId("recon.ping"),
        target=HostTarget(host="a.local"),
        preconditions=[NodePattern(type=GraphNodeType.CREDENTIAL, bind="cred")],
        reason="needs a credential first",
    )
    service = await seeded_service("a.local")
    observation = await service.observe(step=1)

    assert await available_steps(spec(step), service.view(), observation, step=1) == []

    await service.upsert_node(GraphNodeType.CREDENTIAL, key="c1")
    observation = await service.observe(step=2)
    assert len(await available_steps(spec(step), service.view(), observation, step=2)) == 1


async def test_the_candidate_list_is_capped() -> None:
    service = await seeded_service(*[f"h{i}.local" for i in range(30)])
    observation = await service.observe(step=1, max_total=100)

    candidates = await available_steps(
        spec(probe_step()), service.view(), observation, step=1, limit=5
    )
    assert len(candidates) == 5


async def test_edge_preconditions_are_honoured_per_candidate() -> None:
    service = await seeded_service("a.local", "b.local")
    principal = await service.upsert_node(GraphNodeType.PRINCIPAL, key="p1")
    hosts = {n.id: n for n in await service.query(node_type=GraphNodeType.HOST)}
    await service.link(
        GraphEdgeType.CAN_ACCESS,
        principal.id,
        next(iter(sorted(hosts))),  # host:a.local
    )
    step = probe_step()
    step.preconditions.append(NodePattern(type=GraphNodeType.PRINCIPAL, bind="p"))
    step.preconditions[0].related_to = "p"
    step.preconditions[0].edge_type = GraphEdgeType.CAN_ACCESS

    observation = await service.observe(step=1)
    candidates = await available_steps(spec(step), service.view(), observation, step=1)
    assert [host_of(c) for c in candidates] == ["a.local"]


# -- scheduling -----------------------------------------------------------


def test_the_current_agent_keeps_running_until_it_is_finished() -> None:
    registry = AgentRegistry([spec(agent_id="recon"), spec(agent_id="web")])

    assert scheduled(registry.next_agent(current=None, finished=[])).agent_id == AgentId("recon")
    assert scheduled(
        registry.next_agent(current=AgentId("recon"), finished=[])
    ).agent_id == AgentId("recon")
    assert scheduled(
        registry.next_agent(current=AgentId("recon"), finished=[AgentId("recon")])
    ).agent_id == AgentId("web")
    assert (
        registry.next_agent(current=AgentId("web"), finished=[AgentId("recon"), AgentId("web")])
        is None
    )


def test_duplicate_agent_ids_are_rejected() -> None:
    registry = AgentRegistry([spec(agent_id="recon")])
    with pytest.raises(DuplicateAgentError):
        registry.register(spec(agent_id="recon"))


def test_duplicate_step_ids_are_rejected_before_planning() -> None:
    with pytest.raises(ValidationError, match="duplicate step_id"):
        spec(probe_step("same"), probe_step("same"))


async def test_finished_agent_reactivates_only_after_a_graph_revision() -> None:
    recon = spec(agent_id="recon")
    registry = AgentRegistry([recon])
    service = await seeded_service("a.local")
    before = await service.observe(step=1)
    cursor = observation_fingerprint(before)

    assert (
        registry.next_agent(
            current=recon.agent_id,
            finished=[recon.agent_id],
            observation=before,
            finished_revisions={str(recon.agent_id): cursor},
        )
        is None
    )

    await service.upsert_node(GraphNodeType.HOST, key="new.local", attrs={"in_scope": True})
    after = await service.observe(step=2)
    assert scheduled(
        registry.next_agent(
            current=recon.agent_id,
            finished=[recon.agent_id],
            observation=after,
            finished_revisions={str(recon.agent_id): cursor},
        )
    ).agent_id == recon.agent_id


def test_unrelated_graph_revision_does_not_reactivate_a_specialist() -> None:
    registry = default_registry()
    recon_id = AgentId("recon-agent")
    mobile_id = AgentId("mobile-agent")
    before = GraphObservation(
        node_counts={"host": 1, "asset": 1},
        nodes_by_type={"host": ["a.local"], "asset": ["app.apk"]},
    )
    after_host = before.model_copy(
        update={
            "node_counts": {"host": 2, "asset": 1},
            "nodes_by_type": {
                "host": ["a.local", "b.local"],
                "asset": ["app.apk"],
            },
        }
    )
    cursors = {
        str(recon_id): registry.revision_fingerprint(recon_id, before),
        str(mobile_id): registry.revision_fingerprint(mobile_id, before),
    }

    selected = registry.next_agent(
        current=mobile_id,
        finished=[recon_id, mobile_id],
        observation=after_host,
        finished_revisions=cursors,
    )

    assert scheduled(selected).agent_id == recon_id
    assert registry.revision_fingerprint(mobile_id, before) == (
        registry.revision_fingerprint(mobile_id, after_host)
    )


def test_rotation_prevents_one_active_agent_from_starving_another() -> None:
    registry = AgentRegistry([spec(agent_id="recon"), spec(agent_id="web")])

    selected = registry.next_agent(
        current=AgentId("recon"), finished=[], rotate=True
    )

    assert scheduled(selected).agent_id == AgentId("web")


def test_priority_band_orders_agents_before_registration_order() -> None:
    recon = spec(agent_id="recon")
    web = spec(agent_id="web")
    registry = AgentRegistry(
        [recon, web],
        definitions=[
            AgentDefinition(
                agent_id=recon.agent_id,
                role="recon",
                name="Recon",
                provider_id="test",
                priority_band=20,
                context_profile=ContextProfile(profile_id="recon"),
            ),
            AgentDefinition(
                agent_id=web.agent_id,
                role="web",
                name="Web",
                provider_id="test",
                priority_band=10,
                context_profile=ContextProfile(profile_id="web"),
            ),
        ],
    )

    assert scheduled(
        registry.next_agent(
            current=None,
            finished=[],
            observation=GraphObservation(),
        )
    ).agent_id == AgentId("web")


def test_budget_weight_allocates_parallel_slots_across_active_roles() -> None:
    small = spec(agent_id="small")
    large = spec(agent_id="large")
    registry = AgentRegistry(
        [small, large],
        definitions=[
            AgentDefinition(
                agent_id=small.agent_id,
                role="small",
                name="Small",
                provider_id="test",
                budget_weight=1,
                context_profile=ContextProfile(profile_id="small"),
            ),
            AgentDefinition(
                agent_id=large.agent_id,
                role="large",
                name="Large",
                provider_id="test",
                budget_weight=3,
                context_profile=ContextProfile(profile_id="large"),
            ),
        ],
    )
    observation = GraphObservation()

    assert registry.weighted_batch_limit(
        small.agent_id,
        observation=observation,
        phase=AgentPhase.PLAN,
        capacity=4,
    ) == 1
    assert registry.weighted_batch_limit(
        large.agent_id,
        observation=observation,
        phase=AgentPhase.PLAN,
        capacity=4,
    ) == 3


async def test_definition_activation_skips_an_irrelevant_registered_role() -> None:
    mobile = spec(agent_id="mobile")
    recon = spec(agent_id="recon")
    registry = AgentRegistry(
        [mobile, recon],
        definitions=[
            AgentDefinition(
                agent_id=mobile.agent_id,
                role="mobile",
                name="mobile",
                provider_id="mobile",
                activation_rules=[ActivationRule(node_types=[GraphNodeType.ASSET])],
                context_profile=ContextProfile(profile_id="mobile"),
            )
        ],
    )
    service = await seeded_service("a.local")
    observation = await service.observe(step=1)

    picked = registry.next_agent(
        current=None,
        finished=[],
        observation=observation,
        phase=AgentPhase.OBSERVE,
    )

    assert scheduled(picked).agent_id == recon.agent_id


def test_the_default_registry_ships_executable_specialists() -> None:
    registry = default_registry()
    assert registry.agent_ids == [
        AgentId("recon-agent"),
        AgentId("web-api-agent"),
        AgentId("mobile-agent"),
        AgentId("analysis-agent"),
    ]
    assert len(registry) == 4
    assert registry.get(AgentId("nope")) is None
    assert registry.definition_ids == [
        AgentId("recon-agent"),
        AgentId("web-api-agent"),
        AgentId("mobile-agent"),
        AgentId("analysis-agent"),
        AgentId("reporting-agent"),
    ]
    # Architecture invariant: exactly the schedulable roles have executable specs.
    schedulable = [d.agent_id for d in registry.definitions() if d.schedulable]
    assert schedulable == registry.agent_ids
    seams = [d.agent_id for d in registry.definitions() if not d.schedulable]
    assert seams == [AgentId("reporting-agent")]
    analysis = registry.definition(AgentId("analysis-agent"))
    assert analysis is not None
    assert analysis.activation_rules[0].node_types == [GraphNodeType.ASSET]


def test_agent_spec_rejects_a_playbook_capability_it_was_not_granted() -> None:
    step = probe_step().model_copy(
        update={"capability": "web", "tool_id": ToolId("web.http_request")}
    )

    with pytest.raises(ValidationError, match="outside the agent's allowed capabilities"):
        AgentSpec(
            agent_id=AgentId("bounded"),
            name="bounded",
            capabilities=["recon"],
            playbook=[step],
        )


# -- planners -------------------------------------------------------------


async def test_playbook_planner_takes_the_first_candidate() -> None:
    service = await seeded_service("a.local")
    observation = await service.observe(step=1)
    agent = spec(probe_step("first"), probe_step("second"))
    candidates = await available_steps(agent, service.view(), observation, step=1)

    picked = chosen(await PlaybookPlanner().plan(context(agent, candidates, observation)))
    assert picked.step_id == "first"


async def test_a_planner_with_no_candidates_declines() -> None:
    service = await seeded_service()
    observation = await service.observe(step=1)
    assert await PlaybookPlanner().plan(context(spec(), [], observation)) is None
    assert await LLMPlanner(FakeLLMClient()).plan(context(spec(), [], observation)) is None


async def test_the_llm_choice_is_honoured_when_it_was_offered() -> None:
    service = await seeded_service("a.local")
    observation = await service.observe(step=1)
    agent = spec(probe_step("first"), probe_step("second"), planner_kind="llm")
    candidates = await available_steps(agent, service.view(), observation, step=1)
    llm = FakeLLMClient(canned_structured={"step_id": "second"})

    planner = LLMPlanner(llm)
    picked = chosen(await planner.plan(context(agent, candidates, observation, llm=llm)))

    assert picked.step_id == "second"
    assert planner.last_fallback_reason is None


async def test_a_step_that_was_never_offered_is_refused() -> None:
    # The model chooses from what it was shown; it cannot conjure a tool
    # call that was never declared.
    service = await seeded_service("a.local")
    observation = await service.observe(step=1)
    agent = spec(probe_step("first"), planner_kind="llm")
    candidates = await available_steps(agent, service.view(), observation, step=1)
    llm = FakeLLMClient(canned_structured={"step_id": "rm -rf /"})

    planner = LLMPlanner(llm)
    picked = chosen(await planner.plan(context(agent, candidates, observation, llm=llm)))

    assert picked.step_id == "first"
    assert "was not offered" in (planner.last_fallback_reason or "")


async def test_an_llm_failure_falls_back_instead_of_aborting_the_run() -> None:
    class ExplodingLLM(FakeLLMClient):
        async def complete_structured(self, **kwargs):
            raise RuntimeError("gateway down with secret-token")

    service = await seeded_service("a.local")
    observation = await service.observe(step=1)
    agent = spec(probe_step("first"), planner_kind="llm")
    candidates = await available_steps(agent, service.view(), observation, step=1)

    planner = LLMPlanner(ExplodingLLM())
    picked = chosen(await planner.plan(context(agent, candidates, observation, llm=ExplodingLLM())))

    assert picked.step_id == "first"
    assert "RuntimeError" in (planner.last_fallback_reason or "")
    assert "secret-token" not in (planner.last_fallback_reason or "")


async def test_the_prompt_shows_only_available_steps_and_prior_attempts() -> None:
    service = await seeded_service("a.local")
    node = (await service.query(node_type=GraphNodeType.HOST))[0]
    await service.record_attempt(node.id, "recon.endpoint_discovery", step=1)
    observation = await service.observe(step=2)
    agent = spec(probe_step("first"), planner_kind="llm")
    candidates = await available_steps(agent, service.view(), observation, step=2)

    rendered = LLMPlanner._render(context(agent, candidates, observation))

    assert "step_id=first" in rendered
    assert "Already attempted (do not repeat)" in rendered
    assert "recon.endpoint_discovery@host:a.local" in rendered


def test_planner_kind_selects_the_implementation() -> None:
    assert isinstance(build_planner("llm", FakeLLMClient()), LLMPlanner)
    assert isinstance(build_planner("playbook", None), PlaybookPlanner)
    assert isinstance(build_planner("unknown", None), PlaybookPlanner)
