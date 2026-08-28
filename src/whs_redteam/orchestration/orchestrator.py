"""Deterministic, checkpointed coordinator for the existing safe pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from whs_redteam.ai.provider import AIProviderError, PlannerProvider
from whs_redteam.ai.report import OpenAICompatibleReportWriter
from whs_redteam.audit import AuditLogger
from whs_redteam.authorization import AuthorizationMatrixProbe
from whs_redteam.config import EngagementConfig, InjectionFixture
from whs_redteam.exceptions import (
    PolicyViolation,
    ProbeRegistrationError,
    RequestLimitExceeded,
    RuntimeLimitExceeded,
)
from whs_redteam.injection import (
    ReflectedXSSProbe,
    SQLInjectionProbe,
    discover_injection_fixtures,
)
from whs_redteam.inventory import InventoryStore
from whs_redteam.mcp_servers import (
    MCPServerGuard,
    create_actor_session_mcp_server,
    create_api_schema_mcp_server,
    create_browser_mcp_server,
    create_resource_model_mcp_server,
    create_safe_http_mcp_server,
)
from whs_redteam.models import (
    ActionProposal,
    AgentRun,
    AgentRunStatus,
    AttackSurfaceSummary,
    Capability,
    ExploitabilityDecision,
    ExploitabilityVerdict,
    QueueStatus,
    ResourceOwnershipRecord,
    RiskLevel,
    TestPlan,
    utc_now,
)
from whs_redteam.openapi import import_openapi_schemas
from whs_redteam.orchestration.adapters import (
    MCPProbeAdapter,
    MCPToolBinding,
    ToolAdapter,
)
from whs_redteam.orchestration.cleanup import CleanupQueue
from whs_redteam.orchestration.dispatcher import ActionDispatcher, ProbeRegistry
from whs_redteam.orchestration.mcp_transport import OfficialMCPTransport
from whs_redteam.orchestration.probes import (
    ActorSessionProbe,
    ApiSchemaProbe,
    DiscoveryProbe,
    FindingValidationProbe,
    ResourceModelProbe,
)
from whs_redteam.orchestration.queue import ActionQueue, QueueExecutor
from whs_redteam.orchestration.retry import RetryPolicy
from whs_redteam.planning import HybridPlanner, RulesPlanner, planning_context
from whs_redteam.policy.budget import PersistentRequestBudget, RequestBudget
from whs_redteam.policy.capabilities import CapabilityPolicy
from whs_redteam.policy.capability_catalog import CAPABILITY_CATALOG
from whs_redteam.policy.scope import ScopeGuard
from whs_redteam.reporting import write_json_report, write_markdown_report
from whs_redteam.resource_model import model_resources, template_endpoint_url
from whs_redteam.tools.actor_sessions import ActorSessionPool, summarize_actor_sessions
from whs_redteam.tools.browser import WebExplorer
from whs_redteam.tools.http_client import SafeHttpClient
from whs_redteam.tools.http_explorer import HttpExplorer
from whs_redteam.tools.network_gate import NetworkGate
from whs_redteam.tools.xss_browser import BrowserXSSConfirmer
from whs_redteam.validation import FindingValidator


def _canonical_config_value(value: object) -> object:
    """Normalize unordered config containers before hashing across processes."""

    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {
            str(key): _canonical_config_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_config_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, sort_keys=True, default=str),
        )
    if isinstance(value, (list, tuple)):
        return [_canonical_config_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


class AgentOrchestrator:
    """Own stage transitions; workers never decide what stage runs next."""

    def __init__(
        self,
        config: EngagementConfig,
        inventory: InventoryStore,
        audit_logger: AuditLogger,
        retry_policy: RetryPolicy | None = None,
        planner_provider: PlannerProvider | None = None,
    ) -> None:
        self.config = config
        self.inventory = inventory
        self.audit_logger = audit_logger
        self.retry_policy = retry_policy or RetryPolicy()
        self.planner_provider = planner_provider
        self.scope_guard = ScopeGuard(config, audit_logger)
        config_material = _canonical_config_value(
            config.model_dump(
                mode="python",
                exclude={"audit_log_path", "inventory_db_path"},
            )
        )
        config_json = json.dumps(
            config_material,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        self.config_hash = hashlib.sha256(config_json.encode()).hexdigest()

    def create(self, run_id: str | None = None) -> AgentRun:
        if run_id is not None and self.inventory.get_agent_run(run_id) is not None:
            raise ValueError(f"agent run already exists: {run_id}")
        run = AgentRun(
            run_id=run_id or uuid4().hex,
            engagement_name=self.config.name,
            config_hash=self.config_hash,
        )
        self.inventory.save_agent_run(run)
        self.audit_logger.emit("agent_run_created", run_id=run.run_id)
        return run

    def get(self, run_id: str) -> AgentRun:
        run = self.inventory.get_agent_run(run_id)
        if run is None:
            raise ValueError(f"agent run not found: {run_id}")
        if run.config_hash != self.config_hash:
            raise ValueError("engagement configuration changed; refusing unsafe resume")
        return run

    async def run(self, run_id: str | None = None) -> AgentRun:
        run = self.create(run_id) if run_id is None else self.get(run_id)
        if run.status is AgentRunStatus.FAILED:
            if run.resume_status is None:
                raise ValueError("failed run has no resumable checkpoint")
            run = self._save(
                run, status=run.resume_status, resume_status=None, last_error=None
            )
        if run.status is AgentRunStatus.COMPLETED:
            return run
        if run.status is AgentRunStatus.BLOCKED:
            if CleanupQueue(self.inventory).incomplete(run.run_id):
                return run
            if run.resume_status is None:
                raise ValueError("blocked run has no resumable checkpoint")
            run = self._save(
                run,
                status=run.resume_status,
                resume_status=None,
                last_error=None,
            )
        if run.status is AgentRunStatus.WAITING_APPROVAL:
            if self.inventory.queue_counts(run.run_id).get("WAITING_APPROVAL", 0) > 0:
                self._run_budget(run.run_id).pause()
                return run
            if run.resume_status is None:
                raise ValueError("approval-waiting run has no resume checkpoint")
            self._run_budget(run.run_id).resume()
            run = self._save(
                run, status=run.resume_status, resume_status=None, last_error=None
            )

        try:
            while run.status is not AgentRunStatus.COMPLETED:
                current = run.status
                if current is AgentRunStatus.CREATED:
                    run = self._save(run, status=AgentRunStatus.DISCOVERING)
                elif current is AgentRunStatus.DISCOVERING:
                    exploration_id = run.exploration_run_id or f"{run.run_id}-explore"
                    await self._dispatch_discovery(run.run_id, exploration_id)
                    run = self._save(
                        run,
                        status=AgentRunStatus.INVENTORY_READY,
                        exploration_run_id=exploration_id,
                    )
                elif current is AgentRunStatus.INVENTORY_READY:
                    run = self._save(
                        run, status=AgentRunStatus.API_SCHEMA_MODELING
                    )
                elif current is AgentRunStatus.API_SCHEMA_MODELING:
                    if run.exploration_run_id is None:
                        raise RuntimeError(
                            "API schema modeling has no exploration run id"
                        )
                    await self._dispatch_api_schema(
                        run.run_id, run.exploration_run_id
                    )
                    run = self._save(
                        run, status=AgentRunStatus.ACTOR_SESSION_MODELING
                    )
                elif current is AgentRunStatus.ACTOR_SESSION_MODELING:
                    await self._dispatch_actor_sessions(run.run_id)
                    run = self._save(
                        run, status=AgentRunStatus.RESOURCE_MODELING
                    )
                elif current is AgentRunStatus.RESOURCE_MODELING:
                    if run.exploration_run_id is None:
                        raise RuntimeError(
                            "resource modeling has no exploration run id"
                        )
                    await self._dispatch_resource_model(
                        run.run_id, run.exploration_run_id
                    )
                    run = self._save(
                        run, status=AgentRunStatus.ATTACK_SURFACE_MODELING
                    )
                elif current is AgentRunStatus.ATTACK_SURFACE_MODELING:
                    surface = self._model_attack_surface(run)
                    run = self._save(
                        run,
                        status=AgentRunStatus.PLANNING,
                        attack_surface_json=surface.model_dump_json(),
                    )
                elif current is AgentRunStatus.PLANNING:
                    plan = await self._plan_with_provider(run.exploration_run_id)
                    self._enqueue_validation_action(run.run_id, plan)
                    self._enqueue_authorization_actions(run.run_id)
                    self._enqueue_injection_actions(
                        run.run_id,
                        run.exploration_run_id,
                        plan.selected_candidate_ids,
                    )
                    run = self._save(
                        run,
                        status=AgentRunStatus.EXECUTING,
                        plan_json=plan.model_dump_json(),
                    )
                elif current is AgentRunStatus.EXECUTING:
                    await self._execute_pending_actions(run.run_id)
                    counts = self.inventory.queue_counts(run.run_id)
                    if counts.get(QueueStatus.WAITING_APPROVAL.value, 0) > 0:
                        self._run_budget(run.run_id).pause()
                        run = self._save(
                            run,
                            status=AgentRunStatus.WAITING_APPROVAL,
                            resume_status=AgentRunStatus.EXECUTING,
                        )
                        return run
                    if run.plan_json is None:
                        run = self._save(run, status=AgentRunStatus.PLANNING)
                        continue
                    validation_id = self._completed_validation_id(run.run_id)
                    if validation_id is None:
                        raise RuntimeError(
                            "validation action did not complete successfully"
                        )
                    replanned = await self._maybe_replan(run, validation_id)
                    if replanned is not None:
                        self._enqueue_injection_actions(
                            run.run_id,
                            run.exploration_run_id,
                            replanned.selected_candidate_ids,
                        )
                        run = self._save(
                            run,
                            status=AgentRunStatus.EXECUTING,
                            plan_json=replanned.model_dump_json(),
                            validation_id=validation_id,
                        )
                        continue
                    run = self._save(
                        run,
                        status=AgentRunStatus.EXPLOITABILITY_DECISION,
                        validation_id=validation_id,
                    )
                elif current is AgentRunStatus.VALIDATING:
                    if run.plan_json is None:
                        raise RuntimeError("validation checkpoint has no test plan")
                    plan = TestPlan.model_validate_json(run.plan_json)
                    self._enqueue_validation_action(run.run_id, plan)
                    self._enqueue_authorization_actions(run.run_id)
                    self._enqueue_injection_actions(
                        run.run_id,
                        run.exploration_run_id,
                        plan.selected_candidate_ids,
                    )
                    run = self._save(
                        run,
                        status=AgentRunStatus.EXECUTING,
                    )
                elif current is AgentRunStatus.EXPLOITABILITY_DECISION:
                    if run.validation_id is None:
                        raise RuntimeError(
                            "exploitability decision has no validation id"
                        )
                    decision = self._decide_exploitability(run.validation_id)
                    run = self._save(
                        run,
                        status=AgentRunStatus.CLEANING_UP,
                        exploitability_decision_json=decision.model_dump_json(),
                    )
                elif current is AgentRunStatus.CLEANING_UP:
                    incomplete = CleanupQueue(self.inventory).incomplete(
                        run.run_id
                    )
                    if incomplete:
                        details = ", ".join(
                            f"{task.capability.value}:{task.status.value}"
                            for task in incomplete
                        )
                        run = self._save(
                            run,
                            status=AgentRunStatus.BLOCKED,
                            resume_status=AgentRunStatus.CLEANING_UP,
                            last_error="CleanupPending: " + details,
                        )
                        return run
                    self._assert_cleanup_complete(run.run_id)
                    run = self._save(run, status=AgentRunStatus.REPORTING)
                elif current is AgentRunStatus.REPORTING:
                    if run.validation_id is None:
                        raise RuntimeError("reporting checkpoint has no validation id")
                    output_path = await self._report(run)
                    run = self._save(
                        run,
                        status=AgentRunStatus.COMPLETED,
                        findings_path=str(output_path),
                    )
                else:
                    raise RuntimeError(f"unsupported agent state: {current}")
            self.audit_logger.emit("agent_run_completed", run_id=run.run_id)
            return run
        except Exception as exc:
            first_line = str(exc).splitlines()[0][:400]
            incomplete_cleanup = CleanupQueue(self.inventory).incomplete(
                run.run_id
            )
            failure_status = (
                AgentRunStatus.BLOCKED
                if incomplete_cleanup
                else AgentRunStatus.FAILED
            )
            self._save(
                run,
                status=failure_status,
                resume_status=run.status,
                last_error=f"{type(exc).__name__}: {first_line}",
            )
            self.audit_logger.emit(
                "agent_run_failed",
                run_id=run.run_id,
                resume_status=run.status.value,
                error_type=type(exc).__name__,
                cleanup_blocked=bool(incomplete_cleanup),
            )
            raise

    def set_waiting_for_approval(
        self, run_id: str, *, resume_status: AgentRunStatus
    ) -> AgentRun:
        run = self.get(run_id)
        if run.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.FAILED,
            AgentRunStatus.BLOCKED,
        }:
            raise ValueError(f"run cannot wait for approval from {run.status.value}")
        if self.inventory.queue_counts(run_id).get("WAITING_APPROVAL", 0) == 0:
            raise ValueError("run has no queued actions waiting for approval")
        self._run_budget(run_id).pause()
        return self._save(
            run,
            status=AgentRunStatus.WAITING_APPROVAL,
            resume_status=resume_status,
        )

    async def _explore(
        self,
        exploration_id: str,
        budget: RequestBudget | PersistentRequestBudget | None = None,
    ) -> None:
        budget = budget or RequestBudget.from_config(self.config, self.audit_logger)
        gate = NetworkGate(self.scope_guard, budget, self.audit_logger)
        explorer = (
            HttpExplorer(
                self.config,
                self.inventory,
                gate,
                self.audit_logger,
                run_id=exploration_id,
            )
            if self.config.discovery_mode == "http"
            else WebExplorer(
                self.config,
                self.inventory,
                gate,
                self.audit_logger,
                run_id=exploration_id,
            )
        )
        await explorer.explore()

    async def _dispatch_discovery(
        self, agent_run_id: str, exploration_id: str
    ) -> None:
        registry = ProbeRegistry()
        budget = self._run_budget(agent_run_id)
        tool_adapter: ToolAdapter | None = None
        if self.config.tool_backend == "local":
            registry.register(
                DiscoveryProbe(
                    lambda worker_run_id: self._explore(worker_run_id, budget),
                    exploration_id,
                )
            )
        else:
            guard = MCPServerGuard(self.config, self.audit_logger)

            async def discover_handler(request, approved):
                await self._explore(exploration_id, budget)
                return {
                    "exploration_run_id": exploration_id,
                    "target_url": approved.target_url,
                }

            server = create_browser_mcp_server(guard, discover_handler)
            tool_adapter = MCPProbeAdapter(
                (
                    MCPToolBinding(
                        Capability.DISCOVERY, "browser", "discover"
                    ),
                ),
                OfficialMCPTransport({"browser": server}),
            )
        dispatcher = ActionDispatcher(
            CapabilityPolicy(self.config, self.scope_guard),
            registry,
            self.inventory,
            self.audit_logger,
            tool_adapter=tool_adapter,
        )
        queue = ActionQueue(self.inventory)
        queued, _ = queue.enqueue(
            agent_run_id,
            ActionProposal(
                capability=Capability.DISCOVERY,
                target_url=self.config.targets[0],
                method="GET",
                risk_level=RiskLevel.PASSIVE,
                reason="Orchestrator-approved discovery stage.",
            ),
        )
        if queued.status is QueueStatus.SUCCEEDED:
            return
        worker_id = f"orchestrator-{agent_run_id[:12]}"
        while True:
            claimed = queue.claim_next(agent_run_id, worker_id)
            if claimed is None:
                delay = queue.next_retry_delay(agent_run_id)
                if delay is None:
                    current = queue.get(queued.queue_id)
                    status = current.status.value if current else "MISSING"
                    raise RuntimeError(
                        f"discovery action is not executable: {status}"
                    )
                await asyncio.sleep(delay)
                continue
            try:
                await QueueExecutor(
                    queue, dispatcher, self.retry_policy
                ).execute(claimed)
                return
            except Exception:
                current = queue.get(claimed.queue_id)
                if current is not None and current.status is QueueStatus.RETRY_PENDING:
                    continue
                raise

    async def _execute_pending_actions(self, agent_run_id: str) -> None:
        registry = ProbeRegistry()
        run = self.get(agent_run_id)
        tool_adapter: ToolAdapter | None = None
        if run.plan_json is not None:
            plan = TestPlan.model_validate_json(run.plan_json)
            budget = self._run_budget(agent_run_id)
            injection_fixtures = self._injection_fixtures(
                run.exploration_run_id
            )
            if self.config.tool_backend == "local":
                registry.register(
                    FindingValidationProbe(lambda: self._validate(plan, budget))
                )
            else:
                guard = MCPServerGuard(self.config, self.audit_logger)

                async def validation_handler(request, approved):
                    validation_id = await self._validate(plan, budget)
                    return {
                        "validation_id": validation_id,
                        "target_url": approved.target_url,
                    }

                server = create_safe_http_mcp_server(guard, validation_handler)
                tool_adapter = MCPProbeAdapter(
                    (
                        MCPToolBinding(
                            Capability.FINDING_VALIDATION,
                            "safe-http",
                            "validate_findings",
                        ),
                    ),
                    OfficialMCPTransport({"safe-http": server}),
                )
            if self.config.authorization_fixtures or injection_fixtures:
                if self.config.tool_backend != "local":
                    raise RuntimeError(
                        "active validation fixtures require the local backend"
                    )
                sessions = self._actor_session_pool(budget)
            if self.config.authorization_fixtures:
                registry.register(
                    AuthorizationMatrixProbe(
                        self.config.authorization_fixtures,
                        sessions,
                        lambda: self._completed_validation_id(agent_run_id),
                        self.config.validation_repetitions,
                    )
                )
            if injection_fixtures:
                anonymous_client = SafeHttpClient(
                    NetworkGate(self.scope_guard, budget, self.audit_logger),
                    self.audit_logger,
                )
                checks = {
                    check
                    for fixture in injection_fixtures
                    for check in fixture.checks
                }
                probe_arguments = (
                    injection_fixtures,
                    anonymous_client,
                    sessions,
                    lambda: self._completed_validation_id(agent_run_id),
                    self.config.validation_repetitions,
                )
                if "sqli" in checks:
                    registry.register(SQLInjectionProbe(*probe_arguments))
                if "xss_reflection" in checks:
                    browser_confirmer = (
                        BrowserXSSConfirmer(
                            self.config,
                            NetworkGate(
                                self.scope_guard, budget, self.audit_logger
                            ),
                            self.audit_logger,
                        )
                        if self.config.xss_browser_confirmation
                        else None
                    )
                    registry.register(
                        ReflectedXSSProbe(
                            *probe_arguments,
                            browser_confirmer,
                        )
                    )
        dispatcher = ActionDispatcher(
            CapabilityPolicy(self.config, self.scope_guard),
            registry,
            self.inventory,
            self.audit_logger,
            tool_adapter=tool_adapter,
        )
        queue = ActionQueue(self.inventory)
        worker_id = f"orchestrator-{agent_run_id[:12]}"
        while True:
            claimed = queue.claim_next(agent_run_id, worker_id)
            if claimed is None:
                delay = queue.next_retry_delay(agent_run_id)
                if delay is None:
                    break
                await asyncio.sleep(delay)
                continue
            try:
                await QueueExecutor(
                    queue, dispatcher, self.retry_policy
                ).execute(claimed)
            except (RequestLimitExceeded, RuntimeLimitExceeded):
                raise
            except (PolicyViolation, ProbeRegistrationError):
                continue
            except Exception:
                current = queue.get(claimed.queue_id)
                if current is not None and current.status is QueueStatus.RETRY_PENDING:
                    continue
                raise

    async def _dispatch_api_schema(
        self, agent_run_id: str, exploration_id: str
    ) -> None:
        registry = ProbeRegistry()
        tool_adapter: ToolAdapter | None = None
        if self.config.tool_backend == "local":
            registry.register(ApiSchemaProbe(exploration_id))
        else:
            guard = MCPServerGuard(self.config, self.audit_logger)

            async def schema_handler(request, approved):
                openapi_imported = import_openapi_schemas(
                    self.config,
                    self.inventory,
                    exploration_id,
                )
                schemas = self.inventory.list_api_request_schemas(exploration_id)
                return {
                    "exploration_run_id": exploration_id,
                    "schemas_observed": len(schemas),
                    "endpoints_with_query": sum(
                        bool(schema.query_schema) for schema in schemas
                    ),
                    "endpoints_with_body": sum(
                        schema.body_schema is not None for schema in schemas
                    ),
                    "openapi_imported": openapi_imported,
                    "target_url": approved.target_url,
                }

            server = create_api_schema_mcp_server(guard, schema_handler)
            tool_adapter = MCPProbeAdapter(
                (
                    MCPToolBinding(
                        Capability.API_SCHEMA,
                        "api-schema",
                        "summarize_api_schema",
                    ),
                ),
                OfficialMCPTransport({"api-schema": server}),
            )
        dispatcher = ActionDispatcher(
            CapabilityPolicy(self.config, self.scope_guard),
            registry,
            self.inventory,
            self.audit_logger,
            tool_adapter=tool_adapter,
        )
        queue = ActionQueue(self.inventory)
        discovery = queue.find_latest(agent_run_id, Capability.DISCOVERY)
        dependencies = (discovery.queue_id,) if discovery is not None else ()
        queued, _ = queue.enqueue(
            agent_run_id,
            ActionProposal(
                capability=Capability.API_SCHEMA,
                target_url=self.config.targets[0],
                method="GET",
                risk_level=RiskLevel.PASSIVE,
                reason="Summarize value-free API request schemas.",
            ),
            priority=95,
            max_attempts=1,
            depends_on=dependencies,
        )
        if queued.status is QueueStatus.SUCCEEDED:
            return
        claimed = queue.claim_next(
            agent_run_id, f"orchestrator-{agent_run_id[:12]}"
        )
        if claimed is None or claimed.queue_id != queued.queue_id:
            status = queue.get(queued.queue_id)
            value = status.status.value if status else "MISSING"
            raise RuntimeError(f"API schema action is not executable: {value}")
        await QueueExecutor(queue, dispatcher, self.retry_policy).execute(claimed)

    async def _dispatch_actor_sessions(self, agent_run_id: str) -> None:
        registry = ProbeRegistry()
        tool_adapter: ToolAdapter | None = None
        if self.config.tool_backend == "local":
            registry.register(
                ActorSessionProbe(lambda: summarize_actor_sessions(self.config))
            )
        else:
            guard = MCPServerGuard(self.config, self.audit_logger)

            async def actor_handler(request, approved):
                summary = summarize_actor_sessions(self.config)
                return {
                    **summary.model_dump(mode="json"),
                    "target_url": approved.target_url,
                }

            server = create_actor_session_mcp_server(guard, actor_handler)
            tool_adapter = MCPProbeAdapter(
                (
                    MCPToolBinding(
                        Capability.AUTH_SESSION,
                        "actor-session",
                        "summarize_actor_sessions",
                    ),
                ),
                OfficialMCPTransport({"actor-session": server}),
            )
        dispatcher = ActionDispatcher(
            CapabilityPolicy(self.config, self.scope_guard),
            registry,
            self.inventory,
            self.audit_logger,
            tool_adapter=tool_adapter,
        )
        queue = ActionQueue(self.inventory)
        predecessor = queue.find_latest(agent_run_id, Capability.API_SCHEMA)
        dependencies = (
            (predecessor.queue_id,) if predecessor is not None else ()
        )
        queued, _ = queue.enqueue(
            agent_run_id,
            ActionProposal(
                capability=Capability.AUTH_SESSION,
                target_url=self.config.targets[0],
                method="GET",
                risk_level=RiskLevel.SAFE_ACTIVE,
                reason="Summarize actor session readiness without persisting secrets.",
            ),
            priority=94,
            max_attempts=1,
            depends_on=dependencies,
        )
        if queued.status is QueueStatus.SUCCEEDED:
            return
        claimed = queue.claim_next(
            agent_run_id, f"orchestrator-{agent_run_id[:12]}"
        )
        if claimed is None or claimed.queue_id != queued.queue_id:
            current = queue.get(queued.queue_id)
            value = current.status.value if current else "MISSING"
            raise RuntimeError(f"actor session action is not executable: {value}")
        await QueueExecutor(queue, dispatcher, self.retry_policy).execute(claimed)

    async def _dispatch_resource_model(
        self, agent_run_id: str, exploration_id: str
    ) -> None:
        registry = ProbeRegistry()
        tool_adapter: ToolAdapter | None = None
        ownership_records = self._fixture_ownership_records()
        if self.config.tool_backend == "local":
            registry.register(
                ResourceModelProbe(exploration_id, ownership_records)
            )
        else:
            guard = MCPServerGuard(self.config, self.audit_logger)

            async def resource_handler(request, approved):
                resources = model_resources(
                    self.inventory.list_endpoints(exploration_id),
                    self.inventory.list_api_request_schemas(exploration_id),
                )
                self.inventory.replace_resources(exploration_id, resources)
                self.inventory.replace_resource_ownership(
                    exploration_id, list(ownership_records)
                )
                return {
                    "exploration_run_id": exploration_id,
                    "resources_modeled": len(resources),
                    "identifier_candidates": sum(
                        len(resource.identifiers) for resource in resources
                    ),
                    "ownership_assertions": len(ownership_records),
                    "target_url": approved.target_url,
                }

            server = create_resource_model_mcp_server(guard, resource_handler)
            tool_adapter = MCPProbeAdapter(
                (
                    MCPToolBinding(
                        Capability.RESOURCE_MODEL,
                        "resource-model",
                        "model_resources",
                    ),
                ),
                OfficialMCPTransport({"resource-model": server}),
            )
        dispatcher = ActionDispatcher(
            CapabilityPolicy(self.config, self.scope_guard),
            registry,
            self.inventory,
            self.audit_logger,
            tool_adapter=tool_adapter,
        )
        queue = ActionQueue(self.inventory)
        predecessor = queue.find_latest(agent_run_id, Capability.AUTH_SESSION)
        dependencies = (
            (predecessor.queue_id,) if predecessor is not None else ()
        )
        queued, _ = queue.enqueue(
            agent_run_id,
            ActionProposal(
                capability=Capability.RESOURCE_MODEL,
                target_url=self.config.targets[0],
                method="GET",
                risk_level=RiskLevel.PASSIVE,
                reason="Infer value-free resource and identifier candidates.",
            ),
            priority=93,
            max_attempts=1,
            depends_on=dependencies,
        )
        if queued.status is QueueStatus.SUCCEEDED:
            return
        claimed = queue.claim_next(
            agent_run_id, f"orchestrator-{agent_run_id[:12]}"
        )
        if claimed is None or claimed.queue_id != queued.queue_id:
            current = queue.get(queued.queue_id)
            value = current.status.value if current else "MISSING"
            raise RuntimeError(f"resource model action is not executable: {value}")
        await QueueExecutor(queue, dispatcher, self.retry_policy).execute(claimed)

    def _fixture_ownership_records(
        self,
    ) -> tuple[ResourceOwnershipRecord, ...]:
        return tuple(
            ResourceOwnershipRecord(
                fixture_id=fixture.fixture_id,
                endpoint_template=template_endpoint_url(fixture.target_url),
                target_fingerprint=hashlib.sha256(
                    (
                        fixture.fixture_id
                        + "|"
                        + template_endpoint_url(fixture.target_url)
                    ).encode()
                ).hexdigest(),
                owner_actor=fixture.owner_actor,
                non_owner_actors=fixture.non_owner_actors,
            )
            for fixture in self.config.authorization_fixtures
        )

    def _enqueue_validation_action(
        self, agent_run_id: str, plan: TestPlan
    ) -> None:
        queue = ActionQueue(self.inventory)
        resource_model = queue.find_latest(
            agent_run_id, Capability.RESOURCE_MODEL
        )
        dependencies = (
            (resource_model.queue_id,) if resource_model is not None else ()
        )
        queue.enqueue(
            agent_run_id,
            ActionProposal(
                capability=Capability.FINDING_VALIDATION,
                target_url=plan.items[0].target_url,
                method="GET",
                risk_level=RiskLevel.SAFE_ACTIVE,
                reason=f"Execute approved test plan {plan.plan_id}.",
            ),
            priority=90,
            max_attempts=2,
            depends_on=dependencies,
        )

    def _enqueue_authorization_actions(self, agent_run_id: str) -> None:
        if not self.config.authorization_fixtures:
            return
        queue = ActionQueue(self.inventory)
        validation = queue.find_latest(
            agent_run_id, Capability.FINDING_VALIDATION
        )
        resource_model = queue.find_latest(
            agent_run_id, Capability.RESOURCE_MODEL
        )
        dependencies = tuple(
            item.queue_id
            for item in (validation, resource_model)
            if item is not None
        )
        for fixture in self.config.authorization_fixtures:
            queue.enqueue(
                agent_run_id,
                ActionProposal(
                    capability=Capability.IDOR_SAFE,
                    target_url=fixture.approval_scope_url,
                    method="GET",
                    risk_level=RiskLevel.SAFE_ACTIVE,
                    reason=(
                        "Compare the explicit owner-only fixture with declared "
                        f"non-owner actors: {fixture.fixture_id}."
                    ),
                    actor_id=fixture.owner_actor,
                    fixture_id=fixture.fixture_id,
                ),
                priority=80,
                max_attempts=1,
                depends_on=dependencies,
            )

    def _enqueue_injection_actions(
        self,
        agent_run_id: str,
        exploration_run_id: str | None,
        selected_candidate_ids: tuple[str, ...] | None = None,
    ) -> None:
        fixtures = self._injection_fixtures(exploration_run_id)
        if not fixtures:
            return
        queue = ActionQueue(self.inventory)
        validation = queue.find_latest(
            agent_run_id, Capability.FINDING_VALIDATION
        )
        resource_model = queue.find_latest(
            agent_run_id, Capability.RESOURCE_MODEL
        )
        dependencies = tuple(
            item.queue_id
            for item in (validation, resource_model)
            if item is not None
        )
        capability_by_check = {
            "sqli": Capability.SQLI_SAFE,
            "xss_reflection": Capability.XSS_SAFE,
        }
        for fixture in fixtures:
            for check in sorted(fixture.checks):
                if (
                    selected_candidate_ids is not None
                    and check == "sqli"
                    and fixture.fixture_id not in selected_candidate_ids
                ):
                    continue
                capability = capability_by_check[check]
                queue.enqueue(
                    agent_run_id,
                    ActionProposal(
                        capability=capability,
                        target_url=fixture.approval_scope_url,
                        method="GET",
                        risk_level=RiskLevel.SAFE_ACTIVE,
                        reason=(
                            "Run a bounded, non-state-changing query parameter "
                            f"probe for fixture {fixture.fixture_id}: {check}."
                        ),
                        actor_id=fixture.actor_id,
                        fixture_id=fixture.fixture_id,
                    ),
                    priority=(82 if capability is Capability.SQLI_SAFE else 81),
                    max_attempts=1,
                    depends_on=dependencies,
                )

    def _injection_fixtures(
        self, exploration_run_id: str | None
    ) -> tuple[InjectionFixture, ...]:
        automatic = (
            discover_injection_fixtures(
                self.config,
                self.inventory.list_api_request_schemas(exploration_run_id),
            )
            if exploration_run_id is not None
            else ()
        )
        return (*self.config.injection_fixtures, *automatic)

    def _completed_validation_id(self, agent_run_id: str) -> str | None:
        for action in reversed(self.inventory.list_action_results(agent_run_id)):
            if (
                action["capability"] == Capability.FINDING_VALIDATION.value
                and action["status"] == "SUCCEEDED"
            ):
                value = action["output"].get("validation_id")
                return str(value) if value else None
        return None

    def _plan(self, exploration_run_id: str | None = None) -> TestPlan:
        targets = list(self.config.targets)
        candidates = (
            self.inventory.page_urls(exploration_run_id, self.config.max_pages)
            if exploration_run_id is not None
            else self.inventory.latest_page_urls(self.config.max_pages)
        )
        for candidate in candidates:
            if self.scope_guard.evaluate(candidate, "GET").allowed:
                targets.append(candidate)
        return RulesPlanner(self.config).plan(targets)

    async def _plan_with_provider(
        self, exploration_run_id: str | None = None
    ) -> TestPlan:
        """Plan hardening checks and optionally select SQLi candidates with AI."""

        base_plan = self._plan(exploration_run_id)
        fixtures = self._injection_fixtures(exploration_run_id)
        if self.config.planner_mode == "rules" or not fixtures:
            return base_plan
        context = planning_context(
            tuple(fixture for fixture in fixtures if "sqli" in fixture.checks),
            remaining_requests=self.config.max_requests,
            planning_round=0,
        )
        outcome = await HybridPlanner(
            self.config,
            self.audit_logger,
            provider=self.planner_provider,
        ).plan(context, api_calls_used=0)
        selected = tuple(
            action.candidate_id
            for action in outcome.decision.selected_actions
            if action.action == "run_sqli_safe"
        )
        return base_plan.model_copy(
            update={
                "provider": self.config.planner_mode,
                "planner_provider": outcome.provider,
                "model": outcome.model,
                "selected_candidate_ids": selected,
                "planner_fallback_used": outcome.fallback_used,
                "planner_round": context.planning_round,
                "ai_api_calls": outcome.usage.api_calls,
                "ai_input_tokens": outcome.usage.input_tokens,
                "ai_output_tokens": outcome.usage.output_tokens,
            }
        )

    async def _maybe_replan(
        self,
        run: AgentRun,
        validation_id: str,
    ) -> TestPlan | None:
        """Select untested SQLi candidates for at most a few bounded rounds.

        Replanning is deliberately narrow: it cannot add a new capability, alter
        a method, or manufacture a fixture.  The deterministic rules provider is
        used when the optional AI provider is unavailable or returns an invalid
        decision.  A confirmed SQLi finding ends the loop immediately.
        """

        if self.config.planner_mode == "rules":
            return None
        if run.plan_json is None or run.exploration_run_id is None:
            return None
        plan = TestPlan.model_validate_json(run.plan_json)
        next_round = plan.planner_round + 1
        if next_round >= self.config.ai.max_planning_rounds:
            return None
        findings = self.inventory.list_findings(validation_id)
        if any(
            item["status"] == "CONFIRMED" and str(item["category"]).lower() == "sqli"
            for item in findings
        ):
            return None

        fixtures = tuple(
            fixture
            for fixture in self._injection_fixtures(run.exploration_run_id)
            if "sqli" in fixture.checks
        )
        if not fixtures:
            return None
        tested_ids: set[str] = set()
        prior_results: list[dict[str, object]] = []
        for action in self.inventory.list_action_results(run.run_id):
            if (
                action["capability"] != Capability.SQLI_SAFE.value
                or action["status"] != "SUCCEEDED"
            ):
                continue
            output = action["output"]
            if not isinstance(output, dict):
                continue
            fixture_id = output.get("fixture_id")
            if not isinstance(fixture_id, str):
                continue
            tested_ids.add(fixture_id)
            prior_results.append(
                {
                    "fixture_id": fixture_id,
                    "confirmed": bool(output.get("confirmed", False)),
                    "confirmed_modes": tuple(
                        str(mode)
                        for mode in output.get("confirmed_modes", ())
                        if isinstance(mode, str)
                    ),
                }
            )
        remaining = tuple(
            fixture for fixture in fixtures if fixture.fixture_id not in tested_ids
        )
        if not remaining:
            return None
        context = planning_context(
            remaining,
            remaining_requests=max(0, self.config.max_requests - len(tested_ids)),
            planning_round=next_round,
            prior_results=tuple(prior_results[-50:]),
        )
        outcome = await HybridPlanner(
            self.config,
            self.audit_logger,
            provider=self.planner_provider,
        ).plan(context, api_calls_used=plan.ai_api_calls)
        selected = tuple(
            action.candidate_id
            for action in outcome.decision.selected_actions
            if action.action == "run_sqli_safe"
        )
        if not selected:
            self.audit_logger.emit(
                "planner_finished",
                run_id=run.run_id,
                planning_round=next_round,
                provider=outcome.provider,
                remaining_candidates=len(remaining),
            )
            return None
        self.audit_logger.emit(
            "planner_replanned",
            run_id=run.run_id,
            planning_round=next_round,
            provider=outcome.provider,
            fallback_used=outcome.fallback_used,
            remaining_candidates=len(remaining),
            selected_candidates=len(selected),
        )
        return plan.model_copy(
            update={
                "provider": self.config.planner_mode,
                "planner_provider": outcome.provider,
                "model": outcome.model,
                "selected_candidate_ids": selected,
                "planner_fallback_used": outcome.fallback_used,
                "planner_round": next_round,
                "ai_api_calls": plan.ai_api_calls + outcome.usage.api_calls,
                "ai_input_tokens": plan.ai_input_tokens + outcome.usage.input_tokens,
                "ai_output_tokens": plan.ai_output_tokens + outcome.usage.output_tokens,
            }
        )

    async def _validate(
        self,
        plan: TestPlan,
        budget: RequestBudget | PersistentRequestBudget | None = None,
    ) -> str:
        summary, _ = await FindingValidator(
            self.config,
            self.inventory,
            self.scope_guard,
            self.audit_logger,
            budget=budget,
        ).validate(plan)
        return summary.validation_id

    def _run_budget(self, agent_run_id: str) -> PersistentRequestBudget:
        return PersistentRequestBudget.from_config(
            agent_run_id,
            self.config,
            self.inventory,
            self.audit_logger,
        )

    def _actor_session_pool(
        self,
        budget: RequestBudget | PersistentRequestBudget,
    ) -> ActorSessionPool:
        return ActorSessionPool(
            self.config,
            NetworkGate(self.scope_guard, budget, self.audit_logger),
            self.audit_logger,
        )

    def _model_attack_surface(self, run: AgentRun) -> AttackSurfaceSummary:
        if run.exploration_run_id is None:
            raise RuntimeError("attack-surface modeling has no exploration run id")
        counts = self.inventory.counts(run.exploration_run_id)
        actor_output = self._latest_action_output(
            run.run_id, Capability.AUTH_SESSION
        )
        return AttackSurfaceSummary(
            exploration_run_id=run.exploration_run_id,
            targets=len(self.config.targets),
            pages=counts["pages"],
            forms=counts["forms"],
            endpoints=counts["endpoints"],
            api_request_schemas=self.inventory.api_request_schema_count(
                run.exploration_run_id
            ),
            resources=self.inventory.resource_count(run.exploration_run_id),
            ownership_assertions=self.inventory.resource_ownership_count(
                run.exploration_run_id
            ),
            actors_configured=int(actor_output.get("configured", 0)),
            actor_credentials_available=int(
                actor_output.get("credentials_available", 0)
            ),
            actors_authenticated=int(actor_output.get("authenticated", 0)),
            deferred_links=counts["deferred_links"],
            injection_candidates=len(
                self._injection_fixtures(run.exploration_run_id)
            ),
        )

    def _latest_action_output(
        self, agent_run_id: str, capability: Capability
    ) -> dict[str, object]:
        for action in reversed(self.inventory.list_action_results(agent_run_id)):
            if (
                action["capability"] == capability.value
                and action["status"] == "SUCCEEDED"
            ):
                return dict(action["output"])
        return {}

    def _decide_exploitability(self, validation_id: str) -> ExploitabilityDecision:
        findings = self.inventory.list_findings(validation_id)
        confirmed = sum(item["status"] == "CONFIRMED" for item in findings)
        inconclusive = sum(item["status"] == "INCONCLUSIVE" for item in findings)
        if confirmed:
            demonstrated_categories = {
                "idor",
                "bola",
                "sqli",
                "csrf",
                "ssrf",
                "path_traversal",
                "file_upload",
                "business_logic",
                "xss",
            }
            demonstrated_findings = sum(
                item["status"] == "CONFIRMED"
                and str(item["category"]).lower() in demonstrated_categories
                for item in findings
            )
            potential_categories = {"xss_reflection", "xss_candidate"}
            potential_findings = sum(
                item["status"] == "CONFIRMED"
                and str(item["category"]).lower() in potential_categories
                for item in findings
            )
            if demonstrated_findings:
                return ExploitabilityDecision(
                    verdict=ExploitabilityVerdict.CONFIRMED,
                    confirmed_findings=confirmed,
                    inconclusive_findings=inconclusive,
                    bounded_exploit_demonstrated=True,
                    rationale=(
                        "A bounded, approved probe repeatedly demonstrated an exploit "
                        "primitive with its negative control passing."
                    ),
                )
            if potential_findings:
                return ExploitabilityDecision(
                    verdict=ExploitabilityVerdict.POTENTIAL,
                    confirmed_findings=confirmed,
                    inconclusive_findings=inconclusive,
                    rationale=(
                        "A bounded probe confirmed unsafe reflected markup, but script "
                        "execution was not attempted and exploitability remains potential."
                    ),
                )
            return ExploitabilityDecision(
                verdict=ExploitabilityVerdict.HARDENING_GAPS,
                confirmed_findings=confirmed,
                inconclusive_findings=inconclusive,
                rationale=(
                    "Repeated checks confirmed configuration or hardening findings, "
                    "but none demonstrates an exploit primitive."
                ),
            )
        return ExploitabilityDecision(
            verdict=ExploitabilityVerdict.NOT_DEMONSTRATED,
            confirmed_findings=0,
            inconclusive_findings=inconclusive,
            rationale=(
                "The current safe checks did not produce a confirmed finding. This "
                "does not prove that the target has no exploitable vulnerability."
            ),
        )

    def _assert_cleanup_complete(self, agent_run_id: str) -> None:
        cleanup_tasks = CleanupQueue(self.inventory).incomplete(agent_run_id)
        if cleanup_tasks:
            details = ", ".join(
                f"{task.capability.value}:{task.status.value}"
                for task in cleanup_tasks
            )
            raise RuntimeError("cleanup tasks are incomplete: " + details)
        required_actions = {
            str(action["action_id"]): Capability(str(action["capability"]))
            for action in self.inventory.list_action_results(agent_run_id)
            if action["status"] == "SUCCEEDED"
            and CAPABILITY_CATALOG[Capability(str(action["capability"]))].cleanup_required
        }
        receipt_action_ids = {
            task.parent_action_id
            for row in self.inventory.connection.execute(
                "SELECT * FROM cleanup_tasks WHERE run_id = ?",
                (agent_run_id,),
            ).fetchall()
            if (task := CleanupQueue._model(row)).status.value == "SUCCEEDED"
        }
        missing = set(required_actions) - receipt_action_ids
        if missing:
            names = ", ".join(
                sorted(required_actions[action_id].value for action_id in missing)
            )
            raise RuntimeError(
                "cleanup receipts are required before reporting: " + names
            )

    async def _report(self, run: AgentRun) -> Path:
        if run.validation_id is None:
            raise RuntimeError("cannot report without a validation id")
        output_path = (
            self.config.inventory_db_path.parent
            / f"findings-{run.validation_id}.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        attack_surface = (
            json.loads(run.attack_surface_json) if run.attack_surface_json else None
        )
        decision = (
            json.loads(run.exploitability_decision_json)
            if run.exploitability_decision_json
            else self._decide_exploitability(run.validation_id).model_dump(mode="json")
        )
        api_schemas = (
            self.inventory.list_api_request_schemas(run.exploration_run_id)
            if run.exploration_run_id
            else []
        )
        resources = (
            self.inventory.list_resources(run.exploration_run_id)
            if run.exploration_run_id
            else []
        )
        ownership = (
            self.inventory.list_resource_ownership(run.exploration_run_id)
            if run.exploration_run_id
            else []
        )
        actor_sessions = self._latest_action_output(
            run.run_id, Capability.AUTH_SESSION
        )
        authorization_tests = [
            action["output"]
            for action in self.inventory.list_action_results(run.run_id)
            if action["capability"] == Capability.IDOR_SAFE.value
            and action["status"] == "SUCCEEDED"
        ]
        injection_tests = [
            action["output"]
            for action in self.inventory.list_action_results(run.run_id)
            if action["capability"]
            in {Capability.SQLI_SAFE.value, Capability.XSS_SAFE.value}
            and action["status"] == "SUCCEEDED"
        ]
        schema_reports = []
        for schema in api_schemas:
            item = schema.model_dump(mode="json")
            item["endpoint_url"] = template_endpoint_url(
                str(item["endpoint_url"])
            )
            if item.get("source_page"):
                item["source_page"] = template_endpoint_url(
                    str(item["source_page"])
                )
            schema_reports.append(item)
        finding_reports = self.inventory.list_findings(run.validation_id)
        for finding in finding_reports:
            finding["target_url"] = template_endpoint_url(
                str(finding["target_url"])
            )
        planning = (
            json.loads(run.plan_json)
            if run.plan_json
            else {"provider": "rules", "planner_round": 0}
        )
        markdown_path = output_path.with_suffix(".md")
        report_payload: dict[str, object] = {
            "report_version": "orchestrator-report-v3",
            "run_id": run.run_id,
            "engagement_name": run.engagement_name,
            "attack_surface": attack_surface,
            "planning": {
                "mode": planning.get("provider", "rules"),
                "provider": planning.get(
                    "planner_provider", planning.get("provider", "rules")
                ),
                "model": planning.get("model", "deterministic"),
                "planner_round": planning.get("planner_round", 0),
                "fallback_used": planning.get("planner_fallback_used", False),
                "selected_candidate_ids": planning.get("selected_candidate_ids") or [],
                "api_calls": planning.get("ai_api_calls", 0),
                "input_tokens": planning.get("ai_input_tokens", 0),
                "output_tokens": planning.get("ai_output_tokens", 0),
            },
            "api_request_schemas": schema_reports,
            "resources": [
                resource.model_dump(mode="json") for resource in resources
            ],
            "resource_ownership": [
                record.model_dump(mode="json") for record in ownership
            ],
            "actor_sessions": actor_sessions,
            "authorization_tests": authorization_tests,
            "injection_tests": injection_tests,
            "exploitability_decision": decision,
            "findings": finding_reports,
            "markdown_report_path": str(markdown_path),
        }
        ai_explanation: dict[str, object] | None = None
        ai_report_provenance: dict[str, object] = {
            "enabled": self.config.ai.report_writer_enabled,
            "provider": (
                self.config.ai.provider
                if self.config.ai.report_writer_enabled
                else "none"
            ),
            "model": self.config.ai.model or None,
            "fallback_used": False,
            "api_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        if self.config.ai.report_writer_enabled:
            report_writer: OpenAICompatibleReportWriter | None = None
            try:
                report_writer = OpenAICompatibleReportWriter(
                    self.config.ai, self.audit_logger
                )
                ai_explanation = await report_writer.explain(report_payload)
                verdict = str(decision.get("verdict", ""))
                explanation_text = " ".join(
                    str(value) for value in ai_explanation.values()
                ).lower()
                if verdict != "CONFIRMED" and "confirmed" in explanation_text:
                    self.audit_logger.emit(
                        "ai_report_explanation_fallback",
                        reason="machine_verdict_conflict",
                    )
                    ai_explanation = None
            except (AIProviderError, ValueError, TypeError) as exc:
                self.audit_logger.emit(
                    "ai_report_explanation_fallback",
                    reason=type(exc).__name__,
                )
            if report_writer is not None:
                ai_report_provenance.update(
                    {
                        "api_calls": report_writer.last_usage.api_calls,
                        "input_tokens": report_writer.last_usage.input_tokens,
                        "output_tokens": report_writer.last_usage.output_tokens,
                    }
                )
            ai_report_provenance["fallback_used"] = ai_explanation is None
        report_payload["ai_report"] = ai_report_provenance
        report_payload["ai_explanation"] = ai_explanation
        write_json_report(output_path, report_payload)
        write_markdown_report(
            markdown_path,
            report_payload,
            ai_explanation=ai_explanation,
        )
        return output_path

    def _save(self, run: AgentRun, **updates: object) -> AgentRun:
        updated = run.model_copy(update={**updates, "updated_at": utc_now()})
        self.inventory.save_agent_run(updated)
        self.audit_logger.emit(
            "agent_state_changed",
            run_id=run.run_id,
            previous=run.status.value,
            current=updated.status.value,
        )
        return updated
