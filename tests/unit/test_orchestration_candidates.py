"""Candidate compilation is deterministic and domain-neutral."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from redteam.attack_graph.patterns import NodePattern
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import GraphNodeType, RiskLevel, ToolHealthStatus
from redteam.core.ids import AgentId, GraphNodeId, ToolId
from redteam.orchestration.agents import AgentSpec, PlanStep
from redteam.orchestration.candidates.compiler import CandidateCompiler, candidate_key
from redteam.orchestration.candidates.models import PrioritySignals


def agent(tool_id: str = "recon.ping") -> AgentSpec:
    return AgentSpec(
        agent_id=AgentId("recon"),
        name="recon",
        playbook=[
            PlanStep(
                step_id="probe",
                capability="recon",
                operation="service_discovery",
                tool_id=ToolId(tool_id),
                target_bind="host",
                preconditions=[
                    NodePattern(
                        type=GraphNodeType.HOST,
                        attrs={"in_scope": True},
                        bind="host",
                    )
                ],
                reason="probe",
            )
        ],
    )


async def graph():
    service = AttackGraphService(InMemoryAttackGraphStore())
    await service.upsert_node(GraphNodeType.HOST, key="b.local", attrs={"in_scope": True})
    await service.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"in_scope": True})
    return service


async def test_compiler_creates_stable_candidates_per_target() -> None:
    service = await graph()
    observation = await service.observe(step=1)
    compiler = CandidateCompiler()

    first = await compiler.compile_playbook(agent(), service.view(), observation, step=1)
    second = await compiler.compile_playbook(agent(), service.view(), observation, step=99)

    assert [item.candidate_key for item in first] == [item.candidate_key for item in second]
    assert {str(item.draft.target_node_ids[0]) for item in first} == {
        "host:a.local",
        "host:b.local",
    }
    assert all(item.draft.signals.source_refs for item in first)


async def test_catalog_does_not_substitute_an_unbound_tool_with_the_same_tag() -> None:
    service = await graph()
    observation = await service.observe(step=1)
    registry = MCPToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("recon.unhealthy"),
            server_id="bad",
            tool_name="ping",
            capability_tags=["recon"],
            risk_level=RiskLevel.LOW,
            health_status=ToolHealthStatus.UNHEALTHY,
        )
    )
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("recon.alternative"),
            server_id="good",
            tool_name="ping",
            capability_tags=["recon"],
            risk_level=RiskLevel.LOW,
            health_status=ToolHealthStatus.HEALTHY,
        )
    )

    candidates = await CandidateCompiler(registry).compile_playbook(
        agent("recon.missing"), service.view(), observation, step=1
    )

    assert candidates == []


async def test_compiler_discovers_tool_by_action_when_preferred_binding_is_absent() -> None:
    service = await graph()
    observation = await service.observe(step=1)
    registry = MCPToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("recon.http_probe"),
            server_id="recon-passive",
            tool_name="http_probe",
            capability_tags=["recon"],
            operations=["service_discovery"],
            target_types=["host"],
            input_schema={
                "type": "object",
                "properties": {"target_ref": {"type": "string"}},
                "required": ["target_ref"],
            },
            health_status=ToolHealthStatus.HEALTHY,
        )
    )
    catalog = MCPToolCatalog(registry)

    candidates = await CandidateCompiler(catalog).compile_playbook(
        agent("recon.missing"), service.view(), observation, step=1
    )

    assert {candidate.tool.tool_id for candidate in candidates} == {
        ToolId("recon.http_probe")
    }
    assert {candidate.plan_step.arguments["target_ref"] for candidate in candidates} == {
        "a.local",
        "b.local",
    }


async def test_required_tool_schema_fields_are_hard_constraints() -> None:
    service = await graph()
    observation = await service.observe(step=1)
    registry = MCPToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("recon.ping"),
            server_id="example",
            tool_name="ping",
            capability_tags=["recon"],
            input_schema={"type": "object", "required": ["message"]},
            health_status=ToolHealthStatus.HEALTHY,
        )
    )

    assert not await CandidateCompiler(registry).compile_playbook(
        agent(), service.view(), observation, step=1
    )


async def test_full_json_schema_rejects_wrong_argument_types() -> None:
    service = await graph()
    observation = await service.observe(step=1)
    registry = MCPToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("recon.ping"),
            server_id="example",
            tool_name="ping",
            capability_tags=["recon"],
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "integer", "minimum": 1}},
                "required": ["message"],
            },
            health_status=ToolHealthStatus.HEALTHY,
        )
    )
    typed_agent = agent().model_copy(deep=True)
    typed_agent.playbook[0].arguments = {"message": "not-an-integer"}

    assert not await CandidateCompiler(registry).compile_playbook(
        typed_agent, service.view(), observation, step=1
    )


async def test_invalid_tool_schema_is_rejected_as_an_unusable_contract() -> None:
    service = await graph()
    observation = await service.observe(step=1)
    registry = MCPToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("recon.ping"),
            server_id="example",
            tool_name="ping",
            capability_tags=["recon"],
            input_schema={"type": "definitely-not-a-json-schema-type"},
            health_status=ToolHealthStatus.HEALTHY,
        )
    )

    assert not await CandidateCompiler(registry).compile_playbook(
        agent(), service.view(), observation, step=1
    )


async def test_non_json_tool_arguments_are_rejected_even_by_a_permissive_schema() -> None:
    service = await graph()
    observation = await service.observe(step=1)
    invalid = agent().model_copy(deep=True)
    invalid.playbook[0].arguments = {"payload": b"not-json"}

    assert not await CandidateCompiler().compile_playbook(
        invalid, service.view(), observation, step=1
    )


async def test_candidate_key_excludes_score_and_graph_revision() -> None:
    service = await graph()
    observation = await service.observe(step=1)
    candidate = (
        await CandidateCompiler().compile_playbook(agent(), service.view(), observation, step=1)
    )[0]
    changed = candidate.draft.model_copy(deep=True)
    changed.signals.expected_cost = 1.0

    assert candidate_key(changed) == candidate.candidate_key


async def test_candidate_key_distinguishes_different_action_arguments() -> None:
    service = await graph()
    observation = await service.observe(step=1)
    candidate = (
        await CandidateCompiler().compile_playbook(agent(), service.view(), observation, step=1)
    )[0]
    changed = candidate.draft.model_copy(deep=True)
    changed.plan_step.arguments = {"mode": "different"}

    assert candidate_key(changed) != candidate.candidate_key


async def test_compiler_rejects_a_draft_whose_execution_target_is_not_referenced() -> None:
    service = await graph()
    observation = await service.observe(step=1)
    compiler = CandidateCompiler()
    candidate = (
        await compiler.compile_playbook(agent(), service.view(), observation, step=1)
    )[0]
    mismatched = candidate.draft.model_copy(
        update={"target_node_ids": [GraphNodeId("host:somewhere-else.local")]}
    )

    assert compiler.compile_drafts([mismatched]) == []


def test_custom_priority_signals_require_provenance() -> None:
    with pytest.raises(ValidationError, match="source_ref"):
        PrioritySignals(objective_progress=0.9)

    signals = PrioritySignals(
        objective_progress=0.9,
        source_refs=[" graph:host:a.local ", "graph:host:a.local"],
    )
    assert signals.source_refs == ["graph:host:a.local"]
