from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"


class TriState(StrEnum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class AttackerProfile(StrEnum):
    FORK_PR = "fork-pr"
    REPO_WRITER = "repo-writer"


class FindingStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    SUPPORTED = "SUPPORTED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class CandidateKind(StrEnum):
    """Small, stable vocabulary for canonical semantic candidates."""

    UNTRUSTED_PRIVILEGED_EXECUTION = "untrusted-privileged-execution"
    SHELL_INJECTION = "shell-injection"
    ARTIFACT_EXECUTION = "artifact-execution"
    MUTABLE_ACTION = "mutable-action"
    BROAD_TOKEN_PERMISSION = "broad-token-permission"
    SECRET_LOG = "secret-log"
    SELF_HOSTED_PERSISTENCE = "self-hosted-persistence"
    REVISION_TOCTOU = "revision-toctou"
    KNOWN_ACTION_ADVISORY = "known-action-advisory"
    SECRET_ROTATION = "secret-rotation"
    BUILD_NETWORK_SIDE_EFFECT = "build-network-side-effect"
    BUILD_WORKSPACE_MUTATION = "build-workspace-mutation"
    RUNTIME_NETWORK_SIDE_EFFECT = "runtime-network-side-effect"
    RUNTIME_WORKSPACE_MUTATION = "runtime-workspace-mutation"


# Keep both spellings available to callers while retaining one enum vocabulary.
SecurityCandidateKind = CandidateKind


class CandidateDisposition(StrEnum):
    UNREVIEWED = "UNREVIEWED"
    INVESTIGATING = "INVESTIGATING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
    MERGED = "MERGED"


class Confidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ReportStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class AnalysisMode(StrEnum):
    DETERMINISTIC = "deterministic"
    REASONING = "reasoning"


class PermissionLevel(StrEnum):
    NONE = "NONE"
    READ = "READ"
    WRITE = "WRITE"
    UNKNOWN = "UNKNOWN"


class RepositoryWorkflowPermissionDefault(StrEnum):
    READ = "read"
    WRITE = "write"


class ExecutionResult(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class ConditionSecurityEffect(StrEnum):
    ALWAYS_TRUE = "ALWAYS_TRUE"
    ATTACKER_SATISFIABLE = "ATTACKER_SATISFIABLE"
    TRUST_REQUIRED = "TRUST_REQUIRED"
    BLOCKS = "BLOCKS"
    UNKNOWN = "UNKNOWN"


class EvidenceProvider(StrEnum):
    LOCAL = "local"
    GITHUB = "github"
    AWS = "aws"
    AZURE = "azure"
    GCP = "gcp"


class EventPrincipalRole(StrEnum):
    EVENT_SENDER = "event_sender"
    COMMENT_AUTHOR = "comment_author"
    ISSUE_AUTHOR = "issue_author"
    PULL_REQUEST_AUTHOR = "pull_request_author"
    REVIEW_AUTHOR = "review_author"
    REVIEW_COMMENT_AUTHOR = "review_comment_author"
    LABEL_ACTOR = "label_actor"


class EvidenceRequirementKind(StrEnum):
    GITHUB_REPOSITORY_METADATA = "github.repository.metadata"
    GITHUB_OIDC_SUBJECT_CONFIG = "github.oidc.subject_config"
    AWS_IAM_ROLE_TRUST_POLICY = "aws.iam.role_trust_policy"
    AWS_IAM_OIDC_PROVIDER = "aws.iam.oidc_provider"
    GITHUB_REPOSITORY_DEFAULT_WORKFLOW_PERMISSION = "github.repository.default_workflow_permission"
    GITHUB_FORK_PR_WORKFLOW_POLICY = "github.fork_pr.workflow_policy"
    GITHUB_ACTIONS_POLICY = "github.actions.policy"
    GITHUB_EFFECTIVE_BRANCH_RULES = "github.branch.effective_rules"
    GITHUB_ENVIRONMENT_POLICY = "github.environment.policy"
    GITHUB_ENVIRONMENT_BRANCH_PATTERNS = "github.environment.branch_patterns"
    GITHUB_PENDING_DEPLOYMENT_STATE = "github.environment.pending_deployment"
    GITHUB_WORKFLOW_RUN_JOBS = "github.workflow_run.jobs"
    GITHUB_WORKFLOW_RUN_IDENTITY = "github.workflow_run.identity"
    GITHUB_WORKFLOW_RUN_STATUS = "github.workflow_run.status"
    GITHUB_WORKFLOW_RUN_EVENT_LINKAGE = "github.workflow_run.event_linkage"
    GITHUB_EVENT_PRINCIPAL = "github.event.principal"
    GITHUB_REPOSITORY_ACTOR_PERMISSION = "github.repository.actor_permission"
    RUNTIME_TELEMETRY = "runtime.telemetry"
    GITHUB_ACTION_PROVENANCE = "github.action.provenance"
    GITHUB_ACTION_ADVISORY = "github.action.advisory"
    GITHUB_SECRET_METADATA = "github.secret.metadata"
    GITHUB_AUDIT_EVENTS = "github.audit.events"
    OPERATOR_SECRET_ROTATION_POLICY = "operator.secret_rotation_policy"


class EvidenceRequirementRef(SchemaModel):
    kind: EvidenceRequirementKind
    provider: EvidenceProvider = EvidenceProvider.GITHUB
    subject_id: str = Field(min_length=1, max_length=300)
    qualifier: str | None = Field(default=None, max_length=300)


class WorkingHypothesisStatus(StrEnum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    READY_FOR_PROPOSAL = "READY_FOR_PROPOSAL"
    DISCARDED = "DISCARDED"


class RepositorySnapshot(SchemaModel):
    repository_id: str
    root: str
    git_commit: str | None = None
    dirty: bool | None = None
    analyzed_at: datetime


class Evidence(SchemaModel):
    evidence_id: str
    kind: str
    source_path: str
    line_start: int
    line_end: int
    field_path: str
    redacted_excerpt: str
    content_hash: str


class SecretReference(SchemaModel):
    reference_id: str
    expression: str
    scope: str
    workflow_id: str
    job_id: str
    step_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Step(SchemaModel):
    step_id: str
    step_key: str | None = None
    name: str | None = None
    condition: str | None = None
    continue_on_error: bool | str | None = None
    uses: str | None = None
    run: str | None = None
    shell: str | None = None
    # Optional for checkpoint compatibility.  The parser now preserves this because package/build
    # commands resolve manifests and scripts relative to the step execution directory.
    working_directory: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    references: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class Job(SchemaModel):
    job_id: str
    # Retained as optional fields so checkpoints created before runtime-job identity remain valid.
    job_key: str | None = None
    name: str | None = None
    uses: str | None = None
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    needs: list[str] = Field(default_factory=list)
    condition: str | None = None
    continue_on_error: bool | str | None = None
    has_matrix: bool = False
    strategy_fail_fast: bool | str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    runs_on: str | list[str] | dict[str, Any] | None = None
    container: str | dict[str, Any] | None = None
    permissions_declared: dict[str, str] | str | None = None
    permissions_effective: dict[str, str] | str | None = None
    permissions_source: Literal["job", "workflow", "repository-default-unknown"]
    environment: str | dict[str, Any] | None = None
    steps: list[Step] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class Workflow(SchemaModel):
    workflow_id: str
    path: str
    name: str | None = None
    triggers: dict[str, Any] = Field(default_factory=dict)
    permissions: dict[str, str] | str | None = None
    jobs: list[Job] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class EffectivePermissionSet(SchemaModel):
    levels: dict[str, PermissionLevel] = Field(default_factory=dict)
    default_level: PermissionLevel = PermissionLevel.UNKNOWN
    provenance: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)

    def get(self, scope: str) -> PermissionLevel:
        return self.levels.get(scope, self.default_level)


class TriggerExecutionContext(SchemaModel):
    context_id: str
    workflow_id: str
    event_name: str
    activation_type: str
    attacker_can_trigger: TriState
    event_payload_attacker_controlled: TriState
    workflow_revision_trusted: TriState
    caller_job_context_id: str | None = None
    call_depth: int = Field(default=0, ge=0, le=10)
    evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


class JobExecutionContext(SchemaModel):
    context_id: str
    trigger_context_id: str
    workflow_id: str
    job_id: str
    reachable: TriState
    dependency_gate: TriState = TriState.TRUE
    predicted_result: ExecutionResult = ExecutionResult.UNKNOWN
    effective_permissions: EffectivePermissionSet
    github_token_available: TriState = TriState.TRUE
    attacker_code_execution: TriState = TriState.UNKNOWN
    condition_evaluation_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


class PrincipalAuthorizationAssessment(SchemaModel):
    principal_role: EventPrincipalRole
    reference: str = Field(min_length=1, max_length=300)
    principal_login: str | None = Field(default=None, max_length=100)
    principal_id: int | None = Field(default=None, gt=0)
    current_repository_authorization: TriState = TriState.UNKNOWN
    authorization_scope: Literal["none", "current"] = "none"
    event_action_observed: TriState = TriState.UNKNOWN
    historical_repository_authorization: TriState = TriState.UNKNOWN
    missing_evidence: list[str] = Field(default_factory=list)


class ConditionEvaluation(SchemaModel):
    evaluation_id: str
    target_id: str
    target_kind: Literal["job", "step"]
    trigger_context_id: str
    expression: str
    normalized_expression: str
    verdict: TriState
    raw_expression_verdict: TriState | None = None
    status_gate_verdict: TriState | None = None
    implicit_success_applied: bool = False
    referenced_contexts: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    evidence_requirements: list[EvidenceRequirementRef] = Field(default_factory=list)
    current_actor_authorization: TriState | None = None
    authorization_missing_evidence: list[str] = Field(default_factory=list)
    authorization_scope: Literal["none", "current"] = "none"
    principal_assessments: list[PrincipalAuthorizationAssessment] = Field(default_factory=list)
    security_effect: ConditionSecurityEffect = ConditionSecurityEffect.UNKNOWN
    attacker_satisfiable: TriState = TriState.UNKNOWN
    trusted_principal_required: TriState = TriState.UNKNOWN
    revision_bound: TriState = TriState.UNKNOWN
    security_missing_evidence: list[str] = Field(default_factory=list)


def relation_id_for(source_id: str, relation_type: str, target_id: str) -> str:
    digest = hashlib.sha256("|".join((source_id, relation_type, target_id)).encode()).hexdigest()[
        :16
    ]
    return f"rel_{digest}"


class Relation(SchemaModel):
    relation_id: str
    source_id: str
    relation_type: str
    target_id: str
    category: (
        Literal["source", "transform", "execution", "credential", "guard", "context"] | None
    ) = None
    verdict: TriState = TriState.TRUE
    evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    evidence_requirements: list[EvidenceRequirementRef] = Field(default_factory=list, max_length=16)

    @model_validator(mode="before")
    @classmethod
    def populate_relation_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("relation_id"):
            source_id = data.get("source_id")
            relation_type = data.get("relation_type")
            target_id = data.get("target_id")
            if source_id is not None and relation_type is not None and target_id is not None:
                data = dict(data)
                data["relation_id"] = relation_id_for(
                    str(source_id), str(relation_type), str(target_id)
                )
        return data


class ControlAssessment(SchemaModel):
    subject_id: str
    attacker_profile: AttackerProfile
    verdict: TriState
    evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    reason: str


class Finding(SchemaModel):
    finding_id: str
    rule_id: str
    title: str
    status: FindingStatus
    candidate_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    confidence: Confidence
    summary: str


class AttackEdge(SchemaModel):
    relation_id: str
    source_id: str
    relation_type: str
    target_id: str
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def populate_relation_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("relation_id"):
            source_id = data.get("source_id")
            relation_type = data.get("relation_type")
            target_id = data.get("target_id")
            if source_id is not None and relation_type is not None and target_id is not None:
                data = dict(data)
                data["relation_id"] = relation_id_for(
                    str(source_id), str(relation_type), str(target_id)
                )
        return data


class AttackPath(SchemaModel):
    path_id: str
    nodes: list[str]
    edges: list[AttackEdge]
    status: FindingStatus
    candidate_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


class ClaimContract(SchemaModel):
    """Canonical claim boundary used when materializing a candidate proposal."""

    contract_id: str
    rule_id: str
    title: str
    bounded_summary: str
    proposal_status_ceiling: FindingStatus
    required_relation_types: list[str] = Field(default_factory=list)
    required_privileged_relation_types: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_static_ceiling(self) -> ClaimContract:
        if self.proposal_status_ceiling is FindingStatus.CONFIRMED:
            raise ValueError("static claim contracts cannot permit CONFIRMED")
        return self


class SecurityCandidate(SchemaModel):
    """Canonical semantic vulnerability hypothesis shared by both analysis modes."""

    candidate_id: str
    source_finding_id: str | None = None
    candidate_kind: CandidateKind
    root_cause_family: str | None = None
    root_cause_key: str
    workflow_id: str | None = None
    job_id: str | None = None
    step_id: str | None = None
    context_id: str | None = None
    claim_contract: ClaimContract
    proposal_status_ceiling: FindingStatus
    status: FindingStatus
    confidence: Confidence = Confidence.MEDIUM
    disposition: CandidateDisposition = CandidateDisposition.UNREVIEWED
    disposition_reason: str | None = None
    supporting_relation_ids: list[str] = Field(default_factory=list, max_length=64)
    contradicting_relation_ids: list[str] = Field(default_factory=list, max_length=64)
    guards: list[str] = Field(default_factory=list, max_length=32)
    evidence_ids: list[str] = Field(default_factory=list, max_length=64)
    missing_evidence: list[str] = Field(default_factory=list, max_length=32)
    evidence_requirements: list[EvidenceRequirementRef] = Field(
        default_factory=list, max_length=32
    )
    path_variants: list[AttackPath] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_claim_ceiling(self) -> SecurityCandidate:
        if self.proposal_status_ceiling is FindingStatus.CONFIRMED:
            raise ValueError("static candidates cannot have CONFIRMED proposal ceiling")
        if self.claim_contract.proposal_status_ceiling is FindingStatus.CONFIRMED:
            raise ValueError("candidate claim contract cannot have CONFIRMED ceiling")
        if self.status is FindingStatus.CONFIRMED:
            raise ValueError("static candidates cannot be CONFIRMED")
        if (
            self.status is FindingStatus.SUPPORTED
            and self.proposal_status_ceiling is not FindingStatus.SUPPORTED
        ):
            raise ValueError("SUPPORTED candidate status exceeds proposal ceiling")
        return self


class CoverageLedgerEntry(SchemaModel):
    """Terminal accounting for a selected candidate or graph frontier item."""

    item_id: str
    candidate_id: str | None = None
    frontier_id: str | None = None
    disposition: CandidateDisposition = CandidateDisposition.UNREVIEWED
    selected: bool = True
    reason: str | None = None

    @model_validator(mode="after")
    def validate_item_reference(self) -> CoverageLedgerEntry:
        if bool(self.candidate_id) == bool(self.frontier_id):
            raise ValueError("coverage item must reference exactly one candidate or frontier")
        return self


class CloudIdentityCandidate(SchemaModel):
    identity_id: str
    provider: str
    identity_type: str
    mechanism: str
    repository_id: str
    workflow_id: str
    job_id: str
    identity_reference: str | None = None
    role_arn: str | None = None
    secret_reference_ids: list[str] = Field(default_factory=list)
    oidc_enabled: TriState
    attacker_control: TriState
    trust_verified: TriState = TriState.UNKNOWN
    trust_assessment_id: str | None = None
    audience: str | None = None
    subject_claims: list[str] = Field(default_factory=list)
    actual_assume_succeeded: TriState = TriState.UNKNOWN
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Confidence
    summary: str


class OidcAuthenticationMode(StrEnum):
    GITHUB_OIDC = "github-oidc"
    STATIC_CREDENTIALS = "static-credentials"
    WEB_IDENTITY_TOKEN_FILE = "web-identity-token-file"
    ROLE_CHAINING = "role-chaining"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class OidcClaimCandidate(SchemaModel):
    trigger_context_id: str
    variant_group_id: str
    audience: str
    subject: str | None = None
    subject_format: Literal["legacy", "immutable", "custom", "unknown"]
    complete: TriState
    missing_evidence: list[str] = Field(default_factory=list)


class AwsOidcTrustAssessment(SchemaModel):
    assessment_id: str
    workflow_id: str
    job_id: str
    step_id: str
    role_arn: str | None = None
    provider_arn: str | None = None
    authentication_mode: OidcAuthenticationMode
    verdict: TriState
    audience: str | None = None
    claim_candidates: list[OidcClaimCandidate] = Field(default_factory=list)
    matched_statement_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    evidence_requirements: list[EvidenceRequirementRef] = Field(default_factory=list)
    actual_assume_succeeded: TriState = TriState.UNKNOWN


class Termination(SchemaModel):
    reason: str
    detail: str | None = None
    rounds: int = 0
    tokens: int = 0
    cost: float = 0.0
    duration_seconds: float = 0.0


class TraceMetadata(SchemaModel):
    provider: str | None = None
    model: str | None = None
    framework: str = "hackingBuddyGPT"
    framework_commit: str
    trace_path: str | None = None


class HypothesisNextCheck(SchemaModel):
    objective: str = Field(min_length=1, max_length=300)
    preferred_capability: str | None = Field(default=None, max_length=100)
    target: str | None = Field(default=None, max_length=300)


class WorkingHypothesis(SchemaModel):
    hypothesis_id: str = Field(pattern=r"^hyp_[0-9a-f]{16}$")
    candidate_id: str | None = None
    claim: str = Field(min_length=1, max_length=300)
    status: WorkingHypothesisStatus = WorkingHypothesisStatus.ACTIVE
    supporting_relation_ids: list[str] = Field(default_factory=list, max_length=16)
    contradicting_relation_ids: list[str] = Field(default_factory=list, max_length=16)
    evidence_ids: list[str] = Field(default_factory=list, max_length=24)
    missing_evidence: list[str] = Field(default_factory=list, max_length=12)
    decision_summary: str = Field(min_length=1, max_length=600)
    next_check: HypothesisNextCheck | None = None
    created_revision: int = Field(ge=1)
    updated_revision: int = Field(ge=1)


class AgentWorkingMemory(SchemaModel):
    revision: int = Field(default=0, ge=0)
    focus_hypothesis_id: str | None = Field(default=None, pattern=r"^hyp_[0-9a-f]{16}$")
    hypotheses: list[WorkingHypothesis] = Field(default_factory=list, max_length=8)


class WorkingHypothesisUpsert(SchemaModel):
    hypothesis_id: str | None = Field(default=None, pattern=r"^hyp_[0-9a-f]{16}$")
    candidate_id: str | None = None
    claim: str | None = Field(default=None, min_length=1, max_length=300)
    status: WorkingHypothesisStatus | None = None
    supporting_relation_ids: list[str] | None = Field(default=None, max_length=16)
    contradicting_relation_ids: list[str] | None = Field(default=None, max_length=16)
    evidence_ids: list[str] | None = Field(default=None, max_length=24)
    missing_evidence: list[str] | None = Field(default=None, max_length=12)
    decision_summary: str | None = Field(default=None, min_length=1, max_length=600)
    next_check: HypothesisNextCheck | None = None
    clear_next_check: bool = False


class WorkingMemoryPatch(SchemaModel):
    expected_revision: int = Field(ge=0)
    upserts: list[WorkingHypothesisUpsert] = Field(default_factory=list, max_length=3)
    focus_hypothesis_id: str | None = Field(default=None, pattern=r"^hyp_[0-9a-f]{16}$")
    clear_focus: bool = False


class WorkingMemoryUpdateResult(SchemaModel):
    accepted: bool
    changed: bool = False
    revision: int = Field(ge=0)
    updated_hypothesis_ids: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class AnalysisState(SchemaModel):
    repository: RepositorySnapshot
    workflows: list[Workflow] = Field(default_factory=list)
    trigger_contexts: list[TriggerExecutionContext] = Field(default_factory=list)
    job_execution_contexts: list[JobExecutionContext] = Field(default_factory=list)
    condition_evaluations: list[ConditionEvaluation] = Field(default_factory=list)
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    relations: list[Relation] = Field(default_factory=list)
    assessments: list[ControlAssessment] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    paths: list[AttackPath] = Field(default_factory=list)
    candidates: list[SecurityCandidate] = Field(default_factory=list, max_length=256)
    coverage_ledger: list[CoverageLedgerEntry] = Field(default_factory=list, max_length=256)
    identities: list[CloudIdentityCandidate] = Field(default_factory=list)
    aws_oidc_trust_assessments: list[AwsOidcTrustAssessment] = Field(default_factory=list)
    secret_references: list[SecretReference] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)
    inspected_resources: list[str] = Field(default_factory=list)
    repeated_calls: dict[str, int] = Field(default_factory=dict)
    working_memory: AgentWorkingMemory = Field(default_factory=AgentWorkingMemory)
    termination: Termination | None = None


class AnalysisProposal(SchemaModel):
    findings: list[Finding] = Field(default_factory=list)
    paths: list[AttackPath] = Field(default_factory=list)
    identities: list[CloudIdentityCandidate] = Field(default_factory=list)


class AttackPathDraft(SchemaModel):
    path_id: str = Field(min_length=1, max_length=200)
    relation_refs: list[str] = Field(min_length=1, max_length=24)


class FindingDraft(SchemaModel):
    finding_id: str = Field(min_length=1, max_length=200)
    rule_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    path_id: str = Field(min_length=1, max_length=200)
    confidence: Confidence
    summary: str = Field(min_length=1, max_length=1_200)


class CandidateDecisionDraft(SchemaModel):
    candidate_id: str = Field(min_length=1, max_length=200)
    decision: CandidateDisposition
    selected_path_variant: str | None = Field(default=None, max_length=200)
    confidence: Confidence = Confidence.MEDIUM
    analyst_note: str | None = Field(default=None, max_length=1_200)


class FrontierDecisionDraft(SchemaModel):
    frontier_id: str = Field(min_length=1, max_length=200)
    decision: CandidateDisposition
    confidence: Confidence = Confidence.MEDIUM
    analyst_note: str | None = Field(default=None, max_length=1_200)


class AnalysisProposalDraft(SchemaModel):
    candidate_decisions: list[CandidateDecisionDraft] = Field(default_factory=list, max_length=64)
    frontier_decisions: list[FrontierDecisionDraft] = Field(default_factory=list, max_length=64)
    finding_drafts: list[FindingDraft] = Field(default_factory=list, max_length=16)
    path_drafts: list[AttackPathDraft] = Field(default_factory=list, max_length=16)


class ValidationResult(SchemaModel):
    accepted: bool
    errors: list[str] = Field(default_factory=list)


class AnalysisReport(SchemaModel):
    status: ReportStatus
    termination_reason: str
    state: AnalysisState
    warnings: list[str] = Field(default_factory=list)
    trace: TraceMetadata
    run_metadata: dict[str, Any] = Field(default_factory=dict)
