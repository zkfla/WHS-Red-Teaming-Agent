"""Explicit temporary/double-only assembly; the ordinary CLI never selects it."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from redteam.app.agent_profile import selected_specialists

# Keep the existing development API while sharing one planner/worker driver.
from redteam.app.central_run import CentralRun as CentralDevelopment
from redteam.app.runtime_components import ComponentFactoryContext, instantiate, resolve_selection
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.development import DevelopmentToolGateway
from redteam.capability.role_manifest import configure_role_tools
from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.risk import RegistryRiskResolver
from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
from redteam.core.contracts.mission import MissionPlan, Provenance
from redteam.core.enums import MissionStatus
from redteam.core.ids import EngagementId, MissionId, RunId, new_id
from redteam.event_bus.memory import InMemoryEventBus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_results import register_result_tool
from redteam.orchestration.central_runtime import (
    CentralEventBus,
    tool_limit,
)
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.development_jobs import DevelopmentJobs
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.metered_llm import RunMeteredLLM
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.specialist_planner import specialist_planning_inputs
from redteam.orchestration.state import RuntimeState


@asynccontextmanager
async def build_central_development(engagement, *, llm, tools, settings, jobs=None):
    """Requires local doubles and a selected existing specialist profile.

    All writable paths and Run/Engagement identities are fresh. There is no
    arbitrary transport/component injection or opening of a user's old Run.
    """
    if type(llm) is not FakeLLMClient or type(tools) is not DevelopmentToolGateway:
        raise ValueError("central_development_requires_exact_local_doubles")
    if jobs is not None and type(jobs) is not DevelopmentJobs:
        raise ValueError("central_development_requires_exact_local_job_double")
    if settings.code_fixture_policy_path is not None:
        raise ValueError("central_development_code_fixture_selection_not_wired")
    specialists = selected_specialists(settings)
    if specialists is None:
        raise ValueError("central_development_requires_specialist_selection")
    # These imports only assemble existing local stores/Control. No capability builder is called.
    from redteam.app.bootstrap import build_central_graph, build_central_storage
    from redteam.control.service import DefaultControlPlane
    from redteam.evidence.service import EvidenceService
    from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
    from redteam.orchestration.coordination import content_hash

    engagement = engagement.model_copy(
        update={"engagement_id": EngagementId(new_id("eng"))}, deep=True
    )
    run_id = RunId(new_id("run"))
    with TemporaryDirectory(prefix="redteam-central-development-") as temporary:
        root = Path(temporary)
        settings = settings.model_copy(
            update={
                "checkpointing": True,
                "checkpoint_backend": "sqlite",
                "checkpoint_path": str(root / "checkpoints.sqlite3"),
            }
        )
        state, _ = await build_central_storage(
            engagement, run_id=str(run_id), path=root / "state.sqlite3"
        )
        graph, evidence = build_central_graph(state), FilesystemEvidenceStore(root / "evidence")
        metered = RunMeteredLLM(
            llm,
            state.request_ledger(),
            prompts=specialists.prompts,
            request_semaphore=asyncio.Semaphore(
                settings.llm_request_policy.max_concurrent_requests
            ),
        )
        from redteam.orchestration.candidate_review_application import (
            CandidateReviewApplicationService,
        )

        candidate_review_applicator = CandidateReviewApplicationService(
            evidence_store=evidence,
            graph_store=graph,
            central_state=state,
        )
        components, binding = instantiate(
            resolve_selection(),
            ComponentFactoryContext(
                metered,
                evidence,
                graph,
                settings=settings,
                candidate_review_applicator=candidate_review_applicator,
            ),
        )
        components = specialists.assemble(components, include_code_artifacts=False)
        register_result_tool(components.local_tools)
        components.result_mappers.bind_catalog(tools.catalog)
        binding = binding.model_copy(
            update={
                "specialist_fingerprint": specialists.fingerprint,
                "extension_versions": {**binding.extension_versions, "central-development": "3"},
                "role_tools_fingerprint": configure_role_tools(
                    components.agents.definitions(), tools.catalog
                ),
            }
        )
        control = DefaultControlPlane(
            {engagement.engagement_id: engagement},
            audit_log=AuditLog(FilesystemAuditStore(root / "audit")),
            approvals=ApprovalService(FilesystemApprovalStore(root / "approvals")),
            evidence_service=EvidenceService(evidence),
            risk_resolver=RegistryRiskResolver(tools.catalog),
            outbound_descriptor_resolver=tools.catalog.get,
        )
        bus = InMemoryEventBus()
        central_bus = CentralEventBus(state, bus)
        candidate_review_applicator.bind_event_bus(central_bus)
        deps = RuntimeDeps(
            control_plane=control,
            tool_gateway=tools,
            tool_catalog=tools.catalog,
            long_job_adapter=jobs,
            event_bus=central_bus,
            event_history=bus,
            graph_store=graph,
            evidence_store=evidence,
            llm_client=metered,
            engagement=engagement,
            settings=settings,
            agents=components.agents,
            candidate_providers=components.candidate_providers,
            verifier=components.verifier,
            result_mappers=components.result_mappers,
            component_binding=binding,
            eligibility=components.eligibility,
            operation_bindings=components.operation_bindings,
            execution_recipes=components.execution_recipes,
            retry_policy=components.retry_policy,
            candidate_review_applicator=candidate_review_applicator,
            local_tools=components.local_tools,
            central_state=state,
            specialist_policy=specialists.run_policy().model_copy(
                update={
                    "max_instances": min(
                        specialists.run_policy().max_instances, settings.global_concurrency
                    ),
                }
            ),
            agent_prompts=specialists.prompts,
        )
        deps = replace(deps, tool_semaphore=asyncio.Semaphore(tool_limit(deps)))
        now = datetime.now(timezone.utc)
        mission = MissionPlan(
            mission_id=MissionId(new_id("mission")),
            engagement_id=engagement.engagement_id,
            objective=engagement.description or engagement.name,
            status=MissionStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            provenance=Provenance(source="development:operator", metadata={"double_only": True}),
        )
        await state.create_mission(mission)
        await state.bind_run(
            str(mission.mission_id),
            scope=engagement.scope,
            binding_hash=content_hash(specialist_planning_inputs(deps)),
        )
        await state.request_ledger().bind(str(run_id), settings.llm_request_policy)
        await seed_from_engagement(
            AttackGraphService(graph),
            engagement,
            run_id=run_id,
            execution_recipes=components.execution_recipes,
        )
        runtime = RuntimeState(
            run_id=run_id,
            engagement_id=engagement.engagement_id,
            mission_id=mission.mission_id,
            current_goal=mission.objective,
            started_at=now,
            remaining_budget=engagement.execution_budget.max_actions,
            component_binding=binding,
            llm_request_policy=settings.llm_request_policy.model_dump(mode="json"),
        )
        driver = InstanceDriver(deps)
        await driver.open()
        try:
            await driver.own_run(str(run_id))
            yield CentralDevelopment(deps, runtime, driver, development_double=True)
        finally:
            await driver.close()
