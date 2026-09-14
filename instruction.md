# 역할

너는 시니어 소프트웨어 아키텍트이자 AI Agent Framework 엔지니어다.

우리는 **인가된 보안 테스트 환경을 대상으로 하는 멀티에이전트 기반 Red Teaming Framework**를 개발한다.

현재 단계의 목표는 완성된 Red Teaming Agent를 만드는 것이 아니다.

**7명의 개발자가 각자 맡은 영역을 병렬로 개발할 수 있도록, PM이 먼저 GitHub에 올릴 공통 Repository 구조, Core Schema, Interface, Dependency Boundary, 최소 실행 골격을 만드는 것**이 목적이다.

따라서 이번 작업에서는 복잡한 기능 구현보다 다음을 우선한다.

1. 프로젝트 전체 구조 확정
2. 팀 간 경계 정의
3. 공통 데이터 스키마 정의
4. Interface / Protocol 정의
5. 각 컴포넌트 간 의존 방향 정의
6. 최소 실행 가능한 Skeleton 작성
7. Mock/In-memory 구현을 이용한 통합 테스트
8. 각 팀이 구현해야 할 위치와 책임을 명확히 문서화

실제 취약점 공격 로직이나 특정 Exploit은 구현하지 않는다.

---

# 1. 프로젝트 아키텍처 원칙

우리 Framework 자체가 시스템의 주체이며 외부 Agent Framework는 **구현을 도와주는 Runtime**으로만 사용한다.

기본 기술 방향은 다음과 같다.

* Agent Workflow / Orchestration: **LangGraph**
* LLM Provider abstraction: **LiteLLM**
* Security Capability: **기존 MCP 서버 활용**
* hackingBuddyGPT: 필요한 구현 패턴이나 Capability 참고/선택적 Adapter 활용 가능
* Attack Graph: 자체 구현
* Event Bus: 자체 Interface
* Evidence Store: 자체 구현
* Engagement Control Plane: 자체 구현

중요:

**LangGraph, LiteLLM, MCP 등이 프로젝트의 Domain Model을 소유하면 안 된다.**

Framework 자체의 `core/contracts`가 Source of Truth가 되어야 한다.

---

# 2. 전체 구조

전체 논리 구조는 다음과 같다.

```text
                         User / Operator
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Engagement Config   │
                    │ Scope / ROE / Limit │
                    └──────────┬──────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────┐
│                 Agent Orchestration Layer                  │
│                                                            │
│        Observe → Plan → Route → Execute → Verify           │
│          ▲                           │           │          │
│          └──────────── Re-plan ◀─────┴───────────┘          │
│                                                            │
│                    LangGraph Runtime                       │
└───────────────────────────┬────────────────────────────────┘
                            │
                     Action Proposal
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                  Engagement Control Plane                  │
│                                                            │
│ Scope & ROE                                                │
│ Policy Engine                                              │
│ Approval                                                   │
│ Rate Limit                                                 │
│ Budget / Execution Limits                                  │
│ Audit & Logging                                            │
└───────────────────────────┬────────────────────────────────┘
                            │
                    Approved Action
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                    Capability Layer                        │
│                                                            │
│ MCP Tool Registry                                          │
│ MCP Adapter                                                │
│ Tool Gateway                                               │
│                                                            │
│ 기존 MCP Server를 사용하며 새 보안 Tool을 직접 만들지 않는다. │
└───────────────────────────┬────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                   Execution Substrate                      │
│                                                            │
│ MCP Server Containers                                      │
│ Docker / Docker Compose                                    │
│ Health Check                                               │
│ Lifecycle Management                                       │
└───────────────────────────┬────────────────────────────────┘
                            │
                            ▼
                         Target


모든 실행 결과
      │
      ├──────────────→ Evidence Store
      │
      ├──────────────→ Event Bus
      │
      └──────────────→ Shared Attack Graph
```

---

# 3. 팀 구성

개발팀은 다음과 같다.

## PM — 1명

담당:

* 전체 Repository 구조
* 공통 Schema
* Core Interface
* Dependency Rule
* Naming Convention
* Integration Contract
* Architecture Documentation

PM 영역은 다른 팀이 임의로 변경하지 못하도록 설계한다.

---

## Agent 팀 — 3명

담당:

### Agent Orchestration

* LangGraph 기반 Workflow
* Agent routing
* Agent state
* Planner
* Re-plan
* termination
* agent lifecycle

### Shared Attack Graph

* Host
* Service
* Endpoint
* Principal
* Credential
* Asset
* Hypothesis
* Finding
* Relation

등의 보안 Domain State 관리

### Event Bus

컴포넌트 간 Event 전달.

예:

```text
RunStarted
AgentStarted
HypothesisCreated
ActionProposed
PolicyEvaluated
ActionApproved
ActionDenied
ToolExecutionStarted
ToolExecutionFinished
EvidenceCreated
GraphUpdated
FindingVerified
RunFinished
```

---

## Control 팀 — 2명

담당:

### Evidence Store

Tool 실행 결과와 검증 근거 저장.

### Engagement Control Plane

포함 기능:

* Scope
* Rules of Engagement
* Policy Engine
* Approval
* Rate Limit
* Execution Budget
* Audit
* Logging

Control Plane은 Prompt 기반 안전장치가 아니라 **실제 Tool 호출 직전의 코드 기반 강제 Gate**여야 한다.

---

## Capability 팀 — 2명

담당:

### Capability Layer

새로운 보안 Tool을 직접 만드는 것이 목적이 아니다.

**기존 MCP Server를 조사하고 선택하여 Framework에서 사용할 수 있도록 Adapter를 작성한다.**

구조:

```text
Framework
   │
ToolGateway
   │
MCPToolRegistry
   │
MCPAdapter
   │
Existing MCP Server
```

### Execution Substrate

Capability 팀이 함께 소유한다.

담당:

* MCP Server Docker image
* docker-compose
* Container lifecycle
* Health check
* MCP endpoint registration
* Environment isolation
* deployment configuration

---

# 4. 가장 중요한 Architecture Rule

다음 의존 방향을 반드시 지킨다.

```text
core
 ↑
 ├── orchestration
 ├── attack_graph
 ├── event_bus
 ├── control
 ├── evidence
 └── capability
```

`core`는 다른 모듈을 import하면 안 된다.

반대로 다른 모듈은 `core`의 schema/interface를 사용한다.

예:

```text
BAD

core → capability
core → langgraph


GOOD

capability → core
orchestration → core
control → core
```

---

# 5. Framework Source of Truth

Framework-specific library가 Domain State를 소유하면 안 된다.

다음 세 가지가 Source of Truth이다.

```text
Shared Attack Graph
Evidence Store
Core Contracts
```

LangGraph State는 Workflow Runtime에 필요한 최소 정보만 보관한다.

예:

```python
class RuntimeState:
    run_id: RunId
    engagement_id: EngagementId

    phase: AgentPhase
    current_agent: AgentId | None

    current_goal: str | None

    active_hypothesis_id: HypothesisId | None
    pending_action_id: ActionId | None

    retry_count: int
    step_count: int

    remaining_budget: int | None

    last_verdict: Verdict | None
```

Host, Endpoint, Credential, Finding 등의 전체 데이터는 RuntimeState에 복제하지 않는다.

ID를 통해 Attack Graph를 참조한다.

---

# 6. 반드시 만들어야 하는 Core Contracts

다음 데이터 모델은 `core/contracts` 또는 이에 준하는 독립 패키지에 정의한다.

Pydantic 기반 모델을 권장한다.

---

## Identity

명확한 Typed ID를 정의한다.

예:

```text
RunId
EngagementId
AgentId
ActionId
ToolId
EvidenceId
HypothesisId
FindingId
GraphNodeId
GraphEdgeId
ApprovalId
EventId
```

단순 문자열을 프로젝트 전체에서 무분별하게 사용하지 않는다.

---

## Engagement

최소한 다음 구조를 포함한다.

```python
Engagement
EngagementScope
RuleOfEngagement
ExecutionBudget
RateLimitPolicy
ApprovalPolicy
```

예:

```python
class EngagementScope:
    allowed_hosts: list[str]
    allowed_domains: list[str]
    allowed_networks: list[str]
    denied_hosts: list[str]
```

현재 단계에서는 세부 Policy 구현보다 Schema와 Interface 정의가 중요하다.

---

## ActionProposal

Agent가 Tool을 직접 실행하지 않는다.

반드시 다음과 같은 Action Proposal을 만든다.

```python
class ActionProposal:
    action_id: ActionId
    run_id: RunId
    agent_id: AgentId

    capability: str
    operation: str

    target: TargetRef

    arguments: dict

    reason: str

    hypothesis_id: HypothesisId | None

    expected_observation: str | None
```

---

## PolicyDecision

Control Plane 결과.

```python
ALLOW
DENY
REQUIRE_APPROVAL
```

예:

```python
class PolicyDecision:
    action_id: ActionId
    decision: PolicyVerdict
    reasons: list[str]

    matched_rules: list[str]

    approval_id: ApprovalId | None
```

---

## ToolInvocation

Control Plane이 허가한 Action만 ToolInvocation으로 변환 가능하다.

```python
ToolInvocation
```

에는 최소한 다음 정보가 있어야 한다.

```text
invocation_id
action_id
tool_id
operation
arguments
timeout
execution_context
```

---

## ToolResult

모든 MCP Tool 결과를 Framework 공통 형태로 Normalize한다.

```python
class ToolResult:
    invocation_id: str
    tool_id: ToolId

    success: bool

    output: dict | str | None

    error: ToolError | None

    started_at: datetime
    finished_at: datetime

    metadata: dict
```

---

## EvidenceRecord

```python
class EvidenceRecord:
    evidence_id: EvidenceId
    run_id: RunId

    source_type: EvidenceSourceType
    source_id: str

    action_id: ActionId | None

    content_type: str

    artifact_ref: str | None

    digest: str

    created_at: datetime

    metadata: dict
```

Evidence는 가능한 한 append-only 구조를 고려한다.

---

## EventEnvelope

모든 Event가 공통 Envelope를 사용한다.

```python
class EventEnvelope:
    event_id: EventId

    event_type: str

    run_id: RunId

    source: str

    timestamp: datetime

    correlation_id: str | None
    causation_id: str | None

    payload: dict
```

향후 Kafka/NATS/Redis 등으로 교체할 수 있도록 Event Bus Backend와 Event Schema를 분리한다.

초기 구현은 InMemoryEventBus면 충분하다.

---

# 7. Attack Graph 기본 Schema

초기 버전에서는 복잡한 그래프 DB를 도입하지 않는다.

Domain Model과 Store Interface부터 정의한다.

Node 후보:

```text
Host
Service
Endpoint
Principal
Credential
Asset
Resource
Hypothesis
Finding
```

Edge 후보:

```text
HOSTS
EXPOSES
AUTHENTICATES_TO
CAN_ACCESS
OWNS
OBSERVED
RELATED_TO
TARGETS
SUPPORTED_BY
DERIVED_FROM
```

그래프 Backend는 교체 가능해야 한다.

예:

```python
class AttackGraphStore(Protocol):

    async def add_node(...):
        ...

    async def add_edge(...):
        ...

    async def get_node(...):
        ...

    async def neighbors(...):
        ...

    async def query(...):
        ...
```

초기에는 `InMemoryAttackGraphStore`를 구현한다.

Neo4j 등의 외부 그래프 DB는 지금 도입하지 않는다.

---

# 8. Event Bus Interface

다음과 같이 Backend-independent Interface를 만든다.

```python
class EventBus(Protocol):

    async def publish(
        self,
        event: EventEnvelope,
    ) -> None:
        ...

    async def subscribe(
        self,
        event_type: str,
        handler: EventHandler,
    ) -> None:
        ...
```

초기 구현:

```text
InMemoryEventBus
```

향후:

```text
Redis
NATS
Kafka
```

등으로 교체 가능하도록 한다.

---

# 9. Control Plane Interface

Control Plane은 하나의 명확한 진입점을 제공한다.

개념적으로:

```python
decision = await control_plane.evaluate(action, context)
```

내부 pipeline:

```text
ActionProposal
      ↓
Scope Check
      ↓
ROE Check
      ↓
Policy Engine
      ↓
Rate Limit
      ↓
Budget
      ↓
Approval
      ↓
ALLOW / DENY / REQUIRE_APPROVAL
```

절대로 Agent가 Control Plane을 우회해서 Capability를 직접 호출하지 못하게 한다.

---

# 10. Capability Layer

Capability Layer는 다음 구조를 사용한다.

```text
ToolGateway
    │
MCPToolRegistry
    │
MCPAdapter
    │
MCP Server
```

공통 Interface 예:

```python
class ToolGateway(Protocol):

    async def invoke(
        self,
        invocation: ToolInvocation,
    ) -> ToolResult:
        ...
```

MCP Registry는 최소한:

```text
tool_id
server_id
tool_name
description
input_schema
capability_tags
risk_level
health_status
```

정보를 관리하도록 한다.

---

# 11. MCP Adapter

MCP Server마다 Framework 내부 코드를 변경하지 않는다.

Adapter pattern을 사용한다.

예:

```python
class MCPAdapter(Protocol):

    async def discover_tools(self) -> list[ToolDescriptor]:
        ...

    async def invoke(
        self,
        tool: ToolDescriptor,
        arguments: dict,
    ) -> ToolResult:
        ...

    async def health_check(self) -> HealthStatus:
        ...
```

Capability 팀은 기존 MCP Server를 이 Interface에 맞춰 연결한다.

---

# 12. Execution Substrate

다음 디렉터리를 준비한다.

```text
deploy/
    docker/
    compose/
    mcp/
```

최소한:

```text
docker-compose.yml
.env.example
MCP server healthcheck example
container naming convention
network naming convention
volume policy
```

를 제공한다.

현재 단계에서는 실제 공격 도구 MCP Server를 많이 추가하지 않는다.

Dummy MCP 또는 harmless example server 하나만 연결해서 전체 흐름을 검증한다.

---

# 13. LLM Layer

LiteLLM을 Model abstraction layer로 사용한다.

하지만 LiteLLM 호출 코드가 Agent 전체에 흩어지면 안 된다.

다음 Interface를 둔다.

```python
class LLMClient(Protocol):

    async def complete(...):
        ...

    async def complete_structured(...):
        ...
```

구현:

```text
LiteLLMClient
FakeLLMClient
```

테스트에서는 FakeLLMClient를 사용 가능하게 한다.

---

# 14. LangGraph 사용 원칙

LangGraph는 Workflow Runtime으로만 사용한다.

LangGraph의 State에 Domain 전체를 넣지 않는다.

초기 Graph는 아주 단순하게 만든다.

```text
START
  ↓
OBSERVE
  ↓
PLAN
  ↓
POLICY
  ↓
EXECUTE
  ↓
VERIFY
  ↓
UPDATE
  ↓
REPLAN
  ↓
END
```

실제 LLM 기반 복잡한 Agent는 구현하지 않는다.

Stub Node 또는 Fake Agent를 사용해서 전체 Architecture가 연결되는지만 검증한다.

---

# 15. 권장 Repository 구조

다음 구조를 기준으로 더 좋은 구조가 있으면 개선해서 제안하라.

```text
repo/
│
├── README.md
├── ARCHITECTURE.md
├── CONTRIBUTING.md
├── DEVELOPMENT.md
├── pyproject.toml
├── .env.example
├── .gitignore
│
├── docs/
│   ├── architecture/
│   │   ├── overview.md
│   │   ├── dependency-rules.md
│   │   ├── execution-flow.md
│   │   └── team-boundaries.md
│   │
│   ├── contracts/
│   │   ├── events.md
│   │   ├── actions.md
│   │   ├── evidence.md
│   │   └── attack-graph.md
│   │
│   └── adr/
│       ├── 0001-langgraph-runtime.md
│       ├── 0002-litellm-gateway.md
│       └── 0003-mcp-capability.md
│
├── src/
│   └── redteam/
│       │
│       ├── core/
│       │   ├── ids.py
│       │   ├── enums.py
│       │   │
│       │   ├── contracts/
│       │   │   ├── engagement.py
│       │   │   ├── action.py
│       │   │   ├── policy.py
│       │   │   ├── tool.py
│       │   │   ├── evidence.py
│       │   │   ├── events.py
│       │   │   └── graph.py
│       │   │
│       │   └── ports/
│       │       ├── llm.py
│       │       ├── event_bus.py
│       │       ├── attack_graph.py
│       │       ├── control_plane.py
│       │       ├── evidence_store.py
│       │       └── tool_gateway.py
│       │
│       ├── orchestration/
│       │   ├── state.py
│       │   ├── graph.py
│       │   ├── router.py
│       │   └── nodes/
│       │
│       ├── attack_graph/
│       │   ├── service.py
│       │   └── stores/
│       │       └── memory.py
│       │
│       ├── event_bus/
│       │   └── memory.py
│       │
│       ├── control/
│       │   ├── service.py
│       │   ├── scope.py
│       │   ├── policy.py
│       │   ├── approval.py
│       │   ├── rate_limit.py
│       │   ├── budget.py
│       │   └── audit.py
│       │
│       ├── evidence/
│       │   ├── service.py
│       │   └── stores/
│       │       └── filesystem.py
│       │
│       ├── capability/
│       │   ├── gateway.py
│       │   ├── registry.py
│       │   │
│       │   └── adapters/
│       │       └── mcp/
│       │           ├── base.py
│       │           └── example.py
│       │
│       ├── llm/
│       │   ├── litellm.py
│       │   └── fake.py
│       │
│       └── app/
│           ├── bootstrap.py
│           └── cli.py
│
├── deploy/
│   ├── docker/
│   ├── compose/
│   │   └── docker-compose.yml
│   └── mcp/
│
├── examples/
│   └── minimal_run.py
│
└── tests/
    ├── unit/
    ├── contract/
    └── integration/
```

이 구조를 무조건 따르지는 말고, 필요하면 수정하되 **왜 수정했는지 설명하라.**

---

# 16. 반드시 동작해야 하는 Minimal Vertical Slice

실제 취약점 공격 없이 다음 흐름이 실행되어야 한다.

```text
1. Engagement 생성

2. Fake Recon Agent 실행

3. Agent가 ActionProposal 생성

4. Control Plane이 Action 평가

5. ALLOW 결정

6. ToolGateway 호출

7. Dummy MCP Adapter 실행

8. ToolResult 반환

9. Evidence 생성

10. EventBus에 이벤트 발생

11. Attack Graph 업데이트

12. LangGraph가 다음 상태로 진행

13. Run 종료
```

이 흐름 하나가 실제 코드로 동작하도록 한다.

CLI 예:

```bash
python -m redteam.app.cli run examples/engagement.yaml
```

또는 이에 준하는 명령을 제공한다.

---

# 17. 반드시 작성해야 하는 테스트

최소한 다음 테스트를 만든다.

### Contract Test

Pydantic schema validation 테스트.

### Scope Deny Test

허용되지 않은 Target에 대한 ActionProposal이 DENY 되는지 확인한다.

### Control Plane Bypass Test

Agent/Orchestration Layer가 Capability를 직접 호출하지 않는 Architecture를 검증하거나 문서/구조적으로 강제한다.

### Evidence Test

ToolResult가 EvidenceRecord로 변환되는지 확인한다.

### Event Test

Action 실행 과정에서 필요한 Event가 발생하는지 확인한다.

### Attack Graph Test

Observation 또는 Evidence를 이용해 Graph Node/Edge가 추가되는지 확인한다.

### MCP Adapter Test

Dummy MCP Adapter가 ToolGateway Interface를 만족하는지 확인한다.

### End-to-End Test

```text
Agent
→ Control Plane
→ Capability
→ Evidence
→ Event
→ Attack Graph
```

전체 흐름 하나를 검증한다.

---

# 18. Team Boundary 문서

`docs/architecture/team-boundaries.md`를 반드시 만든다.

다음 표를 포함한다.

| 영역                  | 담당              | 변경 가능 영역 |
| ------------------- | --------------- | -------- |
| Core Contracts      | PM              | PM 승인 필요 |
| Architecture        | PM              | PM 승인 필요 |
| Orchestration       | Agent Team      | 자유       |
| Attack Graph        | Agent Team      | 자유       |
| Event Bus           | Agent Team      | 자유       |
| Evidence Store      | Control Team    | 자유       |
| Control Plane       | Control Team    | 자유       |
| Capability          | Capability Team | 자유       |
| MCP Adapter         | Capability Team | 자유       |
| Execution Substrate | Capability Team | 자유       |

공통 Schema 변경이 필요한 경우:

```text
Issue
→ Schema Change Proposal
→ PM Review
→ Merge
```

과정을 따르도록 문서화한다.

---

# 19. 팀 간 Contract

팀들은 서로의 내부 구현을 import하지 않는다.

예를 들어 Orchestration에서:

```python
from redteam.control.service import ControlService
```

처럼 구체 구현을 직접 참조하지 않는 방향을 우선한다.

대신:

```python
from redteam.core.ports.control_plane import ControlPlane
```

을 참조한다.

Bootstrap 단계에서 실제 구현을 연결한다.

예:

```python
control_plane: ControlPlane = DefaultControlPlane(...)
event_bus: EventBus = InMemoryEventBus(...)
graph_store: AttackGraphStore = InMemoryAttackGraphStore(...)
tool_gateway: ToolGateway = MCPToolGateway(...)
```

Dependency Injection을 복잡한 DI Framework 없이 Python 코드로 단순하게 구성한다.

---

# 20. Coding Rule

Python 최신 안정 버전 계열을 기준으로 한다.

다음 원칙을 지킨다.

* type hint 적극 사용
* Pydantic 사용
* async-first
* Protocol 또는 ABC를 이용한 Interface
* dependency injection
* 작은 module
* framework-specific object의 core 침투 금지
* circular dependency 금지
* global state 최소화
* `Any` 남용 금지
* dict 남용 금지
* public contract에는 명확한 schema 사용

Formatter / Linter / Test 도구도 구성한다.

예:

```text
ruff
mypy 또는 pyright
pytest
pytest-asyncio
```

---

# 21. 하지 말아야 할 것

현재 단계에서는 다음을 하지 않는다.

* 실제 Exploit 개발
* 공격 Payload 데이터베이스 구축
* 실제 금융 서비스 연결
* 자동 공격 로직 대량 구현
* Graph DB 도입
* Kafka 도입
* Redis 도입
* Kubernetes 도입
* 복잡한 Web UI 개발
* 자체 MCP Server 대량 개발
* LangGraph 내부에 모든 상태 저장
* Agno/MAF/OpenAI Agents SDK 등을 동시에 추가
* 과도한 abstraction

현재 목표는 **확장 가능한 최소 골격**이다.

---

# 22. 최종 산출물

다음 순서대로 작업하라.

## Step 1

현재 요구사항을 분석하고 최종 Architecture를 제안한다.

먼저 다음을 출력하라.

```text
Architecture decisions
Module responsibilities
Dependency direction
Potential conflicts
```

---

## Step 2

최종 Repository Tree를 제안한다.

각 주요 폴더 옆에 담당 팀도 표시하라.

---

## Step 3

Core Contract와 Port Interface를 먼저 구현한다.

다른 구현보다 이것을 우선한다.

---

## Step 4

각 팀 영역에 최소 Skeleton을 만든다.

Skeleton은 TODO와 Interface 중심으로 작성한다.

---

## Step 5

In-memory / Fake 구현으로 Minimal Vertical Slice를 완성한다.

---

## Step 6

Unit / Contract / Integration Test를 작성한다.

---

## Step 7

README와 Architecture 문서를 작성한다.

README에는 반드시 다음 내용을 포함한다.

```text
Project Goal
Architecture
Quick Start
Repository Structure
Core Concepts
Execution Flow
Team Ownership
Development Rules
How to Add Agent
How to Add Policy
How to Add MCP Server
How to Run Tests
```

---

# 23. 작업 시 중요한 원칙

코드를 많이 만드는 것이 목적이 아니다.

**팀원들이 이 Repository를 clone한 뒤 자기 담당 폴더에서 개발을 시작해도 전체 시스템이 깨지지 않도록 만드는 것이 목적이다.**

따라서 판단 우선순위는:

```text
1. Contract 안정성
2. Module Boundary
3. Dependency Direction
4. Testability
5. Replaceability
6. 최소 실행 가능성
7. 기능 수
```

순서다.

필요하면 요구사항에서 더 좋은 Architecture를 제안해도 되지만 다음 세 가지는 유지한다.

1. 자체 Control Plane
2. 자체 Shared Attack Graph / Evidence Store
3. MCP 기반 Capability Layer

또한 외부 Framework에 프로젝트 전체 Architecture가 종속되지 않도록 한다.

---

# 24. 완료 조건

다음 질문에 모두 YES가 되어야 한다.

* Agent 팀이 Control 팀 코드 내부를 몰라도 개발 가능한가?
* Control 팀이 LangGraph 내부 구현을 몰라도 개발 가능한가?
* Capability 팀이 Agent 구현을 몰라도 MCP Adapter를 만들 수 있는가?
* MCP Server를 교체해도 Agent 코드를 수정하지 않아도 되는가?
* LiteLLM을 다른 LLM Gateway로 교체할 수 있는가?
* LangGraph를 다른 Workflow Runtime으로 교체할 수 있는가?
* Event Bus Backend를 교체할 수 있는가?
* Attack Graph Backend를 교체할 수 있는가?
* 모든 Tool 실행이 Control Plane을 거치는가?
* 모든 중요 실행 결과를 Evidence로 남길 수 있는가?
* 각 팀의 코드가 Core Contract를 통해 연결되는가?

하나라도 NO라면 구조를 다시 검토하라.

먼저 Repository 전체를 바로 대량 구현하지 말고, 기존 파일이 있다면 먼저 분석하라.

빈 Repository라면 **Architecture → Tree → Core Contract → Skeleton → Minimal Vertical Slice → Test → Documentation 순서로 작업하라.**

최종 응답에는 다음을 요약하라.

1. 생성한 Architecture
2. 생성/수정한 주요 파일
3. 각 팀이 개발을 시작해야 할 위치
4. 현재 Stub/Fake로 남겨둔 영역
5. PM이 다음으로 결정해야 하는 Architecture Issue
