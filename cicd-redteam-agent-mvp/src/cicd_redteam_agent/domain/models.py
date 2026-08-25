from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class Relation(SchemaModel):
    source_id: str
    relation_type: str
    target_id: str
    category: Literal[
        "source", "transform", "execution", "credential", "guard", "context"
    ] | None = None
    verdict: TriState = TriState.TRUE
    evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


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
    source_id: str
    relation_type: str
    target_id: str
    evidence_ids: list[str] = Field(default_factory=list)


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
    termination: Termination | None = None


class AnalysisProposal(SchemaModel):
    findings: list[Finding] = Field(default_factory=list)
    paths: list[AttackPath] = Field(default_factory=list)
    identities: list[CloudIdentityCandidate] = Field(default_factory=list)


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
