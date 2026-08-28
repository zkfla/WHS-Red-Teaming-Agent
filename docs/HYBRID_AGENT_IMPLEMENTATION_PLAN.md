# 하이브리드 웹 레드티밍 에이전트 구현 작업계획서

## 1. 문서 목적

현재 프로젝트를 다음 수준으로 발전시키는 것이 목표다.

- 사용자가 허가받은 대상 URL을 웹 화면 또는 CLI에 입력한다.
- 에이전트가 같은 출처의 공격표면을 자동 정찰한다.
- 규칙 엔진이 안전한 SQLi 후보를 만들고 AI Planner가 후보의 우선순위를 정한다.
- 모든 행동은 scope, 요청 예산, capability, 승인 정책을 통과해야 한다.
- SQLi 여부는 AI의 추측이 아니라 반복 가능한 결정론적 Probe가 판정한다.
- AI는 실패 원인을 요약하고 제한된 범위에서 다음 행동을 선택한다.
- 검증된 기계 판정과 AI 설명이 함께 들어간 보고서를 생성한다.
- AI API가 없거나 실패해도 rules-only 모드로 끝까지 실행된다.

이 계획은 새로운 취약점 종류를 늘리는 것이 아니라 현재 SQLi 수직 기능의 자동화,
AI 계획, 재계획, 판정, 보고서, 웹 시연을 완성하는 데 집중한다.

## 2. 현재 상태 진단

### 2.1 이미 구현된 부분

- `orchestration` 패키지의 lazy coordinator import와 독립 import 테스트
- CLI와 향후 웹 UI가 공유하는 `RunService`
- YAML 없이 URL에서 시작하는 `sqli-scan` 명령과 값 없는 재개용 profile
- scope, DNS, method, 요청 예산 정책
- HTTP 또는 브라우저 기반 정찰
- GET query 이름과 자료형 기반 SQLi 후보 생성
- 영속 Action Queue와 승인 대기/재개
- SQL 오류 신호 및 true/false 응답 oracle 반복 검증
- negative control과 최소 재현 횟수
- `CONFIRMED`, `POTENTIAL`, `NOT_DEMONSTRATED` 등의 기계 판정
- SQLite inventory, 감사 로그, JSON 보고서
- 로컬 취약 서버를 이용한 end-to-end 데모

### 2.2 아직 구현되지 않은 부분

- 실제 AI 모델 호출과 provider 설정
- AI가 사용할 정제된 공격표면 Context
- 구조화된 AI 계획 출력과 정책 검증
- 관찰 결과를 보고 제한적으로 재계획하는 루프
- AI가 작성한 보고서 설명과 기계 판정의 분리
- 실행 상태, 승인, 발견 결과를 확인할 웹 UI
- 웹 서버 실행 명령과 Docker 서비스
- AI 장애/잘못된 JSON/프롬프트 주입에 대한 테스트

### 2.3 웹 미리보기 상태

현재 저장소에는 FastAPI, Flask, Streamlit, React, Vite 등의 웹 애플리케이션이 없다.
Docker Compose도 CLI `explore` 프로세스만 실행한다. 따라서 현재 웹 미리보기가 표시되지
않는 것은 포트나 브라우저 문제라기보다 제공할 웹 서버 자체가 없기 때문이다.

로컬 모델의 OpenAI-compatible `/v1` 주소는 모델 API base URL이며 프로젝트 UI 주소가
아니다. 웹 미리보기는 별도의 `whs-redteam serve` 서버로 제공해야 한다.

### 2.4 선행 품질 결함

`tests/test_injection.py`를 독립 실행할 때 `injection -> orchestration.adapters ->
orchestration.__init__ -> orchestrator -> injection` 순환 import가 발생한다. 전체 테스트
순서에서는 가려질 수 있으므로 AI 기능을 추가하기 전에 수정해야 한다.

## 3. 목표 아키텍처

```text
Web UI / CLI
    |
    v
Run Service ---------------------------------------------+
    |                                                     |
    v                                                     |
Deterministic Discovery                                   |
    |                                                     |
    v                                                     |
Sanitized Candidate Registry                              |
    |                                                     |
    +--> Rules Planner --------------------------------+  |
    |                                                 |  |
    +--> AI Planner --> Structured Plan --------------+  |
                                                      |  |
                                                      v  |
                                              Plan Policy Gate
                                                      |
                                                      v
                                              Persistent Action Queue
                                                      |
                                           Approval / Budget / Scope
                                                      |
                                                      v
                                              Deterministic SQLi Probe
                                                      |
                                                      v
                                              Evidence + Machine Verdict
                                                      |
                                  Observation Summary-+
                                      |               |
                                      v               v
                               Limited Re-planning  Report Writer
                                                      |
                                                      v
                                              JSON + Markdown Report
```

핵심 원칙은 AI가 네트워크 도구를 직접 호출하지 않는 것이다. AI는 저장소가 발급한
`candidate_id`와 허용된 action만 선택한다. 실제 URL, 변형값, HTTP 요청은 기존 정책
계층과 SQLi Probe가 생성하고 실행한다.

## 4. 모델 사용 방식

### 4.1 Provider 추상화

상용 API와 로컬 모델을 동일 인터페이스로 지원한다.

```python
class PlanningProvider(Protocol):
    async def create_plan(
        self,
        context: PlanningContext,
    ) -> PlannerDecision: ...


class ReportProvider(Protocol):
    async def write_report(
        self,
        context: ReportContext,
    ) -> AIReportSection: ...
```

구현체는 다음 세 개로 구성한다.

- `RulesPlanningProvider`: 모델 없이 현재 규칙을 사용한다.
- `OpenAICompatibleProvider`: 상용 API 또는 OpenAI-compatible 로컬 서버를 호출한다.
- `HybridPlanningProvider`: AI 우선, 실패 시 rules fallback을 수행한다.

특정 벤더 SDK를 핵심 코드에 직접 결합하지 않는다. 초기 버전은 이미 의존 중인 `httpx`로
OpenAI-compatible HTTP 인터페이스를 호출한다.

### 4.2 설정 모델

```yaml
engagement:
  planner_mode: hybrid
  ai:
    provider: openai_compatible
    base_url: http://127.0.0.1:11434/v1
    model: local-model-name
    api_key_env: WHS_AI_API_KEY
    timeout_seconds: 30
    max_output_tokens: 1200
    max_planning_rounds: 3
    fallback_to_rules: true
    report_writer_enabled: true
```

API 키 값은 설정, SQLite, 감사 로그, 보고서에 저장하지 않는다. 설정에는 환경변수 이름만
저장한다. 로컬 서버가 키를 요구하지 않더라도 provider 호환성을 위해 빈 값이 아닌 별도
더미 환경변수를 선택적으로 사용할 수 있게 한다.

### 4.3 AI 입력 제한

AI에는 다음 정보만 전달한다.

- endpoint path template
- HTTP method
- 파라미터 이름, 위치, 추론된 자료형
- 상태 코드, content type, 길이 구간, body hash 같은 요약
- 인증 여부를 나타내는 actor label
- 후보별 `candidate_id`
- 남은 요청/시간/계획 라운드 예산
- 허용 action 목록

다음 정보는 전달하지 않는다.

- 쿠키, Authorization header, API 키
- 원본 응답 본문 전체
- 사용자가 입력한 비밀번호나 토큰
- SQLi 변형값 원문
- scope 밖 URL

웹 페이지의 텍스트는 명령으로 취급하지 않고 untrusted data로 표시한다. 첫 MVP에서는
본문 전체를 AI에 보내지 않고 정규화된 schema와 metadata만 사용한다.

### 4.4 구조화 출력

AI는 자유 형식 명령이 아니라 아래 스키마만 반환한다.

```json
{
  "schema_version": "planner-decision-v1",
  "selected_actions": [
    {
      "candidate_id": "candidate-17",
      "action": "run_sqli_safe",
      "reason": "Integer query parameter may reach a data lookup.",
      "confidence": 0.82
    }
  ],
  "finish": false,
  "finish_reason": null
}
```

모델이 URL, payload, method, capability를 임의로 추가하지 못하게 한다. Pydantic 검증에
실패하거나 존재하지 않는 후보를 선택하면 해당 계획을 폐기하고 rules fallback을 사용한다.

## 5. 세부 구현 작업

### 작업 A. 기반 안정화

대상 파일:

- `src/whs_redteam/orchestration/__init__.py`
- `src/whs_redteam/injection.py`
- `tests/test_imports.py`

세부 작업:

1. `orchestration.__init__`의 eager import를 제거하거나 lazy export로 변경한다.
2. `ProbeContext`와 protocol을 순환 의존성이 없는 별도 모듈로 이동한다.
3. 각 핵심 모듈을 깨끗한 Python subprocess에서 import하는 테스트를 추가한다.
4. `test_injection.py`와 `test_orchestrator.py`를 각각 단독 실행해도 통과시킨다.

완료 조건:

- 테스트 실행 순서에 관계없이 import 오류가 없다.
- 전체 테스트와 개별 테스트 결과가 동일하다.

### 작업 B. URL 직접 실행 경로

현재 브랜치에서 기본 구현이 완료되었다. 다음 작업은 이 경로를 AI Planner와 웹 API에서
재사용하고, 필요할 때 옵션을 확장하는 것이다.

대상 파일:

- `src/whs_redteam/cli.py`
- `src/whs_redteam/config.py`
- 신규 `src/whs_redteam/run_service.py`

명령 형태:

```bash
whs-redteam sqli-scan https://authorized.example \
  --planner hybrid \
  --approval-token-file /tmp/approval.token
```

완료된 세부 작업:

1. URL을 정규화하고 scheme, hostname, scope를 검증한다.
2. SQLi 전용 engagement와 값 없는 재개용 YAML profile을 생성한다.
3. 기본 capability를 discovery, schema, planning, `sqli_safe`, validation, report로 제한한다.
4. 입력 URL에 query가 있으면 자료형만 추론해 fixture를 만든다.
5. query가 없으면 설정에 따라 같은 출처의 GET link를 제한적으로 탐색한다.
6. 승인이 없으면 `WAITING_APPROVAL`과 다음 명령을 출력한다.
7. 선택적 0600 token으로 모든 대기 SQLi action을 승인하고 재개한다.
8. 완료되면 machine-readable JSON과 보고서 경로를 출력한다.

완료 조건:

- 사용자가 YAML을 직접 작성하지 않아도 실행할 수 있다.
- POST, 파일 업로드, 상태 변경 요청은 생성하지 않는다.

### 작업 C. AI Client와 Provider

신규 파일:

```text
src/whs_redteam/ai/__init__.py
src/whs_redteam/ai/models.py
src/whs_redteam/ai/provider.py
src/whs_redteam/ai/openai_compatible.py
src/whs_redteam/ai/fallback.py
src/whs_redteam/ai/sanitization.py
```

세부 작업:

1. timeout과 취소를 지원하는 비동기 HTTP client를 구현한다.
2. 모델 목록 확인과 간단한 structured response health check를 구현한다.
3. 응답에서 JSON 객체만 추출해 Pydantic으로 검증한다.
4. 잘못된 JSON은 1회 형식 수정 요청 후 rules fallback한다.
5. API 오류, 429, 5xx, timeout은 민감정보 없는 오류 유형으로 기록한다.
6. API 키와 prompt 원문은 기본 감사 로그에서 제외한다.
7. provider, model, prompt version, fallback 여부만 provenance로 남긴다.

완료 조건:

- 로컬/상용 OpenAI-compatible endpoint를 설정만 바꿔 교체할 수 있다.
- 모델 서버가 꺼져 있어도 rules-only 결과와 보고서가 생성된다.

### 작업 D. 후보 Registry와 Hybrid Planner

신규 또는 변경 파일:

```text
src/whs_redteam/planning/context.py
src/whs_redteam/planning/hybrid.py
src/whs_redteam/planning/policy.py
src/whs_redteam/models.py
src/whs_redteam/orchestration/orchestrator.py
```

세부 작업:

1. 발견된 SQLi fixture마다 안정적인 `candidate_id`를 만든다.
2. URL과 파라미터의 실제 값은 registry 내부에만 보관한다.
3. AI에는 값이 제거된 candidate summary만 전달한다.
4. AI가 선택한 ID를 registry의 실제 fixture로 다시 해석한다.
5. PlanPolicy가 candidate 존재, scope, capability, method, actor, 예산을 재검증한다.
6. 통과한 결과만 `ActionProposal`로 변환해 Queue에 등록한다.
7. AI가 모든 후보를 누락했더라도 high-confidence 규칙 후보는 설정에 따라 보완한다.

완료 조건:

- AI가 새 URL이나 변형값을 발명해 실행할 수 없다.
- 동일 inventory에서는 동일 candidate ID가 생성된다.
- AI 유무와 관계없이 정책 계층이 동일하게 적용된다.

### 작업 E. 제한된 재계획 루프

오케스트레이터 상태를 다음처럼 확장한다.

```text
PLANNING
  -> EXECUTING
  -> OBSERVING
  -> REPLANNING
       -> EXECUTING
       -> EXPLOITABILITY_DECISION
```

세부 작업:

1. Probe 결과를 `ObservationSummary`로 축약한다.
2. AI에는 confirmed 여부, signal 종류, control 통과 여부, 오류 유형만 전달한다.
3. AI가 `CONTINUE`, `SKIP`, `REQUEST_APPROVAL`, `FINISH` 중 하나를 선택하게 한다.
4. SQLi의 변형값 조합 자체는 Probe 코드에 고정한다.
5. 최대 라운드, 라운드별 action 수, 총 요청 수를 동시에 제한한다.
6. 동일 candidate/action 조합의 반복 enqueue를 dedup key로 차단한다.
7. AI 장애 시 rules planner가 남은 후보를 처리하고 종료한다.

완료 조건:

- 최대 3라운드 안에 반드시 종료한다.
- 승인되지 않은 action은 어떤 라운드에서도 실행되지 않는다.
- 재시작 후에도 현재 라운드와 남은 예산이 복원된다.

### 작업 F. 판정과 보고서 분리

보고서는 다음 구조로 분리한다.

```json
{
  "machine_verdict": {
    "category": "sqli",
    "status": "CONFIRMED",
    "reproductions": 2,
    "negative_control_passed": true,
    "confirmed_modes": ["error_signal", "boolean_signal"]
  },
  "planning_provenance": {
    "mode": "hybrid",
    "provider": "openai_compatible",
    "model": "local-model-name",
    "fallback_used": false,
    "planning_rounds": 2,
    "prompt_version": "sqli-planner-v1"
  },
  "ai_analysis": {
    "executive_summary": "...",
    "technical_explanation": "...",
    "business_impact": "...",
    "remediation_guidance": "...",
    "limitations": "No data extraction or exploit chain was attempted."
  }
}
```

세부 작업:

1. `machine_verdict`는 기존 결정론적 로직만 수정할 수 있게 한다.
2. AI Report Writer는 검증된 finding과 evidence summary만 입력받는다.
3. AI가 기계 판정과 충돌하는 문장을 생성하면 기본 템플릿으로 대체한다.
4. AI 호출 실패 시 deterministic Markdown 보고서를 생성한다.
5. JSON과 Markdown 두 형식을 출력한다.

완료 조건:

- AI가 `NOT_DEMONSTRATED`를 `CONFIRMED`로 바꿀 수 없다.
- 모든 문장형 결론에 연결된 finding ID가 존재한다.
- 모델 장애 중에도 보고서 파일이 생성된다.

### 작업 G. 웹 미리보기와 운영 화면

경량 프로토타입은 FastAPI + Jinja2 + 소량의 vanilla JavaScript를 권장한다. React 빌드
체인은 현재 시점에 불필요한 복잡도를 추가한다.

신규 파일:

```text
src/whs_redteam/web/__init__.py
src/whs_redteam/web/app.py
src/whs_redteam/web/schemas.py
src/whs_redteam/web/templates/index.html
src/whs_redteam/web/templates/run.html
src/whs_redteam/web/static/app.js
src/whs_redteam/web/static/style.css
```

필수 API:

```text
GET  /health
GET  /
POST /api/runs
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/actions
POST /api/runs/{run_id}/actions/{queue_id}/approve
POST /api/runs/{run_id}/resume
GET  /api/runs/{run_id}/report
GET  /api/ai/health
```

필수 UI 요소:

- 대상 URL 입력
- 허가받은 대상임을 확인하는 체크박스
- planner mode 선택: rules/hybrid
- AI provider 연결 상태
- 현재 오케스트레이터 단계
- 남은 요청 예산
- 발견된 SQLi 후보 목록
- 승인 대기 action과 승인 버튼
- SQLi 검증 행렬 요약
- 최종 verdict와 보고서 링크
- AI fallback 발생 여부

서버 명령:

```bash
whs-redteam serve --host 127.0.0.1 --port 8080
```

기본 bind는 `127.0.0.1`로 제한한다. 외부 bind는 명시 옵션과 경고 없이는 허용하지 않는다.
웹 UI는 요청을 직접 실행하지 않고 `RunService`를 호출한다. CLI와 웹이 동일한
오케스트레이션 코드를 공유해야 한다.

완료 조건:

- 브라우저에서 URL 입력부터 보고서 열기까지 수행할 수 있다.
- 새로고침 후에도 SQLite의 run 상태가 복원된다.
- API 키, 승인 token, 쿠키가 HTML이나 API 응답에 노출되지 않는다.

### 작업 H. Docker와 실행 환경

Docker Compose를 다음 서비스로 분리한다.

```text
web         FastAPI UI/API
worker      오케스트레이터 실행기
model       선택 사항; 외부 또는 로컬 모델
demo-target 의도적으로 취약한 로컬 시연 대상
```

프로토타입에서는 web과 worker를 한 프로세스로 시작할 수 있지만 코드 계층은 분리한다.
취약 데모 대상은 loopback 또는 별도 internal network에만 노출한다.

## 6. 테스트 계획

### 6.1 단위 테스트

- AI config와 환경변수 검증
- Context sanitizer에서 token, cookie, body 제거
- 구조화 응답 parsing
- 존재하지 않는 candidate ID 거부
- scope 밖 candidate 거부
- malformed JSON fallback
- timeout/429/5xx fallback
- 중복 계획 방지
- 최대 라운드 종료
- 기계 판정 불변성

### 6.2 통합 테스트

- Mock AI가 SQLi 후보를 선택하고 Queue에 등록
- Mock AI가 악성 URL을 반환하면 Policy가 거부
- AI 서버가 없을 때 rules fallback으로 완료
- 승인 전에는 SQLi 요청이 전송되지 않음
- 승인 후 `WAITING_APPROVAL -> EXECUTING -> COMPLETED` 전환
- 웹 API로 생성한 run을 CLI에서 조회할 수 있음

### 6.3 End-to-end 테스트

취약 데모와 안전한 대조 서버를 함께 사용한다.

| 시나리오 | 기대 결과 |
|---|---|
| 취약 `/sql?id=1` | SQLi `CONFIRMED`, 2회 재현 |
| 비취약 query | SQLi finding 없음 |
| AI 정상 | hybrid provenance, fallback false |
| AI 중단 | rules provenance, fallback true |
| 승인 없음 | `WAITING_APPROVAL`, SQLi 요청 0회 |
| 잘못된 AI JSON | 정책 오류 없이 rules fallback |
| 페이지 내 prompt injection 문구 | candidate metadata 외에는 AI 입력에 미포함 |

## 7. 구현 일정

### 시연 가능한 1일 MVP

#### 오전: 안정화와 AI 연결

1. 순환 import 수정
2. `AIConfig`, `PlanningContext`, `PlannerDecision` 모델 추가
3. OpenAI-compatible provider 및 health check 구현
4. Mock/local provider 테스트
5. rules fallback 구현

#### 오후: Planner와 웹 미리보기

1. SQLi candidate registry 연결
2. Hybrid Planner가 candidate ID를 선택하도록 구현
3. FastAPI `RunService`와 최소 URL 입력 화면 구현
4. 승인·resume·report API 구현
5. 로컬 취약 서버 end-to-end 시연

1일 MVP에서는 재계획을 최대 1회로 제한하고 UI는 단일 페이지로 만든다. AI 보고서는
executive summary와 remediation만 생성한다.

### 안정화 포함 5일 계획

#### Day 1: 기반 안정화 및 URL 실행

- 순환 import 수정
- 독립 import 테스트
- `sqli-scan URL` CLI
- RunService 분리
- 취약/안전 대조 E2E fixture

#### Day 2: AI Provider와 구조화 계획

- provider 추상화
- OpenAI-compatible client
- sanitized PlanningContext
- Pydantic structured output
- timeout과 rules fallback

#### Day 3: Hybrid Planner와 재계획

- candidate registry
- PlanPolicy 재검증
- observe/replan 상태
- 최대 라운드와 dedup
- 영속 checkpoint 복원

#### Day 4: 웹 미리보기

- FastAPI/Jinja UI
- run 생성/조회/승인/resume API
- 진행 상태 polling 또는 SSE
- 결과 행렬 및 보고서 화면
- 로컬 bind와 비밀정보 마스킹

#### Day 5: 보고서와 최종 검증

- AI report writer
- deterministic fallback 보고서
- provenance와 limitation 기록
- AI 정상/장애/악성 출력 E2E
- Docker Compose와 실행 문서
- 시연 스크립트 및 최종 회귀 테스트

## 8. 완료 기준

다음 데모가 처음부터 끝까지 성공해야 완료로 판단한다.

1. `whs-redteam serve` 실행 후 웹 화면이 열린다.
2. 사용자가 허가받은 URL을 입력한다.
3. AI 연결 상태와 planner mode가 표시된다.
4. 에이전트가 GET query SQLi 후보를 자동 발견한다.
5. AI가 후보를 선택하고 선택 이유를 남긴다.
6. 정책 gate가 scope, capability, method, 예산을 검증한다.
7. 승인이 없으면 실행하지 않고 UI에 승인 대기로 표시한다.
8. 승인 후 SQLi Probe가 정상/대조/따옴표/참/거짓 요청을 2회 수행한다.
9. negative control을 통과한 경우에만 `CONFIRMED`로 판정한다.
10. AI가 결과를 요약하되 기계 판정을 변경하지 못한다.
11. JSON과 Markdown 보고서를 웹에서 열 수 있다.
12. 모델 서버를 중단하고 다시 실행해도 rules fallback으로 완료된다.
13. 안전한 대조 URL에서는 `NOT_DEMONSTRATED`가 출력된다.
14. API 키, token, 쿠키, 원본 payload와 응답 본문이 보고서에 남지 않는다.

## 9. 구현 우선순위와 제외 범위

최우선 순위는 다음과 같다.

1. 순환 import와 독립 테스트 안정화
2. URL 직접 입력과 RunService
3. Hybrid Planner와 안전한 provider 연결
4. SQLi 수직 기능 end-to-end
5. 웹 미리보기
6. AI 보고서와 fallback

이번 단계에서 제외한다.

- 데이터베이스 내용 추출
- time-based blind SQLi
- SQL shell 또는 후속 침투
- POST/JSON/GraphQL 자동 변형
- XSS, IDOR 등 신규 취약점 확대
- AI fine-tuning
- 다중 worker 운영 배포

Fine-tuning은 현재 필요하지 않다. 먼저 structured prompt, provider fallback, candidate 선택
평가셋으로 정확도를 측정하고, 반복적으로 실패하는 사례가 충분히 모인 뒤 검토한다.

## 10. 최종 방향

이 프로젝트의 AI는 취약점 판정기를 대체하지 않는다. AI는 정찰 결과를 해석하고 허용된
후보 중 다음 행동을 선택하며 보고서를 설명한다. SQLi 검증과 최종 취약점 판정은 반복
가능한 코드가 담당한다. 이 하이브리드 구조를 유지해야 모델 교체, 로컬 실행, API 장애,
오탐 통제, 승인 정책을 동시에 만족할 수 있다.
