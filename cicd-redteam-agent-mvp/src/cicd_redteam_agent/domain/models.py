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
    uses: str | None = None
    run: str | None = None
    shell: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    references: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class Job(SchemaModel):
    job_id: str
    uses: str | None = None
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    needs: list[str] = Field(default_factory=list)
    condition: str | None = None
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


def relation_id_for(source_id: str, relation_type: str, target_id: str) -> str:
    digest = hashlib.sha256(
        "|".join((source_id, relation_type, target_id)).encode()
    ).hexdigest()[:16]
    return f"rel_{digest}"


class Relation(SchemaModel):
    relation_id: str
    source_id: str
    relation_type: str
    target_id: str
    category: Literal[
        "source", "transform", "execution", "credential", "guard", "context"
    ] | None = None
    verdict: TriState = TriState.TRUE
    evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)

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
    evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


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
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Confidence
    summary: str


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
    focus_hypothesis_id: str | None = Field(
        default=None, pattern=r"^hyp_[0-9a-f]{16}$"
    )
    hypotheses: list[WorkingHypothesis] = Field(default_factory=list, max_length=8)


class WorkingHypothesisUpsert(SchemaModel):
    hypothesis_id: str | None = Field(default=None, pattern=r"^hyp_[0-9a-f]{16}$")
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
    focus_hypothesis_id: str | None = Field(
        default=None, pattern=r"^hyp_[0-9a-f]{16}$"
    )
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
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    relations: list[Relation] = Field(default_factory=list)
    assessments: list[ControlAssessment] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    paths: list[AttackPath] = Field(default_factory=list)
    identities: list[CloudIdentityCandidate] = Field(default_factory=list)
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


class AnalysisProposalDraft(SchemaModel):
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
