from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.investigation import (
    DeploymentBranchPolicyMode,
    EnvironmentProtectionPolicy,
    InvestigationTask,
    InvestigationTaskKind,
    Observation,
    ObservationKind,
    ObservationSource,
    RedTeamSession,
)
from cicd_redteam_agent.domain.models import RepositoryWorkflowPermissionDefault
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_ACCEPT = "application/vnd.github+json"
GITHUB_USER_AGENT = "cicd-redteam-agent"
_GITHUB_HTTPS_REMOTE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
    flags=re.IGNORECASE,
)
_GITHUB_SSH_REMOTE = re.compile(
    r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
    flags=re.IGNORECASE,
)


class GitHubObserverError(RuntimeError):
    """Raised when a read-only GitHub observation cannot be obtained safely."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        rate_limit_remaining: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.rate_limit_remaining = rate_limit_remaining
        self.retry_after_seconds = retry_after_seconds


def _environment_policy(
    payload: dict[str, object], expected_name: str
) -> EnvironmentProtectionPolicy:
    raw_name = payload.get("name")
    if not isinstance(raw_name, str) or raw_name.casefold() != expected_name.casefold():
        raise GitHubObserverError("GitHub environment response did not match the requested name")

    reviewer_count = 0
    prevent_self_review = False
    wait_timer = 0
    custom_rule_count = 0
    raw_rules = payload.get("protection_rules", [])
    if not isinstance(raw_rules, list):
        raise GitHubObserverError("GitHub environment response had invalid protection_rules")
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            raise GitHubObserverError("GitHub environment response had an invalid protection rule")
        rule_type = raw_rule.get("type")
        if not isinstance(rule_type, str):
            raise GitHubObserverError("GitHub environment protection rule omitted its type")
        if rule_type == "required_reviewers":
            reviewers = raw_rule.get("reviewers", [])
            if not isinstance(reviewers, list):
                raise GitHubObserverError("GitHub environment response had invalid reviewers")
            reviewer_count = len(reviewers)
            prevent_self_review = raw_rule.get("prevent_self_review") is True
        elif rule_type == "wait_timer":
            raw_wait = raw_rule.get("wait_timer", 0)
            if not isinstance(raw_wait, int) or isinstance(raw_wait, bool):
                raise GitHubObserverError("GitHub environment response had an invalid wait timer")
            wait_timer = raw_wait
        elif rule_type != "branch_policy":
            custom_rule_count += 1

    branch_mode = DeploymentBranchPolicyMode.UNKNOWN
    raw_branch_policy = payload.get("deployment_branch_policy")
    if isinstance(raw_branch_policy, dict):
        protected = raw_branch_policy.get("protected_branches")
        custom = raw_branch_policy.get("custom_branch_policies")
        if protected is True and custom is True:
            raise GitHubObserverError("GitHub environment response had conflicting branch policies")
        if protected is True:
            branch_mode = DeploymentBranchPolicyMode.PROTECTED
        elif custom is True:
            branch_mode = DeploymentBranchPolicyMode.CUSTOM
        elif protected is False and custom is False:
            branch_mode = DeploymentBranchPolicyMode.NONE

    try:
        return EnvironmentProtectionPolicy(
            environment_name=expected_name,
            required_reviewer_count=reviewer_count,
            prevent_self_review=prevent_self_review,
            wait_timer_minutes=wait_timer,
            deployment_branch_policy=branch_mode,
            custom_protection_rule_count=custom_rule_count,
        )
    except ValueError as exc:
        raise GitHubObserverError("GitHub environment response exceeded supported bounds") from exc


def parse_github_com_repository(remote_url: str) -> tuple[str, str]:
    remote = remote_url.strip()
    match = _GITHUB_HTTPS_REMOTE.fullmatch(remote) or _GITHUB_SSH_REMOTE.fullmatch(remote)
    if match is None:
        raise GitHubObserverError(
            "origin must be a github.com HTTPS or SSH repository URL for automatic observation"
        )
    return match.group("owner"), match.group("repo")


def github_com_repository_from_boundary(boundary: RepositoryBoundary) -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=boundary.root,
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise GitHubObserverError("unable to resolve the repository origin") from None
    return parse_github_com_repository(result.stdout)


GitHubJsonPayload = dict[str, object] | list[object]


@dataclass(frozen=True)
class GitHubHttpResponse:
    status_code: int
    payload: GitHubJsonPayload | None
    etag: str | None = None
    last_modified: str | None = None
    rate_limit_remaining: int | None = None
    retry_after_seconds: int | None = None

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304


class GitHubJsonTransport(Protocol):
    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> GitHubJsonPayload: ...


@dataclass(frozen=True)
class UrllibGitHubJsonTransport:
    timeout_seconds: float = 15.0

    async def get_response(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> GitHubHttpResponse:
        return await asyncio.to_thread(self._get_response_sync, url, headers)

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> GitHubJsonPayload:
        response = await self.get_response(url, headers=headers)
        if response.payload is None:
            raise GitHubObserverError(
                f"GitHub API returned HTTP {response.status_code} without a JSON payload",
                status_code=response.status_code,
            )
        return response.payload

    @staticmethod
    def _header(headers: object, name: str) -> str | None:
        getter = getattr(headers, "get", None)
        if not callable(getter):
            return None
        value = getter(name)
        return str(value) if value is not None else None

    @classmethod
    def _int_header(cls, headers: object, name: str) -> int | None:
        value = cls._header(headers, name)
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    @classmethod
    def _response_metadata(
        cls,
        status_code: int,
        headers: object,
        payload: GitHubJsonPayload | None,
    ) -> GitHubHttpResponse:
        return GitHubHttpResponse(
            status_code=status_code,
            payload=payload,
            etag=cls._header(headers, "ETag"),
            last_modified=cls._header(headers, "Last-Modified"),
            rate_limit_remaining=cls._int_header(headers, "X-RateLimit-Remaining"),
            retry_after_seconds=cls._int_header(headers, "Retry-After"),
        )

    def _get_response_sync(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> GitHubHttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                raw_payload = response.read()
                status_code = int(getattr(response, "status", 200))
                response_headers = getattr(response, "headers", {})
        except HTTPError as exc:
            if exc.code == 304:
                return self._response_metadata(304, exc.headers, None)
            raise GitHubObserverError(
                f"GitHub API returned HTTP {exc.code} for a read-only observation",
                status_code=exc.code,
                rate_limit_remaining=self._int_header(exc.headers, "X-RateLimit-Remaining"),
                retry_after_seconds=self._int_header(exc.headers, "Retry-After"),
            ) from None
        except (URLError, OSError, TimeoutError):
            raise GitHubObserverError("GitHub API read-only observation failed") from None

        try:
            decoded = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GitHubObserverError("GitHub API returned an invalid JSON response") from None
        if not isinstance(decoded, (dict, list)):
            raise GitHubObserverError("GitHub API returned an unexpected JSON response")
        return self._response_metadata(status_code, response_headers, decoded)


@dataclass(frozen=True)
class GitHubReadOnlyObserver:
    owner: str
    repository: str
    token: str = field(repr=False)
    transport: GitHubJsonTransport = field(default_factory=UrllibGitHubJsonTransport, repr=False)

    @classmethod
    def from_boundary(
        cls,
        boundary: RepositoryBoundary,
        *,
        token: str,
        transport: GitHubJsonTransport | None = None,
    ) -> GitHubReadOnlyObserver:
        owner, repository = github_com_repository_from_boundary(boundary)
        return cls(
            owner=owner,
            repository=repository,
            token=token,
            transport=transport or UrllibGitHubJsonTransport(),
        )

    def __post_init__(self) -> None:
        if not self.owner.strip():
            raise ValueError("GitHub repository owner is required")
        if not self.repository.strip():
            raise ValueError("GitHub repository name is required")
        if not self.token.strip():
            raise ValueError("GitHub API token is required")

    async def observe(
        self,
        session: RedTeamSession,
        task: InvestigationTask,
    ) -> Observation:
        owner = quote(self.owner.strip(), safe="")
        repository = quote(self.repository.strip(), safe="")
        if task.kind is InvestigationTaskKind.OBSERVE_REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS:
            endpoint = f"/repos/{owner}/{repository}/actions/permissions/workflow"
        elif task.kind is InvestigationTaskKind.OBSERVE_ENVIRONMENT_PROTECTION_POLICY:
            if task.environment_name is None:
                raise ValueError("environment protection task has no environment name")
            environment = quote(task.environment_name, safe="")
            endpoint = f"/repos/{owner}/{repository}/environments/{environment}"
        else:
            raise ValueError(f"unsupported GitHub observation task: {task.kind}")
        payload = await self.transport.get_json(
            GITHUB_API_BASE_URL + endpoint,
            headers={
                "Accept": GITHUB_ACCEPT,
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": GITHUB_USER_AGENT,
            },
        )
        if not isinstance(payload, dict):
            raise GitHubObserverError("GitHub API returned an unexpected JSON response")
        if task.kind is InvestigationTaskKind.OBSERVE_ENVIRONMENT_PROTECTION_POLICY:
            policy = _environment_policy(payload, task.environment_name or "")
            observation_id = stable_id(
                "obs",
                session.analysis_state.repository.repository_id,
                task.task_id,
                ObservationSource.GITHUB.value,
                policy.model_dump_json(),
            )
            return Observation(
                observation_id=observation_id,
                task_id=task.task_id,
                kind=ObservationKind.ENVIRONMENT_PROTECTION_POLICY,
                source=ObservationSource.GITHUB,
                subject_id=task.subject_id,
                environment_protection_policy=policy,
            )

        raw_permission = payload.get("default_workflow_permissions")
        try:
            permission = RepositoryWorkflowPermissionDefault(str(raw_permission))
        except ValueError:
            raise GitHubObserverError(
                "GitHub API response omitted a supported default_workflow_permissions value"
            ) from None

        observation_id = stable_id(
            "obs",
            session.analysis_state.repository.repository_id,
            task.task_id,
            ObservationSource.GITHUB.value,
            permission.value,
        )
        return Observation(
            observation_id=observation_id,
            task_id=task.task_id,
            kind=ObservationKind.REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS,
            source=ObservationSource.GITHUB,
            subject_id=task.subject_id,
            repository_default_workflow_permissions=permission,
        )
