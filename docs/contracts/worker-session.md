# 작업별 대화 세션과 요청 사용량 계약 (10a~10d)

`nodes/react.py`는 Task별 `worker_session`을 실제 모델→도구→결과 소비 경로에 사용한다.
정상 턴은 append-only이며 최초 지시 재주입·과거 user 치환·고정 12라운드 삭제·과거 결과
창 재계산을 하지 않는다. Planner는 [독립 요청](planner-request.md)을 사용한다.
종료·대기·재개·명시적 압축과 검증은 [Worker 대화 수명](worker-conversation.md)을 따른다.

## 귀속과 저장

`orchestration/worker_session.py`의 `SessionBinding.capture`는 기존 Run/engagement/
mission/Task/instance와 원래 admitted Task의 `completion_contract_hash`, 후보 snapshot
fingerprint를 묶는다. 후보 revision은 Task의 admitted revision과 일치해야 한다.
최종 선택한 system·ordered tools·검증된 provider binding의 fingerprint도 보존한다.
provider binding에는 기존 설정 fingerprint를 전달하며 키 원문을 전달하지 않는다.

논리적 identity는 기존 식별자 튜플이다. `prefix_identity`는 system/tools/provider
fingerprint로만 계산한다. 서로 다른 작업이 prefix를 공유해도 세션·증거·권한은 공유하지
않는다. 이 값은 provider cache key나 적중 증명이 아니다. 소유권 epoch는 회복 시 바뀔
수 있어 세션 identity에 포함하지 않는다. 현재 소유권 검증은 기존 activation 책임이다.

`WorkerSession` version 2는 binding, 명시적 압축 세대(`generation`),
open/waiting/closed, 대기 사유, 불변 표시 메시지를 담는다. 승인/job/UNKNOWN 대기는
닫힘과 다르다. 이 상태는 대화의 metadata이며 Task 완료나 실행 허가가 아니다.
`restore(..., expected=binding)`는 다른 작업·instance·목적·후보·설정 binding을 거부한다.
실제 소비자는 purpose만 정식으로 바뀌고 미해결 실행이 없을 때 별도 세대 전환을 허용한다.
원래 system, 순서가 고정된 tools, 최초 tool prefix 길이, 이미 전달한 변화의 fingerprint와
이전 세대의 불변 메시지/도구/binding/입력 크기도 같은 checkpoint에 보관한다.

`GraphState.worker_session`은 기존 instance checkpoint 안의 선택적 JSON 값이다.
별도 DB나 session ID를 만들지 않는다. 과거 checkpoint는 조회·보고할 수 있지만 version 1
세션 또는 session 없이 native history/calls가 남은 실행은 자동 변환하여 재개하지 않는다.
기존 instance가 없는 root 실행은 원래 Run thread와 `instance_id=None`을 사용한다.

## 최초 표시 고정

기존 증거 projection이 출력 창을 선택한 뒤 `FrozenMessage.capture`에 명시적 크기 한도와
함께 전달한다. 공통 마스킹·JSON 정규화를 마친 **반환된 표시**를 모델에 보내고 동일하게
checkpoint에 저장하는 것이 소비자 계약이다. 한도를 넘으면 거부하며 JSON 본문을 임의로
잘라 offset·limit·truncated의 의미를 바꾸지 않는다. 선택된 text·provenance·창 metadata가
메시지에 함께 고정된다. 재개 시 `message()`는 새 복사본을 반환하며 재마스킹·창 재계산을
하지 않는다. 공개 메시지 이외의 SDK 최상위 필드는 거부한다.

서명 등 opaque `provider_continuation`은 기존 `NativeCompletion` 계약으로 검증해 그대로
보존한다. adapter가 정규화한 opaque 블록만 전달하며 내부 추론 원문을 추가하지 않는다.
표시 JSON은 repr에서 제외하고 일반 로그나 비교 산출물에 출력하지 않는다.
EvidenceStore/실행 원장이 원본 근거이며 이 transcript는 원본·권한·성공 증명이 아니다.

## 요청별 사용량

공통 port의 선택적 `LLMUsage`는 input/output/total/cache read/cache write를 담는다.
미보고는 `None`, 명시적 0은 0이다. cache read/write는 입력의 부분 집합이며 합산하지
않는다. `NativeCompletion`·`StructuredCompletion`의 기존 위치 인자와 `total_tokens`는
호환된다. 상세 total과 기존 total이 충돌하면 거부한다. text API는 여전히 문자열을
반환하며 선택적 `complete_with_usage` 경로로 계측한다. request-local 응답에 사용량이
붙으며 전역 `last_usage`를 사용하지 않는다.

LiteLLM adapter가 Chat의 prompt/completion, Responses의 input/output 및 각각의 details를
정규화한다. 설치된 SDK의 Anthropic `calculate_usage`는 cache read/creation을 이미
prompt_tokens에 포함하고, creation을 `cache_write_tokens`/`cache_creation_tokens`로
노출하므로 추가 합산하지 않는다. 공식 Responses 사용량의
[input/output 및 cached_tokens 계약](https://developers.openai.com/api/reference/resources/responses/methods/create)과
[prompt caching 문서](https://developers.openai.com/api/docs/guides/prompt-caching)를 기준으로 한다.
OpenCode Go도 같은 adapter를 거쳐 세 protocol의 정규화를 재사용한다.

완료된 Responses 스트림의 terminal response에 사용량이 있으면 보존한다. 누락된 경우
unknown이며 이전 요청의 값이나 문자 수로 보정하지 않는다. 종료 이벤트 없는 스트림은
정상 응답으로 취급하지 않고 기존 원장에 unknown dispatch로 남긴다.
SDK가 이미 기본값으로 만든 0과 실제 wire에서 보고한 0을 구별할 수 없는 경로는
SDK 노출 값이라는 한계가 있다. 현재 SDK의 Chat usage 전체 누락 시 0 생성 동작은 기존
offline wire 테스트로 명시되어 있다. adapter에서 이를 실제 provider 보고의 증명으로
해석하거나 캐시 적중률·요금 절감을 확정하지 않는다.

기존 Run request row에 선택적 `usage`를 추가한다. 과거 row의 상세 값은 unknown이고,
기존 total만 알면 total 집계만 유효하다. reserve→dispatch→settle과 취소/실패/회복의
요청 차감은 유지한다. repair 응답은 JSON 파싱 전에 원래 요청별로 settle한다.
stage/Task/owner 및 Run 집계는 `usage.<field>.known`(보고된 합계)과 `unknown`(미보고 요청 수)을
함께 반환한다. `known_tokens`, `unknown_usage`의 기존 total 기준 의미는 유지한다.
캐시 토큰을 빼서 admission을 완화하지 않는다. terminal report의 기존
`llm_request_accounting`에 같은 snapshot이 포함되며 별도 비용 원장을 만들지 않는다.

## 비교 기준선

`test_investigation_context_baseline.py`는 실제 소비자가 구성한 최종 application 요청을
명시적 double에서 측정한다. system/tools/messages/schema 크기·hash, 이전 요청과의
비교용 system→tools→schema→messages 직렬화의 공통 prefix byte 수,
처음 바뀐 이전 메시지 index, 출력 창 길이, 누적 요청을 남긴다.
산출물에는 hash·길이·개수만 저장한다. 이는 토큰 수나 provider cache hit가 아니다.
adapter의 실제 SDK 변환은 별도 HTTP double 테스트로 확인하며 라이브 통신하지 않는다.

10a에서 동일 Task에 discovery→서로 다른 합성 관측 3회→반환을 실행한 결과:

| 요청 | 메시지 수 | 입력 JSON bytes | 처음 바뀐 이전 메시지 index | 원본 표시 창 한도 |
| --- | ---: | ---: | ---: | --- |
| 1 | 1 | 38,825 | 없음 | 없음 |
| 2 | 4 | 41,183 | 0 | 없음 |
| 3 | 7 | 50,200 | 3 | 4,000 |
| 4 | 10 | 59,106 | 6 | 4,000 / 4,000 |
| 5 | 13 | 64,006 | 5 | 2,666 / 2,666 / 2,666 |

합계 5개 모델 요청, 합성 대상 도구 실행 3회, 253,320 bytes였다. system/tools는 이 fixture에서
같았지만 user 치환과 과거 결과 창 변경이 prefix를 깨뜨렸다. 이 수치는 현재 측정 기록이며
테스트에서 잘못된 동작의 영구 기대값으로 강제하지 않는다. 최종 구현의 같은 scripted
행동은 5요청·3실행·239,905 bytes였다. 과거 메시지 변경은 없고 창은 모두 4,000자로
유지된다. 지침 버전도 갱신되었으므로 이는 실제 토큰/요금 절감률이 아니다.
추가 읽기·긴 대화·압축·재개 측정과 한계는 [대화 계약](worker-conversation.md)에 기록한다.
