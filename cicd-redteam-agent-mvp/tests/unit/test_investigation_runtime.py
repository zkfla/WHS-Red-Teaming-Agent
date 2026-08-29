from __future__ import annotations

from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.graph_frontier import build_attack_frontiers
from cicd_redteam_agent.domain.investigation import (
    DeploymentBranchPolicyMode,
    EnvironmentProtectionPolicy,
    InvestigationTaskKind,
    InvestigationTaskStatus,
)
from cicd_redteam_agent.domain.models import (
    AttackerProfile,
    PermissionLevel,
    RepositoryWorkflowPermissionDefault,
    TriState,
)
from cicd_redteam_agent.framework.environment_observer import (
    FakeEnvironmentProtectionObserver,
    FakeRepositoryPermissionObserver,
)
from cicd_redteam_agent.framework.evidence_resolver import ResolverRegistry
from cicd_redteam_agent.framework.investigation_runtime import (
    create_red_team_session,
    plan_next_task,
    run_observation_once,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURES = Path(__file__).parents[1] / "fixtures"
REPOSITORY_DEFAULT_MISSING = "repository default GITHUB_TOKEN permissions"


@pytest.mark.asyncio
async def test_registry_planner_does_not_fall_back_to_legacy_tasks_for_empty_portfolio(
    tmp_path: Path,
) -> None:
    repository = FIXTURES / "reasoning-matrix" / "gha-05" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.FORK_PR,
        ),
    )
    session = create_red_team_session(report.state)
    assert plan_next_task(session) is not None
    assert plan_next_task(
        session,
        portfolio=(),
        resolver_registry=ResolverRegistry(),
    ) is None


@pytest.mark.asyncio
async def test_observation_recomputes_semantics_and_frontier(tmp_path: Path) -> None:
    repository = FIXTURES / "reasoning-matrix" / "gha-05" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.FORK_PR,
        ),
    )
    session = create_red_team_session(report.state)
    boundary = RepositoryBoundary(repository)

    before_frontiers = build_attack_frontiers(session.analysis_state)
    assert any(
        REPOSITORY_DEFAULT_MISSING in context.effective_permissions.missing_evidence
        for context in session.analysis_state.job_execution_contexts
    )
    assert any(
        relation.relation_type == "executes_with_effective_write_github_token"
        and relation.verdict is TriState.UNKNOWN
        for relation in session.analysis_state.relations
    )

    task = plan_next_task(session)
    assert task is not None
    assert task.kind is InvestigationTaskKind.OBSERVE_REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS
    assert task.basis_relation_ids

    observation = await run_observation_once(
        session,
        FakeRepositoryPermissionObserver(RepositoryWorkflowPermissionDefault.READ),
        boundary=boundary,
        profile=AttackerProfile.FORK_PR,
    )

    assert observation is not None
    assert session.knowledge_state.revision == 1
    assert (
        session.knowledge_state.repository_default_workflow_permissions
        is RepositoryWorkflowPermissionDefault.READ
    )
    assert session.investigation_state.active_lease is None
    assert (
        session.investigation_state.tasks[task.task_id].status is InvestigationTaskStatus.COMPLETED
    )
    assert session.investigation_state.tasks[task.task_id].attempts == 1
    assert plan_next_task(session) is None

    contexts = session.analysis_state.job_execution_contexts
    assert contexts
    assert all(
        context.effective_permissions.get("contents") is PermissionLevel.READ
        for context in contexts
    )
    assert all(
        context.effective_permissions.get("packages") is PermissionLevel.READ
        for context in contexts
    )
    assert all(
        context.effective_permissions.get("id-token") is PermissionLevel.NONE
        for context in contexts
    )
    assert all(
        REPOSITORY_DEFAULT_MISSING not in context.effective_permissions.missing_evidence
        for context in contexts
    )
    assert REPOSITORY_DEFAULT_MISSING not in session.analysis_state.pending_questions
    assert not any(
        relation.relation_type == "executes_with_effective_write_github_token"
        for relation in session.analysis_state.relations
    )

    after_frontiers = build_attack_frontiers(session.analysis_state)
    assert after_frontiers != before_frontiers
    assert not any(
        missing.description == REPOSITORY_DEFAULT_MISSING
        for frontier in after_frontiers.frontiers
        for missing in frontier.missing
    )


@pytest.mark.asyncio
async def test_permissive_default_does_not_implicitly_grant_oidc(tmp_path: Path) -> None:
    repository = FIXTURES / "reasoning-matrix" / "gha-05" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.FORK_PR,
        ),
    )
    session = create_red_team_session(report.state)

    await run_observation_once(
        session,
        FakeRepositoryPermissionObserver(RepositoryWorkflowPermissionDefault.WRITE),
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.FORK_PR,
    )

    context = session.analysis_state.job_execution_contexts[0]
    assert context.effective_permissions.get("contents") is PermissionLevel.UNKNOWN
    assert context.effective_permissions.get("id-token") is PermissionLevel.NONE
    assert "fork pull-request write-token repository setting" in (
        context.effective_permissions.missing_evidence
    )
    assert "fork pull-request OIDC token availability" not in (
        context.effective_permissions.missing_evidence
    )


@pytest.mark.asyncio
async def test_environment_observation_recomputes_guards_and_replans(tmp_path: Path) -> None:
    repository = FIXTURES / "reasoning-matrix" / "gha-10" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.FORK_PR,
        ),
    )
    session = create_red_team_session(report.state)

    task = plan_next_task(session)
    assert task is not None
    assert task.kind is InvestigationTaskKind.OBSERVE_ENVIRONMENT_PROTECTION_POLICY
    assert task.environment_name == "production"
    assert task.basis_relation_ids

    policy = EnvironmentProtectionPolicy(
        environment_name="production",
        required_reviewer_count=2,
        prevent_self_review=True,
        wait_timer_minutes=15,
        deployment_branch_policy=DeploymentBranchPolicyMode.CUSTOM,
        custom_protection_rule_count=1,
    )
    observation = await run_observation_once(
        session,
        FakeEnvironmentProtectionObserver(policy),
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.FORK_PR,
    )

    assert observation is not None
    assert session.knowledge_state.revision == 1
    assert session.knowledge_state.environment_protection_policies == {"production": policy}
    relation_types = {relation.relation_type for relation in session.analysis_state.relations}
    assert "environment_protection_unknown" not in relation_types
    assert "environment_requires_deployment_review" in relation_types
    assert "environment_restricts_deployment_branches" in relation_types
    assert "environment_custom_protection_rules_enabled" in relation_types
    assert "environment_wait_timer_configured" in relation_types
    assert "GitHub environment protection settings" not in (
        session.analysis_state.pending_questions
    )
    assert "environment deployment approval state" in session.analysis_state.pending_questions
    assert plan_next_task(session) is None
