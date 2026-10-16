# 독립 Planner 요청과 고정 prefix (10b)

Planner는 기존과 같이 판단 pass 사이의 대화를 저장하지 않는다. 10b는 실제
`advance_hypotheses`→`_ask_with_context`→`StructuredDecisionClient`→`RunMeteredLLM`→adapter
요청에서 고정 계약과 현재 DATA의 경계를 명시하고 검증한다. 새 실행기·모드·DB는 없다.

## 입력 경계

고정 부분은 선택된 common/role prompt, code-owned Planner 계약, 출력 JSON schema다.
`PromptSnapshot.compose`는 기존 순서인 common→role→runtime contract를 유지하며 이 안에
요청별 값이나 Task·후보 snapshot을 넣지 않는다. split 기본 흐름과 single-stage 호환 흐름은
각각의 고정 계약을 사용한다. 서로 다른 profile/model/output 계약이 같은 prefix라는 뜻은 아니다.
`StructuredDecisionClient`는 schema를 정렬된 JSON으로 한 번 정규화해 lookup/repair에서도
같은 순서로 전달한다. schema의 배열·required/enum 의미나 canonical authority는 바꾸지 않는다.

user 메시지 하나에는 현재 mission/목표, scope/ROE, 운영자 지시, 입력 sequence,
후보·Task·워커 결과·실행·진단, readiness와 환경, 예산 및 최신 lookup 창을 전달한다.
scope/ROE도 이 변경 자료에 남긴다. Run 간 또는 정책 변경 후 이전 범위를 고정 prefix로
재사용하지 않는다. 각 pass는 자신의 현재 snapshot과 scoped 조회로 판단한다.
이전 assistant 답변·추론·user 대화를 덧붙이지 않는다. 필요한 과거 의미는 기존
canonical 결과·후보·공개 review receipt·원본 증거에서 읽는다.

`candidate_review_support`는 표시된 현재 후보와 같은 개정의 Task/result, 결속된 source
result, 보존 원본 참조를 연결하는 제한된 탐색 자료다. Task outcome은 후보 verdict가 아니며
참조 카드도 원본 본문·최종 검토·proof를 대신하지 않는다. 필요한 원본을 읽고 현재 후보의
독립 검토를 요청할지, 추가 관측이 필요한지, 검토 경로가 없는지 판단한다. 코드는 이를
자동으로 실행하거나 후보 판정을 만들지 않는다. 카드에 없는 관계는 부재의 증거가 아니며
생략 개수와 기존 lookup을 사용한다. 요약으로만 표시된 Task는 full-read로 승격되지 않는다.

`StructuredDecisionClient.ask`의 선택적 `request_context`는 호출자가 소유한 읽기 전용
관측 자료 공급자다. actual attempt마다 user DATA의 복사본에만 병합하며 lookup과 repair에도
같은 system/schema를 유지한다. Planner는 기존 ledger의 최신 Run/stage 사용량과
normal/repair 결정 예산, 공유 Control의 남은 action 예산을 전달한다. Task/owner별 상세
집계를 모델 입력에 반복 복사하지 않는다. 전체 집계는 원래 원장/report에 유지한다.
사용량은 전송 직전 준비 단계의 관측이며 admission 예약 자체가 아니다. semaphore 대기 후의
최종 허가·scope·예산 확인은 기존 metered guard와 Control 책임이다.

schema·의미 오류의 repair는 bounded public diagnostic만 user 메시지 뒤에 붙인다.
자유 응답 전체나 내부 추론은 넘기지 않는다. callback은 모델/대상 호출을 하지 않으며,
추가 telemetry를 포함한 필수 문맥도 기존 128 KiB 한도를 넘으면 명시적으로 거부한다.

## 조회와 등록

`PlannerPassContext`는 pass마다 새로 생성한다. 기본 자료가 충분하면 조회 호출은 추가하지
않는다. 선택적 조회는 같은 normal/repair/Run budget에서 사용하며 최신 창만 보낸다.
pass-local 조회 cache를 다음 pass의 기억으로 재사용하지 않는다. lookup과 commit을 섞은
답은 기존 validator에서 repair 대상이며 최종 유효 판단 전에는 후보/Task/cursor를 기록하지 않는다.

조회가 없으면 `context_request=null` 또는 필드 생략을 사용한다. 조회 객체는 version 1의
단일 context/evidence 질의 또는 version 2의 1~4개 queries 중 하나를 포함해야 한다.
version만 있는 빈 객체, 질의가 둘인 단일 항목, version과 맞지 않는 혼합 형태는 모델에
전달되는 JSON schema에서도 거절한다. 런타임의 기존 같은 조건과 조회 권한은 유지한다.

canonical `capture_snapshot`, related reads, exact source/result·후보 revision/purpose 검증,
독립 public receipt, final CAS 및 종료 inventory/pending gate는 유지한다. 문맥에서 안 보인다는
이유로 작업이 없다고 판단하지 않는다. 관련 source drift는 거부하고 무관 형제 변경은 기존
의미대로 처리한다. atomizer는 기존의 관련 handoff, issued IDs, coverage 검증을 유지하며
전체 Planner 자료·모델 원장을 다시 받지 않는다.

다음 모델 요청을 허가할 때도 이미 알려진 관련 readset을 같은 소유 검사로 확인한다.
명시적 lookup의 소비 참조와, 수락된 stage1의 후보·질문·source 참조는 atomizer 및 보정
요청에 이어진다. 단순히 화면에 표시된 형제 Task/후보 전체를 readset으로 확대하지 않는다.
metered 요청은 semaphore/cooldown 뒤 reserve/dispatch 직전에도 검사한다. 그때 관련 입력이
바뀌면 typed `UnstartedRequestHalt`를 소유 workflow로 돌려 같은 `HypothesisPlanningInvalidated`
사건을 기록한다. provider 요청 ID가 없는 provisional decision 시도만 반환하며, 이미 실행한
요청·사용량은 유지한다. transport retry·unknown usage·부분 Task/candidate/cursor 저장으로
바꾸지 않는다. 검사를 통과한 뒤 응답 중 발생한 변경은 기존 최종 CAS가 계속 거절한다.
관련 참조를 아직 알 수 없는 거절 응답에서 모든 snapshot 항목을 관련 입력으로 추측하지 않는다.

Planner가 깨어난 시점의 canonical 후보 inventory가 비어 있으면 최종 judgment는 등록된
target에 연결된 검증 전 후보를 최소 하나 제안해야 한다. 후보에는 아직 확인하지 않은 전제를
명시하고 `open`/`inconclusive`/`held` 상태를 사용한다. 코드 validator가 이 조건을 검사하며,
lookup-only 응답은 조회 후의 최종 judgment가 아니므로 예외다. 이 규칙은 Finding 확정 조건을
낮추지 않으며 후보 생성 자체를 취약점 확인으로 취급하지 않는다.

## provider 전달과 한계

Chat은 첫 system 메시지와 별도 `response_format`을, Responses는 `instructions`와
`text.format`을, Anthropic SDK 변환은 `system`과 `output_config`를 유지한다. user DATA는
그 계약과 분리된 input/messages로 전달한다. structured 경로는 기본 kwargs와 호출 kwargs의
`previous_response_id`·`conversation`을 제거해 숨은 response chain을 연결하지 않는다.
OpenCode의 transport session header는 기존대로 유지하며 모델 대화 기억으로 취급하지 않는다.
subscription Responses에 필요한 기존 textual JSON instruction/schema 전달도 유지한다.
이는 backend 형식 준수를 위한 기존 처리이며 캐시를 위한 별도 사본을 추가한 것이 아니다.

HTTP JSON 필드 순서는 모델이 해석하는 prompt 순서와 같은 개념이 아니다. offline wire
테스트는 설치된 SDK가 변환해 전송한 system/instructions·출력 schema가 동일하고 input은
실제로 달라지는지 확인한다. raw HTTP JSON의 공통 byte prefix로 provider cache hit를 주장하지 않는다.
실제 cache read/write가 보고될 때만 10a의 request-local usage 원장에 기록한다.
지원되지 않거나 누락된 값과 SDK 기본 0의 구분 한계는 [10a 계약](worker-session.md)에 따른다.
이번 단계는 실제 cache hit·요금 절감·취약점 탐색 성능을 측정하지 않았다.

## version과 resume

10b 도입 당시 profile은 16/13이었다. 현재 `native-probes`는 profile 28 / strategy prompt 15,
single-stage 호환 profile은 21 / planner prompt 7이다. Worker prompt는 13이며 atomizer는 8이다.
기존 prompt SHA와 profile fingerprint가 달라지며, `specialist_planning_inputs`에도
`planner_request_contract_version=6`를 포함한다. 활성 Planner의 candidate update는 상태를
명시하고, review 참조는 terminal 판정에서만 소비한다. 따라서 prompt bytes가 그대로인 custom
profile도 기존 코드 계약의 Run으로 조용히 재개되지 않는다. 기존 planning binding 검사에서
다른 입력으로 거부하고 새 Run을 사용한다. 과거 DB/보고서/checkpoint를 수정하거나 backfill하지 않는다.

## 검증 범위

- split/single-stage 각각 합성 워커 관측 두 건과 운영자 변경을 실제 조립·등록·결과 경로로 전달:
  고정 system/schema 동일, user DATA·sequence·예산 변경, 워커 4턴+Planner 2회=6요청.
- lookup→invalid mixed reply→repair: 각각 같은 고정 계약으로 3요청,
  normal 2회+repair 1회, 부분 등록 없음, 요청별 현재 사용량 갱신.
- 실제 SDK의 Chat/Responses/Anthropic HTTP transport double에서 두 요청씩 비교하고
  요청별 total·stage 귀속 확인. subscription의 configured response/conversation chain 제거도
  별도 SDK double로 확인한다. 실제 인증·모델·Kali·대상 통신은 하지 않는다.

Worker의 실제 append-only 루프와 대기·종료·재개·압축은
[Worker 대화 계약](worker-conversation.md)에 구현되어 있다.
