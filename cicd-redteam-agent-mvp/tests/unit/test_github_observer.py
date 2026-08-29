from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.investigation import (
    DeploymentBranchPolicyMode,
    InvestigationTaskKind,
    ObservationSource,
)
from cicd_redteam_agent.domain.models import (
    AttackerProfile,
    PermissionLevel,
    RepositoryWorkflowPermissionDefault,
)
from cicd_redteam_agent.framework.github_observer import (
    GITHUB_ACCEPT,
    GITHUB_API_VERSION,
    GitHubHttpResponse,
    GitHubObserverError,
    GitHubReadOnlyObserver,
    UrllibGitHubJsonTransport,
    parse_github_com_repository,
)
from cicd_redteam_agent.framework.investigation_runtime import (
    create_red_team_session,
    run_observation_once,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/example-org/example-repo.git", ("example-org", "example-repo")),
        ("https://github.com/example-org/example-repo", ("example-org", "example-repo")),
        ("git@github.com:example-org/example-repo.git", ("example-org", "example-repo")),
    ],
)
def test_parse_github_com_repository(remote: str, expected: tuple[str, str]) -> None:
    assert parse_github_com_repository(remote) == expected


def test_parse_github_com_repository_rejects_untrusted_host() -> None:
    with pytest.raises(GitHubObserverError, match="github.com"):
        parse_github_com_repository("https://github.example.invalid/example-org/example-repo.git")


class FakeHttpResponse:
    status = 200
    headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return b'{"default_workflow_permissions":"read"}'


class MetadataHttpResponse(FakeHttpResponse):
    headers = {
        "ETag": '"etag-123"',
        "Last-Modified": "Sat, 29 Aug 2026 01:02:03 GMT",
        "X-RateLimit-Remaining": "4321",
        "Retry-After": "7",
    }


@pytest.mark.asyncio
async def test_urllib_transport_uses_read_only_get(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def fake_urlopen(request, *, timeout: float):
        observed["method"] = request.get_method()
        observed["url"] = request.full_url
        observed["authorization"] = request.get_header("Authorization")
        observed["timeout"] = timeout
        return FakeHttpResponse()

    monkeypatch.setattr(
        "cicd_redteam_agent.framework.github_observer.urlopen",
        fake_urlopen,
    )
    payload = await UrllibGitHubJsonTransport(timeout_seconds=3.0).get_json(
        "https://api.github.com/repos/example/repo/actions/permissions/workflow",
        headers={"Authorization": "Bearer test-token"},
    )

    assert payload == {"default_workflow_permissions": "read"}
    assert observed == {
        "method": "GET",
        "url": "https://api.github.com/repos/example/repo/actions/permissions/workflow",
        "authorization": "Bearer test-token",
        "timeout": 3.0,
    }


@pytest.mark.asyncio
async def test_urllib_transport_exposes_response_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cicd_redteam_agent.framework.github_observer.urlopen",
        lambda *_args, **_kwargs: MetadataHttpResponse(),
    )

    response = await UrllibGitHubJsonTransport(timeout_seconds=3.0).get_response(
        "https://api.github.com/repos/example/repo/actions/permissions/workflow",
        headers={"Authorization": "Bearer test-token"},
    )

    assert isinstance(response, GitHubHttpResponse)
    assert response.status_code == 200
    assert response.payload == {"default_workflow_permissions": "read"}
    assert response.etag == '"etag-123"'
    assert response.last_modified == "Sat, 29 Aug 2026 01:02:03 GMT"
    assert response.rate_limit_remaining == 4321
    assert response.retry_after_seconds == 7
    assert response.not_modified is False


@dataclass
class RecordingTransport:
    payload: dict[str, object]
    urls: list[str] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> dict[str, object]:
        self.urls.append(url)
        self.headers.append(dict(headers))
        return self.payload


async def _candidate_session(tmp_path: Path):
    repository = FIXTURES / "reasoning-matrix" / "gha-05" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.FORK_PR,
        ),
    )
    return repository, create_red_team_session(report.state)


async def _environment_session(tmp_path: Path):
    repository = FIXTURES / "reasoning-matrix" / "gha-10" / "candidate"
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(
            output_dir=tmp_path / "environment-out",
            attacker_profile=AttackerProfile.FORK_PR,
        ),
    )
    return repository, create_red_team_session(report.state)


@pytest.mark.asyncio
async def test_github_observer_drives_runtime_semantic_refresh(tmp_path: Path) -> None:
    repository, session = await _candidate_session(tmp_path)
    transport = RecordingTransport(
        {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        }
    )
    observer = GitHubReadOnlyObserver(
        owner="example-org",
        repository="example-repo",
        token="super-secret-test-token",
        transport=transport,
    )

    observation = await run_observation_once(
        session,
        observer,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.FORK_PR,
    )

    assert observation is not None
    assert observation.source is ObservationSource.GITHUB
    assert (
        observation.repository_default_workflow_permissions
        is RepositoryWorkflowPermissionDefault.READ
    )
    assert session.knowledge_state.revision == 1
    assert transport.urls == [
        "https://api.github.com/repos/example-org/example-repo/actions/permissions/workflow"
    ]
    headers = transport.headers[0]
    assert headers["Accept"] == GITHUB_ACCEPT
    assert headers["Authorization"] == "Bearer super-secret-test-token"
    assert headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION
    assert "super-secret-test-token" not in repr(observer)

    context = session.analysis_state.job_execution_contexts[0]
    assert context.effective_permissions.get("contents") is PermissionLevel.READ
    assert context.effective_permissions.get("id-token") is PermissionLevel.NONE


@pytest.mark.asyncio
async def test_github_observer_rejects_unknown_permission_without_state_commit(
    tmp_path: Path,
) -> None:
    repository, session = await _candidate_session(tmp_path)
    transport = RecordingTransport({"default_workflow_permissions": "unexpected"})
    observer = GitHubReadOnlyObserver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )

    with pytest.raises(GitHubObserverError, match="supported default_workflow_permissions"):
        await run_observation_once(
            session,
            observer,
            boundary=RepositoryBoundary(repository),
            profile=AttackerProfile.FORK_PR,
        )

    assert session.knowledge_state.revision == 0
    assert session.knowledge_state.repository_default_workflow_permissions is None
    assert session.investigation_state.active_lease is None
    assert all(task.attempts == 1 for task in session.investigation_state.tasks.values())
    assert all(task.status.value == "READY" for task in session.investigation_state.tasks.values())


@pytest.mark.asyncio
async def test_github_observer_reads_normalized_environment_policy(tmp_path: Path) -> None:
    repository, session = await _environment_session(tmp_path)
    transport = RecordingTransport(
        {
            "name": "production",
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "prevent_self_review": True,
                    "reviewers": [{"type": "User", "reviewer": {"login": "sensitive-reviewer"}}],
                },
                {"type": "wait_timer", "wait_timer": 30},
                {"type": "custom", "app": {"slug": "deployment-gate"}},
            ],
            "deployment_branch_policy": {
                "protected_branches": True,
                "custom_branch_policies": False,
            },
        }
    )
    observer = GitHubReadOnlyObserver(
        owner="example-org",
        repository="example-repo",
        token="test-token",
        transport=transport,
    )

    observation = await run_observation_once(
        session,
        observer,
        boundary=RepositoryBoundary(repository),
        profile=AttackerProfile.FORK_PR,
    )

    assert observation is not None
    task = session.investigation_state.tasks[observation.task_id]
    assert task.kind is InvestigationTaskKind.OBSERVE_ENVIRONMENT_PROTECTION_POLICY
    assert transport.urls == [
        "https://api.github.com/repos/example-org/example-repo/environments/production"
    ]
    policy = observation.environment_protection_policy
    assert policy is not None
    assert policy.required_reviewer_count == 1
    assert policy.prevent_self_review is True
    assert policy.wait_timer_minutes == 30
    assert policy.deployment_branch_policy is DeploymentBranchPolicyMode.PROTECTED
    assert policy.custom_protection_rule_count == 1
    assert "sensitive-reviewer" not in observation.model_dump_json()
