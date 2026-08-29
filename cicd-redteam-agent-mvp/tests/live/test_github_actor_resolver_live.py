from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.condition_semantics import (
    ConditionRuntimeContext,
    evaluate_condition,
)
from cicd_redteam_agent.domain.evidence_requirement import EvidenceRequirement, requirement_id_for
from cicd_redteam_agent.domain.investigation import (
    RepositoryActorPermissionEvidence,
    WorkflowRunIdentityEvidence,
)
from cicd_redteam_agent.domain.models import (
    AttackerProfile,
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
from cicd_redteam_agent.framework.investigation_runtime import (
    create_red_team_session,
    prepare_resume_revalidation,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

pytestmark = pytest.mark.live_github

FIXTURES = Path(__file__).parents[1] / "fixtures"


@dataclass(frozen=True)
class LiveGitHubConfig:
    owner: str
    repository: str
    run_id: int
    token: str = field(repr=False)
    rerun_id: int | None = None
    missing_run_id: int | None = None

    @property
    def repository_id(self) -> str:
        return f"github:{self.owner}/{self.repository}"


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
        recorded_headers = {
            key: "<redacted>" if key.casefold() == "authorization" else value
            for key, value in headers.items()
        }
        self.requests.append((url, recorded_headers))
        response = await self.inner.get_response(url, headers=headers)
        self.responses.append(response)
        return response


def _positive_int(value: str | None, *, name: str, required: bool) -> int | None:
    raw = (value or "").strip()
    if not raw:
        if required:
            pytest.skip(f"{name} is required for live GitHub acceptance")
        return None
    try:
        parsed = int(raw)
    except ValueError:
        pytest.fail(f"{name} must be a positive integer")
    if parsed <= 0:
        pytest.fail(f"{name} must be a positive integer")
    return parsed


@pytest.fixture(scope="module")
def live_github_config() -> LiveGitHubConfig:
    repository = os.getenv("CICD_LIVE_GITHUB_REPOSITORY", "").strip()
    token = os.getenv("CICD_LIVE_GITHUB_TOKEN", "").strip()
    if not repository or not token:
        pytest.skip(
            "CICD_LIVE_GITHUB_REPOSITORY and CICD_LIVE_GITHUB_TOKEN are required "
            "for live GitHub acceptance"
        )
    parts = repository.split("/")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        pytest.fail("CICD_LIVE_GITHUB_REPOSITORY must use owner/repository form")
    owner, repo = (part.strip() for part in parts)
    run_id = _positive_int(
        os.getenv("CICD_LIVE_GITHUB_RUN_ID"),
        name="CICD_LIVE_GITHUB_RUN_ID",
        required=True,
    )
    assert run_id is not None
    return LiveGitHubConfig(
        owner=owner,
        repository=repo,
        run_id=run_id,
        token=token,
        rerun_id=_positive_int(
            os.getenv("CICD_LIVE_GITHUB_RERUN_ID"),
            name="CICD_LIVE_GITHUB_RERUN_ID",
            required=False,
        ),
        missing_run_id=_positive_int(
            os.getenv("CICD_LIVE_GITHUB_MISSING_RUN_ID"),
            name="CICD_LIVE_GITHUB_MISSING_RUN_ID",
            required=False,
        ),
    )


def _resolver(
    config: LiveGitHubConfig,
    transport: RecordingLiveTransport,
) -> GitHubCompositeResolver:
    return GitHubCompositeResolver(
        owner=config.owner,
        repository=config.repository,
        token=config.token,
        transport=transport,
    )


def _requirement(
    config: LiveGitHubConfig,
    kind: EvidenceRequirementKind,
    *,
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
        reason="live GitHub read-only P0 acceptance",
    )


def _identity_requirement(config: LiveGitHubConfig, run_id: int) -> EvidenceRequirement:
    return _requirement(
        config,
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY,
        subject_suffix=f"run:{run_id}",
        qualifier=str(run_id),
    )


def _permission_requirement(
    config: LiveGitHubConfig,
    actor_login: str,
) -> EvidenceRequirement:
    return _requirement(
        config,
        EvidenceRequirementKind.GITHUB_REPOSITORY_ACTOR_PERMISSION,
        subject_suffix=f"actor:{actor_login}",
        qualifier=actor_login,
    )


def _payload(response: GitHubHttpResponse) -> dict[str, object]:
    assert isinstance(response.payload, dict)
    return response.payload


class StatelessSession:
    pass


@pytest.mark.asyncio
async def test_live_run_identity_permission_and_semantic_authorization(
    live_github_config: LiveGitHubConfig,
) -> None:
    transport = RecordingLiveTransport()
    resolver = _resolver(live_github_config, transport)

    identity = await resolver.resolve(
        StatelessSession(),
        _identity_requirement(live_github_config, live_github_config.run_id),
    )
    assert isinstance(identity, WorkflowRunIdentityEvidence)
    raw_run = _payload(transport.responses[0])
    raw_actor = raw_run.get("actor")
    assert isinstance(raw_actor, dict)
    assert identity.run_id == raw_run.get("id") == live_github_config.run_id
    assert identity.event_name == raw_run.get("event")
    assert identity.actor_login == raw_actor.get("login")
    assert identity.actor_id == raw_actor.get("id")
    assert identity.run_attempt == raw_run.get("run_attempt", 1)

    permission = await resolver.resolve(
        StatelessSession(),
        _permission_requirement(live_github_config, identity.actor_login),
    )
    assert isinstance(permission, RepositoryActorPermissionEvidence)
    raw_permission = _payload(transport.responses[1])
    expected_permission = {"maintain": "write", "triage": "read"}.get(
        str(raw_permission.get("permission")).casefold(),
        str(raw_permission.get("permission")).casefold(),
    )
    assert permission.permission == expected_permission
    assert permission.temporal_scope == "current"
    assert permission.actor_login.casefold() == identity.actor_login.casefold()
    if permission.actor_id is not None:
        assert permission.actor_id == identity.actor_id

    evaluation = evaluate_condition(
        f"github.actor == '{identity.actor_login}' && github.actor_id == {identity.actor_id}",
        target_id="live:job:actor",
        target_kind="job",
        trigger_context_id="live:trigger",
        context=ConditionRuntimeContext(
            event_name=identity.event_name,
            profile=AttackerProfile.REPO_WRITER,
            repository_id=live_github_config.repository_id,
            github_run_id=identity.run_id,
            actor_login=identity.actor_login,
            actor_id=identity.actor_id,
            triggering_actor_login=identity.triggering_actor_login,
            triggering_actor_id=identity.triggering_actor_id,
            run_event_name=identity.event_name,
            actor_permissions={
                identity.actor_login.casefold(): (permission.permission, permission.actor_id)
            },
        ),
    )
    expected_authorization = (
        TriState.TRUE if permission.permission in {"admin", "write"} else TriState.FALSE
    )
    assert evaluation.verdict is TriState.TRUE
    assert evaluation.current_actor_authorization is expected_authorization
    assert evaluation.authorization_scope == "current"
    assert evaluation.authorization_missing_evidence == []
    assert resolver.metrics.network_requests == 2
    assert all(headers.get("Authorization") == "<redacted>" for _, headers in transport.requests)


@pytest.mark.asyncio
async def test_live_rerun_keeps_triggering_actor_non_authoritative(
    live_github_config: LiveGitHubConfig,
) -> None:
    if live_github_config.rerun_id is None:
        pytest.skip("CICD_LIVE_GITHUB_RERUN_ID is not configured")
    transport = RecordingLiveTransport()
    resolver = _resolver(live_github_config, transport)
    identity = await resolver.resolve(
        StatelessSession(),
        _identity_requirement(live_github_config, live_github_config.rerun_id),
    )
    assert isinstance(identity, WorkflowRunIdentityEvidence)
    assert identity.run_attempt > 1
    assert identity.triggering_actor_login is not None

    evaluation = evaluate_condition(
        f"github.triggering_actor == '{identity.triggering_actor_login}'",
        target_id="live:job:rerun",
        target_kind="job",
        trigger_context_id="live:trigger",
        context=ConditionRuntimeContext(
            event_name=identity.event_name,
            profile=AttackerProfile.REPO_WRITER,
            repository_id=live_github_config.repository_id,
            github_run_id=identity.run_id,
            actor_login=identity.actor_login,
            actor_id=identity.actor_id,
            triggering_actor_login=identity.triggering_actor_login,
            triggering_actor_id=identity.triggering_actor_id,
            run_event_name=identity.event_name,
        ),
    )
    assert evaluation.verdict is TriState.TRUE
    assert evaluation.current_actor_authorization is TriState.UNKNOWN
    assert evaluation.authorization_missing_evidence == [
        "github.triggering_actor is not workflow privilege authority"
    ]
    assert resolver.metrics.network_requests == 1


@pytest.mark.asyncio
async def test_live_missing_run_is_ambiguous_not_found(
    live_github_config: LiveGitHubConfig,
) -> None:
    if live_github_config.missing_run_id is None:
        pytest.skip("CICD_LIVE_GITHUB_MISSING_RUN_ID is not configured")
    transport = RecordingLiveTransport()
    resolver = _resolver(live_github_config, transport)

    with pytest.raises(EvidenceResolutionError) as raised:
        await resolver.resolve(
            StatelessSession(),
            _identity_requirement(live_github_config, live_github_config.missing_run_id),
        )

    assert raised.value.kind is ResolutionFailureKind.AMBIGUOUS_NOT_FOUND
    assert resolver.metrics.network_requests == 1


@pytest.mark.asyncio
async def test_live_resume_revalidates_cached_run_identity(
    live_github_config: LiveGitHubConfig,
    tmp_path: Path,
) -> None:
    repository = FIXTURES / "reasoning-matrix" / "gha-05" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(output_dir=tmp_path / "out", attacker_profile=AttackerProfile.FORK_PR),
    )
    session = create_red_team_session(report.state)
    requirement = _identity_requirement(live_github_config, live_github_config.run_id)

    first_transport = RecordingLiveTransport()
    first_resolver = _resolver(live_github_config, first_transport)
    first = await first_resolver.resolve(session, requirement)
    assert isinstance(first, WorkflowRunIdentityEvidence)
    assert session.investigation_state.transport_cache

    restored = type(session).model_validate(session.model_dump(mode="json", by_alias=True))
    prepare_resume_revalidation(
        restored,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.FORK_PR,
    )

    resume_transport = RecordingLiveTransport()
    resume_resolver = _resolver(live_github_config, resume_transport)
    resumed = await resume_resolver.resolve(restored, requirement)
    assert resumed == first
    assert resume_resolver.metrics.network_requests == 1
    assert len(resume_transport.requests) == 1

    first_response = first_transport.responses[0]
    resume_headers = resume_transport.requests[0][1]
    if first_response.etag:
        assert resume_headers.get("If-None-Match") == first_response.etag
    elif first_response.last_modified:
        assert resume_headers.get("If-Modified-Since") == first_response.last_modified

    resume_response = resume_transport.responses[0]
    if resume_response.status_code == 304:
        assert resume_resolver.metrics.not_modified_hits == 1
    else:
        assert resume_response.status_code == 200
        assert resume_resolver.metrics.not_modified_hits == 0
