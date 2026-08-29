from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from cicd_redteam_agent.domain.condition_semantics import (
    ConditionRuntimeContext,
    evaluate_condition,
)
from cicd_redteam_agent.domain.event_provenance import event_principal_qualifier
from cicd_redteam_agent.domain.evidence_requirement import (
    EvidenceRequirement,
    evidence_key,
    requirement_id_for,
)
from cicd_redteam_agent.domain.investigation import (
    EventPrincipalEvidence,
    GitHubEventLocator,
    KnowledgeState,
    RepositoryActorPermissionEvidence,
    WorkflowRunEventLinkageEvidence,
)
from cicd_redteam_agent.domain.models import (
    AttackerProfile,
    EventPrincipalRole,
    EvidenceProvider,
    EvidenceRequirementKind,
    TriState,
)
from cicd_redteam_agent.framework.evidence_resolver import (
    EvidenceResolutionError,
    ResolutionFailureKind,
)
from cicd_redteam_agent.framework.github_observer import (
    GitHubHttpResponse,
    UrllibGitHubJsonTransport,
)
from cicd_redteam_agent.framework.github_resolvers import GitHubCompositeResolver

pytestmark = pytest.mark.live_github


@dataclass(frozen=True)
class P1LiveConfig:
    owner: str
    repository: str
    token: str = field(repr=False)
    actor_login: str
    actor_id: int
    issue_number: int
    comment_id: int
    issue_comment_run_id: int
    pull_number: int
    pull_request_run_id: int
    label_run_id: int
    review_id: int
    review_run_id: int
    review_comment_id: int
    review_comment_run_id: int

    @property
    def repository_id(self) -> str:
        return f"github:{self.owner}/{self.repository}"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"


@dataclass
class RecordingLiveTransport:
    inner: UrllibGitHubJsonTransport = field(default_factory=UrllibGitHubJsonTransport)
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    responses: list[GitHubHttpResponse] = field(default_factory=list)

    async def get_response(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> GitHubHttpResponse:
        self.requests.append(
            (
                url,
                {
                    key: "<redacted>" if key.casefold() == "authorization" else value
                    for key, value in headers.items()
                },
            )
        )
        response = await self.inner.get_response(url, headers=headers)
        self.responses.append(response)
        return response


def _positive_int(name: str) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        pytest.skip(f"{name} is required for P1 live GitHub acceptance")
    try:
        value = int(raw)
    except ValueError:
        pytest.fail(f"{name} must be a positive integer")
    if value <= 0:
        pytest.fail(f"{name} must be a positive integer")
    return value


@pytest.fixture(scope="module")
def p1_live_config() -> P1LiveConfig:
    repository = os.getenv("CICD_P1_LIVE_GITHUB_REPOSITORY", "").strip()
    token = os.getenv("CICD_P1_LIVE_GITHUB_TOKEN", "").strip()
    actor_login = os.getenv("CICD_P1_LIVE_GITHUB_ACTOR_LOGIN", "").strip()
    if not repository or not token or not actor_login:
        pytest.skip("P1 live GitHub repository, token, and actor login are required")
    parts = repository.split("/")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        pytest.fail("CICD_P1_LIVE_GITHUB_REPOSITORY must use owner/repository form")
    owner, repo = (part.strip() for part in parts)
    return P1LiveConfig(
        owner=owner,
        repository=repo,
        token=token,
        actor_login=actor_login,
        actor_id=_positive_int("CICD_P1_LIVE_GITHUB_ACTOR_ID"),
        issue_number=_positive_int("CICD_P1_LIVE_GITHUB_ISSUE_NUMBER"),
        comment_id=_positive_int("CICD_P1_LIVE_GITHUB_COMMENT_ID"),
        issue_comment_run_id=_positive_int("CICD_P1_LIVE_GITHUB_ISSUE_COMMENT_RUN_ID"),
        pull_number=_positive_int("CICD_P1_LIVE_GITHUB_PULL_NUMBER"),
        pull_request_run_id=_positive_int("CICD_P1_LIVE_GITHUB_PULL_REQUEST_RUN_ID"),
        label_run_id=_positive_int("CICD_P1_LIVE_GITHUB_LABEL_RUN_ID"),
        review_id=_positive_int("CICD_P1_LIVE_GITHUB_REVIEW_ID"),
        review_run_id=_positive_int("CICD_P1_LIVE_GITHUB_REVIEW_RUN_ID"),
        review_comment_id=_positive_int("CICD_P1_LIVE_GITHUB_REVIEW_COMMENT_ID"),
        review_comment_run_id=_positive_int("CICD_P1_LIVE_GITHUB_REVIEW_COMMENT_RUN_ID"),
    )


class LiveSession:
    def __init__(self, locator: GitHubEventLocator | None) -> None:
        self.github_event_locator = locator
        self.knowledge_state = KnowledgeState()


def _resolver(config: P1LiveConfig, transport: RecordingLiveTransport) -> GitHubCompositeResolver:
    return GitHubCompositeResolver(
        owner=config.owner,
        repository=config.repository,
        token=config.token,
        transport=transport,
    )


def _requirement(
    config: P1LiveConfig,
    kind: EvidenceRequirementKind,
    subject_suffix: str,
    qualifier: str,
) -> EvidenceRequirement:
    subject_id = f"{config.repository_id}:{subject_suffix}"
    return EvidenceRequirement(
        requirement_id=requirement_id_for(kind, subject_id, qualifier=qualifier),
        kind=kind,
        provider=EvidenceProvider.GITHUB,
        subject_id=subject_id,
        qualifier=qualifier,
        reason="P1 live event-principal acceptance",
    )


async def _resolve_linkage(
    config: P1LiveConfig,
    resolver: GitHubCompositeResolver,
    session: LiveSession,
    run_id: int,
) -> WorkflowRunEventLinkageEvidence:
    requirement = _requirement(
        config,
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
        f"run:{run_id}",
        str(run_id),
    )
    linkage = await resolver.resolve(session, requirement)
    assert isinstance(linkage, WorkflowRunEventLinkageEvidence)
    session.knowledge_state.external_evidence[
        evidence_key(requirement.kind, requirement.subject_id, qualifier=requirement.qualifier)
    ] = linkage
    return linkage


async def _resolve_principal(
    config: P1LiveConfig,
    resolver: GitHubCompositeResolver,
    session: LiveSession,
    linkage: WorkflowRunEventLinkageEvidence,
    role: EventPrincipalRole,
) -> EventPrincipalEvidence:
    qualifier = event_principal_qualifier(role, linkage)
    assert qualifier is not None
    requirement = _requirement(
        config,
        EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL,
        f"run:{linkage.run_id}:principal:{role.value}",
        qualifier,
    )
    principal = await resolver.resolve(session, requirement)
    assert isinstance(principal, EventPrincipalEvidence)
    return principal


@pytest.mark.asyncio
async def test_live_issue_comment_principal_current_permission_and_semantics(
    p1_live_config: P1LiveConfig,
) -> None:
    config = p1_live_config
    transport = RecordingLiveTransport()
    resolver = _resolver(config, transport)
    session = LiveSession(
        GitHubEventLocator(
            event_name="issue_comment",
            action="created",
            repository_full_name=config.full_name,
            issue_number=config.issue_number,
            comment_id=config.comment_id,
            sender_login=config.actor_login,
            sender_id=config.actor_id,
            sender_type="User",
            source_hash="a" * 64,
        )
    )

    linkage = await _resolve_linkage(config, resolver, session, config.issue_comment_run_id)
    principal = await _resolve_principal(
        config,
        resolver,
        session,
        linkage,
        EventPrincipalRole.COMMENT_AUTHOR,
    )
    assert principal.login == config.actor_login
    assert principal.actor_id == config.actor_id
    assert principal.author_association is not None

    permission_requirement = _requirement(
        config,
        EvidenceRequirementKind.GITHUB_REPOSITORY_ACTOR_PERMISSION,
        f"actor:{principal.login}",
        principal.login or "",
    )
    permission = await resolver.resolve(session, permission_requirement)
    assert isinstance(permission, RepositoryActorPermissionEvidence)
    assert permission.temporal_scope == "current"

    evaluation = evaluate_condition(
        f"github.event.comment.user.login == '{config.actor_login}'",
        target_id="live:p1:comment",
        target_kind="job",
        trigger_context_id="live:p1:issue-comment",
        context=ConditionRuntimeContext(
            event_name="issue_comment",
            profile=AttackerProfile.FORK_PR,
            repository_id=config.repository_id,
            github_run_id=config.issue_comment_run_id,
            event_linkage=linkage,
            event_principals={EventPrincipalRole.COMMENT_AUTHOR: principal},
            actor_permissions={
                config.actor_login.casefold(): (permission.permission, permission.actor_id)
            },
        ),
    )
    assessment = evaluation.principal_assessments[0]
    assert evaluation.verdict is TriState.TRUE
    assert assessment.current_repository_authorization is TriState.TRUE
    assert assessment.event_action_observed is TriState.TRUE
    assert assessment.historical_repository_authorization is TriState.UNKNOWN
    assert assessment.missing_evidence == ["historical event principal authorization"]
    assert resolver.metrics.network_requests == 3
    assert all(headers.get("Authorization") == "<redacted>" for _, headers in transport.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_name", "action", "run_attr", "role", "resource_fields"),
    [
        (
            "pull_request",
            "opened",
            "pull_request_run_id",
            EventPrincipalRole.PULL_REQUEST_AUTHOR,
            {"pull_number": "pull_number"},
        ),
        (
            "pull_request_review",
            "submitted",
            "review_run_id",
            EventPrincipalRole.REVIEW_AUTHOR,
            {"pull_number": "pull_number", "review_id": "review_id"},
        ),
        (
            "pull_request_review_comment",
            "created",
            "review_comment_run_id",
            EventPrincipalRole.REVIEW_COMMENT_AUTHOR,
            {"pull_number": "pull_number", "review_comment_id": "review_comment_id"},
        ),
    ],
)
async def test_live_pr_review_and_review_comment_exact_principals(
    p1_live_config: P1LiveConfig,
    event_name: str,
    action: str,
    run_attr: str,
    role: EventPrincipalRole,
    resource_fields: dict[str, str],
) -> None:
    config = p1_live_config
    transport = RecordingLiveTransport()
    resolver = _resolver(config, transport)
    locator_kwargs = {field: getattr(config, attr) for field, attr in resource_fields.items()}
    session = LiveSession(
        GitHubEventLocator(
            event_name=event_name,
            action=action,
            repository_full_name=config.full_name,
            sender_login=config.actor_login,
            sender_id=config.actor_id,
            sender_type="User",
            source_hash="b" * 64,
            **locator_kwargs,
        )
    )
    run_id = getattr(config, run_attr)

    linkage = await _resolve_linkage(config, resolver, session, run_id)
    principal = await _resolve_principal(config, resolver, session, linkage, role)

    assert linkage.binding == "exact"
    assert principal.principal_role is role
    assert principal.login == config.actor_login
    assert principal.actor_id == config.actor_id
    assert resolver.metrics.network_requests == 2


@pytest.mark.asyncio
async def test_live_label_actor_is_exact_event_sender_without_resource_guessing(
    p1_live_config: P1LiveConfig,
) -> None:
    config = p1_live_config
    transport = RecordingLiveTransport()
    resolver = _resolver(config, transport)
    session = LiveSession(
        GitHubEventLocator(
            event_name="pull_request",
            action="labeled",
            repository_full_name=config.full_name,
            pull_number=config.pull_number,
            label_name="safe-to-test",
            sender_login=config.actor_login,
            sender_id=config.actor_id,
            sender_type="User",
            source_hash="c" * 64,
        )
    )

    linkage = await _resolve_linkage(config, resolver, session, config.label_run_id)
    principal = await _resolve_principal(
        config,
        resolver,
        session,
        linkage,
        EventPrincipalRole.LABEL_ACTOR,
    )

    assert principal.login == config.actor_login
    assert principal.actor_id == config.actor_id
    assert principal.event_action == "labeled"
    assert principal.resource_id == "label:safe-to-test"
    assert resolver.metrics.network_requests == 1


@pytest.mark.asyncio
async def test_live_missing_event_principal_resource_is_ambiguous_not_found(
    p1_live_config: P1LiveConfig,
) -> None:
    config = p1_live_config
    transport = RecordingLiveTransport()
    resolver = _resolver(config, transport)
    missing_comment_id = 999_999_999_999_999_999
    linkage = WorkflowRunEventLinkageEvidence(
        run_id=config.issue_comment_run_id,
        event_name="issue_comment",
        action="created",
        binding="exact",
        source="operator_event_payload",
        issue_number=config.issue_number,
        comment_id=missing_comment_id,
        sender_login=config.actor_login,
        sender_id=config.actor_id,
    )
    session = LiveSession(None)
    session.knowledge_state.external_evidence[
        evidence_key(
            EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
            f"{config.repository_id}:run:{config.issue_comment_run_id}",
            qualifier=str(config.issue_comment_run_id),
        )
    ] = linkage
    qualifier = event_principal_qualifier(EventPrincipalRole.COMMENT_AUTHOR, linkage)
    assert qualifier is not None
    requirement = _requirement(
        config,
        EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL,
        f"run:{config.issue_comment_run_id}:principal:comment_author",
        qualifier,
    )

    with pytest.raises(EvidenceResolutionError) as raised:
        await resolver.resolve(session, requirement)

    assert raised.value.kind is ResolutionFailureKind.AMBIGUOUS_NOT_FOUND
    assert resolver.metrics.network_requests == 1
