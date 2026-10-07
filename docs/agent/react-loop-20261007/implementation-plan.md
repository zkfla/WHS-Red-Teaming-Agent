# LangGraph ReAct 루프 전환 구현 계획

## 재개 델타 — 2026-10-07

현재 목표 파일은 `64076d51-6d16-4005-bbc3-9482599be520/goal-objective.md`다. 기존 계획의 흐름·팀 경계를 유지한다. 전체 Python 3.10/3.14 스위트와 전체 mypy 전에 멈추라는 사용자 지시는 유지한다.

- 체크포인트: 실행에 쓰이지 않는 거절 입력과 이미 dispatch에 연결한 중복 입력은 native call 상태에서 제거한다. 실제 실행 봉투를 마스킹하여 다른 입력을 실행하게 만들지 않는다. 유효한 봉투·대기 로컬 입력의 민감 정보 문제는 기존 참조·저장 계약과 별도로 확인하며, 이 수정만으로 전체 개인정보 문제가 해결됐다고 표현하지 않는다.
- 진단: 기존 safe diagnostic을 조회·정산 래퍼의 반환과 저장 payload에 전달하고, ReasoningHalted/DecisionFailure를 기존 호출 담당으로 전파한다. 승인 조건은 추가하지 않는다.
- 정산 이벤트: 실제 상태 정산 담당과 이벤트 소비자를 함께 맞춘다. 레거시 경로와 중복 발행하지 않으며, 삭제된 TASK_COMPLETED/MISSION_UPDATED 단언을 복원한다.
- 대상 연결: 같은 canonical target의 객체 표현 차이를 후보 중복으로 처리하지 않는다. 실제 서로 다른 대상이 모호한 경우에는 기존 거절을 유지하고 구체적인 안전한 안내를 전달한다.
- 인증 후속 연결은 설계 검토 중이다. 공개 session 입력은 그대로 유지하고 코드 소유 principal identity를 등록 자료에서 연결한다. 저장된 HTTP 관측의 비교는 기존 authentication 의미 소유자를 재사용하는 선택적 공개 도구 접점을 검토한다. 일반 실행마다 인증 주석·별도 심사를 요구하지 않는다. 기존 graph.record 최종 발견 경계는 유지한다.
- 해당 설계 검토 후 authentication.assess를 선택적 runtime contribution으로 구현했다. 기존 authentication 의미를 shared helper로 재사용하고 보존된 ActionProposal/ToolResult에서 익명·세션 응답을 재구성한다. 원본 참조와 scope/linkage를 기존 검사로 연결하며, 최종 finding은 graph.record에서 판단한다. 새 모델 심사나 일반 호출의 주석 의무는 추가하지 않는다. API는 두 evidence IDs, 선택한 session_ref 및 선택적 expected_access다.

최신 모델 지정: 구현·조사 워커 High, 독립 리뷰 Opus 4.8 High 및 DeepSeek 4.1 Max, 보조 리뷰 agy Gemini 3.8 Flash High. 검토 요청·수령·반영·검사 통과는 각각 구분해 기록한다.

기준: `dev-orchestration`, `6a2cba4cb219f0101c8e4f4f4ef8a28a8ed2dc58`, 2026-10-07. 목표 파일과 `docs/proposals/langgraph-react-loop-proposal-20261007.md`를 구현 기준으로 한다. 제안서 조사·설계 작업은 완료된 입력이며 제품 전환은 아직 시작되지 않았다.

## 최종 동작

LangGraph에서 한 모델 응답 → 실제 등록 도구 호출 처리 → 보존된 결과/오류 → 다음 모델 응답으로 진행한다. 일반 도구 호출에 경로 초안·전략 승인·계획 단계 연결·단계 완료·큰 TurnDecision 응답을 요구하지 않는다. 기존 실행/관측 기능을 노드 내부의 새 제안 루프로 다시 포장하지 않는다.

문맥 준비는 모델 요청 직전에 기존 관측·지식 조회 담당을 사용한다. 종료·중단·예산 처리는 루프 경계에서 수행한다. 실제 실행과 원본 저장은 기존 담당으로 연결한다. 승인 재개는 원래 호출을 보존한다. 필요한 복구 노드는 유지하며 특정 노드 수를 완료 조건으로 삼지 않는다.

## 책임과 변경 단위

| 단위 | 소유/변경 | 경계 |
|---|---|---|
| 모델 native 호출 | core/ports/llm, llm/litellm, llm/fake, 관련 어댑터 검사 | 모델 요청/반환에 실제 호출 ID·이름·인자·사용량 보존. 텍스트·JSON 반환을 native 호출처럼 표시하지 않음. 공급자 처리만 어댑터에 둠 |
| 모델·도구 루프 | graph/router, 엔진과 cognition, 판단·호출 처리·정산 연결 | 한 응답 단위 진행, 역할·도구 등록에서 스키마 조립. 실제 호출을 코드가 만드는 실행 봉투에 연결. 로컬 도구는 기존 registry dispatch |
| 실행·결과 처리 | execute/verify/update/worker/aggregate | Control/Gateway/원장/원본/mapper 재사용. 일반 호출은 observation 처리로 필수 LLM 검수 없음. 주장 검증 장애가 유효 관측을 막지 않음 |
| 출처·정산 | completion/semantic_completion/completion_proposal/result_claims | 작업·목적·실행 출처 검증, 기존 실제 경로 증거는 기존 의미대로 읽음. 가짜 경로·수동 면제 없음 |
| 공유 통합 | PlanStep/GraphState/dispatch, 원장 연결, bootstrap/복구/보고 | 루트가 단일 소유자로 조정. 실행 식별자·목적 연결은 시스템 소유. 팀원의 새 역할·이벤트·Atomizer·도구 구현은 선점하지 않음 |

기존 PlanStep은 코드 소유 실행 운반체로 재사용할 수 있다. path 필드는 이미 optional이므로 native 호출에 가짜 PathPlan을 생성하지 않는다. 코드가 만든 실행 운반체에는 native 호출 ID·현재 작업·원래 목적 연결을 보존한다. 범용 새 실행 원장·risk 옵션·영구 런타임 모드를 만들지 않는다.

## 모델 호출·결과의 공통 의미

모델 도구 요청은 공급자가 반환한 call ID, 노출 이름, 인자를 보존한다. 노출 이름과 기존 tool ID의 변환은 공개 어댑터·등록 자료에서 결정하며 중앙 도구명 조건을 추가하지 않는다. 잘못된 인자는 그 호출의 오류로 되돌리고 유효한 형제 호출을 버리지 않는다.

한 응답의 **모든 call ID와 처리 상태를 다음 판단·승인 재개까지 보존**한다. 배치/자원 한도 때문에 아직 선택되지 않은 호출은 같은 응답의 대기 목록에 남고 새 모델 응답으로 덮어쓰지 않는다. 거절·형식 오류는 해당 call의 결과로 대응한다. 승인 대기·장기 job은 원래 호출과 원장 ID로 재개/회수한다. 형제 성공은 재실행하지 않는다. 완료된 결과는 한번만 pairing하며, 같은 모델 응답에 여러 승인 호출이 있어도 하나만 남기고 나머지를 버리지 않는다. 새 영구 큐 없이 기존 그래프 진행 상태·원장을 사용한다. 공급자별 미완료 call/result 프로토콜은 어댑터가 처리하며 잘못된 대화 쌍으로 다음 요청하지 않는다.

일반 네트워크 도구는 기존 Control → 호출 변환 → Gateway 경로를 사용한다. 로컬 조회·기록·정산 도구는 기존 LocalToolRegistry와 해당 출처/소유권 검사를 사용한다. 같은 모델 인터페이스로 제공하되 불필요한 외부 실행 승인을 로컬 기록에 추가하지 않는다.

역할·현재 작업·run/engagement·호출/실행 번호·저장 위치는 런타임이 제공한다. 도구 입력의 URL/host/asset/session과 기존 argument/target binding에서 실제 대상·주체를 연결한다. 입력만으로 결정 불가한 실제 선택만 알려진 참조를 선택하게 한다. 모든 도구에 계획/target_node_id/agent_id를 공통 필수 필드로 추가하지 않는다.

공급자별 지원은 어댑터에서 실제 native 호출과 결과 메시지 왕복으로 확인한다. 일반 API 및 현재 구독 스트리밍의 각 공개 경로를 맞추며, 미지원은 명시적 진단이다. 신규 실행에서 큰 JSON 계획 루프로 조용히 fallback하지 않는다. 기존 명시 검수의 structured API는 재사용한다.

## 출처와 실행 정합성

현재 실행 원장·ActionProposal·ToolInvocation·EvidenceRecord를 유지한다. native 호출의 작업 소유권과 목적 연결은 모델 자유 입력 대신 런타임이 생성·저장하고 실제 실행과 대조한다. 새로운 출처 의미를 승인·실행·원본 metadata·기록·완료·재개·보고 모두 소비하도록 맞춘다.

native 실행은 현재 작업의 목적·역할·대상·세션·operator generation과 실행 봉투의 일치를 확인한다. 기존 저장 경로·단계가 있는 실행은 그 역사 출처를 여전히 확인하지만, 일반 native 호출의 실행 허가를 전략 경로 acceptance나 임시 단계 완료에 의존시키지 않는다.

실제 준비 입력·임무 작업 의존·실행 원장의 prerequisite/conflict·Control 예산/승인은 유지한다. 원장의 acquire가 검사하는 충돌 자료를 재사용한다. 정확한 자원 정보가 없는 도구는 병렬 안전성을 추정하지 않으며, 새 tool metadata 체계를 이번에 대신 구현하지 않는다.

공급자 오류와 이미 시작한 실제 도구의 완료/불명확 상태를 분리한다. 결과·job·원장을 회수하고 승인 재개 시 원래 호출을 사용한다. 체크포인트 재생으로 부작용 호출을 무조건 반복하지 않는다. 모델 전체 응답·private reasoning·민감 인자는 일반 checkpoint/log에 저장하지 않는다. 마스킹된 제한 메시지·원본 참조·정규화된 공개 호출 정보로 다음 판단을 구성한다.

## 주장·정산의 축소

최근 R1~R4 의미를 유지한다. 가설은 잠정으로 수용하며 관측은 실제 원본으로 등록한다. 일반 observation 결과를 모델 심사하지 않는다. 발견·목표 확정에 필요한 기존 근거 검사를 재사용하고 동일 의미의 검수를 재사용한다. 이전 결과와 목적/조건이 달라진 부분만 필요한 검토를 한다.

종료 요청/도구 없는 응답은 정산 후보이며 자동 성공이 아니다. 작업 원래 목적·임무 기준·실행 근거로 달성/미해결/실패/중단을 정리한다. 모든 계획 단계 완료를 요구하지 않으며 단계 정산을 일반 진행 조건으로 만들지 않는다. 예산이 없는 상황에도 코드로 할 수 있는 미해결 저장을 막지 않는다. 빈 원장이나 자연어 finish를 성공 증명으로 사용하지 않는다.

필요한 목적 위임은 기존 역할·작업 소유권을 재사용한다. 전문 역할 요청/반환도 native 루프를 사용하고 큰 JSON worker-next-action 루프를 최종 경로에 남기지 않는다. 팀원의 7개 역할 생성·이벤트 기동·프롬프트 로더·Atomizer는 구현하지 않는다.

## 선행 의존성과 검토

### 구현자가 공유하는 구체적인 접점

1. **LLM 경계:** 기존 structured API와 별개로 optional `complete_native(messages, tools, system, **kwargs)`를 제공한다. 반환은 `NativeCompletion(tool_calls, text, total_tokens)`, 호출은 `NativeToolCall(call_id, name, arguments, argument_error)`, 스키마는 `NativeToolSpec(name, description, input_schema)`다. malformed 인자는 해당 호출의 오류이며 원문을 진단에 복사하지 않는다. call ID가 없는 공급자 응답은 프로토콜 오류로 처리하고 임의 ID로 실행하지 않는다. 일반 API의 tool_calls와 구독 스트림의 완성 function-call 항목을 같은 계약으로 정규화한다.
2. **실행 연결:** 기존 PlanStep에 `model_tool_call_id`, `mission_task_id`, `task_purpose_hash`, `execution_origin_version`을 코드가 채운다. 기존 source type TOOL은 그대로 둔다. 새 native 출처 버전 1이 있을 때 현재 작업·목적·권한을 확인하고 path/step 필수 검사를 적용하지 않는다. 단순 path 없음은 native 실행 권한이 아니다. 호출 입력과 등록 target/argument binding에서 대상을 결정한 뒤 기존 실행 봉투·Control·원장에 연결한다.
3. **진행 상태:** 기존 GraphState에 현재 native 응답의 호출별 disposition/원래 실행 요청/완료 결과 참조를 담는 진행 자료와, 마스킹된 제한 대화 쌍을 보존한다. 별도 영구 큐가 아니다. 기존 WorkerOutcome은 call/실행 연결과 source IDs를 반환한다. call 완료 전에 다음 모델 응답으로 pending 목록을 덮어쓰지 않고, 승인 재개 입력이 이 batch 자료를 함께 복원한다.
4. **출처 소비:** `completion.py`의 기존 출처 검증에서 실제 저장된 작업·목적·호출·원장 admission과 원본 proposal/action/invocation/tool 일치를 확인하는 native 분기를 제공한다. legacy 실제 경로 분기는 유지한다. semantic_completion/result_claims/terminal/report는 같은 출처 의미를 사용한다. task/goal/unresolved helper는 `path=None`을 받아 현재 작업 목적에 근거해 정산한다. 임의 metadata marker만으로 출처를 인정하지 않는다.
5. **다음 판단의 메시지:** 마스킹된 assistant tool-call 선언과 call ID에 대응하는 도구 결과 메시지를 native 요청에 전달한다. 결과 본문은 기존 원본에서 필요한 구간으로 읽고, checkpoint에는 제한된 공개 정보와 증거/작업 참조를 남긴다. Chat Completions 메시지와 Responses input의 변환은 어댑터 책임이다. 모든 호출이 결과 또는 명시적 미실행으로 대응되지 않은 대화를 공급자에 보내지 않는다. 도구 없는 공개 응답은 종료 후보이며 자동 목표 성공이 아니다.

모델 native 반환과 코드 소유 실행/출처 연결을 먼저 합의한다. 그 계약을 기준으로 어댑터·실행·완료 소비의 독립 수정이 병렬 가능하다. 루트는 graph/engine/공유 상태·조립을 통합한다. 계획은 독립 리뷰 후 구현하며, 중요한 계약 델타는 구현 중 좁게 재검토할 수 있다.

최신 사용자 지정 모델: 새로 생성하는 조정 에이전트는 DeepSeek V4.1 Flash **high**, 구현 워커는 Muse 1.3 Contributor **high**, 독립 리뷰는 Claude Opus 4.8 **high**, 보조 리뷰는 agy Gemini 3.8 Flash **high**다. 변경 지시 전 생성된 max/xhigh 분석과 GPT-6.1 Sol high 계획 검토는 현재 작업을 마치게 하고, 결과 수령 뒤 새 작업은 새 에이전트로 요청한다. 이전 모델과 최신 지정 모델을 혼동하지 않으며, 최종 계획·의미 리뷰는 최신 리뷰 담당이 수행한다. 실제 가용성과 사용 모델을 기록한다.

## 검증과 완료 근거

기존 테스트를 우선 사용하고 변경된 목적에 맞는 기대값은 정당하게 조정한다. 새 루프를 검증하기 위해 PathPlan을 먼저 만드는 대역을 필수로 유지하지 않는다. 계획 없이 실제 도구 선택/결과 소비, 다른 유효 방법, per-call 입력 오류+형제 성공, 잠정 가설/원본 기록, 근거 없는 확정 거절, 승인 대기/원래 호출 재개, 실행 불명확·재시작을 확인한다.

필수 CI: Python 3.10/3.14 pytest, Ruff src/tests/examples/기존 지정 deploy 파일, mypy src/redteam/tests, compose config, MCP image build, 금융 MCP 5개 smoke와 실제 MCP Agent smoke(모델 대역 여부 구분). 최종 같은 소스에서 필요한 실제 모델/MCP 목표 달성과 미해결 사례를 반복하며 원본·기록·후속 판단·정산·JSON/Markdown을 대조한다. 새 정보 없는 반복은 하지 않는다. 공급자 429는 사용량 문제로 보존하고 인증 값 출력 없이 account1/2의 실행 인증을 전환할 수 있다.

완료는 실제 native 호출 루프가 기본 동작이며 경로·단계·큰 판단 명세가 일반 실행 전제에서 제거되고, 실행·출처·기록·목적 정산·복구·보고가 연결되며 필수 리뷰와 필요한 자동/라이브 검증이 통과한 상태다. 일부 어댑터 단위 통과나 체크포인트만으로 완료하지 않는다.

최종 `dev-orchestration`에 커밋·푸시, `dev` 통합 없음. 사용자 미추적 자료·팀원 파일·기존 서비스 보존. 외부 필수 검증 차단은 근거를 보고하고 목표를 완료로 선언하지 않는다.

## 대상 없는 실행의 추가 확인 — 구현 승인 전

query만 받는 공개 검색 도구는 target argument를 bind하지 않는다고 선언하지만 현재 실행 봉투가 concrete target을 필수로 요구한다. active task의 related target은 비어 있으며 material의 operator URL와 scope host 중 하나를 고르는 것은 실제 실행 대상 추측이다. 이 거절은 generic ReAct 목표에서 남은 결함이다.

다음 의미를 독립 검토한다: 실행 대상 없음과 task/목적 소유권을 구분하고, owner descriptor가 실제 target-independent 실행을 명시한 경우만 nullable action target을 허용한다. false argument-binding만으로 그런 권한을 추론할 수 있는지는 아직 미확인이다. Control의 selected descriptor 확인·outbound/ROE/risk/rate/budget/approval·원래 fingerprint를 유지한다. 관측 graph target은 만들지 않고 원본·run/task/work/call 출처와 semantic 결과만 연결하며 target-specific finding은 기존 실제 대상 근거를 요구한다.

ActionProposal/PlanStep/CandidateDraft/WorkItem와 scope/admission/verify/update/recovery/completion 소비를 실제 확인한 범위에서 맞춘다. ToolInvocation에는 이미 target 필드가 없으므로 이 계약을 바꾸지 않는다. 새 vault·queue·runtime local 실행 wrapper·가짜 host·tool-name 분기는 추가하지 않는다. targetless 계약 변경은 독립 검토 전 구현하지 않는다.

### 대상 없는 실행 독립 검토 반영

Opus4.8High 판정: 기존 binds_target_arguments=False는 인자 shaping만, target_types=[]는 모든 target kind 허용이므로 compound로 scope applicability를 추론하면 과도한 허용이다. 대안 반영: authoritative ToolDescriptor.requires_target=True 기본값의 명시 선언을 추가하고 실제 provider config 고정 검색 담당만 False로 선언한다. selected descriptor를 재조회하여 None 허용을 판정하며 legacy/unknown/True는 거절한다. ToolInvocation은 변경하지 않는다. 기존 flags와 전역 scope 규칙을 재해석하지 않는다.

책임: root core 공개 계약/registration/native binding/react 통합; Control worker scope/applicability 및 기존 재승인·outbound 유지; compiler worker PlanStep/candidate/work 의미; admission worker engine의 task/purpose/epoch/role/source; result worker 실제 raw 보존·None-safe graph/완료/복구 소비; discovery worker explicit metadata. 각자 독립 파일 소유. 영향 없는 영역 확장 안함.

결과 전달은 reference-only checkpoint 메시지를 유지하면서 모델 요청 직전에 EvidenceReader의 bounded window/provenance를 transient tool result에 복원하는 방향이다. 원본 조회 도구는 더 큰 구간이나 오래된 결과가 필요할 때 선택적으로 쓸 수 있다. 실제 모델에게 원본이 전달됨과 persistent state 무원문을 별도로 검사한다.
