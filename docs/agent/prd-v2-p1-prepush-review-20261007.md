# PRD v2 P1 — 푸시 전 검수

검수일: 2026-10-07. 대상: `dev-jm`, 기준 커밋 `ec1b8d9` 이후 로컬 변경.
루트 및 App/Orchestration/Core/Capability의 `AGENTS.md`를 기준으로 실제 구현,
설정, 테스트와 저장된 Run 보고서를 대조했다.

## 1. 결정

**현재 P1 변경의 푸시는 보류한다.** 자동 검사 통과와 실제 조사 완료를 구분한다.
개발 중 코드를 공유하는 것 자체를 금지한다는 뜻은 아니지만,
현재 상태를 안정적인 P1 완료본으로 공유하기에는 아래 실행 안전성·연결 문제가 남아 있다.
이번 검수에서 실행 코드를 수정하거나 commit/push/PR/fetch/pull하지 않았다.
검수 문서만 추가했으며 개인 설정·정책·승인 상태도 변경하지 않았다.

후속(2026-10-07): 위 문장은 수정 전 검수 당시의 기록이다. 이후 로컬 수정과
검증 범위·남은 한계는 [R1~R4 수정 보고서](prd-v2-p1-review-fixes-20261007.md)에
별도로 기록했다. 과거 검수 결과를 최종 코드의 결과로 해석하지 않는다.

## 2. 우선 해결해야 할 사항

### R1. 같은 origin의 기본 포트 표기가 서로 다른 실행 자원으로 계산됨

- 위치: [resource_conflicts.py](../../src/redteam/orchestration/resource_conflicts.py),
  `execution_conflicts`, 15~20행.
- 원인: 범위 비교용 `network_identity(...).authority`는 포트가 명시됐는지를
  보존한다. 이를 병렬 실행 자원 키에 그대로 사용한다.
- 직접 재현: 서로 다른 두 backend에 대해 아래 키가 생성됐다.

```text
http://synthetic.invalid/          → target:synthetic.invalid
http://synthetic.invalid:80/other  → target:synthetic.invalid:80
공통 충돌 키: 없음
```

서로 다른 server ID·자원 참조로 같은 origin을 사용하는 경우,
이 충돌 검사로는 같은 서버의 요청을 직렬화하지 못한다.
같은 backend이거나 같은 opaque ref가 있으면 다른 키가 막을 수 있지만,
모든 배포에서 그 추가 키가 같다는 보장은 없다. 실제 대상 호출 없이 순수 함수로 재현했다.

수정: scope의 포트 명시 의미를 바꾸지 말고 실행 자원 계산에서 이미 확인된
실효 host/port를 사용한다. URL의 기본 포트는 scheme으로 결정할 수 있지만
포트가 없는 임의 HostTarget의 접속 포트는 추측하면 안 된다.
HTTP/HTTPS 기본 포트, 명시 포트, IPv6, 서로 다른 backend의 동등 origin에 대해
충돌 키와 실제 후보 선택/예약까지 회귀 검사한다.

### R2. 계획의 대상과 원본 증거의 대상 연결이 완료 검사까지 이어지지 않음

- 근거: `run_3ecd08acf183`의 저장 보고서. 작업 4건 성공,
  `work_status_counts.succeeded=4`이나 완료 검토는
  `completion_evidence_reference_targets_a_different_object_24`로 거절됐다.
- 관련 코드: [completion.py](../../src/redteam/orchestration/completion.py),
  1486~1517행. 대상이 다르면 연결된 admission receipt와 grounded claim이 필요하다.
- 실제 SDK 증거 대상은 endpoint이고 응답은 404,
  계좌 API 증거 대상도 endpoint이고 응답은 401이었다.
  계획 단계는 host에 연결돼 있었다. 도구 성공은 HTTP 요청 처리 성공이지
  취약점 검증 성공이 아니다.
- 이후 Planner/Atomizer 개정이 반복 거절되고 정상 판단 8회가 소진됐다.
  최종 `planning_error`는 이 흐름의 결과이며 MCP 통신 실패가 아니다.

수정: 계획→원자 작업→실제 action→원본 evidence의 대상·경로·Run·주체 연결을
일관되게 제공한다. 넓은 host 조사에서 구체 endpoint의 결과를 평가한다면 기존
grounded claim/receipt 계약으로 관계를 입증한다. 같은 host라는 이유만으로 다른
계정·객체·세션의 증거를 인정하지 않는다. 원본 대상 metadata를 바꾸거나
Control/완료 검사를 완화해서 해결하지 않는다.
목적에 맞는 관측 완료와 미검증/blocked 결과를 각각 검사한다.

기존 완료 계약과 P1 계획 절차의 통합 문제다. 모든 원인이 이번 P1 변경에서
새로 발생했다고 단정하지 않으며, 이 저장 기록만으로 개정 거절의 세부 규칙은 확정할 수 없다.

### R3. Planner 입력 schema와 검증 규칙 불일치, 개정 오류의 진단 정보 유실

- [specialist_planning.py](../../src/redteam/orchestration/specialist_planning.py),
  32/157행: `completion_criteria`는 임의 dict 배열이고
  Planner에는 `AttackPathProposal.model_json_schema()`를 그대로 전달한다.
- 직접 재현: `{"kind":"observation"}`은 이 JSON schema를 통과하지만
  실제 `validate_strategy`는 description 누락으로 `criterion_fields_required`를 발생시킨다.
  이는 기존 P0에도 있는 계약 불일치이며, 최근 실제 재실행에서도 한 번 발생한 뒤 수정됐다.
- 같은 파일 349~389행의 개정 규칙은 일반 `ValueError`를 발생시킨다.
  [diagnostics.py](../../src/redteam/orchestration/diagnostics.py), 666~674행에서
  일반 오류는 `invalid_decision`, 위치 없음, 상세 rule 없음으로 축약된다.
- 직접 재현: `ValueError("specialist_revision_changed_unaffected_work")`를
  변환해도 결과의 code는 `invalid_decision`이다. 실제 실패 Run에도 이 일반 진단이 남았다.
  어느 개정 규칙이 실제 실패했는지까지 확인한 것은 아니다.

수정: 기존 완료 조건 writing 계약에서 schema와 검증을 함께 제공하고,
개정 규칙은 코드 소유의 typed diagnostic으로 안전한 code/location/correction을 전달한다.
임의 예외 문자열이나 모델 원문을 그대로 저장하지 않는다.
무변경·영향 없는 단계 변경·주체 변경·전제조건 누락을 서로 구분하고,
거절된 단계의 유한한 repair가 불필요하게 전체 재분해를 반복하지 않는지 검사한다.
예산을 무조건 늘리거나 매 재계획마다 초기화하는 변경은 하지 않는다.

### R4. 자연어 대화 경로에 catalog 정보가 누락됨

- 위치: [input_router.py](../../src/redteam/app/cli/input_router.py), 70~79행.
  `ConversationAssistant.decide`에 tools를 전달하지 않는다.
- 결과: 대화 payload의 tools는 기본값 `[]`가 된다. 실제 등록/실행이 있어도
  대화 모델이 "도구 목록이 비어 있다"고 설명할 수 있다.
- 이것은 실제 gateway가 빈 상태라는 뜻은 아니다. 이번 P1 이전 CLI 경로의
  누락이지만 사용자에게 전체 연결 상태를 정확히 설명하려면 보완해야 한다.

수정: Session 조립 경계에서 같은 RuntimeDeps catalog의 안전한 조회 정보를
전달한다. bootstrap 전의 설정 목록과 실제 discovery snapshot을 구분하고,
닫힌 gateway의 과거 snapshot을 현재 live 상태로 표현하지 않는다.
LLM이 임의로 ToolId를 만들거나 조회를 위해 scope/승인을 우회하지 않게 한다.

## 3. 모듈화·하드코딩 평가

확인한 긍정적 사항:

- App에서 프로필을 검증·조립하고 fingerprint에 선택 정보를 남긴다.
- 역할 ID는 YAML 초기 구성에 있으며 중앙 루프에 특정 역할/도구 이름별 분기를
  P1용으로 추가하지 않았다. 단계 이름과 사건 version은 코드 소유 계약이다.
- Registry/Gateway, WorkStore/Scheduler, Coordinator/outbox, Control,
  graph/evidence 경로를 재사용한다. 별도 broker·작업 DB·중복 실행 큐를 만들지 않았다.
- 변경된 production 파일에서 개인 홈 절대 경로, 랩 계좌·경로·포트 정답,
  ZAP/Burp/Nmap 이름별 분기를 검색했으며 발견하지 못했다.
  이는 전체 프로젝트에 이런 값이 전혀 없다는 전수 보증은 아니다.
- `worker_batch_size <= 2`는 P1의 명시적 안전 범위다. 불필요한 대상 하드코딩과
  구분하며, worker batch 2가 모델 판단 병렬화나 단일 대상 병렬화를 뜻하지 않는다.

후속 모듈 경계 개선:

- `engine.py:1601`이 PlanningProvider Protocol에 없는 `enable_replanning` 속성을
  조회해 outbox 복구와 실행 충돌 검사까지 함께 활성화한다.
  현재 기본/P1 factory 조립은 동작하지만 planner 교체와 실행 안전 기능이 묶여 있다.
  기존 조립/설정 계약에서 명시적인 실행 기능 선택을 제공하고 planner의 개정 능력과
  분리하는 것이 바람직하다. 새 registry/queue를 만드는 방식은 피한다.
  이는 R1처럼 현재 입력으로 실행 안전성 실패가 재현된 사항과는 구분한다.
- 실제 stdio/Control/HTTP 병렬 통합 테스트는 작업을 분해·선택한 뒤
  `asyncio.gather(worker_node(...))`로 실행한다. 기존 LangGraph 회귀는 있지만,
  P1 프로필 + 실제 Send 분기 + 승인 재개 + 기존 evidence 재사용을 하나의
  종단 시험으로 묶는 검증을 추가하면 R2와 같은 연결 공백을 줄일 수 있다.

## 4. 이번에 다시 실행한 검사

- P1·결과 집계·기존 로컬 승인/CLI 변경 관련 검사: **70 passed**.
- fan-out 승인/쿼터·resume·coordination·P0 profile 검사: **105 passed**.
- 합계 **175 passed**, 서로 다른 검사 집합이다.
- CI 범위 Ruff 통과, Mypy `src/redteam tests` **592 source files** 통과.
  Mypy에는 기존 untyped-body 안내가 남으며 모든 함수 내부가 타입 검증됐다는 뜻은 아니다.
- `git diff --check` 통과.
- 기본 포트 충돌 키 실패, Planner schema/검증 불일치,
  typed rule 정보 유실은 추가 Python 진단으로 직접 재현했다.
  이 부정 사례들은 위 통과 테스트의 새 자동 회귀 항목이 아니다.
- 이번은 Python 3.11이다. CI의 Python 3.10/3.14 전체 검사와 Docker CI 작업은
  이번 검수에서 수행하지 않았다. 이전 기록의 전체 2,953 passed/51 skipped와
  이번 관련 회귀 175개 재검사를 구분한다.
- 이번 검수에서 실제 LLM·금융 대상에 새 요청을 보내지 않았다.
  직접 재현은 합성 참조를 사용했고 MCP 통합 검사는 임시 localhost 대상이었다.

저장된 최근 라이브 결과도 확인했다:

- `run_3ecd08acf183`: 작업 4건 성공 후 대상 연결/개정 과정에서 planning_error.
- `run_864531edda25`: Recon 2건 성공 후 Web 응답 분석 승인 대기.
  후속 Web 작업이나 전체 완료를 통과한 결과가 아니다.

## 5. 푸시 범위와 해제 조건

기존 로컬 승인 연결 변경 4개는 P1 본체와 구분한다:

- `src/redteam/app/cli/commands/run.py`
- `src/redteam/app/cli/session_controller.py`
- `tests/unit/test_cli_operator_console_hardening.py`
- `tests/unit/test_cli_session_controller.py`

이 변경은 보존했고 관련 테스트를 재검사했으며 별도 논리 커밋으로 분리하는 것이 적절하다.
`.env`, 개인 engagement/policy, `data/p0-live-20261006` 실행 설정은 현재 Git에서
ignore된다. 이 파일들을 강제 add하거나 개인 키·SQLite·실행 원본을 push하지 않는다.
이 목록은 P1 설정을 포함한 서버 연결이 다른 개발자에게 자동 복제된다는 뜻도 아니다.

우선 순서:

1. R1 실행 자원 키를 수정하고 동등 origin 직렬화 회귀를 추가한다.
2. R3 schema/typed 진단을 공통 소유 계약에 맞춰 수정한다.
3. R2 대상·증거 관계와 개정 후 재사용을 종단으로 검증한다.
4. R4 대화 도구 조회와 pre-bootstrap/closed snapshot 표시를 보완한다.
5. P0/승인 재개·독립 병렬 실행·중단/복구 회귀 및 CI 범위를 확인한다.
6. 공유 범위를 명확히 분리한 뒤 개발 브랜치 push 여부를 다시 결정한다.

P2의 모델 판단 병렬화·코드 artifact와 P3 Mobile/runner는 이번 푸시 판단에
P1 완료 조건처럼 요구하지 않는다. 기존 범위·정책·예산을 유지한 수정으로 검증한다.
