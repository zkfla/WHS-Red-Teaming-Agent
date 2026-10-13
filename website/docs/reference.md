# 레퍼런스

## Engagement 설정 스키마

점검은 하나의 `Engagement`로 정의한다. 모든 통제 값이 이 설정에서 나온다.

```python
class EngagementScope(BaseModel):
    allowed_hosts / allowed_domains / allowed_networks / allowed_assets: list[str]
    denied_hosts / denied_domains / denied_assets: list[str]

class RuleOfEngagement(BaseModel):
    allowed_capabilities / denied_capabilities: list[str]
    allowed_operations / denied_operations: list[str]
    max_parallel_actions: int | None
    availability_impact_allowed: bool = False
    start_at / end_at: datetime | None
    allowed_time_windows / prohibited_techniques: list[str]

class ExecutionBudget(BaseModel):
    max_actions: int
    max_tool_calls: int | None
    max_duration_seconds: int | None

class RateLimitPolicy(BaseModel):
    max_actions_per_minute: int
    max_concurrent_actions: int = 1

class ApprovalPolicy(BaseModel):
    require_approval_for: list[str]
    auto_approve: bool = False
    risk_threshold: RiskLevel | None
    auto_approve_up_to: RiskLevel | None
    command_approval_rules: list[CommandApprovalRule]

class Engagement(BaseModel):
    engagement_id: EngagementId
    name: str
    scope: EngagementScope
    rules_of_engagement: list[RuleOfEngagement]
    execution_budget: ExecutionBudget
    rate_limit_policy: RateLimitPolicy
    approval_policy: ApprovalPolicy
    created_at: datetime
```

## 빠른 시작 (CLI)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,agent]"     # 개발 도구와 실제 LLM 제공자

redteam                            # 대화형: 설정, 사전점검, 실행
redteam init                       # ./engagement.yaml 생성
redteam validate engagement.yaml   # 스키마, ROE, capability 검사 (대상 미접촉)
redteam run engagement.yaml        # end-to-end 실행
redteam report generate <run-id>   # 리포트 생성
redteam web                        # 로컬 웹 UI
```

## 모듈 구조

```text
src/redteam/
  core/          # contracts(Pydantic)와 ports(Protocol), 단일 진실 공급원
  orchestration/ # LangGraph 실행, 플래너와 워커, 라우터와 노드
  attack_graph/  # AttackGraphStore 구현과 서비스
  event_bus/     # EventBus 구현
  control/       # Control Plane (scope, policy, rate, budget, approval, audit)
  evidence/      # EvidenceStore 구현
  capability/    # ToolGateway, MCP Registry와 Adapter, servers/
  llm/           # LiteLLM, Fake LLM 클라이언트
  intake/        # 자연어에서 EngagementConfig로
  app/           # bootstrap(조립 루트)과 CLI, web UI
```

## 용어집

| 용어 | 정의 |
| --- | --- |
| ActionProposal | 에이전트가 도구 실행을 요청하는 유일한 수단. ALLOW만 ToolInvocation이 됨 |
| Probe | 가설을 확인하기 위한 검증 가능한 원자 작업 |
| Control Plane | 모든 도구 호출 전 통과하는 6단계 admission 관문 |
| admission | 실행 직전 현재 상태로 scope, time, target, outbound, duration을 재검하는 단계 |
| input_digest | 정확한 proposal을 식별하는 해시. 멱등성과 재개의 기준 |
| execution_fingerprint | 승인과 결정을 원래 실행 의도와 연결하는 지문 |
| OperatorIntent | 실행 중 운영자가 전달하는 개입. 승인과는 별개 경로 |
