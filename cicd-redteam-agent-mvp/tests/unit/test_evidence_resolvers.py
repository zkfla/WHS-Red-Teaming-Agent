from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.event_provenance import event_principal_qualifier
from cicd_redteam_agent.domain.evidence_requirement import (
    EvidenceRequirement,
    evidence_key,
    requirement_id_for,
)
from cicd_redteam_agent.domain.execution_semantics import exact_push_branches
from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.graph_frontier import FrontierQuery, build_attack_frontiers
from cicd_redteam_agent.domain.investigation import (
    ActionsPolicyEvidence,
    EffectiveBranchRulesEvidence,
    EnvironmentBranchPatternsEvidence,
    EnvironmentPolicyEvidence,
    EventPrincipalEvidence,
    ForkPullRequestPolicyEvidence,
    GitHubEventLocator,
    GitHubOidcSubjectConfigEvidence,
    InvestigationTask,
    InvestigationTaskKind,
    KnowledgeState,
    PendingDeploymentStateEvidence,
    RepositoryActorPermissionEvidence,
    RepositoryMetadataEvidence,
    WorkflowRunEventLinkageEvidence,
    WorkflowRunIdentityEvidence,
    WorkflowRunJobsEvidence,
    WorkflowRunStatusEvidence,
)
from cicd_redteam_agent.domain.investigation_policy import select_frontier_portfolio
from cicd_redteam_agent.domain.models import (
    AttackerProfile,
    EventPrincipalRole,
    EvidenceProvider,
    EvidenceRequirementKind,
    PermissionLevel,
    TriState,
    Workflow,
)
from cicd_redteam_agent.domain.proposal_draft import indexed_relations
from cicd_redteam_agent.framework.evidence_planning import compile_evidence_requirements
from cicd_redteam_agent.framework.evidence_resolver import (
    EvidenceResolutionError,
    ResolutionFailureKind,
    ResolverRegistry,
)
from cicd_redteam_agent.framework.github_observer import GitHubHttpResponse, GitHubObserverError
from cicd_redteam_agent.framework.github_resolvers import GitHubCompositeResolver
from cicd_redteam_agent.framework.investigation_runtime import (
    create_red_team_session,
    plan_next_task,
    prepare_resume_revalidation,
    run_observation_once,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURES = Path(__file__).parents[1] / "fixtures"


@dataclass
class RoutingTransport:
    payloads: dict[str, object]
    urls: list[str] = field(default_factory=list)

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ):
        del headers
        self.urls.append(url)
        for suffix, payload in self.payloads.items():
            if url.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"unexpected GitHub URL: {url}")


@pytest.mark.asyncio
async def test_github_oidc_resolver_preserves_numeric_identity_and_subject_mode() -> None:
    resolver = GitHubCompositeResolver(
        owner="example",
        repository="repo",
        token="test-token",
        transport=RoutingTransport(
            {
                "/repos/example/repo": {
                    "id": 22,
                    "owner": {"id": 11},
                    "private": False,
                    "visibility": "public",
                    "default_branch": "main",
                    "fork": False,
                    "created_at": "2026-08-01T00:00:00Z",
                },
                "/actions/oidc/customization/sub": {
                    "use_default": True,
                    "include_claim_keys": [],
                    "use_immutable_subject": True,
                },
            }
        ),
    )
    metadata = await resolver._repository_metadata()
    subject_config = await resolver._oidc_subject_config()
    assert metadata.owner_id == 11
    assert metadata.repository_id_number == 22
    assert isinstance(subject_config, GitHubOidcSubjectConfigEvidence)
    assert subject_config.use_immutable_subject is True


@dataclass
class SequencedResponseTransport:
    responses: list[GitHubHttpResponse | Exception]
    headers: list[dict[str, str]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    async def get_response(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> GitHubHttpResponse:
        self.urls.append(url)
        self.headers.append(dict(headers))
        if not self.responses:
            raise AssertionError(f"unexpected GitHub request: {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _registry(transport) -> ResolverRegistry:
    return ResolverRegistry(
        (
            GitHubCompositeResolver(
                owner="example-org",
                repository="example-repo",
                token="test-token",
                transport=transport,
            ),
        )
    )


def _explicit_task(
    session,
    registry: ResolverRegistry,
    kind: EvidenceRequirementKind,
    *,
    subject_id: str,
    qualifier: str | None = None,
) -> InvestigationTask:
    requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(kind, subject_id, qualifier=qualifier),
        kind=kind,
        provider=EvidenceProvider.GITHUB,
        subject_id=subject_id,
        qualifier=qualifier,
        reason="test explicit typed evidence vertical slice",
    )
    descriptor = registry.descriptor_for(kind)
    assert descriptor is not None
    session.investigation_state.requirements[requirement.requirement_id] = requirement
    task = InvestigationTask(
        task_id=stable_id(
            "task",
            InvestigationTaskKind.RESOLVE_EXTERNAL_EVIDENCE.value,
            requirement.requirement_id,
            descriptor.resolver_id,
        ),
        kind=InvestigationTaskKind.RESOLVE_EXTERNAL_EVIDENCE,
        subject_id=subject_id,
        requirement_id=requirement.requirement_id,
        resolver_id=descriptor.resolver_id,
        provider=descriptor.provider,
    )
    session.investigation_state.tasks[task.task_id] = task
    return task


async def _next_observation(
    session,
    registry,
    repository: Path,
    *,
    profile: AttackerProfile = AttackerProfile.FORK_PR,
):
    result = build_attack_frontiers(session.analysis_state)
    portfolio = select_frontier_portfolio(result, session.analysis_state)
    task = plan_next_task(session, portfolio=portfolio, resolver_registry=registry)
    assert task is not None
    assert task.kind is InvestigationTaskKind.RESOLVE_EXTERNAL_EVIDENCE
    observation = await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=profile,
        task=task,
    )
    assert observation is not None
    return task, observation


def _environment_review_payloads(pending_deployments: object) -> dict[str, object]:
    return {
        "/environments/production": {
            "name": "production",
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "reviewers": [{"type": "User", "reviewer": {"login": "reviewer"}}],
                    "prevent_self_review": True,
                }
            ],
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": False,
            },
        },
        "/actions/runs/4242/pending_deployments": pending_deployments,
    }


async def _seed_environment_review_policy(
    session,
    registry: ResolverRegistry,
    repository: Path,
) -> None:
    repository_id = session.analysis_state.repository.repository_id
    task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_ENVIRONMENT_POLICY,
        subject_id=f"{repository_id}:environment:production",
        qualifier="production",
    )
    observation = await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.REPO_WRITER,
        task=task,
    )
    assert observation is not None
    assert isinstance(observation.payload, EnvironmentPolicyEvidence)


def test_exact_push_branch_scope_does_not_globalize_branch_protection() -> None:
    workflow = Workflow(
        workflow_id="wf-test",
        path=".github/workflows/test.yml",
        triggers={"push": {"branches": ["main", "release"]}},
    )
    assert exact_push_branches(workflow) == ("main", "release")
    assert exact_push_branches(
        workflow.model_copy(update={"triggers": {"push": {"branches": ["release/*"]}}})
    ) == ()
    assert exact_push_branches(
        workflow.model_copy(update={"triggers": {"push": {}}})
    ) == ()


@pytest.mark.asyncio
async def test_generic_resolver_migrates_default_permission_and_fork_policy(
    tmp_path: Path,
) -> None:
    repository = FIXTURES / "reasoning-matrix" / "gha-05" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.FORK_PR),
    )
    session = create_red_team_session(report.state)
    transport = RoutingTransport(
        {
            "/actions/permissions/workflow": {"default_workflow_permissions": "write"},
            "/repos/example-org/example-repo": {
                "private": True,
                "visibility": "private",
                "default_branch": "main",
                "fork": False,
            },
            "/actions/permissions/fork-pr-contributor-approval": {
                "approval_policy": "first_time_contributors"
            },
            "/actions/permissions/fork-pr-workflows-private-repos": {
                "run_workflows_from_fork_pull_requests": True,
                "send_write_tokens_to_workflows": False,
                "send_secrets_and_variables": False,
                "require_approval_for_fork_pr_workflows": True,
            },
        }
    )
    registry = _registry(transport)

    first_task, first = await _next_observation(session, registry, repository)
    assert first_task.requirement_id is not None
    assert first.payload is not None
    assert session.knowledge_state.repository_default_workflow_permissions.value == "write"

    _, metadata = await _next_observation(session, registry, repository)
    assert isinstance(metadata.payload, RepositoryMetadataEvidence)

    _, fork_policy = await _next_observation(session, registry, repository)
    assert isinstance(fork_policy.payload, ForkPullRequestPolicyEvidence)
    assert fork_policy.payload.send_write_tokens_to_workflows is False

    contexts = session.analysis_state.job_execution_contexts
    assert contexts
    assert all(
        context.effective_permissions.get("contents") is PermissionLevel.READ
        for context in contexts
    )
    assert all(
        "fork pull-request write-token repository setting"
        not in context.effective_permissions.missing_evidence
        for context in contexts
    )
    assert any(
        relation.relation_type == "fork_pr_workflow_requires_approval"
        for relation in session.analysis_state.relations
    )


@pytest.mark.asyncio
async def test_github_resolver_deduplicates_successful_gets_within_run() -> None:
    transport = RoutingTransport(
        {
            "/actions/permissions": {
                "enabled": True,
                "allowed_actions": "selected",
                "sha_pinning_required": False,
            },
            "/actions/permissions/selected-actions": {
                "github_owned_allowed": True,
                "verified_allowed": False,
                "patterns_allowed": ["example-org/*"],
            },
        }
    )
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )
    requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_ACTIONS_POLICY,
            "repo-test",
        ),
        kind=EvidenceRequirementKind.GITHUB_ACTIONS_POLICY,
        provider=EvidenceProvider.GITHUB,
        subject_id="repo-test",
        reason="test cache",
    )

    class DummySession:
        pass

    first = await resolver.resolve(DummySession(), requirement)
    second = await resolver.resolve(DummySession(), requirement)

    assert isinstance(first, ActionsPolicyEvidence)
    assert second == first
    assert len(transport.urls) == 2
    assert resolver.metrics.network_requests == 2
    assert resolver.metrics.cache_hits == 2
    assert resolver.metrics.current_resolution_requests == 0


@pytest.mark.asyncio
async def test_github_resolver_collects_run_identity_and_current_actor_permission() -> None:
    transport = RoutingTransport(
        {
            "/actions/runs/42": {
                "id": 42,
                "event": "issue_comment",
                "run_attempt": 2,
                "created_at": "2026-08-29T00:00:00Z",
                "actor": {"login": "maintainer", "id": 7},
                "triggering_actor": {"login": "rerunner", "id": 8},
            },
            "/collaborators/maintainer/permission": {
                "permission": "maintain",
                "role_name": "maintain",
                "user": {"login": "Maintainer", "id": 7},
            },
        }
    )
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )

    class DummySession:
        pass

    identity_requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY,
            "github:example/repo:run:42",
            qualifier="42",
        ),
        kind=EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY,
        provider=EvidenceProvider.GITHUB,
        subject_id="github:example/repo:run:42",
        qualifier="42",
        reason="test workflow run identity",
    )
    permission_requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_REPOSITORY_ACTOR_PERMISSION,
            "github:example/repo:actor:maintainer",
            qualifier="maintainer",
        ),
        kind=EvidenceRequirementKind.GITHUB_REPOSITORY_ACTOR_PERMISSION,
        provider=EvidenceProvider.GITHUB,
        subject_id="github:example/repo:actor:maintainer",
        qualifier="maintainer",
        reason="test current actor permission",
    )

    identity = await resolver.resolve(DummySession(), identity_requirement)
    permission = await resolver.resolve(DummySession(), permission_requirement)

    assert isinstance(identity, WorkflowRunIdentityEvidence)
    assert identity.actor_login == "maintainer"
    assert identity.triggering_actor_login == "rerunner"
    assert identity.run_attempt == 2
    assert isinstance(permission, RepositoryActorPermissionEvidence)
    assert permission.actor_login == "Maintainer"
    assert permission.permission == "write"
    assert permission.temporal_scope == "current"
    assert resolver.metrics.network_requests == 2


@pytest.mark.asyncio
async def test_workflow_event_linkage_uses_exact_operator_event_locator() -> None:
    transport = RoutingTransport(
        {
            "/actions/runs/42": {
                "id": 42,
                "event": "issue_comment",
                "pull_requests": [],
            }
        }
    )
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )
    locator = GitHubEventLocator(
        event_name="issue_comment",
        action="created",
        repository_full_name="example-org/example-repo",
        issue_number=17,
        pull_number=17,
        comment_id=99,
        sender_login="attacker",
        sender_id=7,
        sender_type="User",
        source_hash="a" * 64,
    )

    class DummySession:
        github_event_locator = locator
        knowledge_state = KnowledgeState()

    requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
            "github:example/repo:run:42",
            qualifier="42",
        ),
        kind=EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
        provider=EvidenceProvider.GITHUB,
        subject_id="github:example/repo:run:42",
        qualifier="42",
        reason="test exact event linkage",
    )

    linkage = await resolver.resolve(DummySession(), requirement)

    assert isinstance(linkage, WorkflowRunEventLinkageEvidence)
    assert linkage.binding == "exact"
    assert linkage.source == "operator_event_payload"
    assert linkage.comment_id == 99
    assert linkage.sender_login == "attacker"
    assert resolver.metrics.network_requests == 1


@pytest.mark.asyncio
async def test_pull_request_payload_can_bind_pull_request_target_run() -> None:
    transport = RoutingTransport(
        {
            "/actions/runs/42": {
                "id": 42,
                "event": "pull_request_target",
                "pull_requests": [{"number": 5}],
            }
        }
    )
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )
    locator = GitHubEventLocator(
        event_name="pull_request",
        action="labeled",
        repository_full_name="example-org/example-repo",
        pull_number=5,
        label_name="safe-to-test",
        sender_login="maintainer",
        sender_id=7,
        source_hash="c" * 64,
    )

    class DummySession:
        github_event_locator = locator
        knowledge_state = KnowledgeState()

    requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
            "github:example/repo:run:42",
            qualifier="42",
        ),
        kind=EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
        provider=EvidenceProvider.GITHUB,
        subject_id="github:example/repo:run:42",
        qualifier="42",
        reason="test pull_request_target event payload compatibility",
    )

    linkage = await resolver.resolve(DummySession(), requirement)

    assert linkage.event_name == "pull_request_target"
    assert linkage.action == "labeled"
    assert linkage.pull_number == 5
    assert linkage.binding == "exact"


@pytest.mark.asyncio
async def test_workflow_event_linkage_without_event_payload_is_partial_and_nonheuristic() -> None:
    transport = RoutingTransport(
        {
            "/actions/runs/42": {
                "id": 42,
                "event": "pull_request",
                "pull_requests": [{"number": 5}],
            }
        }
    )
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )

    class DummySession:
        github_event_locator = None
        knowledge_state = KnowledgeState()

    requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
            "github:example/repo:run:42",
            qualifier="42",
        ),
        kind=EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
        provider=EvidenceProvider.GITHUB,
        subject_id="github:example/repo:run:42",
        qualifier="42",
        reason="test partial event linkage",
    )

    linkage = await resolver.resolve(DummySession(), requirement)

    assert isinstance(linkage, WorkflowRunEventLinkageEvidence)
    assert linkage.binding == "partial"
    assert linkage.pull_number == 5
    assert linkage.action is None
    assert linkage.sender_login is None


@pytest.mark.asyncio
async def test_issue_comment_principal_uses_exact_comment_resource() -> None:
    transport = RoutingTransport(
        {
            "/issues/comments/99": {
                "id": 99,
                "user": {"login": "Attacker", "id": 7, "type": "User"},
                "author_association": "NONE",
                "created_at": "2026-08-29T00:00:00Z",
                "body": "/deploy",
            }
        }
    )
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )
    linkage = WorkflowRunEventLinkageEvidence(
        run_id=42,
        event_name="issue_comment",
        action="created",
        binding="exact",
        source="operator_event_payload",
        issue_number=17,
        comment_id=99,
        sender_login="Attacker",
        sender_id=7,
    )

    class DummySession:
        github_event_locator = None
        knowledge_state = KnowledgeState(
            external_evidence={
                evidence_key(
                    EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
                    "github:example/repo:run:42",
                    qualifier="42",
                ): linkage
            }
        )

    qualifier = event_principal_qualifier(EventPrincipalRole.COMMENT_AUTHOR, linkage)
    assert qualifier is not None
    requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL,
            "github:example/repo:run:42:principal:comment_author",
            qualifier=qualifier,
        ),
        kind=EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL,
        provider=EvidenceProvider.GITHUB,
        subject_id="github:example/repo:run:42:principal:comment_author",
        qualifier=qualifier,
        reason="test comment author",
    )

    principal = await resolver.resolve(DummySession(), requirement)

    assert isinstance(principal, EventPrincipalEvidence)
    assert principal.principal_role is EventPrincipalRole.COMMENT_AUTHOR
    assert principal.login == "Attacker"
    assert principal.actor_id == 7
    assert principal.author_association == "NONE"
    assert principal.resource_id == "id:99"
    assert resolver.metrics.network_requests == 1


@pytest.mark.asyncio
async def test_label_actor_uses_exact_event_sender_without_timeline_guessing() -> None:
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=RoutingTransport({}),
    )
    linkage = WorkflowRunEventLinkageEvidence(
        run_id=42,
        event_name="pull_request_target",
        action="labeled",
        binding="exact",
        source="operator_event_payload",
        pull_number=5,
        label_name="safe-to-test",
        sender_login="maintainer",
        sender_id=21,
        sender_type="User",
    )

    class DummySession:
        github_event_locator = None
        knowledge_state = KnowledgeState(
            external_evidence={
                evidence_key(
                    EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
                    "github:example/repo:run:42",
                    qualifier="42",
                ): linkage
            }
        )

    qualifier = event_principal_qualifier(EventPrincipalRole.LABEL_ACTOR, linkage)
    assert qualifier is not None
    requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL,
            "github:example/repo:run:42:principal:label_actor",
            qualifier=qualifier,
        ),
        kind=EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL,
        provider=EvidenceProvider.GITHUB,
        subject_id="github:example/repo:run:42:principal:label_actor",
        qualifier=qualifier,
        reason="test exact label actor",
    )

    principal = await resolver.resolve(DummySession(), requirement)

    assert principal.principal_role is EventPrincipalRole.LABEL_ACTOR
    assert principal.login == "maintainer"
    assert principal.event_action == "labeled"
    assert resolver.metrics.network_requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "endpoint", "locator_fields", "payload"),
    [
        (
            EventPrincipalRole.PULL_REQUEST_AUTHOR,
            "/pulls/5",
            {"pull_number": 5},
            {"number": 5, "user": {"login": "pr-author", "id": 31, "type": "User"}},
        ),
        (
            EventPrincipalRole.REVIEW_AUTHOR,
            "/pulls/5/reviews/13",
            {"pull_number": 5, "review_id": 13},
            {"id": 13, "user": {"login": "reviewer", "id": 32, "type": "User"}},
        ),
        (
            EventPrincipalRole.REVIEW_COMMENT_AUTHOR,
            "/pulls/comments/77",
            {"review_comment_id": 77},
            {"id": 77, "user": {"login": "commenter", "id": 33, "type": "User"}},
        ),
    ],
)
async def test_pr_and_review_principals_use_exact_rest_resources(
    role: EventPrincipalRole,
    endpoint: str,
    locator_fields: dict[str, int],
    payload: dict[str, object],
) -> None:
    transport = RoutingTransport({endpoint: payload})
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )
    event_name = {
        EventPrincipalRole.PULL_REQUEST_AUTHOR: "pull_request",
        EventPrincipalRole.REVIEW_AUTHOR: "pull_request_review",
        EventPrincipalRole.REVIEW_COMMENT_AUTHOR: "pull_request_review_comment",
    }[role]
    linkage = WorkflowRunEventLinkageEvidence(
        run_id=42,
        event_name=event_name,
        action="created" if role is not EventPrincipalRole.REVIEW_AUTHOR else "submitted",
        binding="exact",
        source="operator_event_payload",
        **locator_fields,
    )

    class DummySession:
        github_event_locator = None
        knowledge_state = KnowledgeState(
            external_evidence={
                evidence_key(
                    EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
                    "github:example/repo:run:42",
                    qualifier="42",
                ): linkage
            }
        )

    qualifier = event_principal_qualifier(role, linkage)
    assert qualifier is not None
    requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL,
            f"github:example/repo:run:42:principal:{role.value}",
            qualifier=qualifier,
        ),
        kind=EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL,
        provider=EvidenceProvider.GITHUB,
        subject_id=f"github:example/repo:run:42:principal:{role.value}",
        qualifier=qualifier,
        reason="test exact event principal resource",
    )

    principal = await resolver.resolve(DummySession(), requirement)

    assert principal.principal_role is role
    assert principal.login is not None
    assert resolver.metrics.network_requests == 1


@pytest.mark.asyncio
async def test_issue_comment_frontier_resolves_linkage_principal_and_current_permission(
    tmp_path: Path,
) -> None:
    repository = FIXTURES / "event-principal" / "issue-comment"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.FORK_PR),
    )
    locator = GitHubEventLocator(
        event_name="issue_comment",
        action="created",
        repository_full_name="example-org/example-repo",
        issue_number=17,
        comment_id=99,
        sender_login="maintainer",
        sender_id=7,
        sender_type="User",
        source_hash="b" * 64,
    )
    session = create_red_team_session(
        report.state,
        github_run_id=42,
        github_event_locator=locator,
    )
    transport = RoutingTransport(
        {
            "/actions/runs/42": {
                "id": 42,
                "event": "issue_comment",
                "pull_requests": [],
            },
            "/issues/comments/99": {
                "id": 99,
                "user": {"login": "maintainer", "id": 7, "type": "User"},
                "author_association": "MEMBER",
                "created_at": "2026-08-29T00:00:00Z",
            },
            "/collaborators/maintainer/permission": {
                "permission": "write",
                "role_name": "write",
                "user": {"login": "maintainer", "id": 7},
            },
        }
    )
    registry = _registry(transport)
    observed_kinds: list[EvidenceRequirementKind] = []

    for _ in range(3):
        frontiers = build_attack_frontiers(session.analysis_state)
        portfolio = select_frontier_portfolio(frontiers, session.analysis_state)
        task = plan_next_task(session, portfolio=portfolio, resolver_registry=registry)
        assert task is not None
        assert task.requirement_id is not None
        requirement = session.investigation_state.requirements[task.requirement_id]
        observed_kinds.append(requirement.kind)
        observation = await run_observation_once(
            session,
            registry,
            boundary=RepositoryBoundary(repository),
            profile=AttackerProfile.FORK_PR,
            task=task,
        )
        assert observation is not None

    assert observed_kinds == [
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
        EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL,
        EvidenceRequirementKind.GITHUB_REPOSITORY_ACTOR_PERMISSION,
    ]
    evaluations = [
        item
        for item in session.analysis_state.condition_evaluations
        if "github.event.comment.user.login" in item.referenced_contexts
    ]
    assert evaluations
    assessment = evaluations[0].principal_assessments[0]
    assert assessment.principal_role is EventPrincipalRole.COMMENT_AUTHOR
    assert assessment.principal_login == "maintainer"
    assert assessment.current_repository_authorization is TriState.TRUE
    assert assessment.historical_repository_authorization is TriState.UNKNOWN
    assert assessment.missing_evidence == ["historical event principal authorization"]
    assert transport.urls == [
        "https://api.github.com/repos/example-org/example-repo/actions/runs/42",
        "https://api.github.com/repos/example-org/example-repo/issues/comments/99",
        "https://api.github.com/repos/example-org/example-repo/collaborators/maintainer/permission",
    ]


@pytest.mark.asyncio
async def test_actor_permission_404_remains_ambiguous_unknown() -> None:
    transport = RoutingTransport(
        {
            "/collaborators/external/permission": GitHubObserverError(
                "not found",
                status_code=404,
            )
        }
    )
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )
    requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_REPOSITORY_ACTOR_PERMISSION,
            "github:example/repo:actor:external",
            qualifier="external",
        ),
        kind=EvidenceRequirementKind.GITHUB_REPOSITORY_ACTOR_PERMISSION,
        provider=EvidenceProvider.GITHUB,
        subject_id="github:example/repo:actor:external",
        qualifier="external",
        reason="test ambiguous actor permission",
    )

    class DummySession:
        pass

    with pytest.raises(EvidenceResolutionError) as raised:
        await resolver.resolve(DummySession(), requirement)

    assert raised.value.kind is ResolutionFailureKind.AMBIGUOUS_NOT_FOUND


@pytest.mark.asyncio
async def test_github_resolver_treats_rate_limited_403_as_operational_failure() -> None:
    transport = RoutingTransport(
        {
            "/actions/permissions": GitHubObserverError(
                "rate limited",
                status_code=403,
                rate_limit_remaining=0,
                retry_after_seconds=30,
            )
        }
    )
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )
    requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_ACTIONS_POLICY,
            "repo-test",
        ),
        kind=EvidenceRequirementKind.GITHUB_ACTIONS_POLICY,
        provider=EvidenceProvider.GITHUB,
        subject_id="repo-test",
        reason="test rate limit",
    )

    class DummySession:
        pass

    with pytest.raises(EvidenceResolutionError) as raised:
        await resolver.resolve(DummySession(), requirement)

    assert raised.value.kind is ResolutionFailureKind.RATE_LIMITED
    assert resolver.metrics.rate_limit_errors == 1
    assert resolver.metrics.rate_limit_remaining == 0
    assert resolver.metrics.network_requests == 1


@pytest.mark.asyncio
async def test_github_resolver_retries_short_retry_after_within_budget() -> None:
    transport = SequencedResponseTransport(
        [
            GitHubObserverError(
                "rate limited",
                status_code=429,
                retry_after_seconds=0,
                rate_limit_remaining=10,
            ),
            GitHubHttpResponse(
                status_code=200,
                payload={"default_workflow_permissions": "read"},
                rate_limit_remaining=9,
            ),
        ]
    )
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )
    requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_REPOSITORY_DEFAULT_WORKFLOW_PERMISSION,
            "repo-test",
        ),
        kind=EvidenceRequirementKind.GITHUB_REPOSITORY_DEFAULT_WORKFLOW_PERMISSION,
        provider=EvidenceProvider.GITHUB,
        subject_id="repo-test",
        reason="test bounded retry",
    )

    class DummySession:
        pass

    payload = await resolver.resolve(DummySession(), requirement)

    assert payload.value.value == "read"
    assert resolver.metrics.network_requests == 2
    assert resolver.metrics.retries == 1
    assert resolver.metrics.rate_limit_errors == 1
    assert resolver.metrics.rate_limit_remaining == 9


@pytest.mark.asyncio
async def test_github_resolver_enforces_network_call_budget() -> None:
    transport = RoutingTransport(
        {
            "/one": {},
            "/two": {},
            "/three": {},
            "/four": {},
        }
    )
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )
    resolver.metrics.current_resolution_requests = 0

    await resolver._get("/one")
    await resolver._get("/two")
    with pytest.raises(EvidenceResolutionError) as raised:
        await resolver._get("/three")

    assert raised.value.kind is ResolutionFailureKind.BUDGET_EXHAUSTED
    assert resolver.metrics.network_requests == 2
    assert len(transport.urls) == 2


@pytest.mark.asyncio
async def test_resume_revalidates_persisted_etag_before_reusing_semantics(tmp_path: Path) -> None:
    repository = FIXTURES / "reasoning-matrix" / "gha-05" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.FORK_PR),
    )
    session = create_red_team_session(report.state)
    first_transport = SequencedResponseTransport(
        [
            GitHubHttpResponse(
                status_code=200,
                payload={"default_workflow_permissions": "read"},
                etag='"workflow-v1"',
                rate_limit_remaining=4_999,
            )
        ]
    )
    first_registry = _registry(first_transport)

    _, first_observation = await _next_observation(session, first_registry, repository)
    assert first_observation.payload is not None
    assert session.investigation_state.transport_cache
    cache_entry = next(iter(session.investigation_state.transport_cache.values()))
    assert cache_entry.etag == '"workflow-v1"'
    assert all(
        "repository default GITHUB_TOKEN permissions"
        not in context.effective_permissions.missing_evidence
        for context in session.analysis_state.job_execution_contexts
    )

    restored = type(session).model_validate(session.model_dump(mode="json", by_alias=True))
    prepare_resume_revalidation(
        restored,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.FORK_PR,
    )
    assert restored.investigation_state.resume_revalidation_epoch == 1
    assert any(
        "repository default GITHUB_TOKEN permissions"
        in context.effective_permissions.missing_evidence
        for context in restored.analysis_state.job_execution_contexts
    )

    resume_transport = SequencedResponseTransport(
        [
            GitHubHttpResponse(
                status_code=304,
                payload=None,
                etag='"workflow-v1"',
                rate_limit_remaining=4_999,
            )
        ]
    )
    resume_registry = _registry(resume_transport)
    task, observation = await _next_observation(restored, resume_registry, repository)

    assert task.task_id != first_observation.task_id
    assert observation.payload == first_observation.payload
    assert resume_transport.headers[0]["If-None-Match"] == '"workflow-v1"'
    resolver = next(iter(resume_registry._by_id.values()))
    assert resolver.metrics.not_modified_hits == 1
    assert resolver.metrics.network_requests == 1
    assert restored.investigation_state.revalidated_evidence_keys
    assert all(
        "repository default GITHUB_TOKEN permissions"
        not in context.effective_permissions.missing_evidence
        for context in restored.analysis_state.job_execution_contexts
    )


@pytest.mark.asyncio
async def test_actions_policy_blocks_mutable_external_action(tmp_path: Path) -> None:
    repository = FIXTURES / "gha-04-mutable-action" / "vulnerable"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.REPO_WRITER),
    )
    session = create_red_team_session(report.state)
    transport = RoutingTransport(
        {
            "/actions/permissions": {
                "enabled": True,
                "allowed_actions": "selected",
                "sha_pinning_required": True,
            },
            "/actions/permissions/selected-actions": {
                "github_owned_allowed": True,
                "verified_allowed": False,
                "patterns_allowed": [],
            },
        }
    )
    registry = _registry(transport)

    task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_ACTIONS_POLICY,
        subject_id=session.analysis_state.repository.repository_id,
    )
    observation = await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.REPO_WRITER,
        task=task,
    )
    assert observation is not None
    assert isinstance(observation.payload, ActionsPolicyEvidence)
    external = [
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "external_code_may_execute_in_step"
    ]
    assert external
    assert all(relation.verdict.value == "FALSE" for relation in external)
    assert all(not relation.missing_evidence for relation in external)


@pytest.mark.asyncio
async def test_branch_and_environment_pattern_resolvers_normalize_payloads() -> None:
    transport = RoutingTransport(
        {
            "/rules/branches/main": [
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_approving_review_count": 1,
                        "require_last_push_approval": True,
                    },
                },
                {
                    "type": "required_status_checks",
                    "parameters": {"required_status_checks": [{"context": "ci"}]},
                },
            ],
            "/branches/main": {
                "name": "main",
                "protected": True,
                "protection": {"enabled": False},
            },
            "/environments/production/deployment-branch-policies": {
                "branch_policies": [{"name": "main"}, {"name": "release/*"}]
            },
        }
    )
    resolver = GitHubCompositeResolver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )
    repository_id = "repo-test"
    branch_requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_EFFECTIVE_BRANCH_RULES,
            repository_id,
            qualifier="main",
        ),
        kind=EvidenceRequirementKind.GITHUB_EFFECTIVE_BRANCH_RULES,
        provider=EvidenceProvider.GITHUB,
        subject_id=repository_id,
        qualifier="main",
        reason="test",
    )
    environment_requirement = EvidenceRequirement(
        requirement_id=requirement_id_for(
            EvidenceRequirementKind.GITHUB_ENVIRONMENT_BRANCH_PATTERNS,
            f"{repository_id}:environment:production",
            qualifier="production",
        ),
        kind=EvidenceRequirementKind.GITHUB_ENVIRONMENT_BRANCH_PATTERNS,
        provider=EvidenceProvider.GITHUB,
        subject_id=f"{repository_id}:environment:production",
        qualifier="production",
        reason="test",
    )
    # These resolver methods do not inspect session state for either requirement.
    class DummySession:
        pass

    branch = await resolver.resolve(DummySession(), branch_requirement)
    environment = await resolver.resolve(DummySession(), environment_requirement)
    assert isinstance(branch, EffectiveBranchRulesEvidence)
    assert branch.protected is TriState.TRUE
    assert branch.active_rule_types == ["pull_request", "required_status_checks"]
    assert branch.requires_pull_request is True
    assert branch.required_approving_review_count == 1
    assert branch.require_last_push_approval is True
    assert branch.required_status_check_count == 1
    assert branch.legacy_protection_present is TriState.FALSE
    assert isinstance(environment, EnvironmentBranchPatternsEvidence)
    assert environment.patterns == ["main", "release/*"]
    assert environment.unknown_target_patterns == ["main", "release/*"]


async def _seed_custom_environment_policy(
    session,
    registry: ResolverRegistry,
    repository: Path,
) -> None:
    repository_id = session.analysis_state.repository.repository_id
    branch_task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_EFFECTIVE_BRANCH_RULES,
        subject_id=repository_id,
        qualifier="main",
    )
    await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.REPO_WRITER,
        task=branch_task,
    )
    environment_task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_ENVIRONMENT_POLICY,
        subject_id=f"{repository_id}:environment:production",
        qualifier="production",
    )
    await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.REPO_WRITER,
        task=environment_task,
    )


@pytest.mark.asyncio
async def test_environment_branch_patterns_block_disallowed_exact_push_ref(tmp_path: Path) -> None:
    repository = FIXTURES / "environment-branch-patterns" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.REPO_WRITER),
    )
    session = create_red_team_session(report.state)
    transport = RoutingTransport(
        {
            "/rules/branches/main": [],
            "/branches/main": {
                "name": "main",
                "protected": False,
                "protection": {"enabled": False},
            },
            "/environments/production": {
                "name": "production",
                "protection_rules": [{"type": "branch_policy"}],
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            },
            "/environments/production/deployment-branch-policies": {
                "branch_policies": [{"name": "release/*", "type": "branch"}]
            },
        }
    )
    registry = _registry(transport)
    await _seed_custom_environment_policy(session, registry, repository)

    pending = [
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "environment_restricts_deployment_branches"
    ]
    assert pending
    assert any(
        ref.kind is EvidenceRequirementKind.GITHUB_ENVIRONMENT_BRANCH_PATTERNS
        for relation in pending
        for ref in relation.evidence_requirements
    )

    task, observation = await _next_observation(
        session,
        registry,
        repository,
        profile=AttackerProfile.REPO_WRITER,
    )
    assert task.requirement_id is not None
    requirement = session.investigation_state.requirements[task.requirement_id]
    assert requirement.kind is EvidenceRequirementKind.GITHUB_ENVIRONMENT_BRANCH_PATTERNS
    assert isinstance(observation.payload, EnvironmentBranchPatternsEvidence)
    assert observation.payload.branch_patterns == ["release/*"]
    assert any(
        relation.relation_type == "guard_blocks_untrusted_execution"
        and relation.target_id in {
            context.context_id for context in session.analysis_state.job_execution_contexts
        }
        for relation in session.analysis_state.relations
    )
    assert any(
        frontier.disposition.value == "BLOCKED"
        for frontier in build_attack_frontiers(session.analysis_state).frontiers
    )


@pytest.mark.asyncio
async def test_environment_branch_patterns_clear_guard_for_allowed_exact_push_ref(
    tmp_path: Path,
) -> None:
    repository = FIXTURES / "environment-branch-patterns" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.REPO_WRITER),
    )
    session = create_red_team_session(report.state)
    transport = RoutingTransport(
        {
            "/rules/branches/main": [],
            "/branches/main": {
                "name": "main",
                "protected": False,
                "protection": {"enabled": False},
            },
            "/environments/production": {
                "name": "production",
                "protection_rules": [{"type": "branch_policy"}],
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            },
            "/environments/production/deployment-branch-policies": {
                "branch_policies": [{"name": "main", "type": "branch"}]
            },
        }
    )
    registry = _registry(transport)
    await _seed_custom_environment_policy(session, registry, repository)
    await _next_observation(
        session,
        registry,
        repository,
        profile=AttackerProfile.REPO_WRITER,
    )

    assert not [
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "environment_restricts_deployment_branches"
    ]
    assert not [
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "guard_blocks_untrusted_execution"
    ]


@pytest.mark.asyncio
async def test_environment_branch_patterns_keep_unknown_when_policy_target_is_opaque(
    tmp_path: Path,
) -> None:
    repository = FIXTURES / "environment-branch-patterns" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.REPO_WRITER),
    )
    session = create_red_team_session(report.state)
    transport = RoutingTransport(
        {
            "/rules/branches/main": [],
            "/branches/main": {
                "name": "main",
                "protected": False,
                "protection": {"enabled": False},
            },
            "/environments/production": {
                "name": "production",
                "protection_rules": [{"type": "branch_policy"}],
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            },
            "/environments/production/deployment-branch-policies": {
                "branch_policies": [{"name": "release/*"}]
            },
        }
    )
    registry = _registry(transport)
    await _seed_custom_environment_policy(session, registry, repository)
    await _next_observation(
        session,
        registry,
        repository,
        profile=AttackerProfile.REPO_WRITER,
    )

    guards = [
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "environment_restricts_deployment_branches"
    ]
    assert guards
    assert any(
        "deployment branch pattern matching semantics" in relation.missing_evidence
        for relation in guards
    )
    assert not [
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "guard_blocks_untrusted_execution"
    ]


@pytest.mark.asyncio
async def test_branch_rules_vertical_slice_clears_guard_for_unprotected_exact_push_branch(
    tmp_path: Path,
) -> None:
    repository = FIXTURES / "branch-rules" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.REPO_WRITER),
    )
    session = create_red_team_session(report.state)
    initial_guards = [
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "branch_rules_may_block_direct_push"
    ]
    assert len(initial_guards) == 1
    assert [ref.qualifier for ref in initial_guards[0].evidence_requirements] == ["main"]

    transport = RoutingTransport(
        {
            "/rules/branches/main": [],
            "/branches/main": {
                "name": "main",
                "protected": False,
                "protection": {"enabled": False},
            },
        }
    )
    registry = _registry(transport)
    task, observation = await _next_observation(
        session,
        registry,
        repository,
        profile=AttackerProfile.REPO_WRITER,
    )

    assert task.requirement_id is not None
    requirement = session.investigation_state.requirements[task.requirement_id]
    assert requirement.kind is EvidenceRequirementKind.GITHUB_EFFECTIVE_BRANCH_RULES
    assert requirement.qualifier == "main"
    assert isinstance(observation.payload, EffectiveBranchRulesEvidence)
    assert observation.payload.protected is TriState.FALSE
    assert not [
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "branch_rules_may_block_direct_push"
    ]


@pytest.mark.asyncio
async def test_branch_rules_vertical_slice_keeps_may_block_for_required_review(
    tmp_path: Path,
) -> None:
    repository = FIXTURES / "branch-rules" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.REPO_WRITER),
    )
    session = create_red_team_session(report.state)
    transport = RoutingTransport(
        {
            "/rules/branches/main": [
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_approving_review_count": 1,
                        "require_last_push_approval": False,
                    },
                }
            ],
            "/branches/main": {
                "name": "main",
                "protected": True,
                "protection": {"enabled": False},
            },
        }
    )
    registry = _registry(transport)
    await _next_observation(
        session,
        registry,
        repository,
        profile=AttackerProfile.REPO_WRITER,
    )

    guards = [
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "branch_rules_may_block_direct_push"
    ]
    assert len(guards) == 1
    assert guards[0].verdict is TriState.UNKNOWN
    assert guards[0].missing_evidence == ["required pull-request approval outcome"]
    result = build_attack_frontiers(session.analysis_state)
    portfolio = select_frontier_portfolio(result, session.analysis_state)
    assert plan_next_task(session, portfolio=portfolio, resolver_registry=registry) is None


@pytest.mark.asyncio
async def test_branch_rules_resolution_failure_preserves_external_unknown(tmp_path: Path) -> None:
    repository = FIXTURES / "branch-rules" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.REPO_WRITER),
    )
    session = create_red_team_session(report.state)
    transport = RoutingTransport(
        {
            "/branches/main": GitHubObserverError("forbidden", status_code=403),
        }
    )
    registry = _registry(transport)
    result = build_attack_frontiers(session.analysis_state)
    portfolio = select_frontier_portfolio(result, session.analysis_state)
    task = plan_next_task(session, portfolio=portfolio, resolver_registry=registry)
    assert task is not None

    with pytest.raises(EvidenceResolutionError) as raised:
        await run_observation_once(
            session,
            registry,
            boundary=RepositoryBoundary(repository),
            profile=AttackerProfile.REPO_WRITER,
            task=task,
        )

    assert raised.value.kind is ResolutionFailureKind.FORBIDDEN
    assert session.knowledge_state.revision == 0
    assert not session.knowledge_state.external_evidence
    assert any(
        relation.relation_type == "branch_rules_may_block_direct_push"
        for relation in session.analysis_state.relations
    )


@pytest.mark.asyncio
async def test_pending_deployment_vertical_slice_blocks_current_environment(
    tmp_path: Path,
) -> None:
    repository = FIXTURES / "environment-branch-patterns" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.REPO_WRITER),
    )
    session = create_red_team_session(report.state, github_run_id=4242)
    transport = RoutingTransport(
        _environment_review_payloads(
            [
                {
                    "environment": {"name": "production"},
                    "wait_timer": 15,
                    "current_user_can_approve": False,
                    "reviewers": [{"type": "User", "reviewer": {"login": "reviewer"}}],
                }
            ]
        )
    )
    registry = _registry(transport)
    await _seed_environment_review_policy(session, registry, repository)

    result = build_attack_frontiers(session.analysis_state)
    portfolio = select_frontier_portfolio(result, session.analysis_state)
    requirements = compile_evidence_requirements(session, portfolio)
    pending = [
        requirement
        for requirement in requirements
        if requirement.kind is EvidenceRequirementKind.GITHUB_PENDING_DEPLOYMENT_STATE
    ]
    assert len(pending) == 1
    assert pending[0].qualifier == "4242"

    repository_id = session.analysis_state.repository.repository_id
    task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_PENDING_DEPLOYMENT_STATE,
        subject_id=f"{repository_id}:run:4242",
        qualifier="4242",
    )
    observation = await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.REPO_WRITER,
        task=task,
    )

    assert observation is not None
    assert isinstance(observation.payload, PendingDeploymentStateEvidence)
    assert [
        item.environment_name for item in observation.payload.pending_environments
    ] == ["production"]
    blocking = [
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "guard_blocks_untrusted_execution"
    ]
    assert blocking
    assert all(relation.verdict is TriState.TRUE for relation in blocking)


@pytest.mark.asyncio
async def test_empty_pending_deployment_list_does_not_imply_approval(tmp_path: Path) -> None:
    repository = FIXTURES / "environment-branch-patterns" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.REPO_WRITER),
    )
    session = create_red_team_session(report.state, github_run_id=4242)
    transport = RoutingTransport(_environment_review_payloads([]))
    registry = _registry(transport)
    await _seed_environment_review_policy(session, registry, repository)
    repository_id = session.analysis_state.repository.repository_id
    task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_PENDING_DEPLOYMENT_STATE,
        subject_id=f"{repository_id}:run:4242",
        qualifier="4242",
    )
    observation = await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.REPO_WRITER,
        task=task,
    )

    assert observation is not None
    assert isinstance(observation.payload, PendingDeploymentStateEvidence)
    assert observation.payload.pending_environments == []
    assert not [
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "guard_blocks_untrusted_execution"
    ]
    review_guards = [
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "environment_requires_deployment_review"
    ]
    assert review_guards
    assert all(relation.verdict is TriState.UNKNOWN for relation in review_guards)


@pytest.mark.asyncio
async def test_pending_deployment_resolution_failure_preserves_unknown(tmp_path: Path) -> None:
    repository = FIXTURES / "environment-branch-patterns" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.REPO_WRITER),
    )
    session = create_red_team_session(report.state, github_run_id=4242)
    transport = RoutingTransport(
        _environment_review_payloads(
            GitHubObserverError("not found", status_code=404)
        )
    )
    registry = _registry(transport)
    await _seed_environment_review_policy(session, registry, repository)
    repository_id = session.analysis_state.repository.repository_id
    task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_PENDING_DEPLOYMENT_STATE,
        subject_id=f"{repository_id}:run:4242",
        qualifier="4242",
    )

    with pytest.raises(EvidenceResolutionError) as raised:
        await run_observation_once(
            session,
            registry,
            boundary=RepositoryBoundary(repository),
            profile=AttackerProfile.REPO_WRITER,
            task=task,
        )

    assert raised.value.kind is ResolutionFailureKind.AMBIGUOUS_NOT_FOUND
    assert not [
        payload
        for payload in session.knowledge_state.external_evidence.values()
        if isinstance(payload, PendingDeploymentStateEvidence)
    ]
    assert any(
        relation.relation_type == "environment_requires_deployment_review"
        and relation.verdict is TriState.UNKNOWN
        for relation in session.analysis_state.relations
    )


def _workflow_run_jobs_payload(*, same_runner: bool = False) -> dict[str, object]:
    return {
        "total_count": 2,
        "jobs": [
            {
                "id": 101,
                "name": "Attack job",
                "status": "completed",
                "conclusion": "success",
                "runner_id": 11,
                "runner_group_id": 3,
                "labels": ["self-hosted", "linux"],
            },
            {
                "id": 102,
                "name": "Privileged job",
                "status": "completed",
                "conclusion": "success",
                "runner_id": 11 if same_runner else 12,
                "runner_group_id": 3,
                "labels": ["self-hosted", "linux"],
            },
        ],
    }


async def _runner_runtime_session(tmp_path: Path):
    repository = FIXTURES / "runner-runtime"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.FORK_PR),
    )
    return repository, create_red_team_session(report.state, github_run_id=4242)


@pytest.mark.asyncio
async def test_workflow_run_jobs_vertical_slice_closes_different_runner_path(
    tmp_path: Path,
) -> None:
    repository, session = await _runner_runtime_session(tmp_path)
    transport = RoutingTransport(
        {"/actions/runs/4242/jobs?filter=latest&per_page=100": _workflow_run_jobs_payload()}
    )
    registry = _registry(transport)
    result = build_attack_frontiers(
        session.analysis_state,
        FrontierQuery(mechanism_hints=("self_hosted_runner",)),
    )
    portfolio = select_frontier_portfolio(result, session.analysis_state)
    requirements = compile_evidence_requirements(session, portfolio)
    relation_by_ref = dict(indexed_relations(session.analysis_state))
    assert any(
        requirement.kind is EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_JOBS
        for requirement in requirements
    ), [
        {
            "relation_types": [
                relation_by_ref[reference].relation_type
                for reference in entry.frontier.relation_refs
            ],
            "missing": [item.description for item in entry.frontier.missing],
        }
        for entry in portfolio
    ]

    repository_id = session.analysis_state.repository.repository_id
    task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_JOBS,
        subject_id=f"{repository_id}:run:4242",
        qualifier="4242",
    )
    observation = await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.FORK_PR,
        task=task,
    )

    assert observation is not None
    assert isinstance(observation.payload, WorkflowRunJobsEvidence)
    jobs = {
        job.job_key: job
        for workflow in session.analysis_state.workflows
        for job in workflow.jobs
    }
    persistence = next(
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "runner_process_or_file_state_may_reach_privileged_job"
        and relation.source_id == jobs["attack"].job_id
        and relation.target_id == jobs["privileged"].job_id
    )
    assert persistence.verdict is TriState.FALSE
    assert persistence.missing_evidence == []
    assert any(
        relation.relation_type == "attacker_code_executes_on_self_hosted_runner"
        and relation.verdict is TriState.TRUE
        for relation in session.analysis_state.relations
    )


@pytest.mark.asyncio
async def test_same_runner_resolves_identity_but_preserves_cleanup_unknown(
    tmp_path: Path,
) -> None:
    repository, session = await _runner_runtime_session(tmp_path)
    registry = _registry(
        RoutingTransport(
            {
                "/actions/runs/4242/jobs?filter=latest&per_page=100": (
                    _workflow_run_jobs_payload(same_runner=True)
                )
            }
        )
    )
    repository_id = session.analysis_state.repository.repository_id
    task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_JOBS,
        subject_id=f"{repository_id}:run:4242",
        qualifier="4242",
    )
    await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.FORK_PR,
        task=task,
    )

    jobs = {
        job.job_key: job
        for workflow in session.analysis_state.workflows
        for job in workflow.jobs
    }
    persistence = next(
        relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "runner_process_or_file_state_may_reach_privileged_job"
        and relation.source_id == jobs["attack"].job_id
        and relation.target_id == jobs["privileged"].job_id
    )
    assert persistence.verdict is TriState.UNKNOWN
    assert persistence.missing_evidence == ["runner cleanup", "runner ephemerality"]


@pytest.mark.asyncio
async def test_ambiguous_runtime_job_name_preserves_runner_unknown(tmp_path: Path) -> None:
    repository, session = await _runner_runtime_session(tmp_path)
    payload = _workflow_run_jobs_payload()
    jobs_payload = payload["jobs"]
    assert isinstance(jobs_payload, list)
    jobs_payload.append(
        {
            "id": 103,
            "name": "Attack job",
            "status": "completed",
            "conclusion": "success",
            "runner_id": 99,
            "labels": ["self-hosted"],
        }
    )
    payload["total_count"] = 3
    registry = _registry(
        RoutingTransport(
            {"/actions/runs/4242/jobs?filter=latest&per_page=100": payload}
        )
    )
    repository_id = session.analysis_state.repository.repository_id
    task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_JOBS,
        subject_id=f"{repository_id}:run:4242",
        qualifier="4242",
    )
    await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.FORK_PR,
        task=task,
    )

    assert any(
        relation.relation_type == "attacker_code_executes_on_self_hosted_runner"
        and relation.verdict is TriState.UNKNOWN
        and relation.missing_evidence == ["runner assignment and runtime execution"]
        for relation in session.analysis_state.relations
    )


@pytest.mark.asyncio
async def test_workflow_run_jobs_failure_preserves_runner_unknown(tmp_path: Path) -> None:
    repository, session = await _runner_runtime_session(tmp_path)
    registry = _registry(
        RoutingTransport(
            {
                "/actions/runs/4242/jobs?filter=latest&per_page=100": GitHubObserverError(
                    "not found",
                    status_code=404,
                )
            }
        )
    )
    repository_id = session.analysis_state.repository.repository_id
    task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_JOBS,
        subject_id=f"{repository_id}:run:4242",
        qualifier="4242",
    )

    with pytest.raises(EvidenceResolutionError) as raised:
        await run_observation_once(
            session,
            registry,
            boundary=RepositoryBoundary(repository),
            profile=AttackerProfile.FORK_PR,
            task=task,
        )

    assert raised.value.kind is ResolutionFailureKind.AMBIGUOUS_NOT_FOUND
    assert not [
        payload
        for payload in session.knowledge_state.external_evidence.values()
        if isinstance(payload, WorkflowRunJobsEvidence)
    ]
    assert any(
        relation.relation_type == "attacker_code_executes_on_self_hosted_runner"
        and relation.verdict is TriState.UNKNOWN
        for relation in session.analysis_state.relations
    )


@pytest.mark.asyncio
async def test_workflow_run_jobs_rejects_incomplete_bounded_page(tmp_path: Path) -> None:
    repository, session = await _runner_runtime_session(tmp_path)
    payload = _workflow_run_jobs_payload()
    payload["total_count"] = 101
    registry = _registry(
        RoutingTransport(
            {"/actions/runs/4242/jobs?filter=latest&per_page=100": payload}
        )
    )
    repository_id = session.analysis_state.repository.repository_id
    task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_JOBS,
        subject_id=f"{repository_id}:run:4242",
        qualifier="4242",
    )

    with pytest.raises(EvidenceResolutionError) as raised:
        await run_observation_once(
            session,
            registry,
            boundary=RepositoryBoundary(repository),
            profile=AttackerProfile.FORK_PR,
            task=task,
        )

    assert raised.value.kind is ResolutionFailureKind.BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_ambiguous_404_is_not_committed_as_evidence(tmp_path: Path) -> None:
    repository = FIXTURES / "gha-04-mutable-action" / "vulnerable"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.REPO_WRITER),
    )
    session = create_red_team_session(report.state)
    transport = RoutingTransport(
        {
            "/actions/permissions": GitHubObserverError(
                "not found",
                status_code=404,
            )
        }
    )
    registry = _registry(transport)
    task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_ACTIONS_POLICY,
        subject_id=session.analysis_state.repository.repository_id,
    )

    with pytest.raises(EvidenceResolutionError) as raised:
        await run_observation_once(
            session,
            registry,
            boundary=RepositoryBoundary(repository),
            profile=AttackerProfile.REPO_WRITER,
            task=task,
        )

    assert raised.value.kind is ResolutionFailureKind.AMBIGUOUS_NOT_FOUND
    assert session.knowledge_state.revision == 0
    assert not session.knowledge_state.external_evidence


@pytest.mark.asyncio
async def test_p2_workflow_run_jobs_preserves_bounded_runtime_steps(tmp_path: Path) -> None:
    repository = FIXTURES / "status-semantics"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.REPO_WRITER),
    )
    session = create_red_team_session(report.state, github_run_id=4242)
    registry = _registry(
        RoutingTransport(
            {
                "/actions/runs/4242/jobs?filter=latest&per_page=100": {
                    "total_count": 1,
                    "jobs": [
                        {
                            "id": 9001,
                            "name": "Soft fail job",
                            "status": "completed",
                            "conclusion": "success",
                            "runner_id": 123,
                            "labels": ["ubuntu-latest"],
                            "steps": [
                                {
                                    "number": 1,
                                    "name": "Soft fail",
                                    "status": "completed",
                                    "conclusion": "success",
                                },
                                {
                                    "number": 2,
                                    "name": "Outcome check",
                                    "status": "completed",
                                    "conclusion": "success",
                                },
                            ],
                        }
                    ],
                }
            }
        )
    )
    repository_id = session.analysis_state.repository.repository_id
    task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_JOBS,
        subject_id=f"{repository_id}:run:4242",
        qualifier="4242",
    )
    observation = await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.REPO_WRITER,
        task=task,
    )

    assert isinstance(observation.payload, WorkflowRunJobsEvidence)
    assert len(observation.payload.jobs) == 1
    assert [step.name for step in observation.payload.jobs[0].steps] == [
        "Soft fail",
        "Outcome check",
    ]


@pytest.mark.asyncio
async def test_p2_workflow_run_status_is_separate_typed_evidence(tmp_path: Path) -> None:
    repository = FIXTURES / "status-semantics"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.REPO_WRITER),
    )
    session = create_red_team_session(report.state, github_run_id=4242)
    registry = _registry(
        RoutingTransport(
            {
                "/actions/runs/4242": {
                    "id": 4242,
                    "status": "completed",
                    "conclusion": "cancelled",
                }
            }
        )
    )
    repository_id = session.analysis_state.repository.repository_id
    task = _explicit_task(
        session,
        registry,
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_STATUS,
        subject_id=f"{repository_id}:run:4242",
        qualifier="4242",
    )
    observation = await run_observation_once(
        session,
        registry,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.REPO_WRITER,
        task=task,
    )

    assert isinstance(observation.payload, WorkflowRunStatusEvidence)
    assert observation.payload.run_id == 4242
    assert observation.payload.conclusion == "cancelled"
