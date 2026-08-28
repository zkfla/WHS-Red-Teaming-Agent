# Web Red-Team Agent 확장 로드맵

## 목표와 경계

현재 코어를 허가된 로컬·스테이징 환경에서 동작하는 상태 기반 웹 보안 검증
에이전트로 확장한다. 운영 데이터 파괴, 지속성 확보, 자격증명 탈취, 서비스 거부,
범용 exploit 생성은 범위에서 제외한다. PoC는 재현에 필요한 최소 HTTP 요청 또는
브라우저 절차와 마스킹된 증거만 생성한다.

모든 실행은 다음 경로를 강제한다.

```text
Inventory -> Planner -> ActionProposal -> CapabilityPolicy
          -> ScopeGuard -> ActionDispatcher -> ToolAdapter
          -> Local Probe | allowlisted MCP Tool -> Oracle -> Validator -> Finding
```

Planner와 LLM은 브라우저·HTTP·셸을 직접 호출할 수 없다. 미등록 capability, scope 밖
대상, 승인 없는 민감 검사, 파괴적 행동은 fail-closed로 거부한다.

## 단계별 구현 순서

### Phase O — 오케스트레이터 전환 (현재 진행)

- `AgentOrchestrator`가 단계 전환의 유일한 소유자가 된다.
- SQLite에 run 상태, plan, validation ID, 실패 지점을 checkpoint한다.
- CLI의 `run`, `resume`, `status`가 전체 수명주기를 제어한다.
- 중앙 `ActionDispatcher`, 승인 토큰 검증기, ToolAdapter와 cleanup queue를
  오케스트레이터 아래에 연결한다.
- 개별 `explore`, `assess`, `validate-findings` 명령은 진단·개발 호환용으로 유지하되
  정식 실행 경로는 `run`으로 통일한다.

현재 구현 상태:

```text
CREATED -> DISCOVERING -> INVENTORY_READY -> API_SCHEMA_MODELING
        -> ACTOR_SESSION_MODELING -> RESOURCE_MODELING
        -> ATTACK_SURFACE_MODELING
        -> PLANNING -> EXECUTING -> EXPLOITABILITY_DECISION
        -> CLEANING_UP -> REPORTING -> COMPLETED
```

`DISCOVERING`과 Finding Validation은 `ActionProposal -> CapabilityPolicy ->
ActionDispatcher -> ToolAdapter` 경로로 실행된다. Local adapter는 ProbeRegistry를,
MCP adapter는 코드 고정 capability/tool binding을 사용한다. 결과는 `agent_actions`
테이블에 상위 agent run ID로 저장된다.

Capability의 최소 위험도, 허용 method, 승인 필요 여부, cleanup 필요 여부는
`policy/capability_catalog.py`의 코드 고정 catalog가 결정한다. Planner가 더 낮은
위험도를 제출하거나 engagement가 더 넓은 method를 허용해도 catalog 제한을 낮출 수
없다. 성공·거부·실패 action은 모두 `agent_actions`에 저장된다.

오케스트레이터 후속 순서는 다음으로 고정한다.

1. 영속 ActionQueue와 idempotency key — 기본 구현 완료
2. 서명·만료·engagement·target·capability binding을 검증하는 ApprovalVerifier
   — HMAC 기반 프로토타입 완료
3. `WAITING_APPROVAL`과 승인 후 큐 해제 — 기본 구현 완료, 승인 후 `resume` 필요
4. Validation worker의 probe 전환 — 정식 `run` 경로 완료
5. run 전체에서 공유되는 요청·시간 예산 — SQLite 영속 예산 완료
6. cleanup task와 `CLEANING_UP` — 영속 Queue와 완료 차단 규칙 구현,
   재시작 가능한 자동 worker 조립은 남음
7. pause/cancel 및 안전 종료
8. 이후 Phase 5 API·인증 기능

현재 ActionQueue는 SQLite에 proposal을 실행 전에 저장하고 다음을 지원한다.

- run 내부 idempotency key 기반 중복 방지
- priority 기반 claim
- `BEGIN IMMEDIATE`를 이용한 단일 worker claim
- worker ID와 lease 만료 시각
- 최대 1~5회 bounded retry
- lease 만료 복구 및 최종 실패 처리
- `PENDING`, `WAITING_APPROVAL`, `RUNNING`, `RETRY_PENDING`, `SUCCEEDED`,
  `REJECTED`, `FAILED`, `CANCELLED` 상태
- Queue 항목과 실제 `agent_actions` 결과의 queue ID 연결

Action dependency와 retry backoff 기본 구현은 완료됐다. Cleanup Queue는 영속 LIFO claim,
제한 재시도 및 receipt 검사를 지원하지만 실제 상태 변경 Probe의 자동 정리 worker는
아직 연결되지 않았다. 외부 승인 서비스와 cancel API도 구현 전이다.

HMAC ApprovalVerifier는 승인 원문을 저장하지 않고 digest별 사용 횟수와 취소 상태를
SQLite에 기록한다. 승인 소비와 큐 전환은 하나의 transaction으로 처리한다. 향후에는
HMAC 공유 비밀 방식에서 외부 승인 서비스의 비대칭 서명 방식으로 교체해야 한다.

보안 보강으로 Planner가 임의 `approval_id`를 제출해도 Queue가 이를 제거하고 민감
작업을 무조건 대기시킨다. 검증 여부와 token digest는 Queue 내부 컬럼으로 분리되며,
Dispatcher의 승인 인자는 QueueExecutor만 전달한다. AgentRun은 대기 항목이 있으면
네트워크 작업 없이 반환하고, 승인 후 `resume` 시 `EXECUTING` checkpoint로 복귀한다.

Finding Validation은 `finding_validation` capability로 등록되어 TestPlan을 checkpoint한
뒤 Queue·Dispatcher·FindingValidationProbe를 거친다. 구버전 `VALIDATING` checkpoint도
직접 실행하지 않고 Queue action으로 변환한다. Discovery와 Validation은 동일한
`run_budgets` 행을 사용해 요청 수·rate·runtime을 공유하며 프로세스가 재시작되어도
사용량이 초기화되지 않는다. 요청 한도와 실행 시간 소진은 재시도하지 않는 최종 실패로
분류한다. 사람의 승인 대기 시간은 runtime deadline에서 제외한다.

`action_dependencies`는 같은 AgentRun의 이미 존재하는 action만 선행 작업으로 허용해
순환 관계 생성을 막는다. claim 쿼리는 모든 선행 작업이 성공한 경우만 후속 작업을
선택한다. 선행 작업의 terminal 실패는 일반 대기 작업과 승인 대기 작업 모두에
`BLOCKED`로 전파된다. Finding Validation은 Discovery 성공에 의존한다.

중앙 `RetryPolicy`는 오류를 `RETRY`, `REJECT`, `TERMINAL`로 분류한다. 재시도 가능한
오류에는 제한된 지수 backoff를 적용하고 `available_at`을 영속화한다. Queue scheduler는
해당 시각까지 claim하지 않으며 오케스트레이터는 남은 지연을 기다린다. 알 수 없는
오류를 자동 재시도하지 않고 fail-closed로 종료한다. 다음 cleanup 보강은 원본 작업이
부분 실패하거나 프로세스가 재시작돼도 정리 tool binding을 복원하고 `CLEANING_UP`
worker가 자동으로 drain하도록 만드는 것이다.

승인 대기와 종료 상태:

```text
WAITING_APPROVAL -> EXECUTING -> CLEANING_UP
PAUSED | BLOCKED | CANCELLED | FAILED
```

### MCP 도구 계층 진행 상태

현재 구현:

- `ToolCall`/`ToolExecutionResult` 고정 계약
- 기존 Probe를 감싸는 `LocalProbeAdapter`
- capability에서 server/tool로만 매핑하는 `MCPProbeAdapter`
- 공식 MCP SDK 2.x Streamable HTTP/in-memory `OfficialMCPTransport`
- 정책을 독립 재검증하는 Browser/Safe HTTP MCP server factory
- 값 없는 request schema를 요약하는 API Schema MCP server factory
- credential 값을 받지 않는 Actor Session readiness MCP server factory
- endpoint template과 identifier 후보를 만드는 Resource Model MCP server factory
- 미등록·중복 binding, transport 미설정, run 불일치, 과대 응답 fail-closed
- 민감 capability는 서버 검증용 서명 receipt 생성기가 없으면 호출 거부
- 상태 변경 capability는 cleanup tool binding과 cleanup arguments 강제

아직 구현하지 않은 것:

- 별도 Browser MCP server와 Safe HTTP MCP server 프로세스 실행 구성
- 기존 WebExplorer/FindingValidator handler와 정식 `run` 경로 연결
- MCP server 내부의 DNS, 공유 budget, 서명 receipt 실제 구현
- canary와 report MCP server

LLM은 MCP client를 직접 소유하지 않는다. LLM/Rules Planner는 `ActionProposal`만 만들고,
Queue와 Policy를 통과한 뒤 Dispatcher가 ToolAdapter를 호출한다. 범용 shell, 임의 URL HTTP,
임의 JavaScript 실행 도구는 MCP allowlist에 노출하지 않는다.

완료 기준:

- 어느 단계에서 실패해도 실패 단계와 오류 유형이 영속화된다.
- 같은 설정으로 `resume`할 수 있고 설정이 바뀌면 재개가 거부된다.
- 모든 신규 probe는 dispatcher와 capability policy를 우회할 수 없다.
- 상태 변경 작업은 cleanup 완료 전 run을 완료 처리하지 않는다.

### Phase 5 — API 및 인증 기반 (3~5일)

- Playwright의 request/response 이벤트에서 XHR·fetch query/body 스키마를 수집한다.
  — query/body 값 비저장, 타입 shape 병합과 MCP action까지 기본 구현 완료
- JSON 키와 타입만 저장하고 토큰·쿠키·비밀번호·본문 값은 저장하지 않는다.
- OpenAPI 3.x 문서를 로컬 파일로만 가져와 endpoint·parameter inventory로 정규화한다.
  — 아직 구현 전
- actor별 HTTP cookie jar 분리는 완료했다.
- 환경변수 존재는 인증 성공과 분리해 credentials_available로 기록한다.
- 동일 credential 참조와 런타임 credential collision을 차단한다.
- actor별 browser context/storage state를 분리한다.
- 환경변수로 참조한 인증 헤더는 로그와 DB에 절대 기록하지 않는다.

완료 기준:

- SPA의 XHR endpoint와 method, parameter 이름·타입이 inventory에 저장된다.
- anonymous/user_a/user_b 요청이 서로 다른 context에서 실행된다.
- 로그·DB·JSON에서 secret scanner가 인증 값을 찾지 못한다.
- 로그아웃·결제·삭제 경로는 계속 보류된다.

### Phase 6 — 권한 및 상태 비교 (4~6일)

- ResourceRecord의 resource type과 identifier 위치 저장은 완료했다.
- 명시 fixture의 owner/non-owner 소유권 relation 저장은 완료했다.
- 승인 범위 안에서 owner/non-owner와 denied negative control을 비교하는 GET matrix는
  local backend 기준 완료했다.
- 자동으로 관측 객체의 owner를 학습하는 기능은 아직 구현 전이다.
- IDOR/BOLA는 non-owner가 owner 전용 객체를 읽은 경우에만 후보로 만든다.
- 인증 우회는 로그인 전후의 상태코드뿐 아니라 의미 있는 응답 구조 차이로 판정한다.
- CSRF는 토큰 존재·세션 결합·Origin/SameSite 방어를 우선 수동 분석한다.

완료 기준:

- 의도적으로 취약한 로컬 테스트 앱에서 IDOR fixture를 탐지한다.
- 401/403/404 차이만으로 취약점을 확정하지 않는다.
- 각 finding에 actor matrix와 두 번의 재현 증거가 포함된다.

### Phase 7 — 제한된 입력 변형 (5~7일)

- 원본 요청에서 타입, 경계값, 누락 필드, 추가 필드만 구조적으로 변형한다.
- endpoint당 변형 횟수를 `max_mutations_per_endpoint`로 제한한다.
- SQLi는 오류·boolean differential·timing을 분리하되 sleep/고부하 probe는 제외한다.
- XSS는 무해한 고유 canary의 반사 여부를 먼저 검사하고 격리 브라우저에서만 실행성을
  검증한다. 저장형 검사는 synthetic 전용 계정과 정리 절차가 있을 때만 허용한다.
- 경로 순회는 프로젝트가 제공한 canary 파일만 대상으로 한다.

완료 기준:

- 각 변형은 원본 대비 변경 필드 하나와 기대 oracle을 가진다.
- WAF 차단·네트워크 지연·일반 500 오류를 취약점으로 확정하지 않는다.
- negative/positive fixture에 대한 precision·recall을 자동 측정한다.

### Phase 8 — 제한된 서버 상호작용 (5~7일)

- SSRF는 소유한 canary callback domain만 허용하고 private/metadata 주소 payload는 금지한다.
- 파일 업로드는 비실행 텍스트 marker, 작은 크기, 고유 이름, 자동 정리만 허용한다.
- CSRF·회원가입·장바구니는 synthetic 데이터와 명시적 cleanup handler가 있을 때만 실행한다.
- 모든 POST/PUT/PATCH/DELETE는 engagement opt-in과 `approval_id`를 모두 요구한다.

완료 기준:

- 생성된 모든 테스트 객체를 run 종료 시 정리하고 정리 실패를 보고한다.
- callback correlation ID 외의 외부 데이터는 수집하지 않는다.
- 실제 결제·메일 발송·초대·삭제 endpoint는 실행되지 않는다.

### Phase 9 — 업무 흐름과 LLM Planner (5~8일)

- 페이지·endpoint·actor·resource·transition을 상태 그래프로 만든다.
- LLM 입력에는 마스킹된 schema, 상태 그래프, 이전 observation만 제공한다.
- LLM 출력은 고정 JSON Schema의 `ActionProposal[]`만 허용한다.
- 정책 거부 결과를 planner에 observation으로 돌려주되 정책 변경 권한은 주지 않는다.
- 실패 시 재시도는 최대 2회이며 capability와 요청 예산을 확장할 수 없다.

완료 기준:

- 잘못된 URL, 미등록 capability, shell 명령, 임의 payload를 포함한 출력이 모두 거부된다.
- 동일 fixture에서 rules planner 대비 탐지율과 요청 비용을 비교한다.
- 모델/API 장애 시 rules-only 모드로 안전하게 종료 또는 축소된다.

### Phase 10 — 보고서와 품질 게이트 (3~5일)

- 최소 재현 절차, actor, 전제조건, evidence hash, remediation을 보고한다.
- PoC는 프로젝트 내부 재현용 요청 템플릿이며 secret과 파괴 동작을 포함하지 않는다.
- OWASP Benchmark, Juice Shop 같은 격리된 fixture와 자체 negative fixture를 사용한다.
- precision, recall, false-positive rate, requests/finding, 재현 성공률을 릴리스 지표로 둔다.

## 기능별 구현 모듈

| 기능 | 선행 조건 | 신규 모듈 | 확정 조건 |
|---|---|---|---|
| SQLi | request schema, mutation engine | `probes/sqli.py` | 두 종류 oracle + 반복 재현 |
| XSS | browser isolation, canary | `probes/xss.py` | 고유 canary의 격리 실행 |
| IDOR/BOLA | 2개 이상 actor, resource owner | `probes/authorization.py` | non-owner의 의미 있는 데이터 접근 |
| 인증 우회 | auth state model | `probes/auth.py` | 보호 동작/데이터 접근 재현 |
| CSRF | state-changing opt-in | `probes/csrf.py` | 방어 제거 조건에서만 상태 변화 |
| SSRF | 소유 callback service | `probes/ssrf.py` | correlation ID callback |
| 파일 업로드 | synthetic 계정, cleanup | `probes/upload.py` | 무해 marker 접근 및 정리 |
| 경로 순회 | canary fixture | `probes/traversal.py` | 지정 canary 내용 일치 |
| 업무 로직 | state graph, invariants | `reasoning/workflows.py` | 명시된 invariant 위반 |
| LLM 계획 | JSON schema, eval set | `planning/llm.py` | policy 승인된 proposal만 실행 |

## 출시 판정

실제 승인된 스테이징에 사용하려면 전체 테스트 통과 외에 다음 조건이 필요하다.

- capability별 positive 3개와 negative 5개 이상의 fixture
- critical/high 오탐 확정률 0%
- 상태 변경 테스트의 cleanup 성공률 100%
- secret leakage 테스트 0건
- scope escape, DNS rebinding, redirect escape 회귀 테스트 통과
- 요청 예산 초과와 승인 누락 시 fail-closed 확인
