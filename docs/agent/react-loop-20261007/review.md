# ReAct 전환 독립 리뷰

## 재개 델타 검토 — 2026-10-07

범위: native 호출의 미사용 입력 제거, 대상 중복 처리, 조회·정산의 안전한 진단과 명시적 시간 종료 의미 전달. 전체 미커밋 전환의 최종 승인이 아니다.

- DeepSeek 4.1 Max 소스 독립 리뷰: 해당 세 파일 델타에 필수 회귀 없음. 대상 중복을 그래프 ID로 합치면 정규화된 `/orders/1`, `/orders/2`의 실제 주소까지 합쳐질 수 있다는 우려는 대안으로 반영했다. 실제 실행 필드를 기준으로 graph_node_id만 제외해 동일 실행 대상을 합치며, 서로 다른 실제 URL이 유지되는 resolver 검사를 추가했다.
- agy Gemini 3.8 Flash High (job `1791371481335871300-877ebb05ffbe8283b57db7c5`, conversation `1c147e9e-0e07-42d0-9236-d0313009eeeb`): 종료 결과 수령. 거절 입력 제거와 대상 중복 수정에 지적 없음. 로컬 호출 소비자가 진단을 runtime에 보존하지 않는다는 지적은 반영해 safe payload와 runtime.planning_diagnostic/history에 연결했다. 명시적 지속 시간 종료가 전체 graph 소비와 저장 영수증까지 전달되는 검사를 추가했다.
- 같은 보조 리뷰의 모든 ReasoningHalted/DecisionFailure를 무조건 PLANNING_ERROR로 종료하라는 제안은 기각했다. 진단의 termination_reason은 선택 항목이며, 명시적 시간 소진만 budget_exhausted로 처리한다. 개별 검수 실패나 일반 guard 실패를 정상 예산 종료로 바꾸지 않고, 안전한 실패 영수증·다음 판단 경로를 보존한다. 목표의 부분 실패 보존과 기존 시간 의미를 유지한다.
- Opus 4.8 High 델타 재검토 및 인증 연결 설계 검토는 요청 중. 결과 수령 전 승인으로 기록하지 않는다.
- agy 동일 대화 재검토 job `1791372400995605500-f39e1d3059252c3e8c7821ba`: done 결과 수령. 정산 담당의 이벤트 발행/legacy 중복 제거와 context/result 커서 전달, 로컬 진단 보존·명시적 시간 종료/개별 실패 구분, 구체 실행 주소 기준 중복 제거를 건전으로 판정했다. 전체 ReAct 전환·라이브·G1 인증 비교 완료 승인으로 확대하지 않는다.
- DeepSeek 4.1 Max G2/세션 연결 재검토 수령: 정산 담당·커서·구체 주소 대조에 필수 회귀 없음. collector의 self session alias와 principal/credential 등록 중복 지적은 등록 alias 우선의 대안으로 반영했고, 재검토는 기존 gate와 일치·권한 확대 없음으로 판정했다.
- Opus 4.8 High 델타 결과 수령: 진단·미사용 입력 제거 방향 승인. 대상 설명은 이전 node_id 기반 스냅샷을 인용하므로 최종 구체 주소 key 구현의 승인 근거로 사용하지 않는다(최종 key는 DeepSeek/agy 재검토와 직접 검사로 확인). 유효한 실행 봉투의 민감 입력은 별도 미해결이라고 명시했다.
- G1 선택적 저장 관측 비교 도구 방향은 Opus/DeepSeek 모두 책임 경계에 부합한다고 조언했다. 인증 의미는 authentication 담당의 저장 관측 seam으로 통합하고, 최종 finding은 graph.record를 사용한다. 구현 전에 graph.record 승격이 legacy authentication work marker 없이 실제 원본을 소비할 수 있는지 확인해야 한다. 이 소비 접점은 아직 미확인이다.
- 해당 소비 접점은 루트 소스 확인 완료: graph_recording._promote_finding은 실제 source 원본/target 관계·posthoc review 후 자체 security_check PromotionRequest를 만들어 승격한다. legacy 실행의 authentication marker를 필수로 요구하지 않는다. 실제 인증 통합 실행은 새 담당이 구현 중이며 소스 확인을 실행 통과로 표현하지 않는다.
- durable_resume/handoff 이관을 Opus 4.8 High 및 DeepSeek 4.1 Max가 별도 검토했다. 두 결과 모두 native 원래 호출·work·실행 증거의 fresh-store 연결과 별도 legacy PathPlan fixture, 실제 관측된 pinned 주소를 사용하는 handoff가 의도에 맞고 단언 약화가 아니라고 판정했다. 대역 내부 assertion은 provider failure로 변환될 수 있지만 최종 gateway/evidence 단언이 실패를 계속 잡는 현재 검사는 유지한다.
- Opus 4.8 High 신규 authentication.assess 실제 코드 검토: shared comparison 의미 소유자와 선택적 등록·raw/proposal 재구성·run/engagement/origin 검사·관측 반환에 필수 결함 없음. 표시 path가 derived summary에서 온다는 작은 지적은 반영해 보존 proposal.arguments에서 읽는다. 비교 결과는 최종 finding이 아니며 실행·테스트 통과로 확대하지 않는다. DeepSeek 구현 재검토 및 단위/통합 검사는 진행 중이다.
- DeepSeek 4.1 Max 신규 인증 도구 구현 검토 수령: raw/proposal 양쪽 재구성과 원본 연결·목표 연결에 필수 결함 없음. NetworkIdentity는 scheme을 담지 않는 기존 한계가 있어 pinned URL 동일 host/port의 scheme 차이까지 비교하지 않는 점을 후속으로 기록한다. current가 비HTTP이면 inconclusive로 보존하는 제안은 현재 기존 의미를 유지한다(취약점 확정 없음). 인증 비교 이벤트를 observation으로 표시하는 것은 의도적인 계약이며, 실제 security claim은 graph.record에서 분리하므로 legacy 실행 주석을 일반 호출에 복원하지 않는다.

최근 관련 검사: native_react_loop + native_local_tools 23 passed. 변경된 소비자의 실제 graph 실행·저장 확인을 포함하며, 전체 suite·실제 모델/MCP 라이브 검증을 대신하지 않는다.

## 계획 검토 — 2026-10-07

변경 전 요청 GPT-6.1 Sol High의 독립 정적 검토는 방향을 승인하고 P1 한 건을 제시했다. native 한 응답의 모든 호출·결과·승인 형제를 보존하지 않으면 기존 배치 선택·집계에서 미실행 호출이 사라질 수 있다는 지적이다. 계획의 호출별 처리 상태·대기·형제 성공·원래 호출 재개 원칙에 반영했다.

최신 사용자 지정 Claude Opus 4.8 High는 수정 계획과 현재 LLM/engine/PlanStep/Control/출처 소비를 대조해 **승인, 필수 잔여 없음**으로 판정했다. 일반 호출의 경로·단계 승인과 큰 TurnDecision을 제거하며 실행·원본·작업 목적의 정합성을 보존하는 방향을 승인했다. native 출처를 metadata marker만으로 수용하지 않고 실제 저장 원장·작업과 확인할 것, 위험 영향 신호와 다중 호출 재개를 보존할 것은 계획에 이미 있는 구현 확인 항목이다.

보조 agy Gemini 3.8 Flash High도 수령했다(`done`). 구체적인 모델 메서드·출처 분기·호출별 GraphState·실행 변환 진입점·다음 턴 대화 연결을 계획에 명시하라는 의견을 반영했다. 다중 호출 채널이 전혀 없다는 표현은 현재 dispatch/WorkerOutcome을 무시한 과장으로 채택하지 않았다. 기존 기반을 확장한다.

계획에는 NativeToolSpec/NativeToolCall/NativeCompletion와 complete_native, 코드 소유 PlanStep의 native 출처 필드, GraphState의 pending call disposition/제한 대화 쌍, 저장 Work와 taskpurpose 기반 출처 확인, 공급자 메시지 변환을 구체화했다.

판정: **승인된 범위의 구현 착수.** 이 판정은 구현 코드·자동 테스트·실제 실행 검증의 승인이 아니다. 구현 의미와 소비 경로에 대한 독립 리뷰는 제품 변경 뒤 수행한다.

## HTTP 원본 관계 소비 델타

Opus4.8High 독립 소스 리뷰 수령: HostTarget에 한정한 HOSTS→endpoint 관계는 실제 성공 요청/valid HTTP response에 근거하고, 403/404도 접근 성공과 별개인 리소스 관측이다. update의 measured evidence별 provenance와 claim_target_is_grounded의 relation snapshot 소비가 일치한다. 필수 문제 없음. endpoint→endpoint HOSTS는 인접 검사에서 발견해 제거했다. 일반 도구 이름 분기나 새 모델 검수 의무는 없다.

후속 의견: explicit graph_node_id의 실제 타입 확인, URL/endpoint baseline의 관계 지원은 이번 호스트 원본 결함 해결에 필수는 아니므로 현재 구현 범위에 추가하지 않았다. 다른 baseline 소비 문제를 확인하면 실제 관계 의미로 처리하며 HOSTS를 무차별 확대하지 않는다. 이 판정은 전체 변경·자동 검사·라이브 최종 승인이 아니다.

## 인증 공개 상태코드 델타

Opus4.8High 소스 독립 리뷰 필수 문제 없음. response_status_code는 AuthenticationAssessment의 int|None 값만 이전하며 shared secret redactor를 그대로 적용한다. global session allowlist를 느슨하게 하지 않는다. owner/legacy 계약은 유지되고 현행 소비자는 outcome을 사용한다. legacy session_status와 native public 명명 차이는 기록한 후속 사항이며 신규 소비 추가 시 함께 정렬할 수 있다. 실제 관련19 passed와 독립 소스 검토를 구분한다.

## 재개 후 최신 targetless·job 델타 리뷰

Opus4.8High는 현재 policy의 None 표시 가드, react의 기존 has_ambiguous_execution 재사용, job.reconcile의 실행별 격리·원본 실행 연결·예산·인과 전파·반복 회수 가드를 읽기 전용으로 확인했다. 정상 흐름에 도달하는 필수 문제나 새 모델 의무는 발견하지 않았다. 이전 미기록/import 불가 평가는 과거 스냅샷에 관한 것으로 철회했다. 이 판정은 정적 소스 리뷰이며 라이브나 전체 검사 통과를 뜻하지 않는다.

반복 회수 중복은 정상 정산된 작업에 대해 already_collected 가드로 반영했다. 원장 작업이 별도 경로에서 이미 성공/취소됐지만 백엔드의 종단 상태와 충돌하여 result_collected_ledger_unsettled가 된 경우에는 재호출 시 결과 처리 증거가 중복 생성될 수 있다는 의견이 있었다. 실제 재현 없이 기존 상태 전이에서 추론한 좁은 비정상 경계이며 후속 과제로 판단했다. 이 의견만으로 새 저장 상태나 마커를 추가하지 않는다.

## 최종 감사 보완과 요구 귀속 — 2026-10-08

기본 설정을 상속한 독립 감사에서 대상 없는 승인·재개 조합의 명시 실행 근거가 없다는 지적을 반영했다. test_target_free_approval_resume_retains_input_and_rechecks_descriptor의 두 경우는 원래 None 대상·입력·action을 보존해 승인 뒤 한 번 실행하고, 현재 등록 descriptor가 requires_target=True로 바뀌면 승인 뒤에도 실행을 거절한다. 관련 파일5 passed와 Ruff 통과. 제품 코드나 승인 조건 추가 없이 실행 근거를 보완했다.

동일 감사의 활성 Tavily 양성 성공을 필수 완료 조건으로 추가하는 해석은 목표 원문과 대조해 기각했고, 감사자도 정정했다. 원문은 query-only 실제 native 실행·원본·소비·정산과 근거 있는 달성 또는 미해결, 일부 실제 등록 도구의 라이브를 요구하며 특정 공급자의 양성 성공을 별도 의무로 명시하지 않는다. 실제 모델+recon의 달성 사슬과 실제 모델+production web-search의 미해결 사슬을 각각 확인했다. 활성 Tavily 성공은 인증 부재로 미검증인 한계이며 성공했다고 표현하지 않는다. 최종 전체 CI는 아직 진행 중이므로 이 감사만으로 전체 완료를 선언하지 않는다.

finance_agent_smoke native 이관과 pinned origin 별칭은 별도 독립 읽기 전용 검토에서 필수 문제 없음으로 판정했다. 실제 카탈로그 ID 주입, 기존 승인·4개 도구·역할 카운터·범위·실패 이벤트 단언을 유지한다. scripted 모델과 실제 컨테이너/MCP 검증을 실 모델 라이브와 구분한다.

## target-independent 계획 독립 검토

Opus4.8High 실제 source 검토: target_types=[]는 any compatible target이고 binds_target_arguments=False는 입력 shaping만 뜻한다. 이를 scope not-applicable 권한으로 조합하는 제안은 fail-open이라 기각. 새 공통 의미가 필요한 근거로 explicit provider requires_target=True(default)/False 선언으로 대안 반영. authoritative selected descriptor가 선언한 경우만 scope address applicability 분기하며 unknown/legacy/True None은 deny. outbound/ROE/risk/rate/budget/approval과 source/task 목적 검사 유지. optional fingerprint null 정리는 필수 아님으로 기각(기존 None 문자열도 stable). ToolInvocation에는 target 없어 변경하지 않는다. 일부 실제 engine/None 소비는 구현하면서 재검토 필요. 이 판정은 계획 scope 검토이며 구현 승인·검증 통과 아님.
