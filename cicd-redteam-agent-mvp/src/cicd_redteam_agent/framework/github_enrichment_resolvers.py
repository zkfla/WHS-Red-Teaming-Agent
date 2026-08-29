from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

from cicd_redteam_agent.domain.evidence_requirement import EvidenceRequirement
from cicd_redteam_agent.domain.investigation import (
    ActionAdvisoryEvidence,
    ActionAdvisoryItem,
    ActionProvenanceEvidence,
    AuditEventItem,
    AuditEventsEvidence,
    ObservationSource,
    RedTeamSession,
    SecretMetadataEvidence,
    SecretMetadataItem,
)
from cicd_redteam_agent.domain.models import EvidenceProvider, EvidenceRequirementKind, TriState
from cicd_redteam_agent.framework.evidence_resolver import (
    EvidenceResolutionError,
    ResolutionFailureKind,
    ResolverDescriptor,
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
    github_com_repository_from_boundary,
)
from cicd_redteam_agent.framework.github_resolvers import GitHubResolverMetrics, _resolution_error
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary


def _parse_action_subject(subject_id: str) -> tuple[str, str, str | None, str]:
    raw = subject_id.removeprefix("external:")
    if "@" not in raw:
        raise EvidenceResolutionError(
            ResolutionFailureKind.NOT_APPLICABLE,
            "action provenance subject has no @ref component",
        )
    path, ref = raw.rsplit("@", 1)
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2 or not ref:
        raise EvidenceResolutionError(
            ResolutionFailureKind.NOT_APPLICABLE,
            "action provenance subject is not owner/repository[/path]@ref",
        )
    return parts[0], parts[1], "/".join(parts[2:]) or None, ref


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, int) and not isinstance(value, bool):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


@dataclass(frozen=True)
class GitHubEnrichmentResolver:
    owner: str
    repository: str
    token: str = field(repr=False)
    transport: GitHubJsonTransport = field(default_factory=UrllibGitHubJsonTransport, repr=False)
    metrics: GitHubResolverMetrics = field(default_factory=GitHubResolverMetrics, compare=False)

    observation_source = ObservationSource.GITHUB
    descriptor = ResolverDescriptor(
        resolver_id="github.enrichment.v1",
        provider=EvidenceProvider.GITHUB,
        supported_kinds=frozenset(
            {
                EvidenceRequirementKind.GITHUB_ACTION_PROVENANCE,
                EvidenceRequirementKind.GITHUB_ACTION_ADVISORY,
                EvidenceRequirementKind.GITHUB_SECRET_METADATA,
                EvidenceRequirementKind.GITHUB_AUDIT_EVENTS,
            }
        ),
        required_permissions=(
            "Metadata: read for public action provenance/advisories",
            "Secrets: read for secret metadata",
            "Organization administration/audit-log read for audit evidence",
        ),
        max_calls=4,
        timeout_seconds=15.0,
    )

    @classmethod
    def from_boundary(
        cls,
        boundary: RepositoryBoundary,
        *,
        token: str,
        transport: GitHubJsonTransport | None = None,
    ) -> GitHubEnrichmentResolver:
        owner, repository = github_com_repository_from_boundary(boundary)
        return cls(
            owner=owner,
            repository=repository,
            token=token,
            transport=transport or UrllibGitHubJsonTransport(),
        )

    def __post_init__(self) -> None:
        if not self.owner.strip() or not self.repository.strip() or not self.token.strip():
            raise ValueError("GitHub enrichment resolver requires owner, repository, and token")

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": GITHUB_ACCEPT,
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": GITHUB_USER_AGENT,
        }

    async def _get(self, url: str, *, allow_404: bool = False) -> GitHubJsonPayload | None:
        if self.metrics.current_resolution_requests >= self.descriptor.max_calls:
            raise EvidenceResolutionError(
                ResolutionFailureKind.BUDGET_EXHAUSTED,
                f"GitHub enrichment exceeded {self.descriptor.max_calls} GETs for one requirement",
            )
        self.metrics.current_resolution_requests += 1
        self.metrics.network_requests += 1
        try:
            get_response = getattr(self.transport, "get_response", None)
            if callable(get_response):
                response = await get_response(url, headers=self._headers())
                if not isinstance(response, GitHubHttpResponse):
                    raise EvidenceResolutionError(
                        ResolutionFailureKind.TRANSIENT,
                        "GitHub transport returned an invalid response envelope",
                    )
                if response.payload is None:
                    raise EvidenceResolutionError(
                        ResolutionFailureKind.TRANSIENT,
                        f"GitHub returned HTTP {response.status_code} without JSON",
                    )
                return response.payload
            return await self.transport.get_json(url, headers=self._headers())
        except GitHubObserverError as exc:
            if allow_404 and exc.status_code == 404:
                return None
            raise _resolution_error(exc) from None

    async def resolve(self, session: RedTeamSession, requirement: EvidenceRequirement):
        self.metrics.current_resolution_requests = 0
        if requirement.kind is EvidenceRequirementKind.GITHUB_ACTION_PROVENANCE:
            return await self._action_provenance(requirement)
        if requirement.kind is EvidenceRequirementKind.GITHUB_ACTION_ADVISORY:
            return await self._action_advisory(requirement)
        if requirement.kind is EvidenceRequirementKind.GITHUB_SECRET_METADATA:
            return await self._secret_metadata(requirement)
        if requirement.kind is EvidenceRequirementKind.GITHUB_AUDIT_EVENTS:
            return await self._audit_events(requirement)
        raise EvidenceResolutionError(
            ResolutionFailureKind.UNSUPPORTED,
            f"GitHub enrichment does not support {requirement.kind.value}",
        )

    async def _action_provenance(
        self, requirement: EvidenceRequirement
    ) -> ActionProvenanceEvidence:
        owner, repository, subpath, ref = _parse_action_subject(requirement.subject_id)
        observed_at = datetime.now(UTC)
        if len(ref) == 40 and all(char in "0123456789abcdefABCDEF" for char in ref):
            return ActionProvenanceEvidence(
                action_owner=owner,
                action_repository=repository,
                subpath=subpath,
                requested_ref=ref,
                resolved_sha=ref.lower(),
                ref_kind="full_sha",
                immutable_releases_enabled=None,
                release_exists_for_tag=None,
                immutable_release_tag=TriState.TRUE,
                observed_at=observed_at,
            )

        repo_base = (
            f"{GITHUB_API_BASE_URL}/repos/"
            f"{quote(owner, safe='')}/{quote(repository, safe='')}"
        )
        encoded_ref = quote(ref, safe="")
        tag = await self._get(f"{repo_base}/git/ref/tags/{encoded_ref}", allow_404=True)
        if tag is not None:
            resolved_sha: str | None = None
            if isinstance(tag, dict) and isinstance(tag.get("object"), dict):
                obj = tag["object"]
                raw_sha = obj.get("sha")
                object_type = obj.get("type")
                if object_type == "commit" and isinstance(raw_sha, str) and len(raw_sha) == 40:
                    resolved_sha = raw_sha
                elif object_type == "tag" and isinstance(raw_sha, str):
                    annotated = await self._get(f"{repo_base}/git/tags/{quote(raw_sha, safe='')}")
                    if isinstance(annotated, dict) and isinstance(annotated.get("object"), dict):
                        commit_sha = annotated["object"].get("sha")
                        if isinstance(commit_sha, str) and len(commit_sha) == 40:
                            resolved_sha = commit_sha
            release = await self._get(f"{repo_base}/releases/tags/{encoded_ref}", allow_404=True)
            release_exists = isinstance(release, dict)
            immutable = (
                TriState.TRUE
                if release_exists and release.get("immutable") is True
                else TriState.FALSE
                if release_exists and release.get("immutable") is False
                else TriState.UNKNOWN
            )
            return ActionProvenanceEvidence(
                action_owner=owner,
                action_repository=repository,
                subpath=subpath,
                requested_ref=ref,
                resolved_sha=resolved_sha,
                ref_kind="tag",
                immutable_releases_enabled=None,
                release_exists_for_tag=release_exists,
                immutable_release_tag=immutable,
                observed_at=observed_at,
            )

        head = await self._get(f"{repo_base}/git/ref/heads/{encoded_ref}", allow_404=True)
        if isinstance(head, dict) and isinstance(head.get("object"), dict):
            raw_sha = head["object"].get("sha")
            return ActionProvenanceEvidence(
                action_owner=owner,
                action_repository=repository,
                subpath=subpath,
                requested_ref=ref,
                resolved_sha=(raw_sha if isinstance(raw_sha, str) and len(raw_sha) == 40 else None),
                ref_kind="branch",
                observed_at=observed_at,
            )
        return ActionProvenanceEvidence(
            action_owner=owner,
            action_repository=repository,
            subpath=subpath,
            requested_ref=ref,
            resolved_sha=None,
            ref_kind="unknown",
            observed_at=observed_at,
        )

    async def _action_advisory(self, requirement: EvidenceRequirement) -> ActionAdvisoryEvidence:
        owner, repository, _subpath, ref = _parse_action_subject(requirement.subject_id)
        package = f"{owner}/{repository}"
        query = urlencode(
            {
                "ecosystem": "actions",
                "affects": f"{package}@{ref}",
                "per_page": 100,
            }
        )
        url = f"{GITHUB_API_BASE_URL}/advisories?{query}"
        payload = await self._get(url)
        if not isinstance(payload, list):
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub advisory response was not a list",
            )
        advisories: list[ActionAdvisoryItem] = []
        for item in payload[:100]:
            if not isinstance(item, dict):
                continue
            ghsa_id = item.get("ghsa_id")
            severity = item.get("severity")
            summary = item.get("summary")
            if not all(isinstance(value, str) and value for value in (ghsa_id, severity, summary)):
                continue
            vulnerable_range = None
            vulnerabilities = item.get("vulnerabilities")
            if isinstance(vulnerabilities, list):
                for vulnerability in vulnerabilities:
                    if not isinstance(vulnerability, dict):
                        continue
                    package_info = vulnerability.get("package")
                    if (
                        isinstance(package_info, dict)
                        and str(package_info.get("ecosystem", "")).casefold() == "actions"
                        and str(package_info.get("name", "")).casefold() == package.casefold()
                    ):
                        raw_range = vulnerability.get("vulnerable_version_range")
                        if isinstance(raw_range, str):
                            vulnerable_range = raw_range
                            break
            advisories.append(
                ActionAdvisoryItem(
                    ghsa_id=ghsa_id,
                    severity=severity,
                    summary=summary,
                    published_at=_parse_datetime(item.get("published_at")),
                    updated_at=_parse_datetime(item.get("updated_at")),
                    vulnerable_version_range=vulnerable_range,
                )
            )
        return ActionAdvisoryEvidence(
            package_name=package,
            requested_ref=ref,
            query=query,
            affected=TriState.TRUE if advisories else TriState.FALSE,
            advisories=advisories,
            observed_at=datetime.now(UTC),
        )

    async def _secret_metadata(self, requirement: EvidenceRequirement) -> SecretMetadataEvidence:
        qualifier = requirement.qualifier or "repository"
        owner = quote(self.owner, safe="")
        repository = quote(self.repository, safe="")
        scope: str
        environment_name: str | None = None
        if qualifier == "repository":
            endpoint = f"/repos/{owner}/{repository}/actions/secrets?per_page=100"
            scope = "repository"
        elif qualifier == "organization":
            endpoint = f"/repos/{owner}/{repository}/actions/organization-secrets?per_page=100"
            scope = "organization"
        elif qualifier.startswith("environment:"):
            environment_name = qualifier.split(":", 1)[1]
            if not environment_name:
                raise EvidenceResolutionError(
                    ResolutionFailureKind.NOT_APPLICABLE,
                    "environment secret metadata requires an environment name",
                )
            endpoint = (
                f"/repos/{owner}/{repository}/environments/"
                f"{quote(environment_name, safe='')}/secrets?per_page=100"
            )
            scope = "environment"
        else:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "unsupported secret metadata scope",
            )
        payload = await self._get(f"{GITHUB_API_BASE_URL}{endpoint}")
        if not isinstance(payload, dict) or not isinstance(payload.get("secrets"), list):
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub secret metadata response omitted the secrets list",
            )
        secrets: list[SecretMetadataItem] = []
        for item in payload["secrets"][:1000]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            visibility = item.get("visibility")
            secrets.append(
                SecretMetadataItem(
                    name=item["name"],
                    scope=scope,
                    created_at=_parse_datetime(item.get("created_at")),
                    updated_at=_parse_datetime(item.get("updated_at")),
                    visibility=(
                        visibility
                        if visibility in {"all", "private", "selected"}
                        else None
                    ),
                    environment_name=environment_name,
                )
            )
        return SecretMetadataEvidence(secrets=secrets, observed_at=datetime.now(UTC))

    async def _audit_events(self, requirement: EvidenceRequirement) -> AuditEventsEvidence:
        now = datetime.now(UTC)
        start = now - timedelta(days=180)
        repository_name = f"{self.owner}/{self.repository}"
        base_query = requirement.qualifier or f"repo:{repository_name}"
        query = (
            f"{base_query} created:{start.strftime('%Y-%m-%d')}..{now.strftime('%Y-%m-%d')}"
        )
        url = (
            f"{GITHUB_API_BASE_URL}/orgs/{quote(self.owner, safe='')}/audit-log?"
            + urlencode({"phrase": query, "include": "all", "order": "asc", "per_page": 100})
        )
        payload = await self._get(url)
        if not isinstance(payload, list):
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "GitHub audit-log response was not a list",
            )
        events: list[AuditEventItem] = []
        for item in payload[:1000]:
            if not isinstance(item, dict) or not isinstance(item.get("action"), str):
                continue
            actor_id = item.get("actor_id")
            runner_id = item.get("runner_id")
            events.append(
                AuditEventItem(
                    action=item["action"],
                    actor=item.get("actor") if isinstance(item.get("actor"), str) else None,
                    actor_id=actor_id if isinstance(actor_id, int) and actor_id > 0 else None,
                    created_at=_parse_datetime(item.get("created_at") or item.get("@timestamp")),
                    repository=(
                        item.get("repo")
                        if isinstance(item.get("repo"), str)
                        else item.get("repository")
                        if isinstance(item.get("repository"), str)
                        else None
                    ),
                    runner_id=runner_id if isinstance(runner_id, int) and runner_id > 0 else None,
                    key=(
                        item.get("key")
                        if isinstance(item.get("key"), str)
                        else item.get("secret_name")
                        if isinstance(item.get("secret_name"), str)
                        else None
                    ),
                )
            )
        return AuditEventsEvidence(
            organization=self.owner,
            query=query,
            coverage_start=start,
            coverage_end=now,
            permission_state="read",
            events=events,
            observed_at=now,
        )
