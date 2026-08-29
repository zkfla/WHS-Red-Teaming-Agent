from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from cicd_redteam_agent.domain.evidence_requirement import EvidenceRequirement
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    EventPrincipalRole,
    EvidenceProvider,
    Job,
    RepositoryWorkflowPermissionDefault,
    SchemaModel,
    TriState,
)


def concrete_environment_name(job: Job) -> str | None:
    raw_name: object = job.environment
    if isinstance(raw_name, dict):
        raw_name = raw_name.get("name")
    if not isinstance(raw_name, str):
        return None
    name = raw_name.strip()
    if not name or "${{" in name:
        return None
    return name


class InvestigationTaskKind(StrEnum):
    RESOLVE_EXTERNAL_EVIDENCE = "RESOLVE_EXTERNAL_EVIDENCE"
    # Legacy values are retained so pre-migration checkpoints remain readable.
    OBSERVE_REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS = (
        "OBSERVE_REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS"
    )
    OBSERVE_ENVIRONMENT_PROTECTION_POLICY = "OBSERVE_ENVIRONMENT_PROTECTION_POLICY"


class InvestigationTaskStatus(StrEnum):
    READY = "READY"
    LEASED = "LEASED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


class ObservationKind(StrEnum):
    EXTERNAL_EVIDENCE = "EXTERNAL_EVIDENCE"
    REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS = "REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS"
    ENVIRONMENT_PROTECTION_POLICY = "ENVIRONMENT_PROTECTION_POLICY"


class ObservationSource(StrEnum):
    FAKE = "FAKE"
    LOCAL = "LOCAL"
    GITHUB = "GITHUB"
    AWS = "AWS"


class DeploymentBranchPolicyMode(StrEnum):
    NONE = "NONE"
    PROTECTED = "PROTECTED"
    CUSTOM = "CUSTOM"
    UNKNOWN = "UNKNOWN"


class EnvironmentProtectionPolicy(SchemaModel):
    environment_name: str = Field(min_length=1, max_length=255)
    required_reviewer_count: int = Field(default=0, ge=0, le=6)
    prevent_self_review: bool = False
    wait_timer_minutes: int = Field(default=0, ge=0, le=43_200)
    deployment_branch_policy: DeploymentBranchPolicyMode = DeploymentBranchPolicyMode.UNKNOWN
    custom_protection_rule_count: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def validate_reviewer_policy(self) -> EnvironmentProtectionPolicy:
        if self.prevent_self_review and self.required_reviewer_count == 0:
            raise ValueError("prevent_self_review requires at least one required reviewer")
        return self


class RepositoryMetadataEvidence(SchemaModel):
    type: Literal["github.repository.metadata"] = "github.repository.metadata"
    owner: str = Field(min_length=1, max_length=100)
    repository: str = Field(min_length=1, max_length=100)
    private: bool
    visibility: str = Field(min_length=1, max_length=32)
    default_branch: str = Field(min_length=1, max_length=255)
    fork: bool = False
    owner_id: int | None = Field(default=None, gt=0)
    repository_id_number: int | None = Field(default=None, gt=0)
    created_at: datetime | None = None


class GitHubOidcSubjectConfigEvidence(SchemaModel):
    type: Literal["github.oidc.subject_config"] = "github.oidc.subject_config"
    use_default: bool
    include_claim_keys: list[str] = Field(default_factory=list, max_length=32)
    use_immutable_subject: bool | None = None


class AwsIamRoleTrustPolicyEvidence(SchemaModel):
    type: Literal["aws.iam.role_trust_policy"] = "aws.iam.role_trust_policy"
    role_arn: str = Field(min_length=1, max_length=2048)
    role_name: str = Field(min_length=1, max_length=64)
    role_id: str | None = Field(default=None, max_length=128)
    assume_role_policy: dict[str, object]


class AwsOidcProviderEvidence(SchemaModel):
    type: Literal["aws.iam.oidc_provider"] = "aws.iam.oidc_provider"
    provider_arn: str = Field(min_length=1, max_length=2048)
    url: str = Field(min_length=1, max_length=255)
    client_ids: list[str] = Field(default_factory=list, max_length=100)


class RepositoryWorkflowPermissionEvidence(SchemaModel):
    type: Literal["github.repository.default_workflow_permission"] = (
        "github.repository.default_workflow_permission"
    )
    value: RepositoryWorkflowPermissionDefault


class ForkPullRequestPolicyEvidence(SchemaModel):
    type: Literal["github.fork_pr.workflow_policy"] = "github.fork_pr.workflow_policy"
    approval_policy: str | None = Field(default=None, max_length=100)
    private_repo_settings_applicable: bool = False
    run_workflows_from_fork_pull_requests: bool | None = None
    send_write_tokens_to_workflows: bool | None = None
    send_secrets_and_variables: bool | None = None
    require_approval_for_fork_pr_workflows: bool | None = None


class ActionsPolicyEvidence(SchemaModel):
    type: Literal["github.actions.policy"] = "github.actions.policy"
    enabled: bool
    allowed_actions: str = Field(default="all", max_length=40)
    sha_pinning_required: bool = False
    github_owned_allowed: bool | None = None
    verified_allowed: bool | None = None
    patterns_allowed: list[str] = Field(default_factory=list, max_length=200)


class EffectiveBranchRulesEvidence(SchemaModel):
    type: Literal["github.branch.effective_rules"] = "github.branch.effective_rules"
    branch: str = Field(min_length=1, max_length=255)
    protected: TriState = TriState.UNKNOWN
    active_rule_types: list[str] = Field(default_factory=list, max_length=100)
    requires_pull_request: bool = False
    required_approving_review_count: int | None = Field(default=None, ge=0, le=10)
    require_last_push_approval: bool | None = None
    required_status_check_count: int | None = Field(default=None, ge=0, le=100)
    update_restricted_to_bypass_actors: bool = False
    bypass_visibility: TriState = TriState.UNKNOWN
    legacy_protection_present: TriState = TriState.UNKNOWN


class EnvironmentPolicyEvidence(SchemaModel):
    type: Literal["github.environment.policy"] = "github.environment.policy"
    policy: EnvironmentProtectionPolicy


class EnvironmentBranchPatternsEvidence(SchemaModel):
    type: Literal["github.environment.branch_patterns"] = "github.environment.branch_patterns"
    environment_name: str = Field(min_length=1, max_length=255)
    # `patterns` is retained for checkpoint compatibility. New observations also preserve the
    # branch/tag target when GitHub returns it so semantic consumers do not conflate the two.
    patterns: list[str] = Field(default_factory=list, max_length=100)
    branch_patterns: list[str] = Field(default_factory=list, max_length=100)
    tag_patterns: list[str] = Field(default_factory=list, max_length=100)
    unknown_target_patterns: list[str] = Field(default_factory=list, max_length=100)


class PendingDeploymentEnvironmentEvidence(SchemaModel):
    environment_name: str = Field(min_length=1, max_length=255)
    wait_timer_minutes: int = Field(default=0, ge=0, le=43_200)
    reviewer_count: int = Field(default=0, ge=0, le=100)
    current_user_can_approve: bool | None = None


class PendingDeploymentStateEvidence(SchemaModel):
    type: Literal["github.environment.pending_deployment"] = "github.environment.pending_deployment"
    run_id: int = Field(gt=0)
    pending_environments: list[PendingDeploymentEnvironmentEvidence] = Field(
        default_factory=list,
        max_length=100,
    )


class WorkflowRunStepEvidence(SchemaModel):
    number: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=255)
    status: str = Field(min_length=1, max_length=40)
    conclusion: str | None = Field(default=None, max_length=40)


class WorkflowRunJobEvidence(SchemaModel):
    github_job_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=255)
    status: str = Field(min_length=1, max_length=40)
    conclusion: str | None = Field(default=None, max_length=40)
    runner_id: int | None = Field(default=None, gt=0)
    runner_group_id: int | None = Field(default=None, gt=0)
    labels: list[str] = Field(default_factory=list, max_length=100)
    steps: list[WorkflowRunStepEvidence] = Field(default_factory=list, max_length=200)


class WorkflowRunJobsEvidence(SchemaModel):
    type: Literal["github.workflow_run.jobs"] = "github.workflow_run.jobs"
    run_id: int = Field(gt=0)
    jobs: list[WorkflowRunJobEvidence] = Field(default_factory=list, max_length=100)


class GitHubEventLocator(SchemaModel):
    event_name: str = Field(min_length=1, max_length=100)
    action: str | None = Field(default=None, max_length=100)
    repository_full_name: str | None = Field(default=None, max_length=220)
    issue_number: int | None = Field(default=None, gt=0)
    pull_number: int | None = Field(default=None, gt=0)
    comment_id: int | None = Field(default=None, gt=0)
    review_id: int | None = Field(default=None, gt=0)
    review_comment_id: int | None = Field(default=None, gt=0)
    label_name: str | None = Field(default=None, max_length=255)
    sender_login: str | None = Field(default=None, max_length=100)
    sender_id: int | None = Field(default=None, gt=0)
    sender_type: str | None = Field(default=None, max_length=100)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkflowRunStatusEvidence(SchemaModel):
    type: Literal["github.workflow_run.status"] = "github.workflow_run.status"
    run_id: int = Field(gt=0)
    status: str = Field(min_length=1, max_length=40)
    conclusion: str | None = Field(default=None, max_length=40)


class WorkflowRunIdentityEvidence(SchemaModel):
    type: Literal["github.workflow_run.identity"] = "github.workflow_run.identity"
    run_id: int = Field(gt=0)
    event_name: str = Field(min_length=1, max_length=100)
    workflow_id: int | None = Field(default=None, gt=0)
    workflow_name: str | None = Field(default=None, max_length=255)
    workflow_path: str | None = Field(default=None, max_length=500)
    run_attempt: int = Field(default=1, ge=1)
    actor_login: str = Field(min_length=1, max_length=100)
    actor_id: int = Field(gt=0)
    triggering_actor_login: str | None = Field(default=None, max_length=100)
    triggering_actor_id: int | None = Field(default=None, gt=0)
    created_at: datetime | None = None


class WorkflowRunEventLinkageEvidence(SchemaModel):
    type: Literal["github.workflow_run.event_linkage"] = "github.workflow_run.event_linkage"
    run_id: int = Field(gt=0)
    event_name: str = Field(min_length=1, max_length=100)
    action: str | None = Field(default=None, max_length=100)
    binding: Literal["exact", "partial"]
    source: Literal["workflow_run", "operator_event_payload"]
    issue_number: int | None = Field(default=None, gt=0)
    pull_number: int | None = Field(default=None, gt=0)
    comment_id: int | None = Field(default=None, gt=0)
    review_id: int | None = Field(default=None, gt=0)
    review_comment_id: int | None = Field(default=None, gt=0)
    label_name: str | None = Field(default=None, max_length=255)
    sender_login: str | None = Field(default=None, max_length=100)
    sender_id: int | None = Field(default=None, gt=0)
    sender_type: str | None = Field(default=None, max_length=100)
    source_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class EventPrincipalEvidence(SchemaModel):
    type: Literal["github.event.principal"] = "github.event.principal"
    run_id: int = Field(gt=0)
    event_name: str = Field(min_length=1, max_length=100)
    event_action: str | None = Field(default=None, max_length=100)
    principal_role: EventPrincipalRole
    login: str | None = Field(default=None, max_length=100)
    actor_id: int | None = Field(default=None, gt=0)
    account_type: str | None = Field(default=None, max_length=100)
    author_association: str | None = Field(default=None, max_length=100)
    resource_id: str = Field(min_length=1, max_length=200)
    observed_at: datetime | None = None
    performed_via_github_app: bool | None = None


class RepositoryActorPermissionEvidence(SchemaModel):
    type: Literal["github.repository.actor_permission"] = "github.repository.actor_permission"
    actor_login: str = Field(min_length=1, max_length=100)
    actor_id: int | None = Field(default=None, gt=0)
    permission: Literal["admin", "write", "read", "none"]
    role_name: str | None = Field(default=None, max_length=100)
    temporal_scope: Literal["current"] = "current"


class RuntimeEventIdentity(SchemaModel):
    workflow_path: str = Field(min_length=1, max_length=500)
    job_name: str = Field(min_length=1, max_length=255)
    step_name: str | None = Field(default=None, max_length=255)
    step_number: int | None = Field(default=None, ge=1)


class RuntimeProcessEvent(SchemaModel):
    kind: Literal["process"] = "process"
    timestamp: datetime
    identity: RuntimeEventIdentity
    process_name: str = Field(min_length=1, max_length=255)


class RuntimeNetworkEvent(SchemaModel):
    kind: Literal["network"] = "network"
    timestamp: datetime
    identity: RuntimeEventIdentity
    process_name: str | None = Field(default=None, max_length=255)
    destination: str = Field(min_length=1, max_length=500)
    port: int | None = Field(default=None, ge=1, le=65535)
    protocol: Literal["tcp", "udp", "http", "https", "dns", "other"] = "other"


class RuntimeFileEvent(SchemaModel):
    kind: Literal["file"] = "file"
    timestamp: datetime
    identity: RuntimeEventIdentity
    process_name: str | None = Field(default=None, max_length=255)
    path: str = Field(min_length=1, max_length=1000)
    operation: Literal["create", "modify", "delete"]
    workspace_relative: bool = True


RuntimeTelemetryEvent = Annotated[
    RuntimeProcessEvent | RuntimeNetworkEvent | RuntimeFileEvent,
    Field(discriminator="kind"),
]


class RuntimeTelemetryEvidence(SchemaModel):
    type: Literal["runtime.telemetry"] = "runtime.telemetry"
    run_id: str = Field(min_length=1, max_length=100)
    run_attempt: int = Field(default=1, ge=1)
    source_name: str = Field(min_length=1, max_length=100)
    events: list[RuntimeTelemetryEvent] = Field(default_factory=list, max_length=2000)


class ActionProvenanceEvidence(SchemaModel):
    type: Literal["github.action.provenance"] = "github.action.provenance"
    action_owner: str = Field(min_length=1, max_length=100)
    action_repository: str = Field(min_length=1, max_length=100)
    subpath: str | None = Field(default=None, max_length=300)
    requested_ref: str = Field(min_length=1, max_length=300)
    resolved_sha: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{40}$")
    ref_kind: Literal["full_sha", "tag", "branch", "unknown"]
    immutable_releases_enabled: bool | None = None
    release_exists_for_tag: bool | None = None
    immutable_release_tag: TriState = TriState.UNKNOWN
    observed_at: datetime


class ActionAdvisoryItem(SchemaModel):
    ghsa_id: str = Field(min_length=1, max_length=100)
    severity: str = Field(min_length=1, max_length=30)
    summary: str = Field(min_length=1, max_length=600)
    published_at: datetime | None = None
    updated_at: datetime | None = None
    vulnerable_version_range: str | None = Field(default=None, max_length=300)


class ActionAdvisoryEvidence(SchemaModel):
    type: Literal["github.action.advisory"] = "github.action.advisory"
    package_name: str = Field(min_length=1, max_length=220)
    requested_ref: str = Field(min_length=1, max_length=300)
    query: str = Field(min_length=1, max_length=500)
    affected: TriState = TriState.UNKNOWN
    advisories: list[ActionAdvisoryItem] = Field(default_factory=list, max_length=100)
    observed_at: datetime


class SecretMetadataItem(SchemaModel):
    name: str = Field(min_length=1, max_length=255)
    scope: Literal["repository", "organization", "environment"]
    created_at: datetime | None = None
    updated_at: datetime | None = None
    visibility: Literal["all", "private", "selected"] | None = None
    environment_name: str | None = Field(default=None, max_length=255)


class SecretMetadataEvidence(SchemaModel):
    type: Literal["github.secret.metadata"] = "github.secret.metadata"
    secrets: list[SecretMetadataItem] = Field(default_factory=list, max_length=1000)
    observed_at: datetime


class SecretRotationPolicyEvidence(SchemaModel):
    type: Literal["operator.secret_rotation_policy"] = "operator.secret_rotation_policy"
    max_age_days: int = Field(gt=0, le=3650)


class AuditEventItem(SchemaModel):
    action: str = Field(min_length=1, max_length=200)
    actor: str | None = Field(default=None, max_length=100)
    actor_id: int | None = Field(default=None, gt=0)
    created_at: datetime | None = None
    repository: str | None = Field(default=None, max_length=220)
    runner_id: int | None = Field(default=None, gt=0)
    key: str | None = Field(default=None, max_length=255)


class AuditEventsEvidence(SchemaModel):
    type: Literal["github.audit.events"] = "github.audit.events"
    organization: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=500)
    coverage_start: datetime
    coverage_end: datetime
    permission_state: Literal["read"] = "read"
    events: list[AuditEventItem] = Field(default_factory=list, max_length=1000)
    observed_at: datetime


ExternalEvidencePayload = Annotated[
    RepositoryMetadataEvidence
    | GitHubOidcSubjectConfigEvidence
    | AwsIamRoleTrustPolicyEvidence
    | AwsOidcProviderEvidence
    | RepositoryWorkflowPermissionEvidence
    | ForkPullRequestPolicyEvidence
    | ActionsPolicyEvidence
    | EffectiveBranchRulesEvidence
    | EnvironmentPolicyEvidence
    | EnvironmentBranchPatternsEvidence
    | PendingDeploymentStateEvidence
    | WorkflowRunJobsEvidence
    | WorkflowRunStatusEvidence
    | WorkflowRunIdentityEvidence
    | WorkflowRunEventLinkageEvidence
    | EventPrincipalEvidence
    | RepositoryActorPermissionEvidence
    | RuntimeTelemetryEvidence
    | ActionProvenanceEvidence
    | ActionAdvisoryEvidence
    | SecretMetadataEvidence
    | SecretRotationPolicyEvidence
    | AuditEventsEvidence,
    Field(discriminator="type"),
]


class ExternalTransportCacheEntry(SchemaModel):
    """Operational HTTP cache state persisted separately from semantic evidence."""

    provider: EvidenceProvider
    cache_key: str = Field(min_length=1, max_length=2_000)
    status_code: int = Field(default=200, ge=200, le=399)
    payload: dict[str, object] | list[object]
    etag: str | None = Field(default=None, max_length=1_000)
    last_modified: str | None = Field(default=None, max_length=200)


class KnowledgeState(SchemaModel):
    revision: int = Field(default=0, ge=0)
    external_evidence: dict[str, ExternalEvidencePayload] = Field(default_factory=dict)
    # Legacy projections are retained while old checkpoints and semantic consumers migrate.
    repository_default_workflow_permissions: RepositoryWorkflowPermissionDefault | None = None
    environment_protection_policies: dict[str, EnvironmentProtectionPolicy] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_environment_policy_keys(self) -> KnowledgeState:
        if any(
            key != policy.environment_name
            for key, policy in self.environment_protection_policies.items()
        ):
            raise ValueError("environment policy key does not match environment_name")
        return self


class Observation(SchemaModel):
    observation_id: str = Field(pattern=r"^obs_[0-9a-f]{16}$")
    task_id: str = Field(pattern=r"^task_[0-9a-f]{16}$")
    kind: ObservationKind
    source: ObservationSource
    subject_id: str = Field(min_length=1, max_length=300)
    requirement_id: str | None = Field(default=None, pattern=r"^req_[0-9a-f]{16}$")
    resolver_id: str | None = Field(default=None, min_length=1, max_length=120)
    payload: ExternalEvidencePayload | None = None
    # Legacy payloads remain readable for checkpoint compatibility.
    repository_default_workflow_permissions: RepositoryWorkflowPermissionDefault | None = None
    environment_protection_policy: EnvironmentProtectionPolicy | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_payload(self) -> Observation:
        repository_payload = self.repository_default_workflow_permissions is not None
        environment_payload = self.environment_protection_policy is not None
        if self.payload is not None:
            if repository_payload or environment_payload:
                raise ValueError("generic observation payload cannot include legacy payload fields")
            if self.requirement_id is None or self.resolver_id is None:
                raise ValueError(
                    "generic observation payload requires requirement_id and resolver_id"
                )
            return self
        if self.kind is ObservationKind.REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS:
            if not repository_payload or environment_payload:
                raise ValueError(
                    "repository default workflow permission observation requires only a "
                    "permission value"
                )
        elif self.kind is ObservationKind.ENVIRONMENT_PROTECTION_POLICY:
            if not environment_payload or repository_payload:
                raise ValueError(
                    "environment protection observation requires only an environment policy"
                )
        return self


class InvestigationTask(SchemaModel):
    task_id: str = Field(pattern=r"^task_[0-9a-f]{16}$")
    kind: InvestigationTaskKind
    subject_id: str = Field(min_length=1, max_length=300)
    requirement_id: str | None = Field(default=None, pattern=r"^req_[0-9a-f]{16}$")
    resolver_id: str | None = Field(default=None, min_length=1, max_length=120)
    provider: EvidenceProvider | None = None
    environment_name: str | None = Field(default=None, min_length=1, max_length=255)
    basis_relation_ids: list[str] = Field(default_factory=list, max_length=24)
    status: InvestigationTaskStatus = InvestigationTaskStatus.READY
    attempts: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_subject(self) -> InvestigationTask:
        if self.kind is InvestigationTaskKind.RESOLVE_EXTERNAL_EVIDENCE:
            if self.requirement_id is None or self.resolver_id is None or self.provider is None:
                raise ValueError(
                    "external evidence task requires requirement, resolver, and provider"
                )
            return self
        if self.kind is InvestigationTaskKind.OBSERVE_ENVIRONMENT_PROTECTION_POLICY:
            if self.environment_name is None:
                raise ValueError("environment protection task requires environment_name")
        elif self.environment_name is not None:
            raise ValueError("repository permission task cannot include environment_name")
        return self


class TaskLease(SchemaModel):
    lease_id: str = Field(pattern=r"^lease_[0-9a-f]{16}$")
    task_id: str = Field(pattern=r"^task_[0-9a-f]{16}$")
    knowledge_revision: int = Field(ge=0)


class InvestigationState(SchemaModel):
    requirements: dict[str, EvidenceRequirement] = Field(default_factory=dict)
    tasks: dict[str, InvestigationTask] = Field(default_factory=dict)
    observations: dict[str, Observation] = Field(default_factory=dict)
    transport_cache: dict[str, ExternalTransportCacheEntry] = Field(default_factory=dict)
    resume_revalidation_epoch: int = Field(default=0, ge=0)
    revalidated_evidence_keys: set[str] = Field(default_factory=set)
    active_lease: TaskLease | None = None

    @model_validator(mode="after")
    def validate_ledger(self) -> InvestigationState:
        if any(key != requirement.requirement_id for key, requirement in self.requirements.items()):
            raise ValueError("evidence requirement ledger key does not match requirement_id")
        if any(key != task.task_id for key, task in self.tasks.items()):
            raise ValueError("investigation task ledger key does not match task_id")
        if any(key != observation.observation_id for key, observation in self.observations.items()):
            raise ValueError("observation ledger key does not match observation_id")
        if any(key != entry.cache_key for key, entry in self.transport_cache.items()):
            raise ValueError("transport cache key does not match cache_key")
        for task in self.tasks.values():
            if task.requirement_id is not None and task.requirement_id not in self.requirements:
                raise ValueError("investigation task references an unknown evidence requirement")
        for observation in self.observations.values():
            task = self.tasks.get(observation.task_id)
            if task is None:
                raise ValueError("observation references an unknown investigation task")
            if observation.subject_id != task.subject_id:
                raise ValueError("observation subject does not match its investigation task")
            if (
                observation.requirement_id is not None
                and observation.requirement_id != task.requirement_id
            ):
                raise ValueError("observation requirement does not match its investigation task")

        leased = {
            task.task_id
            for task in self.tasks.values()
            if task.status is InvestigationTaskStatus.LEASED
        }
        if self.active_lease is None:
            if leased:
                raise ValueError("leased investigation task has no active lease")
            return self

        task = self.tasks.get(self.active_lease.task_id)
        if task is None:
            raise ValueError("active lease references an unknown investigation task")
        if leased != {task.task_id}:
            raise ValueError("active lease must reference the only leased investigation task")
        return self


class RedTeamSession(SchemaModel):
    baseline_analysis: AnalysisState
    analysis_state: AnalysisState
    github_run_id: int | None = Field(default=None, gt=0)
    github_event_locator: GitHubEventLocator | None = None
    knowledge_state: KnowledgeState = Field(default_factory=KnowledgeState)
    investigation_state: InvestigationState = Field(default_factory=InvestigationState)

    @model_validator(mode="after")
    def validate_repository_identity(self) -> RedTeamSession:
        repository_id = self.analysis_state.repository.repository_id
        if self.baseline_analysis.repository.repository_id != repository_id:
            raise ValueError("baseline and live analysis repository IDs do not match")
        return self
