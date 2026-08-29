from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.investigation import InvestigationTaskStatus
from cicd_redteam_agent.domain.investigation_policy import InvestigationTransition
from cicd_redteam_agent.domain.models import (
    AttackerProfile,
    PermissionLevel,
    RepositoryWorkflowPermissionDefault,
)
from cicd_redteam_agent.framework.capabilities import CapabilityContext
from cicd_redteam_agent.framework.cicd_usecase import (
    MAX_EXTERNAL_OBSERVATIONS_BEFORE_MODEL,
    CICDUseCase,
)
from cicd_redteam_agent.framework.environment_observer import FakeRepositoryPermissionObserver
from cicd_redteam_agent.framework.evidence_resolver import ResolverRegistry
from cicd_redteam_agent.framework.github_resolvers import GitHubCompositeResolver
from cicd_redteam_agent.framework.investigation_runtime import create_red_team_session
from cicd_redteam_agent.framework.runtime_support import AgentRuntimeMetrics
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURES = Path(__file__).parents[1] / "fixtures"
REPOSITORY_DEFAULT_MISSING = "repository default GITHUB_TOKEN permissions"


class ObservationLoopLimits:
    def __init__(self, max_rounds: int = 2) -> None:
        self.max_rounds = max_rounds
        self.rounds = 0
        self.tokens = 0
        self.cost = 0.0
        self.reason: str | None = None
        self.started = False
        self.completed = False

    def start(self) -> None:
        self.started = True

    def reached(self) -> bool:
        if self.rounds >= self.max_rounds:
            self.reason = f"Reached maximum rounds ({self.max_rounds})"
            return True
        return False

    def complete(self) -> None:
        self.completed = True


class ObservationAwareAgent:
    last_round_tokens = 0

    def __init__(self, context: CapabilityContext) -> None:
        self.context = context
        self.runtime_metrics = AgentRuntimeMetrics()
        self._prompt_history: list[object] = []
        self.active_round = 0
        self.log = None
        self.saw_observed_state = False
        self.saw_investigation_policy = False
        self.state_object_id: int | None = None

    async def before_run(self, _limits: ObservationLoopLimits) -> None:
        return None

    async def perform_round(self, limits: ObservationLoopLimits) -> None:
        self.state_object_id = id(self.context.state)
        contexts = self.context.state.job_execution_contexts
        self.saw_observed_state = bool(contexts) and all(
            context.effective_permissions.get("contents") is PermissionLevel.READ
            and REPOSITORY_DEFAULT_MISSING not in context.effective_permissions.missing_evidence
            for context in contexts
        )
        self.saw_investigation_policy = self.context.investigation_policy is not None
        limits.rounds += 1
        self.context.submitted = True


class UnknownAwareAgent(ObservationAwareAgent):
    async def perform_round(self, limits: ObservationLoopLimits) -> None:
        self.state_object_id = id(self.context.state)
        self.saw_observed_state = any(
            REPOSITORY_DEFAULT_MISSING in context.effective_permissions.missing_evidence
            for context in self.context.state.job_execution_contexts
        )
        limits.rounds += 1
        self.context.submitted = True


class FailingObserver:
    async def observe(self, *_args, **_kwargs):
        raise RuntimeError("read-only transport failed")


class ObservationBudgetProbeUseCase(CICDUseCase):
    policy_calls = 0
    observation_calls = 0

    def _refresh_investigation_policy(self, *, runtime=None):
        del runtime
        self.policy_calls += 1
        transition = (
            InvestigationTransition.OBSERVE
            if self.policy_calls <= 4
            else InvestigationTransition.SUBMIT
        )
        return SimpleNamespace(transition=transition)

    async def _run_pre_model_observations(self) -> int:
        self.observation_calls += 1
        return 1


async def _candidate_state(tmp_path: Path):
    repository = FIXTURES / "reasoning-matrix" / "gha-05" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(
            output_dir=tmp_path / "deterministic-out",
            attacker_profile=AttackerProfile.FORK_PR,
        ),
    )
    return repository, report.state


@pytest.mark.asyncio
async def test_registry_policy_cycle_builds_frontiers_once(monkeypatch, tmp_path: Path) -> None:
    import cicd_redteam_agent.framework.cicd_usecase as usecase_module

    repository, state = await _candidate_state(tmp_path)
    context = CapabilityContext(state, RepositoryBoundary(repository))
    agent = ObservationAwareAgent(context)
    session = create_red_team_session(state)
    calls = 0
    real_build = usecase_module.build_attack_frontiers

    def counted_build(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(usecase_module, "build_attack_frontiers", counted_build)
    usecase = CICDUseCase(
        agent,
        context,
        ObservationLoopLimits(),
        investigation_session=session,
        environment_observer=ResolverRegistry(),
        attacker_profile=AttackerProfile.FORK_PR,
    )

    usecase._refresh_investigation_policy()

    assert calls == 1
    assert agent.runtime_metrics.frontier_searches == 1


@pytest.mark.asyncio
async def test_runtime_observes_before_first_model_round_and_syncs_live_state(
    tmp_path: Path,
) -> None:
    repository, state = await _candidate_state(tmp_path)
    boundary = RepositoryBoundary(repository)
    context = CapabilityContext(state, boundary)
    agent = ObservationAwareAgent(context)
    session = create_red_team_session(state)
    original_state_id = id(state)

    reason = await CICDUseCase(
        agent,
        context,
        ObservationLoopLimits(),
        investigation_session=session,
        environment_observer=FakeRepositoryPermissionObserver(
            RepositoryWorkflowPermissionDefault.READ
        ),
        attacker_profile=AttackerProfile.FORK_PR,
    ).run()

    assert reason == "proposal_validated"
    assert agent.saw_observed_state
    assert agent.saw_investigation_policy
    assert agent.state_object_id == original_state_id
    assert id(context.state) == original_state_id
    assert session.knowledge_state.revision == 1
    assert (
        session.knowledge_state.repository_default_workflow_permissions
        is RepositoryWorkflowPermissionDefault.READ
    )
    assert agent.runtime_metrics.investigation_tasks_completed == 1
    assert agent.runtime_metrics.observations_committed == 1
    assert agent.runtime_metrics.semantic_refreshes == 1
    assert all(
        task.status is InvestigationTaskStatus.COMPLETED
        for task in session.investigation_state.tasks.values()
    )


@pytest.mark.asyncio
async def test_runtime_preserves_unknown_and_continues_when_observer_fails(
    tmp_path: Path,
) -> None:
    repository, state = await _candidate_state(tmp_path)
    boundary = RepositoryBoundary(repository)
    context = CapabilityContext(state, boundary)
    agent = UnknownAwareAgent(context)
    session = create_red_team_session(state)
    usecase = CICDUseCase(
        agent,
        context,
        ObservationLoopLimits(),
        investigation_session=session,
        environment_observer=FailingObserver(),
        attacker_profile=AttackerProfile.FORK_PR,
    )

    reason = await usecase.run()

    assert reason == "proposal_validated"
    assert agent.saw_observed_state
    assert session.knowledge_state.revision == 0
    assert agent.runtime_metrics.investigation_tasks_completed == 0
    assert agent.runtime_metrics.observations_committed == 0
    assert agent.runtime_metrics.semantic_refreshes == 0
    assert any("preserving UNKNOWN" in warning for warning in usecase.warnings)
    assert all(
        task.status is InvestigationTaskStatus.BLOCKED
        for task in session.investigation_state.tasks.values()
    )


def test_runtime_accumulates_resolver_metrics_as_deltas(tmp_path: Path) -> None:
    repository = FIXTURES / "reasoning-matrix" / "gha-05" / "candidate"
    boundary = RepositoryBoundary(repository)
    # The metric bridge does not execute the transport; it only snapshots resolver counters.
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
    )
    registry = ResolverRegistry((resolver,))
    minimal_state = create_initial_state(boundary)
    context = CapabilityContext(minimal_state, boundary)
    agent = UnknownAwareAgent(context)
    agent.runtime_metrics.external_network_requests = 7
    usecase = CICDUseCase(
        agent,
        context,
        ObservationLoopLimits(),
        environment_observer=registry,
    )

    resolver.metrics.network_requests = 2
    resolver.metrics.cache_hits = 1
    resolver.metrics.rate_limit_remaining = 4_990
    usecase._sync_resolver_metrics()
    assert agent.runtime_metrics.external_network_requests == 9
    assert agent.runtime_metrics.external_cache_hits == 1
    assert agent.runtime_metrics.external_rate_limit_remaining == 4_990

    resolver.metrics.network_requests = 5
    resolver.metrics.cache_hits = 3
    resolver.metrics.not_modified_hits = 1
    resolver.metrics.retries = 1
    usecase._sync_resolver_metrics()
    assert agent.runtime_metrics.external_network_requests == 12
    assert agent.runtime_metrics.external_cache_hits == 3
    assert agent.runtime_metrics.external_not_modified_hits == 1
    assert agent.runtime_metrics.external_retries == 1


@pytest.mark.asyncio
async def test_runtime_caps_external_observations_before_model_round(tmp_path: Path) -> None:
    repository, state = await _candidate_state(tmp_path)
    boundary = RepositoryBoundary(repository)
    context = CapabilityContext(state, boundary)
    agent = UnknownAwareAgent(context)
    usecase = ObservationBudgetProbeUseCase(
        agent,
        context,
        ObservationLoopLimits(),
    )

    reason = await usecase.run()

    assert reason == "proposal_validated"
    assert usecase.observation_calls == MAX_EXTERNAL_OBSERVATIONS_BEFORE_MODEL
    assert (
        MAX_EXTERNAL_OBSERVATIONS_BEFORE_MODEL
        * GitHubCompositeResolver.descriptor.max_calls
        <= 8
    )
    assert agent.active_round == 1
