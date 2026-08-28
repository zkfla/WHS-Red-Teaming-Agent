# WHS Web Red-Teaming Agent

승인된 로컬 또는 스테이징 웹 애플리케이션을 대상으로 안전한 보안 테스트를 수행하기 위한 경량 프로토타입입니다.

현재 구현 범위는 안전한 정찰·영속 오케스트레이션·actor 세션 격리·값 없는 API
resource 모델링까지 연결된 프로토타입입니다.

- YAML 기반 engagement 설정 및 검증
- 허용 대상, 포트, 경로, HTTP method 검사
- 리다이렉트 대상 재검사 인터페이스
- 요청 수, 속도, 실행 시간 예산
- JSONL 감사 로그
- Typer CLI
- 동일 origin Playwright 크롤러
- 링크·폼·XHR·fetch 인벤토리
- SQLite 저장 및 페이지 스크린샷
- context 전체 네트워크 라우팅과 DNS pinning
- Service Worker·WebSocket·popup 차단
- 비동기 요청 throttling과 fatal budget 종료
- 민감한 URL query 마스킹
- 위험 GET 링크 자동 보류
- 결정론적 헤더·쿠키·CORS·정보 노출·HTTPS 검사
- 구조화된 PASS·FAIL·INFO·INCONCLUSIVE Observation

> 본 프로젝트는 사용자 본인 소유 또는 명시적으로 테스트 승인을 받은 환경에서만 사용해야 합니다. 운영 데이터 변조, 서비스 거부, 자격증명 탈취, 지속성 확보를 위한 도구가 아닙니다.

## 설치

Python 3.12 이상이 필요합니다.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
playwright install chromium
```

## 설정 및 정책 확인

```bash
whs-redteam validate-config configs/engagement.example.yaml
whs-redteam check-request configs/engagement.example.yaml \
  http://localhost:3000/api/products --method GET
```

`check-request`는 실제 네트워크 요청을 보내지 않습니다.

## 승인된 대상 탐색

먼저 `configs/engagement.example.yaml`의 `targets`를 본인이 소유하거나 테스트 승인을 받은 주소로 설정합니다.

```bash
whs-redteam explore configs/engagement.example.yaml
```

결과는 기본적으로 다음 위치에 저장됩니다.

- `outputs/inventory.db`: 페이지, 폼, XHR·fetch endpoint
- `outputs/screenshots/`: 페이지 스크린샷
- `outputs/events.jsonl`: 정책 및 실행 감사 로그
- `deferred_links` 테이블: 상태 변경 가능성이 있어 방문하지 않은 링크

크롤러는 폼을 제출하거나 버튼을 클릭하지 않습니다. 발견된 링크를 동일 scope 안에서 방문하고 네트워크 요청을 관찰합니다.

`follow_discovered_links`의 기본값은 `false`입니다. 승인된 synthetic 환경에서만 명시적으로 활성화하세요. 활성화해도 위험 키워드가 포함된 GET 링크는 방문하지 않고 `deferred_links`에 기록합니다.

스크린샷은 기본적으로 비활성화되어 있습니다. 테스트 데이터만 사용하는 환경에서 설정의 `capture_screenshots`를 명시적으로 활성화할 수 있습니다.

## 격리 실행

실제 승인 대상을 탐색할 때는 로컬 호스트에서 브라우저를 직접 실행하기보다 Docker 구성을 우선 사용합니다.

```bash
docker compose build
docker compose run --rm explorer explore configs/engagement.example.yaml
```

Docker 구성은 non-root 사용자, read-only filesystem, capability drop, PID·CPU·메모리 제한을 적용합니다. 더 강한 egress 통제가 필요하면 전용 프록시나 방화벽 allowlist를 추가해야 합니다.

## Day 3 기본 보안 평가

> `assess`는 개별 분석기 개발·진단을 위한 호환 명령입니다. 전체 AgentRun의 영속 Queue,
> checkpoint 및 공통 예산이 필요한 정식 실행에서는 `whs-redteam run`을 사용하세요.

승인된 seed target에 안전한 GET 요청과 CORS Origin probe를 실행합니다.

```bash
whs-redteam assess configs/engagement.example.yaml
```

최신 탐색 run에서 성공한 페이지도 검사하려면 명시적으로 활성화합니다.

```bash
whs-redteam assess configs/engagement.example.yaml --include-inventory
```

민감 query가 마스킹된 URL, 실패한 페이지, scope 밖 URL은 자동 평가 대상에서 제외됩니다.

검사 항목은 보안 헤더, 쿠키 속성, CORS Origin 반사, 오류·기술정보 노출 및 HTTPS 사용 여부입니다. 결과는 SQLite `observations` 테이블과 `observations-{run_id}.json` 파일에 저장됩니다. Day 3 결과는 관찰이며 확정 취약점이 아닙니다. 확정 판정은 Day 4의 반복 재현과 negative control을 거쳐야 합니다.

각 Observation에는 다음 재현 메타데이터가 포함됩니다.

- `experiment_id`
- 테스트 카탈로그 버전
- engagement 설정 hash
- 요청 fingerprint
- 응답 fingerprint
- evidence hash

응답 본문 원문이나 인증정보는 fingerprint 메타데이터에 저장하지 않습니다. 개별 분석기가 실패하면 해당 검사만 `INCONCLUSIVE`가 되고 나머지 검사는 계속됩니다. 잘못된 charset은 UTF-8 replacement decoding으로 처리합니다. CORS는 안전한 GET Origin probe와 OPTIONS preflight를 사용하며, 브라우저가 거부하는 wildcard·credentials 조합은 취약점이 아닌 정보성 설정 오류로 분류합니다.

선택적으로 테스트 actor를 설정하면 익명 응답과 인증된 테스트 계정 응답을 비교합니다. 자격증명 값은 YAML에 작성하지 않고 환경변수 이름만 참조합니다.

```yaml
actors:
  - actor_id: user_a
    header_env:
      Authorization: WHS_USER_A_AUTHORIZATION
```

ACTOR_SESSION_MODELING에서 기록하는 값은 인증 성공이 아니라
credentials_available입니다. 동일 환경변수 참조는 설정 단계에서 거부하고, 서로 다른
환경변수의 실제 값이 같으면 실행 시 credential collision으로 차단합니다.

## Day 4 Finding 검증

> `validate-findings`는 검증기 단독 진단을 위한 호환 명령입니다. 정식 오케스트레이터
> 경로에서는 validation이 `finding_validation` capability와 Queue·Dispatcher를 통해
> 자동 실행됩니다.

rules-only Planner가 구조화된 TestPlan을 만들고 정책 gate가 scope와 허용 카테고리를 검사합니다. 같은 계획을 기본 2회 실행하고 카탈로그별 synthetic negative control이 통과한 결과만 `CONFIRMED` Finding으로 승격합니다.

```bash
whs-redteam validate-findings configs/engagement.example.yaml
```

최신 탐색 인벤토리를 포함할 수 있습니다.

```bash
whs-redteam validate-findings \
  configs/engagement.example.yaml \
  --include-inventory
```

결과는 SQLite `findings` 테이블과 `findings-{validation_id}.json` 파일에 저장됩니다. 같은 대상·테스트·카테고리는 SHA-256 dedup key로 병합됩니다. Planner는 브라우저, 네트워크 또는 셸 도구에 직접 접근하지 않으며 현재는 `rules` provider만 허용됩니다.

## 테스트

```bash
pytest
```

## 안전 설계

- 모든 향후 HTTP 및 브라우저 도구는 `ScopeGuard.authorize()`를 호출해야 합니다.
- 대상 호스트와 포트는 설정 allowlist와 정확히 일치해야 합니다.
- 제외 경로와 허용되지 않은 method는 실행 전에 거부됩니다.
- 리다이렉트는 새 URL에 대해 별도로 다시 승인받아야 합니다.
- 정책 판정은 JSONL 감사 로그로 남길 수 있습니다.

자세한 보안 정책은 [SECURITY.md](SECURITY.md)를 참고하세요.

## 다음 확장 단계

인증·역할 비교, API schema, IDOR/BOLA, 제한된 입력 변형, LLM Planner를 확장하는
구현 순서와 각 기능의 완료 기준은
[`docs/EXPANSION_ROADMAP.md`](docs/EXPANSION_ROADMAP.md)에 정리되어 있습니다.
확장 기능은 `ActionProposal`을 만들 뿐 직접 실행할 수 없으며, `CapabilityPolicy`,
`ScopeGuard`, `NetworkGate`를 순서대로 통과해야 합니다. Phase 5의 비파괴 설정 예시는
[`configs/phase5-safe.example.yaml`](configs/phase5-safe.example.yaml)입니다.

## 오케스트레이터 실행

정식 통합 경로는 정찰, 공격표면 모델링, 계획, 제한 실행, 검증 가능성 판정,
정리, 보고를 checkpoint 기반으로 실행합니다.

```text
CREATED -> DISCOVERING -> INVENTORY_READY -> API_SCHEMA_MODELING
        -> ACTOR_SESSION_MODELING -> RESOURCE_MODELING
        -> ATTACK_SURFACE_MODELING
        -> PLANNING -> EXECUTING -> EXPLOITABILITY_DECISION
        -> CLEANING_UP -> REPORTING -> COMPLETED
```

```bash
whs-redteam run configs/engagement.example.yaml
whs-redteam status configs/engagement.example.yaml RUN_ID
whs-redteam resume configs/engagement.example.yaml RUN_ID
```

MCP SDK를 거치는 개발·회귀시험 모드는 다음처럼 실행합니다. 이 모드는 실제 MCP
Client/Server 호출을 사용하지만 server는 같은 프로세스 안에 있으므로 운영 배포 구성이
아닙니다.

```bash
pip install -e '.[mcp]'
whs-redteam run configs/mcp-in-memory.example.yaml
```

실패한 run은 SQLite `agent_runs` 테이블에 실패 단계와 오류 유형을 저장합니다. 재개 시
engagement 설정 hash가 달라졌으면 안전을 위해 실행을 거부합니다. 현재 오케스트레이터는
결정론적 rules-only 파이프라인입니다. 공격표면 요약과 보수적인 exploitability 판정을
run checkpoint와 최종 JSON 보고서에 함께 저장합니다. 일반 보안 설정 문제는
HARDENING_GAPS, 명시 fixture에서 negative control과 반복 재현을 통과한 비인가 객체
읽기는 CONFIRMED로 구분합니다. 발견이 없다는 사실은 안전함의 증명으로 해석하지 않습니다.

현재 실제 취약점 탐지 수직 기능으로 승인된 GET query fixture에 대한 SQL injection과
반사형 HTML/XSS 후보 검증도 포함합니다. SQLi는 정상값, 무해 대조값, 따옴표 변이,
참/거짓 쌍을 2~3회 비교하며 데이터 추출이나 시간 지연 payload를 사용하지 않습니다.
XSS 검증은 JavaScript 대신 무해한 custom element가 HTML로 파싱되는지를 확인합니다.
따라서 SQL response oracle은 `CONFIRMED`, 반사형 HTML 주입만 확인된 경우는
JavaScript 실행이 증명되지 않은 `POTENTIAL`로 보고합니다.

`auto_injection_discovery: true`를 명시하면 정찰에서 관찰한 GET query 이름과 자료형만으로
후보 fixture를 결정론적으로 생성합니다. 원래 query 값은 저장하거나 재사용하지 않으며,
비밀번호·토큰·URL·파일·redirect 계열 파라미터와 위험 경로는 제외합니다. 자동 후보 역시
서명 승인 전에는 실행되지 않고 `max_auto_injection_fixtures` 상한을 적용받습니다.

`xss_browser_confirmation: true`를 추가하면 반사형 HTML 주입이 먼저 확인된 익명 세션
fixture에 한해 무해한 `data:` 이미지 오류 카나리를 Chromium에서 실행합니다. 카나리는
쿠키나 페이지 데이터를 읽지 않고 전용 DOM attribute만 변경합니다. 두 번 모두 실행되면
별도의 `xss` finding과 `CONFIRMED` 판정을 만들고, CSP 차단·브라우저 부재·실행 실패 시에는
기존 `xss_reflection`과 `POTENTIAL` 판정만 유지합니다. actor 인증이 필요한 브라우저
컨텍스트는 아직 지원하지 않습니다.

Chromium을 실행할 수 없는 CI나 경량 데모 환경에서는 `discovery_mode: http`를 사용할 수
있습니다. HTTP 정찰도 GET 전용이며 같은 scope, DNS 재확인, 요청 예산, 감사 로그를
통과합니다. SPA의 실행 후 XHR/fetch 수집이 필요하면 기본값인 `browser` 모드를 사용해야
합니다.

OpenAPI 3 명세가 있으면 `openapi_spec_path`를 추가할 수 있습니다. 명세의 GET/POST
엔드포인트와 query 자료형을 값 없이 inventory에 병합하며, 명세에서 발견된 GET query도
자동 주입 후보화 규칙을 그대로 통과합니다. 경로 파라미터가 포함된 명세 경로는 실제
리소스 값이 없으므로 자동 요청 대상에서 제외합니다.

```yaml
openapi_spec_path: specs/openapi.yaml
```

## 실제 취약점 탐지 데모

아래 데모 서버는 의도적으로 취약하므로 `127.0.0.1`에만 바인딩됩니다. 외부에 노출하면
안 됩니다.

첫 번째 터미널에서 로컬 데모를 실행합니다.

```bash
.venv/bin/python examples/vulnerable_demo.py
```

수동 승인 과정을 포함한 전체 흐름을 한 번에 검증하려면 다음 스크립트를 사용할 수도
있습니다. 스크립트는 로컬 서버를 종료할 때까지 관리하고, 마지막에 `sqli`와
`xss_reflection` finding이 보고서에 있는지 검사합니다.

```bash
.venv/bin/python scripts/run_vulnerability_demo.py
```

두 번째 터미널에서 에이전트를 실행하면 정찰 과정에서 `id`와 `q` 파라미터를 자동
후보화하고, 모델링·기본 검증 후 SQLi 작업 두 개와 XSS 작업 한 개가 서명 승인을
기다립니다. 실제로 취약하지 않은 `q`의 SQLi 후보가 결과에서 제외되는 것도 확인할 수
있습니다.

```bash
export WHS_DEMO_APPROVAL_SECRET='0123456789abcdef0123456789abcdef'
.venv/bin/whs-redteam run configs/vulnerable-demo.yaml
.venv/bin/whs-redteam pending-approvals configs/vulnerable-demo.yaml RUN_ID
```

로컬 프로토타입 승인 토큰을 권한 `0600` 파일로 발급합니다. 토큰은 세 작업에만 사용할
수 있도록 `max_uses=3`으로 제한합니다.

```bash
.venv/bin/whs-redteam issue-approval configs/vulnerable-demo.yaml \
  --output /tmp/whs-demo-approval.token \
  --capability sqli_safe \
  --capability xss_safe \
  --max-uses 3
```

`pending-approvals`에 표시된 세 `QUEUE_ID`를 각각 승인한 뒤 재개합니다.

```bash
.venv/bin/whs-redteam approve configs/vulnerable-demo.yaml RUN_ID SQLI_QUEUE_ID_1 \
  --token-file /tmp/whs-demo-approval.token
.venv/bin/whs-redteam approve configs/vulnerable-demo.yaml RUN_ID SQLI_QUEUE_ID_2 \
  --token-file /tmp/whs-demo-approval.token
.venv/bin/whs-redteam approve configs/vulnerable-demo.yaml RUN_ID XSS_QUEUE_ID \
  --token-file /tmp/whs-demo-approval.token
.venv/bin/whs-redteam resume configs/vulnerable-demo.yaml RUN_ID
```

최종 JSON의 `injection_tests`에는 payload 원문이나 응답 본문 대신 반복별 상태와 판정만
남습니다. `findings`에는 `sqli`와 `xss_reflection`, `exploitability_decision`에는 SQLi
response oracle에 근거한 `CONFIRMED`가 기록됩니다.

탐색과 검증 단계는 중앙 `ActionDispatcher`를 통과합니다. dispatcher는 먼저
`CapabilityPolicy`로 capability, actor, method, scope와 승인 요구를 검사하고 등록된
도구만 호출합니다. `LocalProbeAdapter`는 기존 Probe를 실행하고, `MCPProbeAdapter`는
capability에 코드로 고정된 server/tool binding만 호출합니다. Planner가 MCP tool 이름을
직접 제출할 수 없습니다. 실행 결과는 마스킹 후 SQLite `agent_actions`에 저장됩니다.
민감 capability의 최소 승인 요구 목록은 설정에서 제거할 수 없습니다.

현재 MCP Python SDK 2.x 기반 `OfficialMCPTransport`와 정책을 재검증하는
Browser/API Schema/Actor Session/Resource Model/Safe HTTP server factory까지 구현되어
인메모리 end-to-end 테스트를 통과합니다. 이 handler들은 `mcp_in_memory` 실행 경로에
연결되어 있지만 운영용 별도 server 프로세스 격리는 아직 없습니다. 민감 MCP capability는
중앙 승인 boolean만으로 실행하지 않으며, 별도 서버가
검증할 서명 receipt 생성기가 없으면 adapter 단계에서 fail-closed 됩니다. MCP 기능을
사용하려면 `pip install -e '.[mcp]'`로 선택 의존성을 설치합니다.

Capability의 최소 위험도와 허용 method는 코드 고정 catalog가 결정하므로 Planner가
`state_changing` 작업을 `passive`로 낮춰 제출할 수 없습니다. `agent_actions`에는 성공뿐
아니라 정책 거부와 probe 실패도 오류 타입 및 한 줄 메시지로 저장됩니다.

오케스트레이터의 discovery 작업은 실행 전에 SQLite `action_queue`에 저장됩니다. 같은
run·capability·target·method·actor 조합은 idempotency key로 중복 등록되지 않으며,
worker는 transaction 기반 claim과 제한 시간 lease를 받은 뒤 dispatcher를 호출합니다.
일시적 실행 실패는 설정된 최대 횟수까지만 `RETRY_PENDING`으로 돌아가고 정책 거부는
재시도하지 않습니다. 승인이 필요한 작업은 `WAITING_APPROVAL`에 남지만 실제 서명 승인
검증과 승인 후 `PENDING` 전환도 지원합니다.

승인 비밀키와 승인 토큰은 YAML이나 CLI 인자에 작성하지 않습니다. 설정에는 32바이트
이상 HMAC 비밀키가 들어 있는 환경변수 이름만 지정합니다.

```yaml
approval_hmac_secret_env: WHS_APPROVAL_HMAC_SECRET
```

외부 승인자가 발급한 토큰도 별도 환경변수에 넣고 대기 중인 action 하나를 승인합니다.

```bash
export WHS_ACTION_APPROVAL_TOKEN='SIGNED_TOKEN'
whs-redteam approve configs/phase5-safe.example.yaml RUN_ID QUEUE_ID \
  --token-env WHS_ACTION_APPROVAL_TOKEN
```

토큰은 HMAC-SHA256 서명, issuer, engagement, capability, target path prefix, method,
발급·만료 시각과 최대 사용 횟수를 검증합니다. 원문 토큰과 HMAC 비밀키는 SQLite와
감사 로그에 저장하지 않고 토큰의 SHA-256 digest와 사용 횟수만 보존합니다. 현재
HMAC 발급자는 별도 승인 서비스로 분리되지 않았으므로 운영 환경보다는 프로토타입용
신뢰 모델입니다.

`approval_id`는 Planner가 제출할 수 있는 설명용 값일 뿐 승인 근거로 신뢰하지 않습니다.
민감 action은 Queue 등록 시 기존 `approval_id`를 제거하고 무조건 `WAITING_APPROVAL`로
보냅니다. 서명 검증이 성공한 경우에만 Queue 내부 `approval_verified`와 토큰 digest가
설정되며, Dispatcher는 이 검증 상태가 전달되지 않으면 직접 호출에서도 실행을 거부합니다.
AgentRun은 승인 대기 작업이 남아 있으면 `WAITING_APPROVAL` 상태로 반환되고, 승인 후
`resume`하면 저장된 `EXECUTING` checkpoint에서 계속합니다.

정식 `run` 경로의 Discovery와 Finding Validation은 각각 Queue action으로 저장되고
등록된 Probe를 통해서만 실행됩니다. 두 단계는 SQLite `run_budgets`의 동일한 요청 수,
초당 요청 수와 실행 시간 예산을 공유하므로 단계마다 `max_requests`가 초기화되지
않습니다. 승인 대기 중에는 실행 시간 예산을 일시정지하고 resume 시 대기 시간만큼
deadline을 연장합니다.

Queue 작업은 `action_dependencies`로 선행 작업을 참조할 수 있습니다. 후속 작업은 모든
선행 작업이 `SUCCEEDED`가 되기 전에는 priority가 높아도 claim되지 않습니다. 선행
작업이 `FAILED`, `REJECTED`, `BLOCKED`, `CANCELLED`가 되면 후속 작업도 `BLOCKED`로
전환됩니다. 정식 실행의 Finding Validation은 Discovery Queue action을 선행 조건으로
가집니다.

승인 토큰은 payload와 signature 모두 canonical Base64URL 표현인지 확인합니다. 같은
서명 바이트를 다른 문자열로 표현해 새로운 digest와 사용 횟수를 얻는 token malleability를
차단합니다.

QueueExecutor는 중앙 `RetryPolicy`로 오류를 분류합니다. timeout, connection reset,
HTTP transport 오류와 동기 rate-limit만 재시도하며 2초, 4초, 8초 방식의 지수 backoff를
적용하고 기본 최대 지연을 30초로 제한합니다. 다음 실행 시각은 Queue `available_at`에
저장되므로 프로세스가 재시작되어도 즉시 재시도하지 않습니다. Scope·capability·DNS·
probe 등록 정책 오류는 `REJECTED`, 요청·runtime 예산 소진과 알 수 없는 내부 오류는
최종 `FAILED`로 처리합니다. `Retry-After` 숫자 hint를 받을 수 있는 정책 인터페이스는
있지만 HTTP 응답 header 자동 연결은 아직 구현되지 않았습니다.

상태 변경 capability는 실행 전에 cleanup handler 또는 MCP cleanup tool binding이
등록돼 있어야 합니다. Dispatcher는 상태 변경 실행 전에 `RESERVED` cleanup task부터
영속화하고, 성공한 도구가 `_cleanup_arguments`를 반환하면 이를 `PENDING`으로 전환합니다.
따라서 도구가 부분 실패해도 정리 필요 기록은 남습니다. 인자는 민감 필드를 마스킹해
SQLite `cleanup_tasks`에 저장합니다. Cleanup Queue는 LIFO claim,
최대 3회 제한 재시도, 성공·실패 상태를 지원합니다. `CLEANING_UP` 단계는 미완료 task나
cleanup receipt가 없는 상태 변경 성공 action이 있으면 보고서 완료를 거부합니다.
현재 실제 상태 변경 Probe와 재시작 가능한 자동 cleanup worker 조립은 아직 구현 전입니다.

정찰 중 관찰한 XHR/fetch 요청은 endpoint URL에서 query 값을 제거하고, query 이름·추론
타입과 JSON/form body의 필드명·중첩 타입만 `api_request_schemas`에 저장합니다. 원본 query와
body 값은 저장하지 않으며 감사 로그의 일반 query 값도 전부 마스킹합니다. 같은 endpoint를
여러 번 관찰하면 값이 아니라 schema shape만 병합합니다. `API_SCHEMA_MODELING`은 별도
ActionQueue/ToolAdapter action이며 `mcp_in_memory`에서는 API Schema MCP tool을 통과합니다.

`ACTOR_SESSION_MODELING`은 YAML에 적힌 환경변수 이름의 존재 상태만 저장합니다. 실제
Authorization/API key/Cookie 값은 실행 시점 메모리에서만 읽으며 action 결과·SQLite·보고서에
기록하지 않습니다. actor마다 별도 `SafeHttpClient`와 cookie jar를 사용하므로 한 계정이
받은 세션 쿠키가 다른 계정 요청으로 넘어가지 않습니다.

`RESOURCE_MODELING`은 관측 endpoint와 request schema에서 integer/UUID/ULID/object-id
형태의 path segment와 `id`, `*_id`, `uuid` 계열 query/body 필드를 후보로 만듭니다.
path 값은 `{resource_id}` template으로 일반화하여 `resources` 테이블과 보고서에 저장합니다.
휴리스틱 ResourceRecord 자체는 취약점 판정이 아닙니다.

명시적 owner-only fixture를 설정하면 GET 전용 IDOR actor matrix를 사용할 수 있습니다.
에이전트는 ID를 자동 변조하지 않으며 target_url과 negative_control_url만 호출합니다.
두 URL은 모두 approval_scope_url 아래여야 하고, idor_safe 서명 승인을 받기 전에는
action이 WAITING_APPROVAL에서 멈춥니다.

~~~yaml
authorization_fixtures:
  - fixture_id: order-idor
    approval_scope_url: https://staging.example.test/api
    target_url: https://staging.example.test/api/orders/123
    negative_control_url: https://staging.example.test/api/controls/denied
    owner_actor: user_a
    non_owner_actors: [user_b]
enabled_capabilities:
  - discovery
  - api_schema
  - auth_session
  - resource_model
  - finding_validation
  - idor_safe
approval_hmac_secret_env: WHS_APPROVAL_HMAC_SECRET
tool_backend: local
~~~

owner 요청이 성공하고 non-owner negative control은 거부되지만 동일 owner-only URL에서는
owner와 동일한 응답을 2회 이상 받은 경우에만 IDOR Finding을 CONFIRMED로 저장합니다.
현재 이 fixture probe는 local backend에서만 지원합니다.
