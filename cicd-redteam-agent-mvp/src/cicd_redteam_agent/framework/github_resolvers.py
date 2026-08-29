from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase
from urllib.parse import quote

from cicd_redteam_agent.domain.event_provenance import (
    event_principal_qualifier,
    parse_event_principal_qualifier,
)
from cicd_redteam_agent.domain.evidence_requirement import EvidenceRequirement, evidence_key
from cicd_redteam_agent.domain.investigation import (
    ActionsPolicyEvidence,
    EffectiveBranchRulesEvidence,
    EnvironmentBranchPatternsEvidence,
    EnvironmentPolicyEvidence,
    EventPrincipalEvidence,
    ExternalTransportCacheEntry,
    ForkPullRequestPolicyEvidence,
    GitHubOidcSubjectConfigEvidence,
    ObservationSource,
    PendingDeploymentEnvironmentEvidence,
    PendingDeploymentStateEvidence,
    RedTeamSession,
    RepositoryActorPermissionEvidence,
    RepositoryMetadataEvidence,
    RepositoryWorkflowPermissionEvidence,
    WorkflowRunEventLinkageEvidence,
    WorkflowRunIdentityEvidence,
    WorkflowRunJobEvidence,
    WorkflowRunJobsEvidence,
    WorkflowRunStatusEvidence,
    WorkflowRunStepEvidence,
)
from cicd_redteam_agent.domain.models import (
    EventPrincipalRole,
    EvidenceProvider,
    EvidenceRequirementKind,
    RepositoryWorkflowPermissionDefault,
    TriState,
)
from cicd_redteam_agent.framework.evidence_resolver import (
    EvidenceResolutionError,
    ResolutionFailureKind,
    ResolverDescriptor,
    ResolverRegistry,
)
from cicd_redteam_agent.framework.github_observer import (
    GITHUB_ACCEPT,
    GITHUB_API_BASE_URL,
    GITHUB_API_VERSION,
    GITHUB_USER_AGENT,
    GitHubHttpResponse,
    GitHubJsonPayload,
    GitHubJsonTransport,
    GitHubObserverError,
    UrllibGitHubJsonTransport,
    _environment_policy,
    github_com_repository_from_boundary,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

MAX_AUTOMATIC_RETRY_WAIT_SECONDS = 2.0
TRANSIENT_RETRY_DELAY_SECONDS = 0.25


def _resolution_error(exc: GitHubObserverError) -> EvidenceResolutionError:
    status = exc.status_code
    if status == 401:
        kind = ResolutionFailureKind.AUTH_REQUIRED
    elif status == 403 and (exc.retry_after_seconds is not None or exc.rate_limit_remaining == 0):
        kind = ResolutionFailureKind.RATE_LIMITED
    elif status == 403:
        kind = ResolutionFailureKind.FORBIDDEN
    elif status == 404:
        kind = ResolutionFailureKind.AMBIGUOUS_NOT_FOUND
    elif status == 429:
        kind = ResolutionFailureKind.RATE_LIMITED
    elif status is not None and status >= 500:
        kind = ResolutionFailureKind.TRANSIENT
    else:
        kind = ResolutionFailureKind.TRANSIENT
    return EvidenceResolutionError(
        kind,
        str(exc),
        retryable=kind in {ResolutionFailureKind.TRANSIENT},
    )


def _dict(payload: GitHubJsonPayload, *, context: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise EvidenceResolutionError(
            ResolutionFailureKind.TRANSIENT,
            f"GitHub {context} response was not a JSON object",
        )
    return payload


def _bool(payload: dict[str, object], key: str, *, required: bool = True) -> bool | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if not required and value is None:
        return None
    raise EvidenceResolutionError(
        ResolutionFailureKind.TRANSIENT,
        f"GitHub response omitted boolean field {key}",
    )


def _str(payload: dict[str, object], key: str, *, required: bool = True) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    if not required and value is None:
        return None
    raise EvidenceResolutionError(
        ResolutionFailureKind.TRANSIENT,
        f"GitHub response omitted string field {key}",
    )


@dataclass
class GitHubResolverMetrics:
    network_requests: int = 0
    cache_hits: int = 0
    not_modified_hits: int = 0
    rate_limit_errors: int = 0
    retries: int = 0
    rate_limit_remaining: int | None = None
    current_resolution_requests: int = 0


@dataclass(frozen=True)
class GitHubCompositeResolver:
    owner: str
    repository: str
    token: str = field(repr=False)
    transport: GitHubJsonTransport = field(default_factory=UrllibGitHubJsonTransport, repr=False)
    metrics: GitHubResolverMetrics = field(default_factory=GitHubResolverMetrics, compare=False)
    _response_cache: dict[str, GitHubHttpResponse] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _resolution_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    _revalidate_urls: set[str] = field(default_factory=set, repr=False, compare=False)
    _hydrated_revalidation_epoch: int | None = field(default=None, repr=False, compare=False)

    observation_source = ObservationSource.GITHUB
    descriptor = ResolverDescriptor(
        resolver_id="github.security-policy.v1",
        provider=EvidenceProvider.GITHUB,
        supported_kinds=frozenset(
            {
                EvidenceRequirementKind.GITHUB_REPOSITORY_METADATA,
                EvidenceRequirementKind.GITHUB_OIDC_SUBJECT_CONFIG,
                EvidenceRequirementKind.GITHUB_REPOSITORY_DEFAULT_WORKFLOW_PERMISSION,
                EvidenceRequirementKind.GITHUB_FORK_PR_WORKFLOW_POLICY,
                EvidenceRequirementKind.GITHUB_ACTIONS_POLICY,
                EvidenceRequirementKind.GITHUB_EFFECTIVE_BRANCH_RULES,
                EvidenceRequirementKind.GITHUB_ENVIRONMENT_POLICY,
                EvidenceRequirementKind.GITHUB_ENVIRONMENT_BRANCH_PATTERNS,
                EvidenceRequirementKind.GITHUB_PENDING_DEPLOYMENT_STATE,
                EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_JOBS,
                EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY,
                EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_STATUS,
                EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
                EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL,
                EvidenceRequirementKind.GITHUB_REPOSITORY_ACTOR_PERMISSION,
            }
        ),
        required_permissions=(
            "Metadata: read",
            "Actions: read",
            "Administration: read",
            "Issues: read",
            "Pull requests: read",
        ),
        max_calls=2,
        timeout_seconds=15.0,
    )

    @classmethod
    def from_boundary(
        cls,
        boundary: RepositoryBoundary,
        *,
        token: str,
        transport: GitHubJsonTransport | None = None,
    ) -> GitHubCompositeResolver:
        owner, repository = github_com_repository_from_boundary(boundary)
        return cls(
            owner=owner,
            repository=repository,
            token=token,
            transport=transport or UrllibGitHubJsonTransport(),
        )

    def __post_init__(self) -> None:
        if not self.owner.strip() or not self.repository.strip() or not self.token.strip():
            raise ValueError("GitHub resolver requires owner, repository, and token")

    def _hydrate_transport_cache(self, session: RedTeamSession) -> None:
        investigation = getattr(session, "investigation_state", None)
        if investigation is None:
            return
        epoch = investigation.resume_revalidation_epoch
        if self._hydrated_revalidation_epoch == epoch:
            return
        self._response_cache.clear()
        self._revalidate_urls.clear()
        for cache_key, entry in investigation.transport_cache.items():
            if entry.provider is not EvidenceProvider.GITHUB:
                continue
            self._response_cache[cache_key] = GitHubHttpResponse(
                status_code=entry.status_code,
                payload=entry.payload,
                etag=entry.etag,
                last_modified=entry.last_modified,
            )
            if epoch > 0:
                self._revalidate_urls.add(cache_key)
        object.__setattr__(self, "_hydrated_revalidation_epoch", epoch)

    def _persist_transport_cache(self, session: RedTeamSession) -> None:
        investigation = getattr(session, "investigation_state", None)
        if investigation is None:
            return
        for cache_key, response in self._response_cache.items():
            if response.payload is None:
                continue
            investigation.transport_cache[cache_key] = ExternalTransportCacheEntry(
                provider=EvidenceProvider.GITHUB,
                cache_key=cache_key,
                status_code=200,
                payload=response.payload,
                etag=response.etag,
                last_modified=response.last_modified,
            )

    async def _request_response(
        self,
        url: str,
        *,
        headers: dict[str, str],
    ) -> GitHubHttpResponse:
        if self.metrics.current_resolution_requests >= self.descriptor.max_calls:
            raise EvidenceResolutionError(
                ResolutionFailureKind.BUDGET_EXHAUSTED,
                (
                    f"GitHub resolver exceeded its {self.descriptor.max_calls}-request "
                    "per-resolution budget"
                ),
            )
        self.metrics.current_resolution_requests += 1
        self.metrics.network_requests += 1
        get_response = getattr(self.transport, "get_response", None)
        if callable(get_response):
            response = await get_response(url, headers=headers)
            if not isinstance(response, GitHubHttpResponse):
                raise EvidenceResolutionError(
                    ResolutionFailureKind.TRANSIENT,
                    "GitHub transport returned an invalid response envelope",
                )
            return response
        payload = await self.transport.get_json(url, headers=headers)
        return GitHubHttpResponse(status_code=200, payload=payload)

    async def _get(self, endpoint: str) -> GitHubJsonPayload:
        owner = quote(self.owner.strip(), safe="")
        repository = quote(self.repository.strip(), safe="")
        url = f"{GITHUB_API_BASE_URL}/repos/{owner}/{repository}{endpoint}"
        cached = self._response_cache.get(url)
        requires_revalidation = url in self._revalidate_urls
        if cached is not None and cached.payload is not None and not requires_revalidation:
            self.metrics.cache_hits += 1
            return cached.payload
        headers = {
            "Accept": GITHUB_ACCEPT,
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": GITHUB_USER_AGENT,
        }
        if requires_revalidation and cached is not None:
            if cached.etag:
                headers["If-None-Match"] = cached.etag
            elif cached.last_modified:
                headers["If-Modified-Since"] = cached.last_modified
        retry_attempted = False
        while True:
            try:
                response = await self._request_response(url, headers=headers)
                break
            except GitHubObserverError as exc:
                failure = _resolution_error(exc)
                if failure.kind is ResolutionFailureKind.RATE_LIMITED:
                    self.metrics.rate_limit_errors += 1
                if exc.rate_limit_remaining is not None:
                    self.metrics.rate_limit_remaining = exc.rate_limit_remaining
                retry_delay: float | None = None
                if (
                    not retry_attempted
                    and self.metrics.current_resolution_requests < self.descriptor.max_calls
                ):
                    if (
                        failure.kind is ResolutionFailureKind.RATE_LIMITED
                        and exc.retry_after_seconds is not None
                        and 0 <= exc.retry_after_seconds <= MAX_AUTOMATIC_RETRY_WAIT_SECONDS
                    ):
                        retry_delay = float(exc.retry_after_seconds)
                    elif failure.kind is ResolutionFailureKind.TRANSIENT:
                        retry_delay = TRANSIENT_RETRY_DELAY_SECONDS
                if retry_delay is None:
                    raise failure from None
                retry_attempted = True
                self.metrics.retries += 1
                await asyncio.sleep(retry_delay)

        if response.rate_limit_remaining is not None:
            self.metrics.rate_limit_remaining = response.rate_limit_remaining
        if response.not_modified:
            if cached is None or cached.payload is None:
                raise EvidenceResolutionError(
                    ResolutionFailureKind.TRANSIENT,
                    "GitHub returned 304 without a persisted cached representation",
                )
            self.metrics.not_modified_hits += 1
            refreshed = GitHubHttpResponse(
                status_code=200,
                payload=cached.payload,
                etag=response.etag or cached.etag,
                last_modified=response.last_modified or cached.last_modified,
                rate_limit_remaining=response.rate_limit_remaining,
            )
            self._response_cache[url] = refreshed
            self._revalidate_urls.discard(url)
            return cached.payload
        if response.payload is None:
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                f"GitHub returned HTTP {response.status_code} without a JSON payload",
            )
        self._response_cache[url] = response
        self._revalidate_urls.discard(url)
        return response.payload

    async def resolve(self, session: RedTeamSession, requirement: EvidenceRequirement):
        async with self._resolution_lock:
            self._hydrate_transport_cache(session)
            self.metrics.current_resolution_requests = 0
            try:
                return await self._resolve_unlocked(session, requirement)
            finally:
                self._persist_transport_cache(session)

    async def _resolve_unlocked(self, session: RedTeamSession, requirement: EvidenceRequirement):
        kind = requirement.kind
        if kind is EvidenceRequirementKind.GITHUB_REPOSITORY_METADATA:
            return await self._repository_metadata()
        if kind is EvidenceRequirementKind.GITHUB_OIDC_SUBJECT_CONFIG:
            return await self._oidc_subject_config()
        if kind is EvidenceRequirementKind.GITHUB_REPOSITORY_DEFAULT_WORKFLOW_PERMISSION:
            return await self._repository_default_permission()
        if kind is EvidenceRequirementKind.GITHUB_FORK_PR_WORKFLOW_POLICY:
            return await self._fork_pr_policy(session)
        if kind is EvidenceRequirementKind.GITHUB_ACTIONS_POLICY:
            return await self._actions_policy()
        if kind is EvidenceRequirementKind.GITHUB_EFFECTIVE_BRANCH_RULES:
            return await self._effective_branch_rules(requirement)
        if kind is EvidenceRequirementKind.GITHUB_ENVIRONMENT_POLICY:
            return await self._environment_policy(requirement)
        if kind is EvidenceRequirementKind.GITHUB_ENVIRONMENT_BRANCH_PATTERNS:
            return await self._environment_branch_patterns(requirement)
        if kind is EvidenceRequirementKind.GITHUB_PENDING_DEPLOYMENT_STATE:
            return await self._pending_deployments(requirement)
        if kind is EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_JOBS:
            return await self._workflow_run_jobs(requirement)
        if kind is EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY:
            return await self._workflow_run_identity(requirement)
        if kind is EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_STATUS:
            return await self._workflow_run_status(requirement)
        if kind is EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE:
            return await self._workflow_run_event_linkage(session, requirement)
        if kind is EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL:
            return await self._event_principal(session, requirement)
        if kind is EvidenceRequirementKind.GITHUB_REPOSITORY_ACTOR_PERMISSION:
            return await self._repository_actor_permission(requirement)
        raise EvidenceResolutionError(
            ResolutionFailureKind.UNSUPPORTED,
            f"GitHub resolver does not support {kind.value}",
        )

    async def _repository_metadata(self) -> RepositoryMetadataEvidence:
        payload = _dict(await self._get(""), context="repository metadata")
        private = _bool(payload, "private")
        return RepositoryMetadataEvidence(
            owner=self.owner,
            repository=self.repository,
            private=bool(private),
            visibility=(
                _str(payload, "visibility", required=False) or ("private" if private else "public")
            ),
            default_branch=_str(payload, "default_branch") or "",
            fork=bool(_bool(payload, "fork", required=False) or False),
            owner_id=(
                payload.get("owner", {}).get("id")
                if isinstance(payload.get("owner"), dict)
                and isinstance(payload.get("owner", {}).get("id"), int)
                else None
            ),
            repository_id_number=(
                payload.get("id") if isinstance(payload.get("id"), int) else None
            ),
            created_at=(
                datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
                if isinstance(payload.get("created_at"), str)
                else None
            ),
        )

    async def _oidc_subject_config(self) -> GitHubOidcSubjectConfigEvidence:
        payload = _dict(
            await self._get("/actions/oidc/customization/sub"),
            context="OIDC subject customization",
        )
        include_claim_keys = payload.get("include_claim_keys", [])
        if not isinstance(include_claim_keys, list) or not all(
            isinstance(item, str) for item in include_claim_keys
        ):
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub returned invalid OIDC include_claim_keys",
            )
        immutable = payload.get("use_immutable_subject")
        if immutable is not None and not isinstance(immutable, bool):
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub returned invalid use_immutable_subject",
            )
        return GitHubOidcSubjectConfigEvidence(
            use_default=bool(_bool(payload, "use_default")),
            include_claim_keys=include_claim_keys,
            use_immutable_subject=immutable,
        )

    async def _repository_default_permission(self) -> RepositoryWorkflowPermissionEvidence:
        payload = _dict(
            await self._get("/actions/permissions/workflow"),
            context="default workflow permission",
        )
        raw = payload.get("default_workflow_permissions")
        try:
            value = RepositoryWorkflowPermissionDefault(str(raw))
        except ValueError:
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub response omitted a supported default_workflow_permissions value",
            ) from None
        return RepositoryWorkflowPermissionEvidence(value=value)

    async def _fork_pr_policy(self, session: RedTeamSession) -> ForkPullRequestPolicyEvidence:
        approval = _dict(
            await self._get("/actions/permissions/fork-pr-contributor-approval"),
            context="fork PR contributor approval",
        )
        approval_policy = _str(approval, "approval_policy", required=False)
        repository_id = session.analysis_state.repository.repository_id
        metadata = session.knowledge_state.external_evidence.get(
            evidence_key(EvidenceRequirementKind.GITHUB_REPOSITORY_METADATA, repository_id)
        )
        if not isinstance(metadata, RepositoryMetadataEvidence):
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "fork PR policy resolution requires repository metadata first",
            )
        if not metadata.private:
            return ForkPullRequestPolicyEvidence(
                approval_policy=approval_policy,
                private_repo_settings_applicable=False,
            )
        private_settings = _dict(
            await self._get("/actions/permissions/fork-pr-workflows-private-repos"),
            context="private fork PR workflow policy",
        )
        return ForkPullRequestPolicyEvidence(
            approval_policy=approval_policy,
            private_repo_settings_applicable=True,
            run_workflows_from_fork_pull_requests=_bool(
                private_settings, "run_workflows_from_fork_pull_requests"
            ),
            send_write_tokens_to_workflows=_bool(
                private_settings, "send_write_tokens_to_workflows"
            ),
            send_secrets_and_variables=_bool(private_settings, "send_secrets_and_variables"),
            require_approval_for_fork_pr_workflows=_bool(
                private_settings, "require_approval_for_fork_pr_workflows"
            ),
        )

    async def _actions_policy(self) -> ActionsPolicyEvidence:
        payload = _dict(await self._get("/actions/permissions"), context="Actions policy")
        enabled = _bool(payload, "enabled")
        allowed_actions = _str(payload, "allowed_actions", required=False) or "all"
        sha_pinning_required = bool(_bool(payload, "sha_pinning_required", required=False) or False)
        github_owned_allowed: bool | None = None
        verified_allowed: bool | None = None
        patterns_allowed: list[str] = []
        if allowed_actions == "selected":
            selected = _dict(
                await self._get("/actions/permissions/selected-actions"),
                context="selected Actions policy",
            )
            github_owned_allowed = _bool(selected, "github_owned_allowed", required=False)
            verified_allowed = _bool(selected, "verified_allowed", required=False)
            raw_patterns = selected.get("patterns_allowed", [])
            if not isinstance(raw_patterns, list) or not all(
                isinstance(item, str) for item in raw_patterns
            ):
                raise EvidenceResolutionError(
                    ResolutionFailureKind.TRANSIENT,
                    "GitHub selected Actions policy returned invalid patterns_allowed",
                )
            patterns_allowed = list(raw_patterns)
        return ActionsPolicyEvidence(
            enabled=bool(enabled),
            allowed_actions=allowed_actions,
            sha_pinning_required=sha_pinning_required,
            github_owned_allowed=github_owned_allowed,
            verified_allowed=verified_allowed,
            patterns_allowed=patterns_allowed,
        )

    async def _effective_branch_rules(
        self,
        requirement: EvidenceRequirement,
    ) -> EffectiveBranchRulesEvidence:
        branch = requirement.qualifier
        if not branch:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "effective branch rules require a concrete branch qualifier",
            )
        encoded = quote(branch, safe="")
        branch_payload = _dict(
            await self._get(f"/branches/{encoded}"),
            context="branch metadata",
        )
        protected_raw = _bool(branch_payload, "protected", required=False)
        protected = (
            TriState.TRUE
            if protected_raw is True
            else TriState.FALSE
            if protected_raw is False
            else TriState.UNKNOWN
        )
        legacy_protection_present = TriState.UNKNOWN
        raw_protection = branch_payload.get("protection")
        if isinstance(raw_protection, dict):
            enabled = raw_protection.get("enabled")
            if isinstance(enabled, bool):
                legacy_protection_present = TriState.TRUE if enabled else TriState.FALSE
        elif protected is TriState.FALSE:
            legacy_protection_present = TriState.FALSE

        payload = await self._get(f"/rules/branches/{encoded}")
        if not isinstance(payload, list):
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub effective branch rules response was not a JSON array",
            )
        rule_types: list[str] = []
        requires_pull_request = False
        required_approving_review_count: int | None = None
        require_last_push_approval: bool | None = None
        required_status_check_count: int | None = None
        update_restricted_to_bypass_actors = False
        for item in payload:
            if not isinstance(item, dict):
                continue
            rule_type = item.get("type")
            if not isinstance(rule_type, str):
                continue
            rule_types.append(rule_type)
            parameters = item.get("parameters")
            if rule_type == "update":
                update_restricted_to_bypass_actors = True
            elif rule_type == "pull_request":
                requires_pull_request = True
                if isinstance(parameters, dict):
                    raw_count = parameters.get("required_approving_review_count")
                    if isinstance(raw_count, int) and not isinstance(raw_count, bool):
                        required_approving_review_count = raw_count
                    raw_last_push = parameters.get("require_last_push_approval")
                    if isinstance(raw_last_push, bool):
                        require_last_push_approval = raw_last_push
            elif rule_type == "required_status_checks" and isinstance(parameters, dict):
                raw_checks = parameters.get("required_status_checks")
                if isinstance(raw_checks, list):
                    required_status_check_count = len(raw_checks)

        return EffectiveBranchRulesEvidence(
            branch=branch,
            protected=protected,
            active_rule_types=sorted(set(rule_types)),
            requires_pull_request=requires_pull_request,
            required_approving_review_count=required_approving_review_count,
            require_last_push_approval=require_last_push_approval,
            required_status_check_count=required_status_check_count,
            update_restricted_to_bypass_actors=update_restricted_to_bypass_actors,
            bypass_visibility=TriState.UNKNOWN,
            legacy_protection_present=legacy_protection_present,
        )

    async def _environment_policy(
        self,
        requirement: EvidenceRequirement,
    ) -> EnvironmentPolicyEvidence:
        environment = requirement.qualifier
        if not environment:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "environment policy requires a concrete environment qualifier",
            )
        encoded = quote(environment, safe="")
        payload = _dict(await self._get(f"/environments/{encoded}"), context="environment")
        return EnvironmentPolicyEvidence(policy=_environment_policy(payload, environment))

    async def _environment_branch_patterns(
        self,
        requirement: EvidenceRequirement,
    ) -> EnvironmentBranchPatternsEvidence:
        environment = requirement.qualifier
        if not environment:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "environment branch patterns require a concrete environment qualifier",
            )
        encoded = quote(environment, safe="")
        payload = _dict(
            await self._get(f"/environments/{encoded}/deployment-branch-policies"),
            context="environment deployment branch policy",
        )
        raw = payload.get("branch_policies", [])
        if not isinstance(raw, list):
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub environment branch policy response had invalid branch_policies",
            )
        patterns: set[str] = set()
        branch_patterns: set[str] = set()
        tag_patterns: set[str] = set()
        unknown_target_patterns: set[str] = set()
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            name = str(item["name"])
            patterns.add(name)
            target = item.get("type")
            if target == "branch":
                branch_patterns.add(name)
            elif target == "tag":
                tag_patterns.add(name)
            else:
                unknown_target_patterns.add(name)
        return EnvironmentBranchPatternsEvidence(
            environment_name=environment,
            patterns=sorted(patterns),
            branch_patterns=sorted(branch_patterns),
            tag_patterns=sorted(tag_patterns),
            unknown_target_patterns=sorted(unknown_target_patterns),
        )

    async def _pending_deployments(
        self,
        requirement: EvidenceRequirement,
    ) -> PendingDeploymentStateEvidence:
        raw_run_id = requirement.qualifier
        try:
            run_id = int(raw_run_id) if raw_run_id is not None else 0
        except ValueError:
            run_id = 0
        if run_id <= 0:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "pending deployment state requires a concrete workflow run ID",
            )
        payload = await self._get(f"/actions/runs/{run_id}/pending_deployments")
        if not isinstance(payload, list):
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub pending deployments response was not a JSON array",
            )
        environments: list[PendingDeploymentEnvironmentEvidence] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            raw_environment = item.get("environment")
            if not isinstance(raw_environment, dict):
                continue
            name = raw_environment.get("name")
            if not isinstance(name, str) or not name:
                continue
            raw_wait_timer = item.get("wait_timer", 0)
            wait_timer = (
                raw_wait_timer
                if isinstance(raw_wait_timer, int) and not isinstance(raw_wait_timer, bool)
                else 0
            )
            raw_reviewers = item.get("reviewers", [])
            reviewer_count = len(raw_reviewers) if isinstance(raw_reviewers, list) else 0
            raw_can_approve = item.get("current_user_can_approve")
            current_user_can_approve = (
                raw_can_approve if isinstance(raw_can_approve, bool) else None
            )
            environments.append(
                PendingDeploymentEnvironmentEvidence(
                    environment_name=name,
                    wait_timer_minutes=wait_timer,
                    reviewer_count=reviewer_count,
                    current_user_can_approve=current_user_can_approve,
                )
            )
        return PendingDeploymentStateEvidence(
            run_id=run_id,
            pending_environments=sorted(
                environments,
                key=lambda item: item.environment_name.casefold(),
            ),
        )

    async def _workflow_run_jobs(
        self,
        requirement: EvidenceRequirement,
    ) -> WorkflowRunJobsEvidence:
        raw_run_id = requirement.qualifier
        try:
            run_id = int(raw_run_id) if raw_run_id is not None else 0
        except ValueError:
            run_id = 0
        if run_id <= 0:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "workflow run jobs require a concrete workflow run ID",
            )
        payload = _dict(
            await self._get(f"/actions/runs/{run_id}/jobs?filter=latest&per_page=100"),
            context="workflow run jobs",
        )
        raw_jobs = payload.get("jobs")
        if not isinstance(raw_jobs, list):
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub workflow run jobs response omitted the jobs array",
            )
        total_count = payload.get("total_count")
        if (
            isinstance(total_count, int)
            and not isinstance(total_count, bool)
            and total_count > len(raw_jobs)
        ):
            raise EvidenceResolutionError(
                ResolutionFailureKind.BUDGET_EXHAUSTED,
                "workflow run contains more than the bounded 100-job evidence page",
            )

        jobs: list[WorkflowRunJobEvidence] = []
        for item in raw_jobs:
            if not isinstance(item, dict):
                continue
            github_job_id = item.get("id")
            name = item.get("name")
            status = item.get("status")
            if (
                not isinstance(github_job_id, int)
                or isinstance(github_job_id, bool)
                or github_job_id <= 0
                or not isinstance(name, str)
                or not name.strip()
                or not isinstance(status, str)
                or not status.strip()
            ):
                continue

            def positive_int(value: object) -> int | None:
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
                return None

            raw_conclusion = item.get("conclusion")
            conclusion = raw_conclusion if isinstance(raw_conclusion, str) else None
            raw_labels = item.get("labels")
            labels = (
                sorted(
                    {
                        label.strip()
                        for label in raw_labels
                        if isinstance(label, str) and label.strip()
                    }
                )
                if isinstance(raw_labels, list)
                else []
            )
            steps: list[WorkflowRunStepEvidence] = []
            raw_steps = item.get("steps")
            if isinstance(raw_steps, list):
                for raw_step in raw_steps[:200]:
                    if not isinstance(raw_step, dict):
                        continue
                    number = raw_step.get("number")
                    step_name = raw_step.get("name")
                    step_status = raw_step.get("status")
                    if (
                        not isinstance(number, int)
                        or isinstance(number, bool)
                        or number < 1
                        or not isinstance(step_name, str)
                        or not step_name.strip()
                        or not isinstance(step_status, str)
                        or not step_status.strip()
                    ):
                        continue
                    step_conclusion = raw_step.get("conclusion")
                    steps.append(
                        WorkflowRunStepEvidence(
                            number=number,
                            name=step_name.strip(),
                            status=step_status.strip(),
                            conclusion=(
                                step_conclusion if isinstance(step_conclusion, str) else None
                            ),
                        )
                    )
            jobs.append(
                WorkflowRunJobEvidence(
                    github_job_id=github_job_id,
                    name=name.strip(),
                    status=status.strip(),
                    conclusion=conclusion,
                    runner_id=positive_int(item.get("runner_id")),
                    runner_group_id=positive_int(item.get("runner_group_id")),
                    labels=labels,
                    steps=steps,
                )
            )
        return WorkflowRunJobsEvidence(
            run_id=run_id,
            jobs=sorted(jobs, key=lambda item: (item.name.casefold(), item.github_job_id)),
        )

    async def _workflow_run_status(
        self,
        requirement: EvidenceRequirement,
    ) -> WorkflowRunStatusEvidence:
        raw_run_id = requirement.qualifier
        try:
            run_id = int(raw_run_id) if raw_run_id is not None else 0
        except ValueError:
            run_id = 0
        if run_id <= 0:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "workflow run status requires a concrete workflow run ID",
            )
        payload = _dict(
            await self._get(f"/actions/runs/{run_id}"),
            context="workflow run status",
        )
        if payload.get("id") != run_id:
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub workflow run response did not match the requested run ID",
            )
        status = payload.get("status")
        conclusion = payload.get("conclusion")
        if not isinstance(status, str) or not status.strip():
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub workflow run response omitted status",
            )
        return WorkflowRunStatusEvidence(
            run_id=run_id,
            status=status.strip(),
            conclusion=conclusion if isinstance(conclusion, str) else None,
        )

    async def _workflow_run_identity(
        self,
        requirement: EvidenceRequirement,
    ) -> WorkflowRunIdentityEvidence:
        raw_run_id = requirement.qualifier
        try:
            run_id = int(raw_run_id) if raw_run_id is not None else 0
        except ValueError:
            run_id = 0
        if run_id <= 0:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "workflow run identity requires a concrete workflow run ID",
            )
        payload = _dict(
            await self._get(f"/actions/runs/{run_id}"),
            context="workflow run identity",
        )
        returned_id = payload.get("id")
        if returned_id != run_id:
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub workflow run response did not match the requested run ID",
            )
        actor = payload.get("actor")
        if not isinstance(actor, dict):
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub workflow run response omitted actor identity",
            )
        actor_login = actor.get("login")
        actor_id = actor.get("id")
        event_name = payload.get("event")
        if (
            not isinstance(actor_login, str)
            or not actor_login.strip()
            or not isinstance(actor_id, int)
            or isinstance(actor_id, bool)
            or actor_id <= 0
            or not isinstance(event_name, str)
            or not event_name.strip()
        ):
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub workflow run response contained an invalid actor or event",
            )
        triggering = payload.get("triggering_actor")
        triggering_login: str | None = None
        triggering_id: int | None = None
        if isinstance(triggering, dict):
            raw_login = triggering.get("login")
            raw_id = triggering.get("id")
            if isinstance(raw_login, str) and raw_login.strip():
                triggering_login = raw_login.strip()
            if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0:
                triggering_id = raw_id
        raw_attempt = payload.get("run_attempt", 1)
        run_attempt = (
            raw_attempt
            if isinstance(raw_attempt, int)
            and not isinstance(raw_attempt, bool)
            and raw_attempt > 0
            else 1
        )
        raw_created_at = payload.get("created_at")
        raw_workflow_id = payload.get("workflow_id")
        raw_workflow_name = payload.get("name")
        raw_workflow_path = payload.get("path")
        return WorkflowRunIdentityEvidence(
            run_id=run_id,
            event_name=event_name.strip(),
            workflow_id=(
                raw_workflow_id
                if isinstance(raw_workflow_id, int)
                and not isinstance(raw_workflow_id, bool)
                and raw_workflow_id > 0
                else None
            ),
            workflow_name=(
                raw_workflow_name.strip()
                if isinstance(raw_workflow_name, str) and raw_workflow_name.strip()
                else None
            ),
            workflow_path=(
                raw_workflow_path.strip()
                if isinstance(raw_workflow_path, str) and raw_workflow_path.strip()
                else None
            ),
            run_attempt=run_attempt,
            actor_login=actor_login.strip(),
            actor_id=actor_id,
            triggering_actor_login=triggering_login,
            triggering_actor_id=triggering_id,
            created_at=raw_created_at if isinstance(raw_created_at, str) else None,
        )

    async def _workflow_run_event_linkage(
        self,
        session: RedTeamSession,
        requirement: EvidenceRequirement,
    ) -> WorkflowRunEventLinkageEvidence:
        try:
            run_id = int(requirement.qualifier or "0")
        except ValueError:
            run_id = 0
        if run_id <= 0:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "workflow run event linkage requires a concrete workflow run ID",
            )
        payload = _dict(
            await self._get(f"/actions/runs/{run_id}"),
            context="workflow run event linkage",
        )
        if payload.get("id") != run_id:
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub workflow run linkage response did not match the requested run ID",
            )
        raw_event = payload.get("event")
        if not isinstance(raw_event, str) or not raw_event.strip():
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub workflow run linkage response omitted the event name",
            )
        event_name = raw_event.strip()
        locator = session.github_event_locator
        if locator is not None:
            expected_repository = f"{self.owner}/{self.repository}"
            if (
                locator.repository_full_name is not None
                and locator.repository_full_name.casefold() != expected_repository.casefold()
            ):
                raise EvidenceResolutionError(
                    ResolutionFailureKind.NOT_APPLICABLE,
                    "operator event payload belongs to a different repository",
                )
            locator_event = locator.event_name.casefold()
            run_event = event_name.casefold()
            event_matches = locator_event == run_event or (
                locator_event == "pull_request" and run_event == "pull_request_target"
            )
            if not event_matches:
                raise EvidenceResolutionError(
                    ResolutionFailureKind.NOT_APPLICABLE,
                    "operator event payload does not match the workflow run event",
                )
            return WorkflowRunEventLinkageEvidence(
                run_id=run_id,
                event_name=event_name,
                action=locator.action,
                binding="exact",
                source="operator_event_payload",
                issue_number=locator.issue_number,
                pull_number=locator.pull_number,
                comment_id=locator.comment_id,
                review_id=locator.review_id,
                review_comment_id=locator.review_comment_id,
                label_name=locator.label_name,
                sender_login=locator.sender_login,
                sender_id=locator.sender_id,
                sender_type=locator.sender_type,
                source_hash=locator.source_hash,
            )

        pull_number: int | None = None
        raw_pulls = payload.get("pull_requests")
        if isinstance(raw_pulls, list) and len(raw_pulls) == 1 and isinstance(raw_pulls[0], dict):
            raw_number = raw_pulls[0].get("number")
            if isinstance(raw_number, int) and not isinstance(raw_number, bool) and raw_number > 0:
                pull_number = raw_number
        return WorkflowRunEventLinkageEvidence(
            run_id=run_id,
            event_name=event_name,
            binding="partial",
            source="workflow_run",
            pull_number=pull_number,
        )

    async def _event_principal(
        self,
        session: RedTeamSession,
        requirement: EvidenceRequirement,
    ) -> EventPrincipalEvidence:
        try:
            locator = parse_event_principal_qualifier(requirement.qualifier)
            role = EventPrincipalRole(str(locator.get("role")))
            run_id = int(locator.get("run_id", 0))
        except (TypeError, ValueError) as exc:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "event principal requirement has an invalid exact locator",
            ) from exc
        linkage = next(
            (
                payload
                for payload in session.knowledge_state.external_evidence.values()
                if isinstance(payload, WorkflowRunEventLinkageEvidence) and payload.run_id == run_id
            ),
            None,
        )
        if linkage is None:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "event principal resolution requires workflow event linkage first",
            )
        expected_qualifier = event_principal_qualifier(role, linkage)
        if expected_qualifier is None or expected_qualifier != requirement.qualifier:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "event principal locator does not match committed workflow event linkage",
            )

        if role in {EventPrincipalRole.EVENT_SENDER, EventPrincipalRole.LABEL_ACTOR}:
            resource_id = (
                f"label:{linkage.label_name or 'unknown'}"
                if role is EventPrincipalRole.LABEL_ACTOR
                else f"run:{run_id}:sender"
            )
            return EventPrincipalEvidence(
                run_id=run_id,
                event_name=linkage.event_name,
                event_action=linkage.action,
                principal_role=role,
                login=linkage.sender_login,
                actor_id=linkage.sender_id,
                account_type=linkage.sender_type,
                resource_id=resource_id,
            )

        endpoint: str
        expected_id: int | None = None
        expected_number: int | None = None
        if role is EventPrincipalRole.COMMENT_AUTHOR:
            expected_id = int(locator["comment_id"])
            endpoint = f"/issues/comments/{expected_id}"
        elif role is EventPrincipalRole.ISSUE_AUTHOR:
            expected_number = int(locator["issue_number"])
            endpoint = f"/issues/{expected_number}"
        elif role is EventPrincipalRole.PULL_REQUEST_AUTHOR:
            expected_number = int(locator["pull_number"])
            endpoint = f"/pulls/{expected_number}"
        elif role is EventPrincipalRole.REVIEW_AUTHOR:
            pull_number = int(locator["pull_number"])
            expected_id = int(locator["review_id"])
            endpoint = f"/pulls/{pull_number}/reviews/{expected_id}"
        elif role is EventPrincipalRole.REVIEW_COMMENT_AUTHOR:
            expected_id = int(locator["review_comment_id"])
            endpoint = f"/pulls/comments/{expected_id}"
        else:
            raise EvidenceResolutionError(
                ResolutionFailureKind.UNSUPPORTED,
                f"unsupported event principal role: {role.value}",
            )

        payload = _dict(await self._get(endpoint), context=f"event principal {role.value}")
        if expected_id is not None and payload.get("id") != expected_id:
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub event principal resource ID did not match the requested locator",
            )
        if expected_number is not None and payload.get("number") != expected_number:
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub event principal resource number did not match the requested locator",
            )
        user = payload.get("user")
        login: str | None = None
        actor_id: int | None = None
        account_type: str | None = None
        if isinstance(user, dict):
            raw_login = user.get("login")
            raw_id = user.get("id")
            raw_type = user.get("type")
            if isinstance(raw_login, str) and raw_login.strip():
                login = raw_login.strip()
            if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0:
                actor_id = raw_id
            if isinstance(raw_type, str) and raw_type.strip():
                account_type = raw_type.strip()
        raw_association = payload.get("author_association")
        author_association = (
            raw_association.strip()
            if isinstance(raw_association, str) and raw_association.strip()
            else None
        )
        raw_observed_at = payload.get("submitted_at") or payload.get("created_at")
        performed_via_github_app = (
            payload.get("performed_via_github_app") is not None
            if "performed_via_github_app" in payload
            else None
        )
        return EventPrincipalEvidence(
            run_id=run_id,
            event_name=linkage.event_name,
            event_action=linkage.action,
            principal_role=role,
            login=login,
            actor_id=actor_id,
            account_type=account_type,
            author_association=author_association,
            resource_id=(
                f"id:{expected_id}" if expected_id is not None else f"number:{expected_number}"
            ),
            observed_at=raw_observed_at if isinstance(raw_observed_at, str) else None,
            performed_via_github_app=performed_via_github_app,
        )

    async def _repository_actor_permission(
        self,
        requirement: EvidenceRequirement,
    ) -> RepositoryActorPermissionEvidence:
        actor_login = (requirement.qualifier or "").strip()
        if not actor_login or any(char in actor_login for char in "/\\"):
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "repository actor permission requires a concrete actor login",
            )
        payload = _dict(
            await self._get(f"/collaborators/{quote(actor_login, safe='')}/permission"),
            context="repository actor permission",
        )
        raw_permission = payload.get("permission")
        normalized = {"maintain": "write", "triage": "read"}.get(
            str(raw_permission).casefold(),
            str(raw_permission).casefold(),
        )
        if normalized not in {"admin", "write", "read", "none"}:
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub actor permission response omitted a supported permission",
            )
        user = payload.get("user")
        actor_id: int | None = None
        returned_login = actor_login
        if isinstance(user, dict):
            raw_login = user.get("login")
            raw_id = user.get("id")
            if isinstance(raw_login, str) and raw_login.strip():
                returned_login = raw_login.strip()
            if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0:
                actor_id = raw_id
        if returned_login.casefold() != actor_login.casefold():
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub actor permission response did not match the requested actor",
            )
        raw_role = payload.get("role_name")
        role_name = raw_role if isinstance(raw_role, str) and raw_role else None
        return RepositoryActorPermissionEvidence(
            actor_login=returned_login,
            actor_id=actor_id,
            permission=normalized,
            role_name=role_name,
        )


def build_github_resolver_registry(
    boundary: RepositoryBoundary,
    *,
    token: str,
    transport: GitHubJsonTransport | None = None,
) -> ResolverRegistry:
    resolver = GitHubCompositeResolver.from_boundary(boundary, token=token, transport=transport)
    return ResolverRegistry((resolver,))


def external_action_allowed(reference: str, policy: ActionsPolicyEvidence) -> bool | None:
    if not policy.enabled:
        return False
    target = reference.removeprefix("external:")
    if policy.sha_pinning_required:
        ref = target.rsplit("@", 1)[-1] if "@" in target else ""
        if len(ref) != 40 or any(char not in "0123456789abcdefABCDEF" for char in ref):
            return False
    if policy.allowed_actions == "all":
        return True
    if policy.allowed_actions == "local_only":
        return False
    if policy.allowed_actions != "selected":
        return None
    repo_ref = target
    owner_repo = repo_ref.split("@", 1)[0]
    if owner_repo.startswith("actions/") and policy.github_owned_allowed is True:
        return True
    return any(
        fnmatchcase(repo_ref, pattern) or fnmatchcase(owner_repo, pattern)
        for pattern in policy.patterns_allowed
    ) or (None if policy.verified_allowed else False)
