"""Candidate compilation is deterministic and domain-neutral."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from redteam.attack_graph.patterns import NodePattern
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import GraphNodeType, RiskLevel, ToolHealthStatus
from redteam.core.ids import AgentId, GraphNodeId, ToolId
from redteam.orchestration.agents import AgentSpec, PlanStep
from redteam.orchestration.candidates.compiler import CandidateCompiler, candidate_key
from redteam.orchestration.candidates.models import PrioritySignals


def catalog_for(
    registry: MCPToolRegistry, *, operation: str = "service_discovery"
) -> MCPToolGateway:
    """A real, operation-aware ``ToolCatalog`` over ``registry`` — every
    registered tool bound to ``operation`` (the operation every playbook
    step in this file declares), mirroring how ``bootstrap.py`` always
    wires ``MCPToolGateway`` in production (``get``/``list_by_capability``
    from the Registry, ``list_tools_for_action`` from its
    ``MCPToolCatalog``). A bare ``MCPToolRegistry`` has no operation-binding
    data at all, so it cannot stand in for a ``ToolCatalog`` in tests that
    exercise selection.
    """
    return MCPToolGateway(
        registry, {}, {str(d.tool_id): [operation] for d in registry.all()}
    )


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


async def test_no_candidate_when_no_registered_tool_is_bound_to_the_operation() -> None:
    """Selection is action-aware (capability + operation), not tag-only: a
    tool tagged ``recon`` but bound to a different canonical operation must
    not be offered for ``service_discovery``."""
    service = await graph()
    observation = await service.observe(step=1)
    registry = MCPToolRegistry()
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
    catalog = MCPToolGateway(
        registry, {}, {"recon.alternative": ["port_scan"]}
    )  # bound to a different operation than the playbook declares

    candidates = await CandidateCompiler(catalog).compile_playbook(
        agent("recon.missing"), service.view(), observation, step=1
    )

    assert candidates == []


async def test_unhealthy_tool_is_never_selected_even_if_exact_match() -> None:
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

    candidates = await CandidateCompiler(catalog_for(registry)).compile_playbook(
        agent("recon.unhealthy"), service.view(), observation, step=1
    )

    assert candidates == []


async def test_action_aware_selection_ignores_the_playbook_placeholder_tool_id() -> None:
    """The whole point of action-aware selection: the provider's declared
    ``tool_id`` is a namespace hint, never a hardcoded pin. A tool that was
    never registered under that exact id is still selected, as long as an
    eligible, healthy tool is registered for the same capability+operation."""
    service = await graph()
    observation = await service.observe(step=1)
    registry = MCPToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("recon.nmap_scan"),
            server_id="good",
            tool_name="nmap_scan",
            capability_tags=["recon"],
            risk_level=RiskLevel.LOW,
            health_status=ToolHealthStatus.HEALTHY,
        )
    )

    # The playbook's declared tool_id ("recon.placeholder") was never
    # registered by any MCP server; selection must not require it to be.
    candidates = await CandidateCompiler(catalog_for(registry)).compile_playbook(
        agent("recon.placeholder"), service.view(), observation, step=1
    )

    assert candidates
    assert all(c.tool.tool_id == ToolId("recon.nmap_scan") for c in candidates)


async def test_selection_prefers_lower_risk_then_stable_lexical_order() -> None:
    """Deterministic priority: healthy > unknown (unhealthy excluded
    upstream) > lower risk_level > stable ``tool_id`` lexical tie-break."""
    service = await graph()
    observation = await service.observe(step=1)
    registry = MCPToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("recon.masscan_scan"),
            server_id="a",
            tool_name="masscan_scan",
            capability_tags=["recon"],
            risk_level=RiskLevel.HIGH,
            health_status=ToolHealthStatus.HEALTHY,
        )
    )
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("recon.nmap_scan"),
            server_id="b",
            tool_name="nmap_scan",
            capability_tags=["recon"],
            risk_level=RiskLevel.LOW,
            health_status=ToolHealthStatus.HEALTHY,
        )
    )

    candidates = await CandidateCompiler(catalog_for(registry)).compile_playbook(
        agent("recon.irrelevant"), service.view(), observation, step=1
    )

    assert candidates
    # Lower risk (LOW < HIGH) wins regardless of tool_id lexical order.
    assert all(c.tool.tool_id == ToolId("recon.nmap_scan") for c in candidates)

    # Same risk on both -> stable lexical tie-break ("recon.alpha" < "recon.beta").
    tied = MCPToolRegistry()
    tied.register(
        ToolDescriptor(
            tool_id=ToolId("recon.beta"),
            server_id="b",
            tool_name="beta",
            capability_tags=["recon"],
            risk_level=RiskLevel.LOW,
            health_status=ToolHealthStatus.HEALTHY,
        )
    )
    tied.register(
        ToolDescriptor(
            tool_id=ToolId("recon.alpha"),
            server_id="a",
            tool_name="alpha",
            capability_tags=["recon"],
            risk_level=RiskLevel.LOW,
            health_status=ToolHealthStatus.HEALTHY,
        )
    )
    tied_candidates = await CandidateCompiler(catalog_for(tied)).compile_playbook(
        agent("recon.irrelevant"), service.view(), observation, step=1
    )
    assert all(c.tool.tool_id == ToolId("recon.alpha") for c in tied_candidates)


async def test_selection_is_scoped_to_the_declared_capability_namespace() -> None:
    """A tool registered under a different namespace is never eligible,
    even if its ``tool_name`` happens to equal the requested operation."""
    service = await graph()
    observation = await service.observe(step=1)
    registry = MCPToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("web.service_discovery"),
            server_id="other",
            tool_name="service_discovery",
            capability_tags=["web"],
            risk_level=RiskLevel.LOW,
            health_status=ToolHealthStatus.HEALTHY,
        )
    )

    candidates = await CandidateCompiler(catalog_for(registry)).compile_playbook(
        agent("recon.irrelevant"), service.view(), observation, step=1
    )

    assert candidates == []


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

    assert not await CandidateCompiler(catalog_for(registry)).compile_playbook(
        agent(), service.view(), observation, step=1
    )


async def test_incompatible_first_tool_does_not_hide_executable_alternative() -> None:
    service = await graph()
    observation = await service.observe(step=1)
    registry = MCPToolRegistry()
    for name, required in (("a_url_tool", "url"), ("z_host_tool", "target_ref")):
        registry.register(ToolDescriptor(
            tool_id=ToolId(f"recon.{name}"), server_id="test", tool_name=name,
            input_schema={
                "type": "object", "properties": {required: {"type": "string"}},
                "required": [required], "additionalProperties": False,
            },
            capability_tags=["recon"], health_status=ToolHealthStatus.HEALTHY,
        ))
    spec = agent()
    candidates = await CandidateCompiler(catalog_for(registry)).compile_playbook(
        spec, service.view(), observation, step=1
    )
    assert len(candidates) == 2
    assert all(c.tool.tool_id == "recon.z_host_tool" for c in candidates)
    assert {c.draft.plan_step.arguments["target_ref"] for c in candidates} == {
        "a.local", "b.local",
    }
    assert spec.playbook[0].arguments == {}


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

    assert not await CandidateCompiler(catalog_for(registry)).compile_playbook(
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

    assert not await CandidateCompiler(catalog_for(registry)).compile_playbook(
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
