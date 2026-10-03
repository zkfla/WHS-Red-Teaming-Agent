"""Runtime extension integration: pluggy collection to actual registries."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.app.extensions import ExtensionAssembly, ExtensionAssemblyError, collect_extensions
from redteam.app.runtime_components import (
    ComponentFactoryContext,
    apply_extensions,
    bind_extensions,
    binding_for_resume,
    default_component_registry,
    instantiate,
    resolve_selection,
)
from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolDescriptor, ToolResult
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.extensions import (
    Contribution,
    ContributionReference,
    ExtensionDeclaration,
    hookimpl,
)
from redteam.core.ids import ActionId, AgentId, InvocationId, ToolId
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.agents.base import AgentSpec, PlanStep
from redteam.orchestration.agents.definition import AgentDefinition, ContextProfile
from redteam.orchestration.agents.packages import RolePackage, role_package_contributions
from redteam.orchestration.agents.providers import PlaybookCandidateProvider
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.candidates.models import CandidateDraft
from redteam.orchestration.cognition import (
    DecisionBudget,
    DecisionFailure,
    StructuredDecisionClient,
)
from redteam.orchestration.component_binding import (
    ComponentBindingError,
    builtin_component_binding,
)
from redteam.orchestration.observations.mapper import GraphMutation
from redteam.orchestration.planning import BudgetedInference, PlanningRequest
from tests.fakes.evidence_store import FakeEvidenceStore
from tests.integration._engine_llm_harness import ScriptedLLM


def _factory_context() -> ComponentFactoryContext:
    return ComponentFactoryContext(
        llm_client=FakeLLMClient(),
        evidence_store=FakeEvidenceStore(),
        graph_store=InMemoryAttackGraphStore(),
    )


def _builtin_components() -> tuple[Any, ComponentFactoryContext]:
    context = _factory_context()
    registry = default_component_registry()
    registration = resolve_selection(None, registry=registry)
    components, _binding = instantiate(registration, context)
    return components, context


def _synth_provider_factory() -> PlaybookCandidateProvider:
    return PlaybookCandidateProvider()


def _synth_role_package() -> RolePackage:
    agent_id = AgentId("synth-agent")
    definition = AgentDefinition(
        agent_id=agent_id,
        role="synth",
        name="Synth Agent",
        capabilities=["synth"],
        provider_id="playbook:synth",
        context_profile=ContextProfile(profile_id="synth"),
    )
    target = HostTarget(host="host-01")
    spec = AgentSpec(
        agent_id=agent_id,
        name="Synth Agent",
        capabilities=["synth"],
        playbook=[
            PlanStep(
                step_id="synth-probe",
                capability="synth",
                operation="probe_check",
                tool_id=ToolId("synth.probe"),
                target=target,
                arguments={"target_ref": "host-01"},
                reason="synthetic extension probe",
            )
        ],
    )
    return RolePackage(
        agent_id=agent_id,
        definition=definition,
        spec=spec,
        provider_id="playbook:synth",
        provider_factory=_synth_provider_factory,
    )


def _synth_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId("synth.probe"),
        server_id="synth-server",
        tool_name="probe",
        description="synthetic probe tool",
        input_schema={
            "type": "object",
            "properties": {
                "target_ref": {"type": "string"},
                "host": {"type": "string"},
            },
            "required": ["target_ref"],
            "additionalProperties": True,
        },
        operations=["probe_check"],
        capability_tags=["synth"],
        target_types=["host"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )


async def test_pluggy_collect_assembles_new_role_and_compiles_with_actual_catalog() -> None:
    components, context = _builtin_components()
    assert components.agents.get(AgentId("recon-agent")) is not None

    package = _synth_role_package()
    declaration = ExtensionDeclaration(
        "synth.ext",
        "1",
        tuple(role_package_contributions(package)),
    )

    class _SynthPlugin:
        def __init__(self, held: ExtensionDeclaration) -> None:
            self._held = held

        @hookimpl
        def redteam_extensions(self) -> tuple[ExtensionDeclaration, ...]:
            return (self._held,)

    assembly = collect_extensions(plugins=[_SynthPlugin(declaration)])
    assert assembly.resolve("agent_role", "synth-agent").contribution_id == "synth-agent"

    extended = apply_extensions(components, context, assembly)
    assert extended.agents.get(AgentId("synth-agent")) is not None
    assert extended.agents.get(AgentId("recon-agent")) is not None

    registry = MCPToolRegistry()
    registry.register(_synth_descriptor())
    catalog = MCPToolCatalog(registry)
    assert [
        str(item.tool_id) for item in catalog.list_tools_for_action("synth", "probe_check")
    ] == ["synth.probe"]

    assert package.spec is not None
    target = HostTarget(host="host-01")
    draft = CandidateDraft(
        agent_id=AgentId("synth-agent"),
        target_node_ids=[node_id_for_target(target)],
        plan_step=package.spec.playbook[0],
    )
    compiler = CandidateCompiler(
        catalog=catalog,
        operation_bindings=extended.operation_bindings,
    )
    candidates, rejections = compiler.compile_drafts_with_rejections(
        [draft],
        allowed_capabilities={"synth"},
    )
    assert len(candidates) == 1 and not rejections
    assert str(candidates[0].tool.tool_id) == "synth.probe"
    assert candidates[0].plan_step.operation_key == "synth.probe_check"


def test_missing_required_reference_and_partial_role_rejected() -> None:
    components, context = _builtin_components()

    def _orphan_factory(_ctx: Any) -> PlaybookCandidateProvider:
        return PlaybookCandidateProvider()

    orphan = Contribution(
        kind="candidate_provider",
        contribution_id="synth-orphan",
        version="1",
        factory=_orphan_factory,
        requires=(
            ContributionReference(
                kind="candidate_provider",
                contribution_id="missing-provider",
            ),
        ),
    )
    bad_assembly = ExtensionAssembly(
        [ExtensionDeclaration("synth.bad", "1", (orphan,))],
    )
    with pytest.raises(ExtensionAssemblyError, match="requires missing"):
        apply_extensions(components, context, bad_assembly)

    partial_id = AgentId("synth-partial")
    partial_definition = AgentDefinition(
        agent_id=partial_id,
        role="synth_partial",
        name="Partial Agent",
        capabilities=["synthpartial"],
        provider_id="playbook:synthpartial",
        context_profile=ContextProfile(profile_id="synthpartial"),
    )

    def _partial_factory() -> PlaybookCandidateProvider:
        return PlaybookCandidateProvider()

    partial_package = RolePackage(
        agent_id=partial_id,
        definition=partial_definition,
        spec=None,
        provider_id="playbook:synthpartial",
        provider_factory=_partial_factory,
    )
    partial_assembly = ExtensionAssembly(
        [
            ExtensionDeclaration(
                "synth.partial",
                "1",
                tuple(role_package_contributions(partial_package)),
            )
        ],
    )
    with pytest.raises(ExtensionAssemblyError, match="missing executable role"):
        apply_extensions(components, context, partial_assembly)


def test_single_mapper_serves_two_tool_ids() -> None:
    components, context = _builtin_components()
    calls: list[str] = []

    def _mapper_factory(_ctx: Any) -> Any:
        def _mapper(*, target: Any, arguments: dict[str, Any], result: ToolResult) -> GraphMutation:
            calls.append(str(result.tool_id))
            return GraphMutation()

        return _mapper

    contribution = Contribution(
        kind="result_mapper",
        contribution_id="synth.first",
        version="1",
        factory=_mapper_factory,
        target_ids=("synth.first", "synth.second"),
    )
    declaration = ExtensionDeclaration("synth.mapper-ext", "1", (contribution,))

    class _MapperPlugin:
        def __init__(self, held: ExtensionDeclaration) -> None:
            self._held = held

        @hookimpl
        def redteam_extensions(self) -> tuple[ExtensionDeclaration, ...]:
            return (self._held,)

    assembly = collect_extensions(plugins=[_MapperPlugin(declaration)])
    extended = apply_extensions(components, context, assembly)
    assert extended.result_mappers.has_mapper("synth.first")
    assert extended.result_mappers.has_mapper("synth.second")

    target = HostTarget(host="host-01")
    moment = datetime.now(timezone.utc)
    for tool_name in ("synth.first", "synth.second"):
        result = ToolResult(
            invocation_id=InvocationId("inv-" + tool_name),
            action_id=ActionId("act-1"),
            tool_id=ToolId(tool_name),
            success=True,
            output={},
            started_at=moment,
            finished_at=moment,
        )
        mutation = extended.result_mappers.map_result(
            tool_id=ToolId(tool_name),
            target=target,
            arguments={},
            result=result,
        )
        assert mutation == GraphMutation()
        assert extended.result_mappers.last_failure is None
    assert calls == ["synth.first", "synth.second"]


def test_changed_declaration_rejects_resume_binding() -> None:
    def _provider_factory(_ctx: Any) -> PlaybookCandidateProvider:
        return PlaybookCandidateProvider()

    def _contribution(
        *,
        requires: tuple[ContributionReference, ...] = (),
        target_ids: tuple[str, ...] = (),
    ) -> Contribution:
        return Contribution(
            kind="candidate_provider",
            contribution_id="synth-provider",
            version="1",
            factory=_provider_factory,
            requires=requires,
            target_ids=target_ids,
        )

    base = ExtensionAssembly(
        [ExtensionDeclaration("synth.ext", "1", (_contribution(),))],
    )
    stored = bind_extensions(
        builtin_component_binding(),
        base,
        selected=(),
        planning_provider_id=None,
    )
    registry = default_component_registry()
    assert binding_for_resume(stored, registry=registry, extensions=base) == stored

    owner_changed = ExtensionAssembly(
        [ExtensionDeclaration("synth.ext-renamed", "1", (_contribution(),))],
    )
    with pytest.raises(ComponentBindingError):
        binding_for_resume(stored, registry=registry, extensions=owner_changed)

    requires_changed = ExtensionAssembly(
        [
            ExtensionDeclaration(
                "synth.ext",
                "1",
                (
                    _contribution(
                        requires=(
                            ContributionReference(
                                kind="candidate_provider",
                                contribution_id="extra",
                            ),
                        ),
                    ),
                ),
            )
        ],
    )
    with pytest.raises(ComponentBindingError):
        binding_for_resume(stored, registry=registry, extensions=requires_changed)

    target_changed = ExtensionAssembly(
        [ExtensionDeclaration("synth.ext", "1", (_contribution(target_ids=("synth.first",)),))],
    )
    with pytest.raises(ComponentBindingError):
        binding_for_resume(stored, registry=registry, extensions=target_changed)


async def test_alternative_planning_shares_budgeted_inference_limit() -> None:
    components, context = _builtin_components()

    class _AltProvider:
        def __init__(self) -> None:
            self.proposals = 0

        async def propose(self, request: PlanningRequest) -> dict[str, Any]:
            self.proposals += 1
            return await request.inference.infer(
                stage=request.stage,
                payload=request.payload,
                response_schema=request.response_schema,
                system=request.system,
            )

    def _planning_factory(_ctx: Any) -> _AltProvider:
        return _AltProvider()

    planning_contribution = Contribution(
        kind="planning",
        contribution_id="alt-planner",
        version="1",
        factory=_planning_factory,
    )
    planning_declaration = ExtensionDeclaration(
        "synth.planning",
        "1",
        (planning_contribution,),
    )

    class _PlanningPlugin:
        def __init__(self, held: ExtensionDeclaration) -> None:
            self._held = held

        @hookimpl
        def redteam_extensions(self) -> tuple[ExtensionDeclaration, ...]:
            return (self._held,)

    assembly = collect_extensions(plugins=[_PlanningPlugin(planning_declaration)])
    extended = apply_extensions(
        components,
        context,
        assembly,
        planning_provider_id="alt-planner",
    )
    assert isinstance(extended.planning_provider, _AltProvider)

    llm = ScriptedLLM()
    llm.add("unknown", {"ok": True})
    llm.add("unknown", {"ok": True})
    client = StructuredDecisionClient(llm, max_attempts=10)
    budget = DecisionBudget(max_attempts=2)
    inference = BudgetedInference(client, budget)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": True,
    }
    request = PlanningRequest(
        stage="synth-plan",
        payload={"goal": "probe"},
        response_schema=schema,
        inference=inference,
    )
    provider = extended.planning_provider
    first = await provider.propose(request)
    second = await provider.propose(request)
    assert first == {"ok": True}
    assert second == {"ok": True}
    assert budget.used == 2
    assert len(llm.calls) == 2
    with pytest.raises(DecisionFailure) as excinfo:
        await provider.propose(request)
    assert excinfo.value.kind == "budget"
    assert len(llm.calls) == 2
    assert budget.used == 2
