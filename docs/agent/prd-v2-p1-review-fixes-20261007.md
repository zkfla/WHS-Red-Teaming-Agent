# PRD v2 P1 — 검수 지적 수정과 로컬 검증

작성: 2026-10-07. 작업 대상은
`WHS-Red-Teaming-Agent-dev-orchestration-p0-20261006`의 `dev-jm` 로컬 checkout이다.
기준 HEAD는 `ec1b8d9`이며 기존 P1·승인 수정 위에서 작업했다.
[수정 전 검수](prd-v2-p1-prepush-review-20261007.md)의 R1~R4를 대상으로 한다.
fetch/pull/브랜치 전환/commit/push/PR은 하지 않았다.

## 1. 수정한 내용

### R1 — 같은 실행 대상의 기본 포트 표기 통일

`orchestration/resource_conflicts.py`에서 실행 자원 키를 실제로 확인 가능한
host/port로 계산한다. HTTP의 암묵적인 80과 명시적인 80, HTTPS의 암묵적인
443과 명시적인 443은 각각 같은 자원을 예약한다. IPv6 주소도 같은 방식이다.

- 기존 scope 비교의 포트 명시 의미는 변경하지 않았다.
- 접속 포트가 없는 임의 HostTarget에는 포트를 추측해 넣지 않는다.
- 서버 ID·불투명 자원 참조의 기존 직렬화도 유지한다.
- 순수 함수뿐 아니라 Memory/SQLite WorkStore의 실제 lease 예약을 검사했다.
  서로 다른 backend의 동등 origin은 한 작업만 예약하며, 해제 후 다음 예약이 가능하다.

이것은 DNS alias나 알려지지 않은 공유 브라우저/기기를 자동 판별하는 기능이 아니다.
동일 서버가 직렬화되는 기존 보수적 제한도 해제하지 않았다.

### R3 — Planner schema와 검증, 오류 수정 루프의 일치

`orchestration/specialist_planning.py`의 완료 조건을 기존 `CompletionCriterion`과
`paths.writing`의 schema/검증/정규화로 연결했다. description이 빠진 조건을
유효한 Planner 출력으로 통과시키지 않으며, 생략 가능한 kind는 기존 writing
계약의 semantic 기본값을 사용한다. 저장 모델의 unknown 기본값으로 바꾸지 않는다.

질문·대상·주체·의존 관계·원자화·개정 오류는 `SpecialistValidationError`의
코드 소유 diagnostic으로 전달한다. 예를 들어 다음 원인을 구분한다.

- `specialist_strategy_unknown_target`
- `specialist_atomization_origins_mismatch`
- `specialist_revision_changed_unaffected_work`
- `specialist_revision_question_not_reflected_in_tasks`

`planning.Inference`와 `cognition.StructuredDecisionClient`의 기존 추론 경로에
선택적 응답 검증 callback을 추가했다. Planner/Atomizer 단계의 계약 위반을
같은 단계의 기존 bounded repair로 전달한다. 잘못된 Atomizer 출력 때문에
이미 검증된 Planner 질문을 매번 새로 생성하지 않는다.

별도 재시도 루프나 새 예산은 만들지 않았다. 모든 시도는 기존 공유 판단 예산,
repair 한도, 요청별 사용량, 중단/시간 검사 아래에서 계산한다.
fixture 시험에서는 Planner 1회 + Atomizer 최초 1회 + repair 1회가
정상 2회/repair 1회/총 3회로 계산됐다. repair가 계속 실패하면 동일 한도에서
안전하게 중단하며 남은 정상 예산으로 repair 한도를 우회하지 않는다.

diagnostic에는 거절된 개인 대상 문자열, 모델 원문, 임의 예외 메시지를 넣지 않는다.
기존 location 마스킹에서 questions/origins의 알려진 필드만 추가했다.

### R2 — 다른 대상의 작업을 실행한 뒤에야 완료가 거절되는 경로 차단

P1의 원자 작업은 accepted step의 정확한 `target_node_ids`에 연결한다.
`engine.compile_actions`에서 새 action이 다른 대상을 선택하면 실행 전에
`atomic_execution_subject_mismatch`로 거절한다. 다른 known endpoint/object가
필요하면 Planner 질문과 Atomizer 작업을 먼저 개정하도록 문맥을 제공한다.

이는 **예방적 계약 정렬**이다. host와 endpoint를 같다고 간주하거나,
기존 401/404 증거를 취약점 성공으로 바꾸는 수정이 아니다.

- 원본 ToolResult·evidence metadata·과거 완료 상태·Graph·Control은 변경하지 않았다.
- 같은 대상의 정상 action은 계속 후보로 생성된다. 모든 작업을 hold하지 않는다.
- HTTP 401/404 응답의 원본 관측은 그 목적의 observation 완료에 사용할 수 있지만,
  PoC/취약점 성공의 근거로 자동 인정되지 않는다.
- 다른 대상의 증거를 사용하는 완료는 기존 source-backed measured graph 관계와
  source-linked final admission receipt가 모두 필요하다. 관계만 있거나
  엉뚱한 대상의 receipt만 있는 경우 거절된다. inferred 관계도 대체 근거가 아니다.
- 기존 공개 Graph 기록/receipt 소유 기능을 fixture에서 사용해 이 양쪽 조건과
  원본 불변·Finding 미생성을 검증했다. 자동 receipt 발급 기능을 만든 것은 아니다.

따라서 과거 실패 Run을 소급해 완료로 바꾸지 않으며, 실제 모델이 올바른 대상
개정을 제안한다거나 금융 랩의 취약점을 찾는다고 보장하지 않는다.
현재까지의 자동 검증은 compiler, 기존 완료 계약, 개정 보존, 실행 배치의 연결이다.
실제 모델의 넓은 조사 → 새 endpoint 개정 → 증거 검토 → 전체 목표 완료는 후속 검증이다.

### R4 — CLI 대화에 같은 런타임의 도구 조회 정보 전달

`OperatorConsole.catalog_labels()`라는 읽기 전용 접점에서 실제 RuntimeDeps의
ToolCatalog를 조회하고, SessionController → InputRouter → ConversationAssistant에
기존 bounded label 형식으로 전달한다. 조회를 위해 도구 호출이나 새 transport를
열지 않는다. 실행과 resume의 기존 OperatorConsole 연결을 사용한다.

설정 서버 목록은 discovery 결과와 분리하고 다음 lifecycle을 문맥에 표시한다.

| 상태 | 의미 |
| --- | --- |
| `not_discovered` | 아직 실행 조립/discovery 전 |
| `discovered` | 현재 붙은 OperatorConsole에서 얻은 조회 snapshot |
| `unavailable` | 해당 연결이 catalog 조회를 제공하지 않음 |
| `run_detached` | 종료된 실행의 과거 snapshot; 새 실행은 재조립/discovery 필요 |

도구 label은 실행 권한이나 현재 호출 성공 보장이 아니다. transport가 닫힌 뒤에도
과거 label은 설명에 사용할 수 있지만 live gateway로 표현하지 않는다.
기존 CLI 최소 fixture가 catalog를 갖지 않는 호환 경로도 검사하고 보존했다.
이 경로에 가짜 도구 목록을 만들어 넣지 않는다.

## 2. 모듈 경계와 보존 사항

루트와 App/Orchestration/Core `AGENTS.md`의 책임 경계를 적용했다.
schema/완료 조건은 기존 writing 소유자, 예산은 기존 cognition,
실행 후보와 상태는 engine/WorkStore, 대화 조회 조립은 App에서 처리한다.
새 DB·큐·broker·registry 또는 Finance Lab 계정/경로/포트별 예외는 추가하지 않았다.

Control의 승인·scope·위험도·쿼터, 원본 증거 기록과 Graph/완료 gate는 유지한다.
개인 `.env`, engagement/MCP policy, 시작 스크립트, 저장된 Run/승인은 변경하지 않았다.
기존 로컬 P1 및 승인 연결 수정도 보존했다.

## 3. 검증

최종 코드의 전체 회귀: **2,972 passed / 51 skipped**, 268.40초.
건너뛴 검사는 통과나 외부 환경 검증으로 계산하지 않는다.

- 실제 Control + stdio MCP + 임시 localhost HTTP 통합: **4 passed**.
  기존 isolated worker 시험과 실제 LangGraph Send/결과 reducer 경로를 각각 검사했다.
  허용된 독립 요청 2건은 handler barrier를 모두 통과해 동시 in-flight임을 확인했다.
  승인 필요 사례는 HTTP 요청 0건, 대기 승인 1건, 형제 작업 대기를 확인했다.
  검증 경계는 첫 실행 배치 집계까지이며 전체 목표 완료나 승인 후 resume의
  실제 MCP 종단 시험을 통과했다고 의미하지 않는다.
- CI 범위 Ruff 통과, Mypy `src/redteam tests` **593 source files** 통과,
  `git diff --check` 통과. Mypy의 기존 untyped-body 안내는 남아 있다.
- 새 회귀: 기본 포트/IPv6/예약, schema/typed 진단, 단계별 repair/예산,
  원자 대상 불일치와 정상 대상, 401/404 관측, 다른 대상 관계/receipt,
  CLI pre-bootstrap/attach/detach lifecycle을 포함한다.

첫 전체 검사에서 CLI 최소 fixture에 catalog가 없는 호환 경로가 실패했다.
이 경로는 조회 unavailable을 명시하도록 수정하고 PTY 회귀를 재검사했다.
위 전체 수치는 이 수정과 실제 Send 통합 검사를 모두 반영한 최종 재실행 결과다.
로컬 socket 접근이 제한된 첫 통합 시도도 있었으며, 임시 localhost 검증 권한을
받은 뒤 재실행했다. 환경 제한을 코드 성공/실패로 대체해서 기록하지 않았다.

실행 명령:

```bash
env -u REDTEAM_SPECIALIST_PROFILE REDTEAM_MCP_SERVERS='' REDTEAM_EXTERNAL_MCP_SERVERS='' \
  .venv/bin/pytest -q
```

Python 3.11의 로컬 테스트다. 실제 모델은 명시적인 scripted fixture로 대체했고,
임시 로컬 저장소와 HTTP 서버만 사용했다. 설치된 ZAP/Burp, 실제 LLM, 기존
Finance Lab 및 외부 금융 대상에는 새 요청을 보내지 않았다.
Python 3.10/3.14, Docker CI, 실제 Mobile 환경은 이번에 검증하지 않았다.

## 4. 푸시 판단과 다음 확인

이번에는 로컬 변경만 유지한다. 자동 테스트 통과는 실제 조사 성능 검증이 아니며,
PRD v2 전체나 Finance Lab 해결 완료로 표현하지 않는다.

후속으로 확인할 것은 다음과 같다.

1. 승인된 합성 환경에서 실제 모델의 대상 개정·증거 검토·종료를 새 Run으로 확인한다.
   키·정책·개인 경로/SQLite/원본 응답은 공유 대상에서 제외한다.
2. P1 + 실제 Send + 실제 MCP + 승인 후 resume + 개정 후 기존 증거 사용을
   하나의 전체 종단 시나리오로 추가 검증한다. 개별 기존 회귀의 통과와 구분한다.
3. planner의 `enable_replanning`에 실행 사건/충돌 기능 선택이 묶이는 기존
   조립 경계는 별도 개선 사항으로 남긴다. 이번 네 지적 수정 때문에 공통
   설정/프로필 fingerprint를 재설계하지 않았다.
4. 공유할 경우 P1과 기존 승인 수정의 논리 범위를 분리하고 별도 push 승인을 받는다.

잘못된 모델 출력, 미지원 대상/주체, 증거 부족, 승인 거절은 여전히 안전하게
중단될 수 있다. 모든 `planning_error`를 숨기거나 실행을 강행하는 변경은 아니다.
