# 오케스트레이션 병합 구현 계획

작성일: 2026-10-01. 작업 브랜치: `dev-orchestration`. 최종 반영 브랜치: `dev`(사용자 최종 지정).

개정 4. 호출 계약, 공통 스키마 선행 작업, 승인 재개, 추가 지시의 실행 변경 기준, 파일별 처분, 충돌 목록, 시간 제한과 회귀 관측점 및 워커 접점을 명시한다.

## 목표와 비교 기준

우리 경로 중심 실행 엔진을 유지하면서 팀원 구현의 자연어 대화, 사용자 추가 지시, 구조화된 실행 피드백, 결정 과정 표시를 통합한다. 실제 Git 병합으로 팀원 변경의 이력을 포함하되, 두 개의 계획 엔진을 병렬 운영하지 않는다.

- 우리 기준: `40aedb27c2f389f93bcb4d649787b543cdbc4913`.
- 팀원 기준: `8979cac5f73de106c7433405cdb26c69ae3b4f74` (`dev`).
- 공통 기준: `e4b2990b34cd68e708819dd2f8a0687c1cb1f1f1`.
- 팀원 변경 48개 파일 중 양쪽 변경의 교집합은 30개다. 교집합 수와 실제 충돌 파일 수는 다르다.
- 기존 미추적 문서 두 개와 `probe_atomic_tmp.txt`는 수정·추가·삭제하지 않는다. 새 클론이나 독립 작업 스레드는 만들지 않는다.

## 유지해야 하는 실행 의미

1. 운영 판단은 실제 LLM 필수 단일 흐름이다. 규칙 전용·혼합 모드, 가짜 모델 자동 선택, LLM 장애 시 규칙 실행을 되살리지 않는다.
2. LLM이 경로를 생성·검토·수정하고 다음 행동을 결정한다. 현재 채택 경로의 버전·단계·대상·계정·작업·선행조건을 코드가 검사한다.
3. 경로 채택과 실행 승인은 구분한다. 모든 실제 실행은 정책 검사와 실행 직전 경로·사용자 지시·실행 소유권 검사를 통과한다.
4. 사용자 제공 정보, 관측 사실, 가설, 실행 피드백을 구분한다. 계획에 적힌 속성이나 도구 성공만으로 사실·취약점·완료를 확정하지 않는다.
5. 단계·작업·목표 완료는 기존 의미 검토와 같은 실행·호출·경로·대상·계정의 실제 증거 및 최종 검증 기록을 요구한다.
6. 승인 재개는 원래 요청을 재개한다. 경로·사용자 지시가 바뀌면 이전 요청을 재검토한다. 대기 작업의 연결과 소유권, 병렬 예산, 중복 실행 방지, 장기 작업 복구를 보존한다.

## 통합할 기능과 경계

### 사용자 대화와 추가 지시

팀원의 대화 처리기를 활용해 질문·설명·명확화·조사 요청을 구분한다. 고정된 종료·현황·승인 명령은 기존 명령 처리기를 사용한다. 대화 처리기에는 직접 도구 호출 권한이 없다. 운영 모델 오류나 잘못된 분류 응답을 실행 요청으로 바꾸는 결정론적 대체는 사용하지 않는다.

실행 중 자연어 전략 지시는 기존 사용자 지시 저장 영역에 `guidance`로 기록하고 다음 모델 문맥에 전달한다. 지시는 새로운 목표 사실, 범위 확대, 실행 승인으로 해석하지 않는다. 문맥상 표시용 답변·가정·계획과 실제 실행 사실을 구분한다. 사용자 언어를 유지하고 공개 텍스트는 기존 민감정보 마스킹을 적용한다.

조사 종료 후 설명 질문은 이전 실행의 실제 요약으로 답하고 새 조사를 시작하지 않는다. 새 조사를 시작할 때 우리 설정 재로드·범위·ROE 검사를 유지한다. 기존 승인 식별자를 통한 승인·거부와 재개 동작을 보존한다.

### 구조화된 실행 피드백

팀원의 최근 실행 피드백 모델과 최대 12개 창을 활용한다. 정책 허용·거부·승인 대기, 도구 성공·실패, 상태 갱신을 구분하고 작업·행동 식별자, 적용 정책, 승인 식별자, 대안 검토 가능 여부를 유지한다. 원본 인자·증거·토큰을 이 창에 복사하지 않는다.

정책·실행·갱신의 단일 작업 경로와 병렬 집계 경로 모두에서 최종 상태에 피드백을 남긴다. 병렬 작업별 결과를 하나의 거부로 축소하지 않는다. 같은 행동·단계·결과의 재생은 피드백을 중복 기록하지 않도록 일관된 기준을 둔다. 미실행 정책 거부를 도구 실패로 세지 않는다. 이전 체크포인트에는 빈 창을 기본값으로 제공한다.

우리 엔진의 기존 실패 문자열, 작업 이력, 증거 연결은 삭제하지 않는다. 구조화 피드백을 모델 문맥에 명시적인 실행 결과 영역으로 추가하고 원본 결과의 권위는 증거 저장 영역에 유지한다. 과거·중복 피드백은 새로운 사실이나 진척으로 세지 않는다.

### 결정 과정 표시와 경로 엔진 연결

팀원의 사용자·에이전트·관측·도구·정책·상태 표시를 우리 모델 결정 지점과 실행 이벤트에 연결한다. 경로 작성·검토·수정, 다음 행동, 보류, 완료 제안, 결정 실패를 실제 흐름에 맞춰 표시한다. 공개 결론만 기록하고 내부 사고 과정은 요구·저장하지 않는다.

추가적인 설명 전용 모델 호출은 만들지 않는다. 구조화 결정의 공개 이유와 선택적 답변·가정으로 표시한다. 기존 테스트/체크포인트 응답과 호환되는 기본값을 제공한다. 대화 처리기의 표시용 계획은 채택 경로를 대신하지 않으며, 완료 메시지는 실제 목표·작업 상태를 따라야 한다.

이벤트 인과관계는 컨텍스트 준비 → 모델 결정 → 도구 선택 → 실행 제안 → 정책 결과 → 실제 결과 → 상태 갱신 순으로 연결한다. 경로·버전·단계와 실제 행동 식별자를 표시 이벤트에 붙여 CLI와 감사 기록이 같은 실행을 가리키게 한다. 표시 이벤트 실패를 실행 권한 확대로 처리하지 않는다.

### Playbook과 문맥 보강

기존 규칙 후보는 경로 검토 참고자료로 유지한다. LLM 행동이 형식·참조·도구·선행조건 검사에서 탈락하면 구체적 실패 이유와 관련 참고 절차를 제한된 재시도 문맥에 제공한다. 규칙 후보를 실행 풀에 추가하거나 Playbook 일치 여부를 통과 조건으로 삼지 않는다. 모델의 행동 순서와 기존 공통 시도 예산을 보존한다.

모델 문맥에서 장기 작업 복구용 원본 실행 인자와 비공개 증거 메타데이터를 제외한다. 일반 판단은 허용된 공개 요약을 사용하고 상세 원본은 기존 같은 실행의 증거 부분 조회 경로로 읽는다. 저장된 복구 기록은 손상시키지 않는다.

### 설정과 팀원 부수 변경

우리 실제 모델 구성과 `from_environment` 검증, 실행 상한을 유지한다. 팀원의 모드 선택과 가짜 모델 허용 코드는 반영하지 않는다. 최초 MCP 설정은 기존 사용자 선택을 보존하고 팀원의 두 서버 자동 등록을 무조건 켜지 않는다. 별도 새 SDK나 라이브러리는 요구하지 않는다.

팀원 변경의 문서·검증 명령 개선은 새 실제 흐름과 맞춰 반영한다. `tests/vulnbank/vuln-bank`는 팀원 커밋에서 mode 160000으로 추가됐지만 `.gitmodules`가 없고 실행 코드의 의존성이 아니다. 루트가 병합 인덱스에서 이 gitlink만 제외하며 기존 로컬 내용은 삭제하지 않는다. 최종 트리에 매달린 gitlink가 없고 팀원 커밋은 조상으로 남는지 검사한다. 테스트 기대값은 새 실행 의미에 맞춰 이관하며 검사·단언을 제거해 통과시키지 않는다.

## 파일별 병합 책임

| 책임 | 파일 영역 | 방식 |
|---|---|---|
| 루트 | `engine.py`, `cognition.py`, `evidence_access.py`, `events.py`, `nodes/plan.py`, 경로·완료·문맥, `agents/llm_provider.py`, `planners.py`, `providers.py`, `web.py`, `core/config.py`, `bootstrap.py`, `settings.py`, 공유 연결 테스트 | 우리 실행 의미를 유지하고 결정 이벤트·구조화 피드백·참고자료·공개 문맥을 연결한다. |
| 대화 워커 | `app/cli/conversation.py`, `commands/interactive.py`, `run.py`, `resume.py`, `validate.py`, `_shared.py`, `interactive_commands.py`, `renderer.py`, `tests/unit/test_cli_*`, `test_interactive_commands.py`, `test_interactive_mcp_setup.py`, `tests/integration/test_cli_*` | 팀원의 대화·표시 기능을 이식하되 실제 모델 필수, 설정 재로드, 기존 승인·재개를 유지한다. |
| 피드백 워커 | `runtime_observation.py`, `state.py`, `dispatch.py`, `nodes/worker.py`, `policy.py`, `execute.py`, `aggregate.py`, `update.py`, 새 `test_runtime_observation.py`, `test_runtime_feedback.py` | 우리 노드에 구조화 피드백만 추가하고 경로/소유권/증거 가드를 보존한다. |
| 사용자 지시 워커 | `core/enums.py`의 추가 지시 종류, `operator_intents/interpreter.py`, `service.py`, 사용자 지시 테스트 | 팀원의 guidance를 기존 기록·거부·범위·사용자 지시 변경 기준과 연결한다. |
| 오케스트레이터 | 구현 파일 편집 없음 | 워커 생성·범위 관리·통합 상태 확인·결과 취합. 충돌하거나 범위 밖 변경이 필요하면 루트에 보고한다. |
| 루트: 문서·부수 변경 | `README.md`, `.env.example`, `docs/architecture/execution-flow.md`, 계획·기록, gitlink 제외 | 단일 실제 흐름과 기능·설정·검증 결과를 일치시킨다. 중복 동작 설명 문서는 최종 커밋에서 제외한다. |
| 루트: 횡단 회귀 | `tests/integration/test_agent_loop.py`, `test_end_to_end.py`, `test_event_identity.py`, `test_engine_llm_investigation_flow.py`, 새 `test_merged_agent_runtime.py`, `tests/unit/test_agents.py`, `test_llm_candidate_provider.py`, `test_orchestration_nodes.py`, `test_orchestration_settings.py`, `test_stage6_*`, `test_work_job_settlement.py`, `test_durable_resume.py`, `test_semantic_completion_receipts.py` | 기존 경로·계정·완료·예산·소유권·병렬 승인 단언을 유지한다. 기타 변경 테스트도 루트가 인수한다. |

워커는 담당 파일만 편집한다. 공유 파일과 테스트의 최종 수정 권한은 루트가 소유권을 인수한 뒤 행사한다. 워커는 브랜치 전환·Git 변경·커밋·푸시·외부 메시지를 하지 않는다. 내부 워커 생성에 제한이 있으면 루트가 생성하고 오케스트레이터가 조율한다.

사용자가 지정한 모델은 오케스트레이터 DeepSeek 4.1 Flash · Max, 워커 Muse Spark 1.3 · XHigh다. 내부 생성 도구가 오케스트레이터에 노출되지 않으면 루트가 해당 모델의 내부 워커를 생성하고 상태·보고·추가 지시를 중계한다.

구현에서는 대화 담당을 대화 모델·표시 담당과 CLI 흐름·재개 담당으로 나눌 수 있다. 전자는 `conversation.py`, `renderer.py`, 두 단위 테스트만 소유하고 후자는 나머지 CLI 파일·테스트를 소유한다. 통합 회귀와 문서는 중앙 인터페이스와 실제 구현이 확정된 뒤 별도 워커에 넘길 수 있다. 파일 소유권 양도와 수정 종료는 기록하며 내부 동시 실행 상한 안에서 여러 웨이브로 운영한다.

교집합 30개 파일의 처분은 다음 목록으로 고정한다. 실제 merge에서 추가 충돌이 나오면 같은 역할 기준으로 루트가 인수하며 원칙을 바꾸지 않는다.

| 파일 | 최종 처분 |
|---|---|
| `.env.example`, `README.md` | 우리 실제 모델·단일 엔진·실행 상한 예제 유지, 팀원 대화 기능만 실제 구현과 맞춰 기술. 모드 선택 예제 삭제. |
| `src/redteam/app/bootstrap.py` | 우리 실제 LLM 팩토리·경로 저장·원래 Mission 목표 유지, 공개 목표 이벤트 정보만 필요시 추가. |
| `src/redteam/app/cli/_shared.py`, `commands/interactive.py` | 팀원 대화 UX + 우리 환경검증·설정재로드·MCP opt-in·실제 모델 필수. |
| `src/redteam/core/config.py`, `src/redteam/orchestration/settings.py` | 우리 `llm_candidate_generation: Literal[True]` 유지, 팀원 `planning_mode` 및 승격 분기 폐기. 기존 max_steps/max_retries와 예산 설정 유지. |
| `src/redteam/orchestration/agents/llm_provider.py`, `planners.py`, `web.py` | 우리 호환 제공/운영 선택기 퇴역·실행 금지 유지. 팀원 생성기·fallback·순위 선택을 경로 엔진에 연결하지 않음. 공개 결정 스키마는 `engine.py`에서 확장하며 `cognition.py`는 구조화 호출·오류 처리만 담당. |
| `src/redteam/orchestration/nodes/plan.py` | 우리 전체 노드를 기준으로 엔진 결정 호출·선택 이벤트만 보강. 팀원 llm_first/hybrid/recipe 후보 풀 분기는 삭제. 반환 `(path, candidates, outcome)`는 유지하고 최신 이벤트는 엔진 속성으로 인수. |
| `src/redteam/orchestration/nodes/aggregate.py`, `execute.py`, `policy.py`, `update.py` | 우리 경로·소유권·증거·예산·재개 코드를 기준으로 피드백과 공개 이벤트만 추가. 계획 속성의 사실 승격은 가져오지 않음. |
| `src/redteam/orchestration/state.py` | 우리 경로·오류·재개 참조를 유지하고 빈 기본값의 최근 관측 창만 추가. |
| `tests/integration/test_agent_loop.py`, `test_end_to_end.py`, `test_event_identity.py` | 우리 경로·실행·완료 단언을 유지하고 새 피드백·이벤트 인과관계 단언 추가. |
| `tests/integration/test_cli_interactive.py`, `test_cli_session.py` | 우리 실제 모델 주입·설정 검사 유지 + 팀원 대화 분류·새 실행 방지 회귀 이관. |
| `tests/unit/test_agents.py`, `test_llm_candidate_provider.py`, `test_orchestration_nodes.py`, `test_stage6_policy_task_semantics.py` | 우리 운영 의미·LLM 필수·거부 후 재계획 단언 유지. 팀원 규칙 모드 기대는 폐기하고 새 공개 피드백 단언 이관. |
| `tests/unit/test_cli_operator_console.py`, `test_cli_operator_console_hardening.py`, `test_cli_resume.py`, `test_interactive_mcp_setup.py`, `test_operator_intents.py` | 우리 승인·복구·사용자 선택·의도 제한 단언 유지, 새 guidance·대화·구조화 결과 회귀를 합침. |

자동 병합으로 들어오는 나머지 18개 파일도 담당표에 포함된다. 팀원 전용 `conversation.py`, `runtime_observation.py`, `events.py` 확장, `providers.py` 문맥, `operator_intents`·enum, `commands/run.py`·`validate.py`·`interactive_commands.py`·`renderer.py`·전용 테스트·아키텍처 문서는 각각 위 담당이 인수한다. 팀원 모드 전용 `test_orchestration_settings.py`는 우리 단일 설정·이전 체크포인트 기본값 검증으로 이관한다.

## 구현 전에 동결할 호출 계약

루트가 실제 병합 충돌을 정리하고 아래 공통 스키마를 먼저 반영한다. CLI·지시·피드백 워커의 병렬 편집은 공통 import가 가능한 뒤 시작한다. 이 선행 변경은 구현 시작을 위한 준비이며 검사 완료로 세지 않는다.

| 계약과 담당 | 고정 동작 |
|---|---|
| 루트 → 피드백 워커: `RuntimeObservation`와 `RuntimeState.recent_observations` | 팀원 모델의 control/tool/state/operator 종류, 행동·작업 관련 참조, 결과, 이유, 적용 규칙, 승인 참조·대안 가능 여부를 사용한다. 요약 500자, 이유 최대 8개·각 200자, 정책 최대 12개·각 200자, 최근 창 12개. 텍스트는 기존 마스킹. 상태의 `Field(default_factory=list)`로 이전 직렬화 상태에 빈 창을 제공한다. SQLite 이전 체크포인트 역직렬화와 재개로 실제 호환을 검사한다. |
| 피드백 워커: `append_runtime_observation(runtime, observation)` | `(step, action_id, kind, outcome)`를 갱신 키로 사용한다. 동일 키의 이유·정책은 기존/새 순서대로 중복 제거 후 최근 최대 8/12개를 보존하고 요약·대안 여부는 최신 값을 사용한다. 정규화한 모델 dump가 같으면 완전 동일 재생으로 창을 늘리지 않는다(별도 저장 해시는 불필요). 승인 대기→허용, 정책→도구→상태는 다른 결과/종류로 남는다. 상태 관측에도 해당 action_id를 반드시 채운다. 최대 12개 창은 완전 이력이 아니며 전체 작업/증거/감사 참조를 별도로 유지한다. |
| 피드백 워커: `WorkerOutcome.runtime_observations` | `list[RuntimeObservation]`와 `Field(default_factory=list)`를 추가해 이전 결과의 JSON/Pydantic 역직렬화를 지원한다. 격리 워커가 실제로 기록한 자기 행동의 피드백을 전달한다. `merge_worker_outcomes`의 action-id dict 병합 리듀서는 변경하지 않으며 집계 노드가 `dispatch_items` 순으로 `append_runtime_observation`을 호출한다. `Overwrite` 기반 이전 배치 결과 제거와 대기 `work_id` 보존은 유지한다. |
| 루트: 모델 문맥 | `runtime_observations`에 공개 피드백을 넣고 `feedback`은 마스킹한 기존 오류 요약으로 유지한다. 작업/증거의 전체 개수·누락 개수와 참조를 제공해 제한 창을 전체 조사 이력으로 오해하지 않게 한다. 실제 도구 카탈로그의 개별 스키마·대상 유형·상태는 제공하되 unhealthy 도구는 실행 메뉴에서 제외하고 설명의 비밀을 가린다. |
| 루트: 공개 결정 | `TurnDecision`의 기존 `mode/reason/actions`를 유지하고 `response`와 `assumptions`를 선택적 기본값으로 추가한다. 응답 최대 2000자, 가정 최대 5개·각 500자, 공개 이유는 마스킹한다. 과거 응답은 reason으로 표시하고 모드와 행위 의미는 바꾸지 않는다. 검토 결정·경로 초안도 기존 공개 reason을 사용한다. |
| 루트: 이벤트 발행 | 엔진의 모델 호출 도우미가 호출 전 `AgentContextPrepared`, 성공 응답 후 `AgentTurnDecided`, 결정 오류는 `PlanningFailed`를 발행한다. 엔진이 최신 이벤트 식별자를 보유하고 계획 노드가 이를 인수한다. 계획 노드는 실제 선택에 `AgentDecisionRecorded`와 `MCPToolSelected`를 붙인 뒤 `ActionProposed`를 발행한다. stage, 경로/버전/단계, 행동 참조, 공개 이유를 연결한다. 원본 프롬프트·응답 전체를 이벤트에 넣지 않는다. |
| 루트: `repair_references(observation, service, runtime, attempted_intents)` | 기존 `reference_paths`로 검증 실패한 행동 묶음의 역할/대상 관련 절차를 추출해 `{items: list[dict], omitted_count: int}`로 반환한다. 각 item은 기존 operation/purpose/targets/prerequisites를 유지한다. 최대 8개, 목적 300자·최대 4개 대상/선행참조다. `decide`의 컴파일 오류 분기에서 `context['repair_references']`로 전달한다. 입력은 `decision.actions`이며 컴파일러 거부 객체를 ActionIntent로 오해하지 않는다. 읽기/참고만 하며 실행 후보·새 모델 호출·호출 예산을 만들지 않는다. |
| 루트: 공개 증거 투영 | evidence metadata의 정확한 허용 키는 `record_kind`, `stage`, `admission`, `supersedes`, `reviewer_id`, `verdict`, `outcome`, `reason`, `claim_supported`, `action_id`, `invocation_id`, `capability`, `operation`, `target_node_id`, `hypothesis_id`, `source_evidence_ids`, `supporting_evidence_ids`, `refuting_evidence_ids`, `covered_principals`, `path_id`, `path_version`, `path_step_id`, `principal_ref`, `receipt_kind`다. `invocation_id` 식별자는 허용하지만 `invocation` 호출 본문, 원본 결과/proposal/plan_step, headers, arguments, 알 수 없는 추가 키는 제외한다. 중첩·자유 텍스트는 기존 `redact_secret_material`로 처리하고 opaque 참조는 보존한다. 원본 저장과 같은 실행의 증거 부분 조회는 그대로다. |
| 지시 워커: `allow_guidance` | 고정 제어·질의의 기존 우선순위를 보존한다. 자유 조언은 실행 중에만 opt-in으로 받는다. LLM이 해석한 조언은 명시적 guidance 기록 경로를 이용해 원문 안의 단어가 stop/exclude 등으로 재분류되지 않게 한다. 의도 기록·이벤트·길이 제한은 기존 저장을 사용한다. |
| 루트: 추가 지시와 변경 기준 | `guidance`는 다음 판단용 조언이고 scope/승인/현재 행동을 변경하지 않으므로 `operator_epoch`에서 status/explanation과 함께 제외한다. 전달은 기존 `operator_instructions`와 `mission.operator_preferences.guidance`를 유지하고 별도 지시 저장소/새 문맥 키는 만들지 않는다. focus/exclude/reprioritize/pause/resume/stop 등은 변경 기준에 남겨 대기 행동 재검토를 유지한다. 가짜/거부 의도는 기존대로 제외한다. |
| 대화 워커: `ConversationAssistant.decide` | Fake 모델 타입 분기와 `_fallback` 실행 분기를 제거한다. 테스트 모델도 같은 구조화 호출을 사용한다. `StructuredDecisionClient(max_attempts=3)`와 전체 대화 판단 `asyncio.wait_for(..., timeout=30.0)`을 사용한다. SDK 재시도0, 총 요청<=3, 타임아웃/사용자 취소는 실행0이며 asyncio 취소는 삼키지 않는다. 생성자의 timeout_seconds/max_attempts는 기본값 있는 테스트 주입점이다. 전송·스키마·분류 오류는 `ConversationError`로 전달하고 idle은 안내 후 복귀, active는 기존 조사 유지, 최초 목표는 실행 없이 오류 반환한다. answer→execute 강제 변환도 제거한다. |
| 대화 워커: 최종 요약과 세션 순서 | 최종 요약은 실제 상태·최근 공개 관측으로 만들고 추가 LLM 호출이나 Fake 특례를 두지 않는다. 한국어/영어 사용자 입력에 맞춰 상태 안내를 제공한다. 입력→고정 명령→설정 재로드/범위·ROE검사→최신 범위로 대화 판단→필요하면 재로드 재확인→동일 `_execute` 진입 순서다. 대화 중 파일이 변경되면 새 범위로 요청을 다시 판단한다. |
| 대화 워커: 승인 후 재개 | idle 승인/거부는 실제 승인 기록의 pending 여부와 현재 run 연결을 확인한다. 기록 변경 성공 후 기존 `commands/resume.py` 복구 경로를 호출한다. `run(args, *, on_finished: Callable[[RuntimeState], None] | None = None)` 및 내부 `_run`에 콜백을 추가하고 실제 `settle_engagement` 완료 후 호출해 `session.record_run(final)`로 갱신한다. CLI parser와 기존 `run(args)` 호출은 그대로 호환한다. 새 Mission/Action은 만들지 않는다. 복구 실패/미결/이미 처리 승인은 안내하며 중복 실행0. |

idle 대화의 질문·명확화·가정·답변은 `renderer`가 `ConversationDecision`을 직접 표시하며 run_id가 없는 가짜 실행 이벤트를 만들지 않는다. active 대화도 공개 메시지는 화면으로 표시하고 실제 guidance 적용은 기존 사용자 지시 이벤트에 연결한다. 엔진의 공개 결정 이벤트와 대화 결정 모델은 서로 다른 계약으로 유지한다.

정책 피드백의 승인 연결은 action_id가 기준이며 승인 대기 당시 approval_id를 사용한다. resolved 판정이 approval_id를 생략하면 피드백에는 기존 runtime.pending_approval_id를 보존하고 실제 제안/Control 결정은 바꾸지 않는다. 임의 새 승인 참조로 옛 승인을 치환하지 않는다. 재개 콜백은 같은 원본 run/action/work를 검증한 실제 복구 결과만 받는다.

체크포인트 호환 샘플은 `40aedb2`의 `RuntimeState` JSON에서 새 관측 창을 제외한 상태다. 새 상태로 역직렬화하면 창=[]이며 기존 경로/승인/work 참조는 같다. 기존 durable/resume 검사처럼 SQLite checkpointer에 이 상태를 기록하고 실제 recover/resume 경로로 읽는다. 손상 JSON·소유권 불명·EXECUTE 재생 가능 상태는 기존 복구 실패 정책으로 중단하고 새 Mission이나 빈 실행 상태로 대체하지 않는다. 경로 SQLite 스냅샷 형식은 변경하지 않는다.

최종 요약은 세션의 실제 `record_run` 결과와 공개 관측에서 만들고 이전 실행 설명은 `InteractiveSession.explain_last_run`을 사용한다. 상세 현황/설명은 기존 `commands/status.py`, 사용자 지시 서비스의 status/explain 경로를 사용한다. 원본 증거를 요구하는 엔진 결정만 `EvidenceReader.read`로 같은 run의 offset/limit/query 부분 조회를 한다.

git 절차는 `git merge --no-commit --no-ff 8979cac5f73de106c7433405cdb26c69ae3b4f74` 후 충돌 처리, `git rm --cached -- tests/vulnbank/vuln-bank`로 새 gitlink만 제외, 정확한 작업 파일 stage, 최종 검사 후 병합 커밋이다. `git ls-files --stage -- tests/vulnbank/vuln-bank` 출력0, `git diff --name-only --diff-filter=U` 출력0, `git merge-base --is-ancestor 8979cac HEAD` 성공을 확인한다. 원래 사용자 미추적3개만 stage 제외이고 새 병합 계획/기록은 명시적으로 추적한다. 문서는 새 실제 단일 흐름과 모순되는 설명·모드 예제만 수정하고 팀원 대화 기능 설명은 보존한다.

계획 방식 선택값 세 종류, 구형 false 전환, fake 제공자 허용은 설정·부트스트랩·CLI에서 반영하지 않는다. 현재 `Literal[True]`와 실제 모델 팩토리를 유지한다. `nodes/plan.py`는 우리 엔진 호출 흐름을 유지하고 팀원 fallback/hybrid 후보 추가 분기를 가져오지 않는다. `agents/planners.py`의 운영 사용 금지를 유지하고 `llm_provider.py`는 기존 호환 검사만 보존한다. 팀원의 그래프 기반 인자 덮어쓰기를 실행 권한 경로에 이식하지 않는다. 현재 컴파일러의 누락 인자 보충과 대상·계정 불일치 거부가 기준이다. `agents/providers.py`의 문맥 확장은 호환 자료이고 직접 실행 권한이 아니다. `agents/web.py`의 형식 변경은 우리 코드와 합칠 수 있지만 규칙 후보 실행은 켜지 않는다. `bootstrap.py`의 기존 사용자 목표·필수 작업·증거 완료 연결을 유지하며 팀원 공개 목표 이벤트만 필요할 때 반영한다.

승인 대기·거부·배치 재개의 work settlement는 기존 상태를 유지한다. 승인 대기는 작업 참조와 원래 제안을 체크포인트에 보존하고 소유권을 반납한다. 거부는 해당 작업을 blocked/stale로 정리하되 필수 MissionTask를 근거 없이 완료하지 않는다. 혼합 배치의 이미 실행한 형제는 예산과 증거에 한 번만 반영하고 재개에서는 승인 대상만 POLICY로 진입한다. 실행 없이 미룬 형제는 기존 WorkLedger에 남아 이후 모델 판단을 거친다.

## 회귀 사례와 실제 관측점

| 담당·테스트 파일 | 입력/관측/합격 기준 |
|---|---|
| 대화: `test_cli_conversation.py` | 성공한 질문/조사/명확화 응답과 전송·스키마 오류를 주입. 질문/오류에서 execute 호출 0, 목표가 실제 행동을 만들지 않음, 테스트 대역도 구조화 호출을 거침. |
| 대화: `test_cli_session.py`, `test_cli_interactive.py` | 종료 후 한국어 설명·현황 입력은 신규 run/Mission 생성 0. scope 파일을 판단 전/판단 중 변경하면 최신 범위를 사용하고 재검사함. 설정 오류는 프롬프트 복귀. |
| 대화: `test_cli_operator_console.py`, `test_cli_operator_console_hardening.py`, `test_operator_intents.py` | 실제 active run에서 자유 조언을 기록하고 다음 문맥에서 확인. 질문은 지시 변경 0, 조언은 승인 0/도구 호출 0, 범위 확장은 실행 불가. 분류 실패가 새 실행으로 바뀌지 않음. |
| 대화·루트: `test_cli_resume.py`, `test_approval_resume.py`, `test_stage6_fanout_policy_semantics.py` | 승인 대기 배치의 일부 성공 후 승인/거부. 같은 run/action/work 복구, 이미 실행한 형제 호출 1회, 대기 참조 보존, 미결/복구 실패에는 실행 0. 완료 콜백은 세션의 실제 상태를 갱신함. |
| 피드백: `test_runtime_observation.py`, `test_runtime_feedback.py` | 단일 허용/거부/승인, 무효 경로 조기 거부, 도구 오류/정상 결과, 상태 갱신, 여러 워커와 동일 결과 재생. kind/outcome/rules/approval/action 식별자 정확, 창<=12, replay 크기 증가0, 원본 secret/인자 없음. |
| 루트: `test_merged_agent_runtime.py` | 모델 경로 작성→검토→잘못된 행동→Playbook 참고 수정→정책→실제 실행→최종 증거 완료. 모델 호출 순서·공통 예산·실제 카탈로그·경로 참조·표시 이벤트 인과관계 검사. 규칙 후보 직접 실행0, Playbook 밖 등록 도구 허용. |
| 루트: `test_engine_llm_investigation_flow.py`, `test_claim_provenance_same_run.py` | 원본 복구 호출에 임의 헤더·인자·중첩 secret을 넣어 일반 문맥 제외와 원본 저장 보존 확인. opaque refs 유지. 최신 경로·제공/사실/가설 분리·같은 실행 증거 검사. |
| 루트: `test_stage6_policy_task_semantics.py`, `test_stage6_resume_safety.py`, `test_operator_endpoint_suppression.py` | advice는 대기 행동 취소0·다음 문맥 반영; exclude/focus 변경은 기존 행동 거부. 거부는 태스크 유지/재계획이며 자동 완료0. |
| 루트: `test_fanout_approval_accounting.py`, `test_durable_resume.py`, `test_work_job_settlement.py`, `test_execution_safety.py`, `test_agent_loop.py` | 전체/역할/대상/턴 예산과 동시성 단언 유지. old checkpoint→새 runtime의 빈 관측 창 복구. lease 상실·장기 작업 제출 불확실·raw 처리 재시도에서 같은 부작용 재실행0. |
| 루트: `test_semantic_completion_receipts.py`, `test_engine_task_completion.py` | 공개 finish/응답이 증거 검증을 우회하지 않음. 오류/가설반박/정상HTTP 성공으로 PoC 완료0. 필수 작업별 증거와 최종 승인 기록 필요. |
| 대화·루트: `test_cli_renderer.py`, `test_event_identity.py`, `test_cli_validate.py`, `test_interactive_mcp_setup.py`, `test_bootstrap_app_config.py`, `test_cli_runtime_config.py` | 실제 event payload/인과ID/한국어 표시·마스킹. config 검사에 불필요한 LLM 호출0. 실제 모델 필수/false거부와 기존 MCP 선택 유지. |

대화 회귀에 무응답 대기→짧게 주입한 timeout→ConversationError/실행0, 반복 형식 오류→호출 정확히3회/실행0, 사용자 취소 전파를 추가한다. 지시 회귀에 "잠시 멈춰 주세요"/"그 경로는 제외해 주세요"는 기존 pause/exclude, "조언 내용에는 stop이라는 단어가 있습니다"라는 명시적 조언은 guidance이며 scope/승인 변화0을 검사한다. 엔진 통합의 정상 최초 호출은 path_draft→path_review→next_action 순서다. 초기 작성+검토 예산과 다음 결정 예산은 기존 각3회이며 실패/수정도 같은 인스턴스의 남은 횟수를 소모한다. 단순 인자 복구 한 번은 두 번의 next_action 호출, 규칙 실행0, 실제 도구 호출1로 단언한다. 설명 이벤트 추가에 따른 모델 호출 수 증가는0이다.

외부 리뷰 전달 묶음은 고정 부모 SHA 두 개, 이 계획의 실행 의미/수용 기준, `git diff 40aedb2 -- src tests examples deploy`, 변경 파일 목록, 마지막 테스트·정적 검사 로그, 실패 재현 절차다. 외부 모델은 실제 현재 파일을 읽되 파일/브랜치/테스트 기대값 수정은 금지한다. 먼저 root가 독립 발견 목록을 작성하고 외부 결과와 대조하여 동일 결함은 하나로 묶는다. 수정 후 새 diff/로그/재현을 전달해 해결·새 회귀를 확인한다. 부분/실패 리뷰는 완료 증거로 계산하지 않는다.

팀원 Fake 전용 테스트는 같은 구조화 경로를 반환하는 명시적 Scripted/Stub 모델로 이관한다. 테스트의 질문/실행 횟수/승인/범위/결과 단언은 유지한다. 공용 테스트 도우미가 필요하면 CLI fixture와 경로 엔진 fixture의 응답 스키마를 구분해 실제 모델 계약을 따르고 응답 자체를 규칙 실행으로 대체하지 않는다. 실패 단언 삭제·통과 전용 mock·새 skip/xfail은 금지한다.

새 계획·작업 기록은 `docs/agent/orchestration-merge-plan-20261001.md`, `orchestration-merge-work-log-20261001.md`에 보관한다. 기록 양식은 단계/변경 파일/실제 검사·리뷰/결과/남은 항목이다. 각 리뷰 발견은 심각도, 파일·줄, 트리거, 결과, 근거·재현, 반영/기각/미해결 판정과 이유를 남긴다. 루트와 외부 모델은 먼저 독립적으로 변경 전체와 실행 의미를 검토하고 그 뒤 교차한다. 틀린 가정은 근거와 함께 기각하며 실제 결함은 수정 후 같은 실패 사례와 영향 회귀, 외부 재리뷰, 전체 검사를 다시 수행한다.

Linux 검사는 최종 tracked 파일과 이번 새 소스/테스트/설정만 tar로 복사한 독립 검증 폴더에서 수행한다. 원본 `.env`·사용자 미추적 파일·캐시·가상환경·중첩 저장소는 복사하지 않는다. Windows Python으로 파일별 SHA256 목록을 생성하고 Linux에서 같은 목록을 재계산한 뒤 pytest/mypy를 실행한다. 최종 수정 후 원본 목록과 해시를 다시 대조한다. Python 3.12.3과 Windows 3.10.4의 실제 범위 및 CI 3.10/3.14 전체 조합 미실행 여부를 분명히 기록한다.

정확한 최종 검사: `pytest -q`; `ruff check src tests examples deploy/docker/mcp-example/server.py deploy/docker/finance-mock/server.py`; `mypy src/redteam tests`(별도 새 캐시); `python -m pip check`; `docker compose -f deploy/compose/docker-compose.yml config -q`; `docker compose -f deploy/compose/finance-lab.yml config -q`. 외부 환경 조건의 기존 skip은 개별 이유를 수집한다. 워커가 모두 편집을 마친 뒤 마지막 소스에서 실행한 검사만 최종 통과 증거로 사용한다.

## 실행 순서

1. 원격 기준과 작업 폴더를 고정하고 사용자 미추적 파일의 내용이 보존되는지 기록한다.
2. 이 계획을 이전 점수 없이 독립적으로 평가한다. 결함은 계획에 반영하고 새 평가로 다시 확인한다.
3. `dev-orchestration`에서 팀원 커밋을 실제 병합하되 자동 커밋은 하지 않는다. 모든 충돌을 역할·실행 의미 기준으로 해소한다.
4. 루트가 실행 엔진과 공통 인터페이스를 확정하고 오케스트레이터가 네 워커의 서로 다른 영역을 병렬 구현한다.
5. 편집 완료 후 루트가 전체 변경·이벤트·대화와 경로의 연결·실패 경계를 검토하고 필요한 회귀 검사로 수정한다.
6. 외부 모델 `Gemini 3.8 Flash (High)`에 읽기 전용 구현 리뷰를 맡긴다. 루트 리뷰와 독립적으로 작성한 결과를 실제 소스·재현 사례와 교차 대조한다. 시간 초과나 부분 결과를 완료 리뷰로 세지 않는다.
7. 실제 결함을 수정하고 영향 회귀 검사 → 재리뷰 → 전체 검사 순으로 반복한다. 결함이 없고 마지막 수정 이후 전체 검사가 통과해야 완료다.
8. 계획·작업 기록과 최종 검증 결과를 갱신한다. 루트가 작업 브랜치에서 병합 커밋을 만든 뒤 원격 `dev`에 fast-forward로 반영한다. 팀원 기준 커밋이 최종 이력에 포함되고 사용자 미추적 파일이 포함되지 않았는지 확인한다.

## 검증 행렬과 완료 조건

| 요구 | 필수 검증 |
|---|---|
| 질문과 조사 구분 | 종료 후 설명/현황 질문이 새 실행을 만들지 않음; 실행 요청은 실제 경로 엔진을 사용함; 모델 오류는 실행으로 대체되지 않음 |
| 명확화·추가 지시 | 불명확 요청 명확화; 실행 중 guidance 기록과 다음 문맥 반영; 범위 확대·직접 도구 실행·승인 우회 없음 |
| 공개 결정 표시 | 경로/행동/보류/실패/완료 이벤트가 실제 상태와 일치; 인과 연결·한국어·민감정보 가림; 존재하지 않는 실행/성공 표시 없음 |
| 단일·병렬 피드백 | 정책 차단과 도구 실패 구분; 승인 대기·재개; 각 병렬 결과; 이전 체크포인트; 창 제한과 중복 재생 |
| Playbook 보조 | 검증 실패 후 모델이 참고 절차로 수정; 기존 Playbook 밖 행동도 실행 가능; LLM 장애 시 규칙 실행 없음; 공통 호출 예산 유지 |
| 경로·증거 계약 | 최신 경로 버전·계정·사용자 지시 검사, 사실 출처, 원본 결과 보존, 단계/작업/목표 완료와 같은 실행 증거 연결 |
| 기존 실행 제한 | 전체/역할/대상/턴 예산, 동시성, 승인 work 연결, 소유권 갱신, 장기 작업 복구·불명확 실행 재조정 |
| 운영 구성 | 설정 재로드, 실제 모델 필수, 기존 MCP 선택, 실제 도구 카탈로그, 의존성·배포 구성 |

최종 필수 명령은 CI 범위의 전체 `pytest -q`, `ruff check src tests examples deploy/docker/mcp-example/server.py deploy/docker/finance-mock/server.py`, `mypy src/redteam tests`, 설치 의존성 검사, 기존 두 Docker Compose 구성 검사다. Windows의 관련 회귀와 Linux의 전체 검사를 수행하고 Linux 복사본이 최종 소스·테스트·설정과 같은지 대조한다. 기존 외부 환경 조건의 skip은 이유를 확인하고 새 skip/xfail로 실패를 숨기지 않는다.

검사 실패는 실제 원인을 수정한 뒤 다시 실행한다. 모델 테스트 대역의 통과를 실제 모델 품질 검증으로 보고하지 않는다. 외부 모델·모바일 기기·Docker 실행이 수행되지 않았다면 그 제한을 기록한다. 최종 목표 완료는 계획 점수가 아닌 위 요구와 마지막 소스에 대한 리뷰·검사의 증거로 판단한다.

## 블라인드 평가 기준

### 워커 접점의 최종 동결

`reference_paths`의 기존 목록 반환과 경로 검토 호출은 그대로 유지한다. 새 `repair_references`만 그 목록을 관련 대상/역할 기준으로 필터링·제한하여 items/omitted_count 묶음으로 감싼다. 기존 호출부를 새 반환 형태로 변경하지 않는다.

루트의 엔진 `last_event_id` 속성이 최신 사건을 보유한다. `decide(runtime, observation, *, last_event_id=None)`에서 이전 사건을 인수하고 모델 호출 도우미 `_ask`가 `orchestration.engine` 발행 주체로 호출 전 준비, 성공 후 공개 결정을 연결한다. 계획 노드는 엔진의 최신 식별자를 인수한 뒤 `orchestration.plan` 주체로 결정 기록→도구 선택→실행 제안을 발행한다. 예외에서도 엔진 최신 식별자를 실패 사건의 원인으로 쓴다. 루트만 엔진과 계획 노드를 편집하며 워커는 여기서 사건을 중복 발행하지 않는다.

최근 창 비교는 검증된 관측의 `model_dump(mode='json', exclude_none=True)` 딕셔너리 동등성으로 한다. 키 순서는 비교에 무관하고 문자열 공백은 원본 의미를 보존한다. 별도 JSON 문자열/해시 비교를 하지 않는다. 동일 키의 이유/규칙은 마스킹된 값의 안정적 중복 제거·상한 적용 후 비교한다. 승인 대기→허용은 다른 outcome이고 정책→도구→상태는 다른 kind라 별도 기록으로 유지한다. resolved 결정의 approval_id가 없을 때 기존 pending 승인 식별자가 허용 피드백에 남고 원래 행동 식별자가 같다는 회귀 단언을 추가한다.

설정 회귀는 `OrchestrationConfig(llm_candidate_generation=False)`와 `OrchestrationSettings(llm_candidate_generation=False)`가 검증 오류를 내는 기존 사례를 보존하고, 환경 false값도 거부됨을 확인한다. 실제 모델 설정이 누락된 운영 실행은 설정 오류이며 명시적 테스트 모델 주입과 구분한다.

대화·표시 워커의 공개 심볼은 `ConversationAssistant`, `ConversationDecision`, `ConversationError`, `catalog_tool_labels`다. 기존 `decide` 인자와 `final_response(state)` 호출은 호환되며 `prefer_execute`는 강제 모드 변환을 하지 않는다. 표시 모듈의 기존 공개 함수·이벤트 렌더러·패널·프롬프트 시그니처를 유지하고 `render_conversation_decision(console, decision)`을 추가한다. CLI 공용 `mask_secrets(value: Any) -> Any`도 유지한다. 구조화 호출은 기존 `redteam.orchestration.cognition.StructuredDecisionClient(llm, max_attempts=3)`를 import하며 SDK 재시도는 이 도우미가 0으로 관리한다.

명시적 조언 진입점은 `OperatorIntentService.receive_guidance_and_apply(instruction: str, *, mission_id, run_id=None, publisher=None, last_event_id=None, now=None, source='operator') -> IntentApplication`로 고정한다. 자유 문장을 재분류하지 않고 기존 지시 모델의 GUIDANCE로 구성해 같은 저장·이벤트·apply 경로를 사용한다. 고정 제어 명령은 기존 `receive_and_apply`로 처리한다. 기존 안내용 `allow_guidance`는 호환하되 승인·대상 변경 권한을 부여하지 않는다.

대화·표시 워커는 두 소스와 `test_cli_conversation.py`, `test_cli_renderer.py`만 소유한다. CLI 흐름 워커는 이 두 파일·테스트를 제외한 나머지 CLI 변경·테스트를 소유한다. 루트는 표에 열거된 횡단 회귀와 표에 없는 변경 테스트만 소유한다. 이벤트 상수가 추가로 필요하면 워커가 파일을 편집하지 않고 루트가 먼저 공통 동결 단계에서 추가한다.

평가는 이전 점수·평가자의 작성 의도 없이 계획과 요구·소스만 보고 수행한다. 요구 충족 20점, 실행 의미와 실패 경계 20점, 파일/인터페이스/의존 관리 20점, 검증과 완료 증거 20점, 회귀/운영/작업 보존 20점으로 총 100점이다. 각 감점에는 파일·실패 경로·누락 요구가 필요하다. 100점은 이 기준의 누락이 없다는 평가이며 구현의 무결점 보증이 아니다. 평가 결과와 수정은 작업 기록에 누적한다.
