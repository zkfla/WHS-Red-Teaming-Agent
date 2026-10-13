# 도메인 계약

도메인 데이터는 `core/contracts`의 Pydantic 모델로, 상호작용은 `core/ports`의 Protocol로 표현한다. 아래는 핵심 계약의 코드 레벨 요약이다.

## 식별자와 열거형

식별자는 `core/ids`의 `NewType`(`ActionId`, `RunId`, `EngagementId`, `TaskId`, `InvocationId`, `EvidenceId` 등)으로 타입을 분리한다. 주요 열거형은 다음과 같다.

| Enum | 값 |
| --- | --- |
| `PolicyVerdict` | `ALLOW`, `DENY`, `REQUIRE_APPROVAL` |
| `Severity` (DENY 꼬리표) | `BLOCK_ACTION`, `REPLAN_GOAL`, `ABORT_RUN` |
| `RiskLevel` | `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` |
| `AgentPhase` (LangGraph 노드) | `BOOTSTRAP, OBSERVE, PLAN, POLICY, EXECUTE, VERIFY, UPDATE, REPLAN, FINISHED, FAILED` |
| `ExecutionState` | `PENDING, RUNNING, SUCCEEDED, FAILED, CANCELLED, UNKNOWN` |
| `TaskStatus` | `PENDING, READY, RUNNING, BLOCKED, COMPLETED, FAILED, CANCELLED` |
| `MissionStatus` | `DRAFT, ACTIVE, PAUSED, BLOCKED, COMPLETED, FAILED, CANCELLED` |
| `OperatorIntentType` | `GUIDANCE, REPRIORITIZE, FOCUS, EXCLUDE, PAUSE, RESUME, STOP, EXPLANATION_QUERY, STATUS_QUERY` |
| `GraphNodeType` | `host, service, endpoint, principal, credential, asset, resource, hypothesis, finding` |
| `GraphEdgeType` | `hosts, exposes, authenticates_to, can_access, owns, observed, related_to, targets, supported_by, derived_from` |
| `EvidenceSourceType` | `tool, agent, system, operator` |
| `ApprovalStatus` | `pending, approved, rejected, expired` |

## ActionProposal, 에이전트의 유일한 실행 요청

에이전트는 Capability Layer를 직접 호출하지 못한다. `ActionProposal`을 만들어 `ControlContext`와 함께 Control Plane에 넘긴다. ALLOW 결정만 `ToolInvocation`이 될 수 있다.

```python
class ActionProposal(BaseModel):
    action_id: ActionId
    run_id: RunId
    engagement_id: EngagementId
    agent_id: AgentId
    instance_id: AgentInstanceId | None = None   # 한 역할의 여러 동시 인스턴스
    binding_mode: BindingMode = LEGACY_ACTION     # LEGACY_ACTION | SELECTED_TOOL
    tool_id: ToolId | None = None
    capability: str = ""
    operation: str = ""
    target: TargetRef | None
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str
    hypothesis_id: HypothesisId | None = None
    expected_observation: str | None = None
    availability_impact: bool = False             # 가용성 영향 가능 행동인지, 에이전트가 선언
    created_at: datetime
```

`availability_impact`는 Control이 `operation`에서 추론하지 않고 에이전트가 선언한다. 이 값이 True여야 `RuleOfEngagement.availability_impact_allowed`가 의미를 갖는다.

## ControlContext, PolicyDecision, Approval

```python
class PolicyDecision(BaseModel):
    action_id: ActionId
    verdict: PolicyVerdict
    reasons: list[str] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)   # "severity:<level>" 태그도 여기 실림
    approval_id: ApprovalId | None = None
    rate_limit_remaining: int | None = None
    budget_remaining: int | None = None
    evaluated_at: datetime
    severity: Severity | None = None
    human_required: bool = False
    execution_fingerprint: str | None = None
```

`ControlContext`는 `evaluate`가 proposal 외에 필요한 값(engagement_id, correlation_id, `in_flight_actions`, 실행 경로가 채우는 `selected_tool_id`)을 담는다. `Approval`은 승인ID, action_id, status, `execution_fingerprint`로 원래 행동과 승인 기록을 연결한다.

## ToolInvocation, ToolResult, ToolDescriptor

```python
class ExecutionContext(BaseModel):
    engagement_id: EngagementId
    run_id: RunId
    agent_id: AgentId | None = None
    instance_id: AgentInstanceId | None = None
    approved_target: TargetRef | None = None   # ALLOW proposal에서 기계적으로 복사됨
    correlation_id: str | None = None
    dry_run: bool = False

class ToolResult(BaseModel):
    invocation_id: InvocationId
    action_id: ActionId
    tool_id: ToolId
    success: bool
    output: dict[str, Any] | str | None = None
    error: ToolError | None = None            # code, message, retryable, details
    started_at: datetime
    finished_at: datetime                      # finished >= started 검증
```

`ToolDescriptor`는 도구 메타데이터(능력 태그, target과 resource 바인딩)를 담고 `ToolCatalog` 포트로 조회한다. 실제 MCP 원격 도구 이름은 `tool_id`로부터 `ToolDescriptor`에서 해석된다.

## EvidenceRecord

```python
class EvidenceRecord(BaseModel):
    evidence_id: EvidenceId
    run_id: RunId
    engagement_id: EngagementId
    source_type: EvidenceSourceType
    source_id: str
    action_id: ActionId | None = None
    invocation_id: InvocationId | None = None
    hypothesis_id: HypothesisId | None = None
    content_type: str
    artifact_ref: str | None = None   # 큰 바이너리는 직접 embed하지 않고 참조
    digest: str
    summary: str | None = None
    created_at: datetime
```

## Attack Graph: Node, Edge, Hypothesis, Finding

공유 공격 그래프는 발견한 자산과 관계, 가설, 결론을 하나의 그래프로 축적한다.

```python
class GraphNode(BaseModel):
    id: GraphNodeId
    type: GraphNodeType            # host, service, endpoint, principal, credential, asset, resource, hypothesis, finding
    labels: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

class Finding(BaseModel):
    finding_id: str
    title: str
    description: str = ""
    severity: str = "info"
    verified: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
```

`GraphEdge`는 `type`(`hosts`, `exposes`, `authenticates_to`, `can_access`, `owns`, `observed`, `related_to`, `targets`, `supported_by`, `derived_from`)으로 자산 사이 관계를 표현한다. `Finding`의 `verified`와 `evidence_ids`가 검증된 결론과 원본 증거를 연결한다. 그래프 변경은 `GraphMutationReceipt`로 네임스페이스와 Run, 변경 ID 단위로 기록된다.

## Mission, Task, InvestigationResult, CompletionCriterion

`MissionPlan`과 `MissionTask`가 목표와 작업을 표현한다. `InvestigationResult`는 confirmed, refuted, inconclusive, blocked를 Task 진행과 Mission 성공으로부터 구분하고, 질문(`ResultQuestion`)은 결과 항목으로 남는다.

작업 완료 조건은 코드가 소유하는 `CompletionCriterion`으로 고정한다. 실행 권한이 아니라 완료 판정의 기준이다.

```python
ALLOWED_CRITERION_KINDS = {
    "evidence", "observation", "hypothesis_confirmed", "hypothesis_refuted",
    "coverage", "poc", "semantic",
}  # 목록에 없는 legacy kind는 "unknown"으로 정규화되어 영구히 증명 불가

class CompletionCriterion(BaseModel):
    kind: str = "unknown"
    description: str = ""
    min_evidence: int = Field(default=1, ge=1)
    hypothesis_id: str | None = None
    principal_refs: list[str] = Field(default_factory=list)
```

`completion_contract_hash`는 목적, 기준, 대상, principal, 가설만으로 계산되며 런타임 상태와 소유권, revision은 제외한다. 완료 조건이 바뀌면 해시가 바뀐다.

## EventEnvelope와 EventType

모든 이벤트는 하나의 봉투를 쓴다. `payload`는 저장 시점에 비밀값이 자동 마스킹된다(`access_token`, `client_secret` 같은 키와 본문 내 토큰 패턴).

```python
class EventEnvelope(BaseModel):
    event_id: EventId
    event_type: str            # EventType 상수를 사용
    run_id: RunId
    engagement_id: EngagementId
    source: str
    timestamp: datetime
    correlation_id: str | None = None
    causation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)  # 비밀값 자동 마스킹
```

`EventType`은 schema 마이그레이션 없이 확장할 수 있도록 StrEnum이 아닌 문자열 상수 모음이다. 대표 값으로 `RunStarted`, `ActionProposed`, `PolicyEvaluated`, `ActionApproved`, `ActionDenied`, `ApprovalRequired`, `ToolExecutionStarted/Finished/Failed`, `EvidenceCreated`, `GraphUpdated`, `FindingCreated/Verified`, `TaskCreated/Started/Completed/Failed`, `OperatorIntentApplied/Rejected`가 있다.

## 포트 (Protocol)

| 포트 | 핵심 메서드 |
| --- | --- |
| `ControlPlane` | `evaluate(proposal, context) -> PolicyDecision`, `approval_outcome(approval_id, current_proposal)` |
| `InvocationAdmissionControl` | `admit_invocation(...)`, `reserve_extra_tool_call(...)`. 실행 직전 재검과 재시도 회계 |
| `ToolGateway`, `ToolCatalog` | 도구 실행, `get`, `all_descriptors`, `list_by_capability` |
| `EvidenceStore` | `append`, `get`, `list_for_run`, `list_for_action` |
| `AttackGraphStore` | `add_node`, `add_edge`, `get_node`, `neighbors`, `query` |
| `EventBus` | `publish`, `subscribe` |
| `LLMClient`, `NativeToolCallingLLMClient`, `MeteredLLMClient` | `complete`, `complete_structured`, `complete_native`, `complete_structured_with_usage` |
| `MissionTaskStore` | `create/get/list/update_task`, `transition_task`, `split_task`, `block/unblock` |
| `ExecutionRecoveryProvider` | `prepare_reference`, `query_execution`, `fetch_execution`. 재개 시 원본 상태 조회 |
