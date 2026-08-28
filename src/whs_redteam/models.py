"""Structured web inventory models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class PageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    final_url: str
    depth: int = Field(ge=0)
    title: str = ""
    status_code: int | None = None
    screenshot_path: str | None = None
    discovered_from: str | None = None
    error: str | None = None
    observed_at: datetime = Field(default_factory=utc_now)


class FormRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_url: str
    action_url: str
    method: str
    input_names: tuple[str, ...] = ()
    input_types: tuple[str, ...] = ()
    in_scope: bool
    policy_reason: str
    observed_at: datetime = Field(default_factory=utc_now)


class EndpointRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    method: str
    resource_type: str
    source_page: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    observed_at: datetime = Field(default_factory=utc_now)


class ObservedValueType(StrEnum):
    UNKNOWN = "unknown"
    NULL = "null"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"
    ARRAY = "array"
    OBJECT = "object"
    MIXED = "mixed"


class ObservedSchemaNode(BaseModel):
    """Value-free shape inferred from one or more observed requests."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: ObservedValueType
    properties: dict[str, ObservedSchemaNode] = Field(default_factory=dict)
    items: ObservedSchemaNode | None = None


class ApiRequestSchemaRecord(BaseModel):
    """Request names and types only; raw query/body values are never retained."""

    model_config = ConfigDict(extra="forbid")
    endpoint_url: str
    method: str
    resource_type: str
    source_page: str | None = None
    query_schema: dict[str, ObservedSchemaNode] = Field(default_factory=dict)
    body_schema: ObservedSchemaNode | None = None
    request_content_type: str | None = None
    observed_at: datetime = Field(default_factory=utc_now)

    @field_validator("method")
    @classmethod
    def normalize_api_schema_method(cls, method: str) -> str:
        return method.upper()


class IdentifierLocation(StrEnum):
    PATH = "path"
    QUERY = "query"
    BODY = "body"


class ResourceIdentifier(BaseModel):
    """A value-free candidate identifier used by later authorization checks."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    location: IdentifierLocation
    schema_path: str
    confidence: float = Field(ge=0, le=1)
    reason: str


class ResourceRecord(BaseModel):
    """A normalized resource endpoint without observed identifier values."""

    model_config = ConfigDict(extra="forbid")
    endpoint_template: str
    method: str
    resource_type: str
    identifiers: tuple[ResourceIdentifier, ...] = ()
    observed_endpoint_count: int = Field(default=1, ge=1)
    observed_at: datetime = Field(default_factory=utc_now)

    @field_validator("method")
    @classmethod
    def normalize_resource_method(cls, method: str) -> str:
        return method.upper()


class ResourceOwnershipRecord(BaseModel):
    """Value-free ownership assertion supplied by an authorized fixture."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    fixture_id: str
    endpoint_template: str
    target_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_actor: str
    non_owner_actors: tuple[str, ...]
    source: str = "explicit_authorization_fixture"
    observed_at: datetime = Field(default_factory=utc_now)


class DeferredLinkRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_page: str
    target_url: str
    reason: str
    observed_at: datetime = Field(default_factory=utc_now)


class ExplorationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    pages_visited: int
    pages_failed: int
    forms_discovered: int
    endpoints_discovered: int
    links_deferred: int
    requests_observed: int
    requests_blocked: int


class ObservationVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INFO = "INFO"
    INCONCLUSIVE = "INCONCLUSIVE"


class Severity(StrEnum):
    INFORMATIONAL = "informational"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_id: str
    category: str
    target_url: str
    verdict: ObservationVerdict
    severity: Severity
    title: str
    summary: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    remediation: str | None = None
    experiment_id: str | None = None
    evidence_hash: str | None = None
    catalog_version: str = "day3-v1"
    observed_at: datetime = Field(default_factory=utc_now)


class AssessmentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    targets_assessed: int
    observations: int
    passed: int
    failed: int
    informational: int
    inconclusive: int


class TestCategory(StrEnum):
    SECURITY_HEADERS = "security_headers"
    COOKIE_ATTRIBUTES = "cookie_attributes"
    CORS_CONFIGURATION = "cors_configuration"
    INFORMATION_EXPOSURE = "information_exposure"
    TRANSPORT_SECURITY = "transport_security"
    ACTOR_RESPONSE_COMPARISON = "actor_response_comparison"


class Capability(StrEnum):
    DISCOVERY = "discovery"
    AUTH_SESSION = "auth_session"
    ROLE_COMPARE = "role_compare"
    API_SCHEMA = "api_schema"
    RESOURCE_MODEL = "resource_model"
    INPUT_MUTATION = "input_mutation"
    SQLI_SAFE = "sqli_safe"
    XSS_SAFE = "xss_safe"
    IDOR_SAFE = "idor_safe"
    CSRF_SAFE = "csrf_safe"
    SSRF_CANARY = "ssrf_canary"
    FILE_UPLOAD_BENIGN = "file_upload_benign"
    PATH_TRAVERSAL_CANARY = "path_traversal_canary"
    BUSINESS_LOGIC = "business_logic"
    POC_SAFE = "poc_safe"
    LLM_PLANNING = "llm_planning"
    FINDING_VALIDATION = "finding_validation"


class RiskLevel(StrEnum):
    PASSIVE = "passive"
    SAFE_ACTIVE = "safe_active"
    STATE_CHANGING = "state_changing"


class ActionProposal(BaseModel):
    """A planner proposal; it cannot execute until a policy gate approves it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    capability: Capability
    target_url: str
    method: str = "GET"
    risk_level: RiskLevel = RiskLevel.SAFE_ACTIVE
    reason: str = Field(min_length=1, max_length=500)
    actor_id: str | None = None
    approval_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$",
    )
    fixture_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$",
    )
    destructive: bool = False


class ActionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class ActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str
    queue_id: str | None = None
    run_id: str
    capability: Capability
    target_url: str
    status: ActionStatus
    output: dict[str, Any] = Field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    started_at: datetime
    finished_at: datetime


class ToolBackend(StrEnum):
    LOCAL = "local"
    MCP = "mcp"


class ToolCall(BaseModel):
    """Policy-approved, immutable envelope sent to exactly one tool adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    call_id: str = Field(default_factory=lambda: uuid4().hex)
    action_id: str
    queue_id: str | None = None
    run_id: str
    engagement_name: str
    capability: Capability
    target_url: str
    method: str
    risk_level: RiskLevel
    reason: str
    actor_id: str | None = None
    fixture_id: str | None = None
    destructive: bool = False
    approval_verified: bool = False
    issued_at: datetime = Field(default_factory=utc_now)

    @field_validator("method")
    @classmethod
    def normalize_tool_method(cls, method: str) -> str:
        return method.upper()

    def to_proposal(self) -> ActionProposal:
        """Recreate the approved proposal without propagating approval credentials."""

        return ActionProposal(
            capability=self.capability,
            target_url=self.target_url,
            method=self.method,
            risk_level=self.risk_level,
            reason=self.reason,
            actor_id=self.actor_id,
            fixture_id=self.fixture_id,
            destructive=self.destructive,
        )


class ToolExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    call_id: str
    backend: ToolBackend
    tool_name: str
    output: dict[str, Any] = Field(default_factory=dict)
    cleanup_tool_name: str | None = None
    cleanup_arguments: dict[str, Any] | None = None


class CleanupStatus(StrEnum):
    RESERVED = "RESERVED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class CleanupTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cleanup_id: str
    run_id: str
    parent_action_id: str
    parent_queue_id: str | None = None
    capability: Capability
    backend: ToolBackend
    tool_name: str
    arguments_json: str
    status: CleanupStatus
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(default=2, ge=1, le=3)
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def arguments(self) -> dict[str, Any]:
        import json

        value = json.loads(self.arguments_json)
        if not isinstance(value, dict):
            raise ValueError("cleanup arguments must be an object")
        return value


class QueueStatus(StrEnum):
    PENDING = "PENDING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    RETRY_PENDING = "RETRY_PENDING"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class QueuedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queue_id: str
    run_id: str
    idempotency_key: str
    proposal_json: str
    status: QueueStatus
    priority: int = Field(ge=0, le=1000)
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1, le=5)
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    action_id: str | None = None
    approval_verified: bool = False
    approval_digest: str | None = None
    last_error: str | None = None
    available_at: datetime
    created_at: datetime
    updated_at: datetime

    @property
    def proposal(self) -> ActionProposal:
        return ActionProposal.model_validate_json(self.proposal_json)


class ApprovalClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    approval_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
    issuer: str = Field(min_length=1, max_length=128)
    engagement_name: str = Field(min_length=1, max_length=100)
    capabilities: frozenset[Capability] = Field(min_length=1)
    targets: tuple[str, ...] = Field(min_length=1)
    methods: frozenset[str] = Field(min_length=1)
    issued_at: datetime
    expires_at: datetime
    max_uses: int = Field(default=1, ge=1, le=1000)

    @field_validator("methods")
    @classmethod
    def normalize_approval_methods(cls, methods: frozenset[str]) -> frozenset[str]:
        return frozenset(method.upper() for method in methods)

    @model_validator(mode="after")
    def validate_approval_window(self):
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("approval timestamps must include a timezone")
        if self.expires_at <= self.issued_at:
            raise ValueError("approval expires_at must be after issued_at")
        return self


class PlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_url: str
    category: TestCategory
    reason: str
    method: str = "GET"


class TestPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plan_id: str = Field(default_factory=lambda: uuid4().hex)
    provider: str
    planner_provider: str = "rules"
    model: str = "deterministic"
    items: tuple[PlanItem, ...]
    selected_candidate_ids: tuple[str, ...] | None = None
    planner_fallback_used: bool = False
    planner_round: int = Field(default=0, ge=0)
    ai_api_calls: int = Field(default=0, ge=0)
    ai_input_tokens: int = Field(default=0, ge=0)
    ai_output_tokens: int = Field(default=0, ge=0)


class FindingStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    INCONCLUSIVE = "INCONCLUSIVE"


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_id: str
    dedup_key: str
    category: str
    test_id: str
    target_url: str
    status: FindingStatus
    severity: Severity
    title: str
    summary: str
    remediation: str | None = None
    reproduction_count: int = Field(ge=0)
    required_reproductions: int = Field(ge=2)
    negative_control_passed: bool
    experiment_ids: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    first_observed_at: datetime
    validated_at: datetime = Field(default_factory=utc_now)


class ValidationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    validation_id: str
    plan_id: str
    targets: int
    planned_tests: int
    confirmed: int
    inconclusive: int
    findings_path: str | None = None


class AttackSurfaceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "attack-surface-v3"
    exploration_run_id: str
    targets: int = Field(ge=1)
    pages: int = Field(ge=0)
    forms: int = Field(ge=0)
    endpoints: int = Field(ge=0)
    api_request_schemas: int = Field(ge=0)
    resources: int = Field(ge=0)
    ownership_assertions: int = Field(ge=0)
    actors_configured: int = Field(ge=0)
    actor_credentials_available: int = Field(ge=0)
    actors_authenticated: int = Field(ge=0)
    deferred_links: int = Field(ge=0)
    injection_candidates: int = Field(default=0, ge=0)


class ExploitabilityVerdict(StrEnum):
    CONFIRMED = "CONFIRMED"
    POTENTIAL = "POTENTIAL"
    HARDENING_GAPS = "HARDENING_GAPS"
    NOT_DEMONSTRATED = "NOT_DEMONSTRATED"


class ExploitabilityDecision(BaseModel):
    """Conservative decision; POTENTIAL is not evidence of a working exploit."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "exploitability-decision-v2"
    verdict: ExploitabilityVerdict
    confirmed_findings: int = Field(ge=0)
    inconclusive_findings: int = Field(ge=0)
    exploit_chain_demonstrated: bool = False
    bounded_exploit_demonstrated: bool = False
    rationale: str


class AgentRunStatus(StrEnum):
    CREATED = "CREATED"
    DISCOVERING = "DISCOVERING"
    INVENTORY_READY = "INVENTORY_READY"
    API_SCHEMA_MODELING = "API_SCHEMA_MODELING"
    ACTOR_SESSION_MODELING = "ACTOR_SESSION_MODELING"
    RESOURCE_MODELING = "RESOURCE_MODELING"
    ATTACK_SURFACE_MODELING = "ATTACK_SURFACE_MODELING"
    PLANNING = "PLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VALIDATING = "VALIDATING"
    EXPLOITABILITY_DECISION = "EXPLOITABILITY_DECISION"
    CLEANING_UP = "CLEANING_UP"
    REPORTING = "REPORTING"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class AgentRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    engagement_name: str
    config_hash: str
    status: AgentRunStatus = AgentRunStatus.CREATED
    resume_status: AgentRunStatus | None = None
    exploration_run_id: str | None = None
    attack_surface_json: str | None = None
    plan_json: str | None = None
    validation_id: str | None = None
    exploitability_decision_json: str | None = None
    findings_path: str | None = None
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
