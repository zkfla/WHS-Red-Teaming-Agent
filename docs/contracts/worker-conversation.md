# Worker 대화 수명과 입력 경계 (10c·10d)

## 실제 연결

- `central_runtime.ready_workers`는 기존 Task claim/instance를 원자적으로 배정하고,
  Run identity·시작 시각·설정 binding만 공유하는 새 Runtime을 만든다. 다른 Worker의
  owner·읽기 이력·진단·임시 결과·카운터를 복사하지 않는다. 공통 예산의 근거는 기존
  Control/Run request ledger이며 새 Task의 지역 카운터가 이를 초기화하지 않는다.
- `AgentLoopRunner`의 기존 `run:instance:<id>` checkpoint thread가 `worker_session`
  version 2를 소유한다. `run()`으로 기존 대화를 초기화할 수 없고 원래 resume 경로를
  사용해야 한다. 별도 세션 DB·대화용 provider ID는 없다.
- `nodes.react.prepare_node`는 최초에만 `engine.context`와 `context_for_probe`로
  목적·조건·scope/ROE·principal·admitted 후보·관련 증거를 전달한다. source가 지정되면
  같은 Mission의 정확한 result ID에 대응하는 공개 결과를 전달한다. 다른 대화는 상속하지
  않으며 source가 바뀌면 unavailable DATA를 표시한다. 이것이 source/review CAS를 대신하지 않는다.
- 그래프는 `prepare → model → tools → settle`이다. 준비된 입력/문맥 세대를 model dispatch
  전에 checkpoint한다. 모델 응답을 받았으나 assistant checkpoint 전에 중단돼도 최초 표시와
  이미 차감한 request는 유지된다. 아직 제출하지 않은 도구를 실행된 것으로 간주하지 않는다.
- 정상 후속 턴은 불변 대화에 새 assistant 호출, 새 도구 결과, 실제 변경 알림만 추가한다.
  `_compact_context_messages`·`_message_history`의 치환/12라운드 삭제는 제거했다.
  권한 검증용 `execution_material`·Control·scope 검사·정산은 계속 현재 상태를 읽는다.
- 최초 선택한 도구는 고정한다. 공급된 Bash schema로 바로 실행할 수 있다. 추가 schema는
  기존 detail receipt로 검증한 뒤 결정적 순서로 끝에 추가하며 이전 정의를 퇴출하지 않는다.
  도구 확장은 cache prefix를 바꾸지만 실행 허가를 주지 않는다.

`FrozenMessage`는 최초 선택된 마스킹/표시 창을 저장하고 동일한 복사본을 반환한다.
`_result_messages`는 새 결과 batch에만 적용한다. 나중에 결과 수가 늘어도 앞선 창을
다시 읽거나 줄이지 않는다. `evidence.read`는 원래 evidence ID·offset·limit·query와
실제 반환한 창을 그대로 추가한다. 원본은 EvidenceStore에 남고 표시는 원본 전체나
취약점 검증 receipt가 아니다. 서명·call ID·opaque continuation은 기존 port 계약대로 보존한다.

## 대기·재개·종료

`open/waiting/closed`는 checkpoint metadata이며 Task·후보·Finding·Mission 판정과 다르다.
`settle_node`는 canonical Task가 completed/cancelled이고 미해결 native call 및 실행 책임이
없을 때 닫는다. 원래 의미대로 반환한 inconclusive 작업도 닫을 수 있다. `blocked` 자체를
terminal로 보지 않는다. 승인·pause·job/resource·UNKNOWN·미확정 local outcome은 보존한다.

`runner._binding_for`는 원래 Runtime/Task owner 검사에 이어 세션 버전·system·최초 ordered
tools·prompt/provider binding·admitted 후보·call/result pairing을 **reconciliation/실행 전에**
검사한다. 최신 후보는 변화 DATA로만 보이며 원래 admitted snapshot을 덮어쓰지 않는다.
같은 Task는 기존 call/action/invocation/job/approval ID로 재개한다. scope·stop·승인 취소는
기존 admission 경로에서 즉시 적용하며 과거 메시지는 실행 권한이 아니다.

외부 실행은 기존 submission/result receipt·실행 원장을 reconciliation한다. checkpoint가
뒤처져도 `restore_completed_calls`는 검증된 결과 참조를 붙이고 성공한 Bash를 재실행하지
않는다. 도구 결과는 batch 전체가 완료됐을 때 한 번 append하므로 pending sibling의
call/result 연결도 유지된다. 작업 반환은 이미 canonical commit됐는데 표시 checkpoint만
늦은 경우에도 recorded result를 복구하고 모델/도구 실행 없이 대화를 닫는다.

local 도구도 기존 EvidenceStore에 `runtime_tool_submission`/`runtime_tool_result`를 남긴다.
기존 Task/instance·세대·assistant 메시지 위치/hash·call ID·인자 hash로 같은 호출을 식별한다.
인자 원문을 새로 기록하지 않는다. 결과가 있으면 그 bounded 결과를 재사용한다. submission만
있고 outcome이 없으면 `worker_session_local_outcome_unknown`으로 보류하며 부작용을 재실행하지
않는다. 이는 별도 큐가 아니며 미확정 local 효과를 자동 판정하는 기능도 아니다.

## 명시적 세대 전환

정상 턴에는 압축하지 않는다. 기존 `NATIVE_INPUT_LIMIT`의 UTF-8 application envelope
256 KiB 한도를 dispatch 전에 검사한다. 모델별 실제 token window를 문자 수로 추정하지
않으며 provider 오류도 성공/빈 결과로 바꾸지 않는다.

입력 초과 또는 기존 `update_task` 계약으로 목적이 바뀐 경우에만 새 세대를 시도한다.
같은 Task의 후보 binding은 유지하고 미해결 실행이 있으면 목적 변경을 허용하지 않는다.
정식 목적 변경 전의 완료 receipt는 그 세대에 고정된 원래 Task/hash로 검증한다.

세대 전환은 다음을 수행한다.

1. pending call/승인/job/UNKNOWN·불투명 provider continuation이 있으면
   `worker_session_compaction_held`로 보류한다. 서명 상태를 임의로 절단하지 않는다.
2. 기존 Task/증거/지식 projection으로 현재 목적·조건·scope/ROE·admitted 후보·공개 결과·
   검토/유효성·제약을 구성한다. reader/local-result 본문을 이 기준 문맥에 중복 복원하지 않는다.
3. 기존 assistant/tool 쌍과 공개 판단·실패·부정 결과·상태·원본/provenance 참조는 유지한다.
   이전 reader 창의 text/프로그램 stream만 명시적 omission·이전 표시 hash/세대·원본 참조로
   바꾼다. 읽지 않은 부분을 음성 증거나 취약점 요약으로 판정하지 않는다.
4. 한도 안에 들어가고 실제로 줄어들 때만 `generation += 1`을 적용한다. 이전의 정확한
   메시지·도구·binding·변경 이유·전후 bytes는 같은 checkpoint의 `archives`에 남긴다.
   Task/Run 예산은 유지하고 요약 LLM을 추가 호출하지 않는다.

목적 변경 세대는 크기 감소가 필수는 아니지만 전체 한도는 지킨다. 필수 기준 문맥이나
call/result 참조만으로도 초과하면 `native_input_limit_exceeded`로 명확히 보류한다.
그 후 정상 턴은 새 세대 뒤에 append한다. 세대 전환/도구 확장은 cache prefix를 바꾸며
실제 할인은 보장하지 않는다. 반복 압축으로 원본/필수 계약을 무제한 삭제하지 않는다.

## 동적 DATA와 사용량 표시

같은 이벤트/내용은 ID·fingerprint로 중복 알리지 않는다. 관련 Mission/Task·source·operator·
정책·실행 준비/가용성·관련 후보/증거 유효성 변화와 예산 경계를 새 user DATA로 전달한다.
매 턴 전체 DB나 shared budget snapshot을 다시 보내지 않는다. 기존 승인/실행 결과는
해당 tool result로 전달한다. 무관한 형제 변화가 세션을 새로 만들지 않는다.

초기 `probe` 입력에는 현재 Task의 action/model/repair 잔량이 한 번 포함되고, 이후에는
실제 경계 변화만 `worker_updates`로 추가된다. 동일한 local-read tool과 인자에서 결과가
바뀌지 않으면 기존 frozen payload를 재사용하고 해당 model decision의 progress를 false로
기록한다. 다른 offset/query 또는 달라진 context 결과는 새 조회다. 이 최적화는 조회 권한,
원본 receipt, 다음 모델 요청이나 Task 총 request 차감을 생략하지 않는다.

웹 `graph_model`은 원래 request row에서 Planner/Task/Worker별 input/output/cache read/write의
확인된 합계와 미보고 요청 수를 계산하고 `graph_detail.js`가 표시한다. CLI status/report는
기존 report projection의 같은 usage를 표시한다. UI는 대화 문자열을 파싱하거나 세션을
저장하지 않는다. 과거 상세 usage/total이 없는 row는 unknown이다. 보고된 0과 unknown은
구별하며 cache read/write를 빼서 admission을 완화하지 않는다. 실제 요금은 미측정이다.

중앙 execution row에는 provider descriptor와 program environment에서 캡처한 `selection`
(route/readiness/reason/program)과 원본 결과 처리 뒤의 `outcome`(generic stdout, canonical
protocol receipt, provider failure, UNKNOWN 및 구조화된 재개 조건)을 선택적으로 보존한다.
Planner와 Worker는 기존 execution history를 통해 같은 값을 읽고, web/status/report도 이
원장을 그대로 투영한다. 과거 row에 필드가 없으면 unknown으로 표시하며 오늘의 inventory로
보충하지 않는다. approval/resource 대기와 route/privilege/scope/timeout/program 부재/응답
부재/detach UNKNOWN은 서로 다른 코드와 재개 조건이며 후보 반증이 아니다.

## 호환과 한계

기본 profile 22 / 단일 판단 호환 profile 19 / worker prompt 14,
`worker_conversation_version=3` / session version 2를 사용한다. Worker 코드 계약은 custom 및
이전 profile에도 binding되며, probe Planner는 `planner_request_contract_version=3`을 유지한다.
구 session version 1·native history만 있는 checkpoint·맞지 않는 prompt/provider는 새 모델/
도구 실행 전에 거절한다. 기존 read/report/원본은 유지하고 자동 migration/backfill하지 않는다.

Worker는 현재 선택·가용한 Kali Bash와 실행 경로가 지원하는 조사에서는 Bash를 우선한다.
필수 경로·프로그램·권한·기능이 없으면 다른 선택 도구를 사용하며, 우선순위를 채우기 위한
무의미한 Bash 호출은 하지 않는다. 문맥·증거 조회와 기록·작업 반환은 기존 helper를 사용한다.

별도 atomizer, Planner snapshot 생성의 DB 읽기, 일부 관련 증거 유효성 조회의 Run record
읽기는 남아 있다. 새로운 Docker SDK/Kali MCP·네트워크 도달성·Bash target binding 문제나
취약점 발견률은 이번 변경으로 해결 또는 입증됐다고 보지 않는다.

## 오프라인 검증과 측정

모든 수치는 explicit model/tool/HTTP transport double이다. 실제 모델·Docker·Kali·대상은
실행하지 않는다. 비교 산출물에는 수치/hash만 저장하고 대화·스크립트·본문은 출력하지 않는다.

| 사례 | 확인 |
| --- | --- |
| 10a 동일 scripted 5턴 / 3실행 | 253,320 bytes; 과거 user/창 변경 존재 |
| 최종 동일 5턴 / 3실행 | 239,905 bytes; 모든 이전 메시지 불변, 창 4,000 유지 |
| 3회 Bash + 추가 읽기 + 반환 | 5회 모델, 306,977 bytes; 최초 지시 1회; caller budget 그대로 |
| 1회 Bash + 13회 추가 읽기 + 반환 | 15회 모델, 1,081,818 bytes; 12라운드 삭제 없음; 모든 창 불변 |
| 입력 한계 fixture | 65,221 → 47,085 bytes, 세대 1회; 요약 모델 0회 |
| 불투명 continuation + 입력 한계 | 세대 0 유지, 추가 모델 dispatch 전 명시적 보류 |
| 승인·job·UNKNOWN 프로세스 reopen | 원래 세션/ID 유지; 물리 실행 또는 job 제출 중복 없음 |
| Bash/추가 읽기/반환 commit 뒤 crash | 원래 결과 복구; 3실행/5모델 유지; result pairing 1회 |
| 모델 응답 뒤 assistant checkpoint 전 crash | 같은 준비 입력으로 재개; 유실 응답 포함 3요청 차감, 실행 1회 |
| Planner → A → 독립 Planner → B | 판단 2회+Worker 8회=10요청; source만 전달, 세션 분리 |
| 1d split 통합 Run | Planner 2 + atomizer 1 + Worker 5 + review 2 = 10요청; 외부 실행 1, local read 1, settlement 1, total token known 40 / unknown 6; input/output/cache는 10회 모두 미보고 |

5턴 기준 누적 application JSON은 약 5.3% 작아졌다. 지침 버전도 함께 갱신된 같은 scripted
행동 비교이며 실제 token/요금 비교가 아니다. 긴 대화에서는 append-only 전송량이 늘 수
있다. `conversation-metrics.json`, `boundary-metrics.json`, `resume-metrics.json` fixture
산출물은 요청별 bytes·prefix/변경 위치·세대·호출 수·usage/unknown을 구분한다.
Chat/Responses/Anthropic의 실제 설치 SDK 변환도 offline HTTP double에서 prefix·도구·
call/result/usage를 검사했다. usage fixture의 cache read/write는 전달 정확성 확인용이며
실제 cache hit나 할인, 비용 절감률은 미측정이다.
