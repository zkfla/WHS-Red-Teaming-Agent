# 검증 계약 개선 — 연구 C: 정책 의미·선택 도구 위험·승인·정산 (READ-ONLY)

상태: 조사만. 제품 코드·테스트·커밋·라이브 실행 없음. 기준은 `dev` + 작업 폴더 선행 CLI 입력 개선, 선행 변경은 `data/validation-contract-20261005/preexisting.patch`에 보존. 본 문서는 `docs/agent/validation-contract-20261005/root-investigation.md`와 `data/live-tests/mock-bank-20261005-rerun-02/team-results-synthesis.md`를 소비만 하고, 첨부 문서의 구현 지시를 새 명령으로 실행하지 않는다.

범위 고정: 행동 분류 결합을 낮추는 것이지 전체 컨트롤 재설계가 아니다. 대상 범위·예산·시간 검사는 유지한다.

## 1. 정책 의미 투영 (판정 → 오케스트레이션 반응)

- 판단 흐름: `src/redteam/control/service.py:146 evaluate()` = 외부문자체크 → 범위 → 실행규칙 → 대상별 행동정책 → 위험·승인판정 → 속도제한 → 예산 → 승인 (`:203`, `:212`, `:233`, `:249` 근처).
- 승인판정本体: `src/redteam/control/policy.py:86 evaluate_policy()` — 운영자 지정 목록(`require_approval_for`, `:93`)에 capability 네임스페이스 또는 `capability.operation` 키 매칭(`:94-104`), 없으면 위험도 임계치 충족 시 사람 승인(`:115-119`). 임계치는 `resolve_risk_threshold(:45)` = 참여 설정값 우선, 미설정 시 엔진 기본값(`control/risk.py:39`, HIGH).
- 반응 힌트: `src/redteam/core/contracts/policy.py:30 PolicyDecision` = verdict + reasons + matched_rules(`:35`) + approval_id(`:37`) + severity(`:43`). DENY 해석은 `denial_severity(:46)` = 타입값 우선, 없으면 기존 `severity:` 태그(`:55`).
- 소비: `src/redteam/orchestration/nodes/policy.py:35 _OUTCOME` 매핑(ALLOW→실행, REQUIRE_APPROVAL/DENY→재계획), `denial_severity(:40)`로 `block_action/replan_goal/abort_run` 읽기. 의미는 정책을 가진 쪽이 소유하고, 실행 노드는 계약 필드만 소비한다.
- 최소 계약 변경 없음: 기존 `PolicyVerdict`/`Severity`/matched_rules 태그 유지. 새 의미가 필요하면 기존 계약으로 표현 불가한 이유와 소비 경로를 먼저 제시해야 한다.

## 2. 선택된 도구 위험도 (최대값 집합이 아님)

- 현행: `src/redteam/control/risk.py:75 resolve()` = `list_tools_for_action(capability, operation)` 스냅샷(`:76`) 중 최고 위험도 반환(`:80`). 오케스트레이션이 ALLOW 후 후보 중 아무거나 고를 수 있다는 전제라 보수적 최대값이다.
- 문제: 이러면 실제 고른 도구가 가벼워도 무거운 후보 때문에 승인에 걸린다. 반대로 가벼운 후보만 보고 ALLOW하면 실제 무거운 도구가 나갈 수 있다.
- 이미 있는 선택 도구 통로: 실행·정책 경로가 `ControlContext.selected_tool_id`(`core/contracts/policy.py:27`)를 채운다 — 루트 정책·실행 경로(`orchestration/engine.py:2059-2062`), 실행 노드(`orchestration/nodes/execute.py:65`), 정책 노드(`orchestration/nodes/policy.py:166`, plan_step.tool_id).
- 최소 연결안: 위험 해소는 선택 도구 기술서(`core/contracts/tool.py:19 ToolDescriptor`, `risk_level :33`)를 우선 참조하고, 선택값이 없을 때만 기존 최대값으로 폴백. 컨트롤 안에 모델·도구별 하드코딩 표를 복제하지 않는다. 해소 실패(예외)는 `service.py:1120 _resolve_risk` → `(None, True)` 로 escalate, `evaluate :212`에서 ALLOW를 사람 승인으로 승격(DENY 아님). 미확인 위험 자동승인 금지는 `policy.py:72 may_auto_approve` 유지.
- 조립: `src/redteam/app/bootstrap.py:354 risk_resolver=RegistryRiskResolver(discovered_catalog)` 그대로 재사용. 선택 도구 조회가 필요하면 외부문자체크용으로 이미 있는 기술서 조회(`:355 outbound_descriptor_resolver=(tool_catalog or discovered_catalog).get`, `service.py:1110 _resolve_outbound_descriptor`)와 같은 형태를 쓴다.

## 3. 명령 위험 패턴 → 사람 승인 (범위·예산·시간 유지)

- 현존 확인: 명령어 위험 패턴 전용 설정 키·기본값은 못 찾음(미확인). 있는 것은 외부문자 게이트뿐: 참여 설정 `ExternalTextPolicy.prohibited_patterns`(`core/contracts/engagement.py:47-50`) + 도구 선언 `outbound_text_fields`(`core/contracts/tool.py:41`) + 공유 좁은 검사(`core/outbound_text.py:70 check_outbound_text`, `:85 check_outbound_arguments`, 리터럴 대소문자무시 매칭 `:54`, 정규식 아님) + 컨트롤 어댑터(`control/outbound.py:49 evaluate_with_descriptor`, 미선언 기술서=None→통과 `:64`, 선언 빈목록→검사없음).
- 2차 참여 조건(`mock-bank-20261005-rerun-02/engagement.yaml`)은 `prohibited_patterns: []`, 승인정책 전부 비어있음. 즉 이번 재현 실패는 위험패턴 게이트와 무관하다.
- 최소 연결안(재설계 아님): 새 패턴 저장소·새 중앙계층을 만들지 말고, 기존 `prohibited_patterns` + 선택 도구 선언 필드 방식을 명령 인자 범위로 확장하거나, 승인정책의 기존 `require_approval_for` + 위험임계치(`ApprovalPolicy :84-102`)로 흡수. 판단은 정책을 가진 쪽에 두고 조립 지점에서 설정·주입만 연결.
- 유지 의무: 승인 후에도 범위·시간은 재확인한다 — `admit_invocation(:902)` → `_current_admission_check(:700)` = receipt 상관관계만 보고 scope(`:check_scope`), ROE 시간(`_recheck_time_gates :661`), 대상정책, 외부문자, duration을 새로 검사. `approval_outcome(:371)`은 Scope/ROE/Policy를 다시 돌리지 않고(동일 제안·동일 설정이라 `:결정적` 주석) 속도·예산만 재확인 후 1회 과금. 재시도 추가호출은 `reserve_extra_tool_call(:947)`로 1회씩만 과금, 소진키 중복 방지. 이 경계를 깨는 설계는 금지.

## 4. 승인 저장·재개 (기존 것 재사용)

- 저장: `control/approval.py ApprovalService` = request(PENDING 생성)/resolve(APPROVED·REJECTED만, EXPIRED는 sweep)/get/list_pending. 영속은 조립에서 주입: `bootstrap.py:344-345 AuditLog(FilesystemAuditStore)`, `ApprovalService(FilesystemApprovalStore)`; 인메모리 기본값은 기존 호출자 호환용.
- 대기 연결(프로세스 내): `evaluate` 승인 필요 시 Approval 생성 + `_pending_proposals[approval_id]=proposal` 저장, 속도·예산 미과금(`service.py :276-278` 주석). `resolve_approval(:301)`은 사람 결정 기록 + 감사 `APPROVAL_RESOLVED`만 남기고 링크를 지우지 않아 `approval_outcome`이 이어서 처리.
- 재개: 정책 노드 `nodes/policy.py:129 resuming_approval = pending_approval_id is not None and pending_action_id == proposal.action_id` → 참이면 새 판정 없이 `approval_outcome(:136)` 호출. 실패·대기중이면 ALLOW를 위조하지 않고 동일 REQUIRE_APPROVAL 형상으로 복귀. 해소되면 마커 정리(`:211-222`).
- 영속 재개: 새 프로세스는 `_pending_proposals`가 비어 있으므로 `register_pending_proposal(:327)`로 체크포인트 제안서를 원본 그대로 다시 연결. 감사 스냅샷(편집·손실 가능)에서 역추측 금지, action_id 불일치·미등록 참여·충돌 링크는 raise. 연결 없으면 stale DENY(`approval_outcome` 내 ABORT_RUN급) — fail-closed. `approval_outcome`은 `_resolved_outcomes` 캐시로 멱등.
- 관련 기존 테스트(실행 안 함, 최종 단계 제안용): `tests/integration/test_approval_resume.py`, `tests/unit/test_approval_outcome.py`, `tests/unit/test_approval.py`.

## 5. 메타데이터·설정 패턴은 기존 확장 조합으로

- 선언: `core/extensions.py:17 ENTRY_POINT_GROUP`, `Contribution(:37)`(kind+id+version+factory+requires+meaningful_change_keys+target_ids), `ExtensionDeclaration(:50)`, `redteam_extensions(:58)` 훅. 수집은 `app/extensions.py collect_extensions` = 메타데이터 먼저 보고 선택된 것만 로드.
- 연결: `app/runtime_components.py:214 apply_extensions` = `available_references` 구성 → `validate_references(:240)` → `validate_kinds(:241)` → 기존 레지스트리에 부분 기여 연결. 허용 kind 목록에 `capability.action_binding`·`execution_recipe`·`result_mapper`·`eligibility_provider` 등이 이미 있음. `binding_for_resume`은 저장된 ids·버전 불일치 시 폴백 없이 에러.
- 함의: 명령 위험 패턴·도구 선언(`outbound_text_fields`, `risk_level`, `argument_bindings`) 같은 제공자 메타는 새 중앙 허용목록으로 복제하지 말고 기존 선언·매니페스트·등록 경로(`capability/registration.py:484,519`의 필드 선언 예시, `bootstrap._build_default_capability` 발견 카탈로그)로 넣는다. 중앙이 알아야 할 것은 공통 계약·실행 흐름뿐이다.

## 6. 막힘·미해결 타입과 보고·종료 정산 (증거없음=미확인, 반박아님)

- 완료 검사: `orchestration/completion.py:30 _VALID_OUTCOMES={success,refuted,inconclusive,blocked}`, `:1087` outcome 화이트리스트, `:1122 inconclusive/blocked→성공아님 거절`. `:1551-1555` 반박 완료는 refuted/rejected 상태 요구(팀 BOLA 보고서의 `completion_reference_hypothesis_reference_is_not_refuted_49`가 바로 이 거절). `:1617-1623,1716-1747` refuted는 조사 결과이지 PoC 성공 아님. 증거연결은 `check_record_linkage(:386)` = action+invocation+tool+제안보존원본 일치, 첫 실패에서 중단.
- 보고 투영(읽기전용): `orchestration/reporting.py:231 ReportingAgent.project`는 터미널 런타임에서만(`:243` 아니면 raise). `TerminalReportProjection(:195)` = findings/evidence_ids는 현재 run+engagement 교집합만, `integrity_blockers(:211)`, `unresolved_hypotheses(:212)` + 필수/유예 분리(`required_unresolved_hypotheses/deferred_hypotheses :216-217`, 합산이 원본과 일치, additive only). `ready_for_release(:336)` = FINISHED + 해제허용 종료사유 + integrity 비차단 + 필수 미해결 없음 + 정리완료 + (미션 있으면 COMPLETED·기준충족·필수태스크완료). 고갈된 후보(all depleted)는 성공 증거가 아니다.
- 원칙: 증거 없음 → 확정 findings 0, 미확인은 미확인 유지. 불확정을 반박·성공으로 승격하는 예외 금지. 부분 상태는 `work_status_counts`, 미션 `partial/blocked`(`_build_workstream`), `blocked_reason`, 커버리지(`coverage_status unknown→ok`)로 보존. 컨트롤의 DENY/REQUIRE_APPROVAL도 `EvidenceService.record_blocked_action(evidence/service.py:63)`로 남기되(`service.py:1149 _record_blocked_action` best-effort, 증거 실패가 판정을 바꾸지 않음) 실행 증거와 혼동하지 않는다.
- 라이브 종합과 정합(`team-results-synthesis.md §2-4`): 팀원 BOLA 건은 세션 비교 2행동·증거 13건이나 가설 inconclusive + 반박기준 불일치로 막힘 — 형식 오류가 아니라 의미 정합성 거절. 세션 참조는 원본 실행결과에 남기고(토큰값 기록금지) 실제사용세션 확인과 세션-계정 연결확인을 분리한다. 별도 보고서의 `run_432dd512b678` 익명/인증 비교 제안은 원본 결과·증거 메타 없이 확정 불가(루트조사 미확인 항목).

## 7. 제공자 오류는 모델 어댑터에서 정규화, 기존 진단으로 전달

- 어댑터: `llm/litellm.py:253 complete_structured_with_usage` → `StructuredCompletion(ports/llm.py:값+total_tokens)`. 구독 스트리밍은 미완료·실패·중단·무텍스트를 `RuntimeError`로만 올림(`:378,381,392,395,402,404`). 모델명별 분기 금지 — chatgpt 분기는 제공자 판별(`litellm.get_llm_provider`, `:190`) + Responses API 브리지 회피라는 고정 규격 사유로 어댑터 안에 둔다. 사용량은 `_extract_total_tokens(:152)` = 정수≥0만 인정, 나머지는 None(unknown, 0 아님).
- 인지 클라이언트: `orchestration/cognition.py:384-424 _attempt_once` = metered 우선·레거시는 usage unknown(`add_usage(None)`)·예외는 `llm_transport_failed/kind=transport/retryable` 진단으로만 정규화. 스키마·JSON 오류는 format계. 예산 소진은 `DecisionFailure(kind=budget)`(`:461,484,497`), 빈 결과 반환 금지. 토큰가드는 `token_halt_required(:185)` = 캡있고 unknown이면 halt.
- 진단: `orchestration/diagnostics.py:76 DecisionDiagnostic`(code/stage/kind/location/summary/retryable, `:65 safe_feedback` 500자·민감정보 마스킹 후 절단). `diagnostic_for_exception(:210)`·`schema_diagnostic(:179)`는 규칙명·알려진 필드 위치만 전달, 원문·입력·모델응답 복사 금지. 팀 종합 §1과 정합: 팀원 6로그는 `llm_transport_failed`+한도소진까지만 확인되고 HTTP상태·예외본문이 없어 인증·한도·모델명·네트워크를 확정할 수 없음. 우리 1차의 429를 소급 적용하지 않는다. 모델 문자열 대소문자(`chatgpt/gpt-6-LUNA` vs `chatgpt/gpt-6-luna`)는 확인할 입력 차이로만 남기고, 중앙에서 임의 소문자화하는 해결은 제안하지 않는다.
- 비밀·예외 규율: `redact_secret_material`·`redact_sensitive_text` 기존 마스킹 재사용, 내부 추론 전체·민감 입력 기록 금지. 모델 예외 타입별 분기를 중앙 판정 조건으로 추가 금지.

## 8. 최소 계약·설정과 최종단계 테스트 제안 (실행 안 함)

- 계약(신규 타입 없음): `ControlContext.selected_tool_id` 소비, `ToolDescriptor.risk_level/outbound_text_fields` 선언 우선, `PolicyDecision(severity+matched_rules)` 투영, `Approval(ApprovalId/action_id/status)` 영속, `TerminalReportProjection` 필수/유예 분리, `DecisionDiagnostic/DecisionBudget` 그대로.
- 설정(신규 중앙키 없음): `ApprovalPolicy(require_approval_for/risk_threshold/auto_approve_up_to)`, `ExternalTextPolicy.prohibited_patterns`, `ExecutionBudget/RateLimitPolicy` 기존 키 + 확장 선언( kind `capability.*`, `vendor_policy` 등)으로 제공자 메타 확장. 명령 패턴이 필요해도 기존 패턴 키 확장 또는 승인목록+임계치 조합을 우선 검토.
- 테스트 제안(최종 단계, 지금 실행 금지): 승인정책 임계치·선택도구 우선·미해결위험 승격 단위검사; `register_pending_proposal` 충돌·stale DENY·`approval_outcome` 멱등·재개 경로 단위/통합; 선택 기술서 좁히기(선언빈목록=무검사·미선언=기존 미지원진단) 단위; 완료 outcome 분리(inconclusive/blocked≠성공, refuted≠PoC) 단위; 보고 투영(증거교집합·필수/유예·release 조건·부분보존) 단위; 진단 마스킹·usage-unknown halt 단위. 관련 전체·라이브·네트워크 검사는 제외. 기존 `test_approval_resume`, `test_approval_outcome`, `test_blocked_action_wiring`, `test_budget` 재사용 우선.

## 9. 미확인·후속

- 명령 위험 패턴 설정 키 존재 여부: 못 찾음 → 미확인. 있으면 키·기본값·승인훅 확장점을 그때 확정.
- 자연문 판정값 공개 통과 경로·완료계약 단일화 시 검사 유지/이동 항목: 워커 B 결과로 확정(본 조사 범위 밖).
- 컴파일러 재탐색 지점·도구별 바인딩 소비 전체: 워커 A 결과로 확정(본 조사 범위 밖).
- 팀원 전송 실패의 실제 제공자 원인·과거 실행 세션 연결: 원본 로그·메타 부재로 미확인 유지.

---
검증 구분: 위는 코드·문서 읽기만. 자동 테스트·실행 검증 없음. 제품 코드 수정·커밋·푸시 없음.
## 10. 보충 정정: 명령 패턴 분리·선택 기술서 직참조·승인 결론 동일성 (READ-ONLY)

아래는 본 문서 §2·§3·§4의 정정이다. §2의 {선택값 없으면 기존 최대값 폴백}과 §3의 {기존 외부문자 패턴 틀 확장안}은 폐기한다. 제품 코드·테스트 수정 없음.

### 10.1 명령 위험 패턴은 외부문자 패턴을 재사용하지 않는다 (확정)

- 의미·소비자가 다르다. 외부문자 쪽은 전송 차단(DENY)이며, 소비자는 판정 앞단·입장 재확인·능력망 사전검사다. 원문 미보관·안전 사유코드만 남긴다. 명령 위험 패턴은 사람 승인(REQUIRE_APPROVAL)이며, 소비자는 승인 판정·승인 저장과 재개·자동승인 상한이다. 같은 목록을 공유하면 기존 참여 조건의 차단 의미가 승인 의미로 오염되고, 반대로 승인용 패턴이 전송 차단으로 오작동한다.
- 최소 분리안(새 계층·새 저장소 없음): 제공자 기술서(도구 설명 계약)에 명령어 텍스트 필드 선언 1개를 추가한다. 기존 외부문자 필드와 평행한 선언이며 기본값은 빈 목록이다. 값은 등록·매니페스트 경로(발견 바인딩·실행 매니페스트 검증과 같은 자리)에서 채우고, 중앙 코드에 도구 이름·명령어 문자열을 하드코딩하지 않는다. 명령 실행면의 실제 인자는 실행파일 1개와 인자 배열(최대 64개)이며, 이 선언이 가리키는 범위에만 매칭한다.
- 참여 승인 정책(승인 정책 계약)에 연산자 패턴 목록 1개를 별도로 둔다. 매칭 의미는 기존 리터럴 대소문자무시 부분문자열(정규식 금지, 기존 공유 좁은 검사와 동일 근거)을 재사용하되 저장소는 분리한다. 사유에는 원문 명령어를 싣지 않고 안전 코드만 남긴다(외부문자 쪽과 동일 규율).
- 사람필수 깃발: 패턴 매칭 시 위험도와 무관하게 사람 승인이며, 기존 상한 이하 자동승인 판정을 우회한다. 현재 상한 판정은 알려진 위험도만 상한 이하일 때 자동승인을 허락하고 미상은 항상 사람이 본다. 명령 매칭은 여기에 매달리지 않고 별도 표시(판정 matched_rules 태그 1개)로 저장 단계에서 상한 검사를 건너뛴다. 상한 판정 함수 자체는 손대지 않는다.
- 변경 경계(구현 시, 지금은 변경 없음): 참여 계약의 승인 정책 필드 1개, 도구 설명 계약의 명령어 필드 선언 1개, 승인 판정 분기 1개와 사람필수 표시, 판단 서비스의 선택 기술서 조회 재사용(외부문자 조회와 같은 형태)과 상한 우회 1조건, 등록·매니페스트의 선언값 기입과 검증. 설정 스키마는 승인 정책을 그대로 포함하므로 추가 키 없이 통과한다.

### 10.2 선택 도구는 제안서 필드가 아니라 기존 전달 수단으로, 미상은 최대값 폴백 금지

- 정정: 제안서 계약(행동 제안 모델)에는 도구 식별자 필드가 없다. {제안서의 도구 식별자}라는 표현은 현 계약에 존재하지 않는다. 실제 선택 도구는 계획 단계의 도구 식별자(계획 단계 모델) → 판단 맥락의 선택 도구 칸(판정 맥락 계약) → 실행 시 호출 생성(실행 노드, 단계의 도구 식별자 그대로)으로 흐른다. 정책 사전검사·정책 노드·실행 노드의 호출 맥락이 모두 이 통로를 쓴다. 그러므로 제안서 계약에 새 필드를 만들지 말고 기존 전달 수단을 정본으로 쓴다.
- 위험 해소는 선택 기술서의 위험도를 직접 참조한다. 기술서 미상(조회 None, 바인딩 없음)이면 기존 행동분류 최대값(해당 분류·동작의 등록 도구 중 최고값)으로 폴백하지 않는다. 폴백은 행동분류 결합을 되살리고, 가벼운 실제 도구를 무겁게 막거나 무거운 실제 도구를 가볍게 통과시킨다. 미상은 기존 미확인 위험 경로로: 허용 결정을 사람 승인으로 승격하고(거부 아님), 자동승인 판정은 미상을 항상 불허하므로 자동 통과가 없다.
- 외부문자 쪽의 미상 통과(기술서 None이면 이 게이트에서 막지 않고 미지원 진단에 위임)와 혼동하지 않는다. 전송 검사의 {모르면 여기서 막지 않음}과 승인 판단의 {모르면 사람이 본다}는 서로 다른 fail 방향이며 각자 유지한다.

### 10.3 승인 결론의 동일 전제와 스킵 경계 (일반 허용·거부는 스킵, 명시 제약은 유지)

- 현행: 승인 결론(승인 저장소 소비 쪽)은 보관된 제안서를 꺼내 속도·예산만 재확인한다. 범위·실행규칙·승인판정 재실행을 건너뛰는 근거는 {동일한 불변 제안서와 동일 참여 설정에 대해 결정적}이라는 주석과 포트 문서의 {Scope·ROE·Policy 재실행 안 함, 변할 수 있는 속도·예산만 재확인}이다. 두 번째 승인이 생기는 재귀를 막기 위한 것이다.
- 동일성 검사의 실제 위치: 등록 시 전체 동등성(기존 linked와 다르면 raise), 재개 진입 시 행동 식별자 일치, 정책·실행 직전 현행 검사(제안서 대 단계 필드별 비교: 실행·참여·분류·동작·대상·인자·가용영향, plus 경로·위임·운영자 epoch·도구 바인딩·스키마·적격성). 인자 digest 단독 비교는 없으며, 전체 모델 동등성과 필드별 비교가 동일성 수단이다. digest 헬퍼(기계 변환 모듈의 digest, 엔진의 content_hash, 실행 영수증 digest)는 존재하지만 제안서 동일성에는 미사용한다.
- 확인된 구멍: 승인 결론 호출은 승인 식별자만 받고 현행 제안서·단계를 받지 않는다. 보관본과 현행본이 어긋나도(같은 행동 식별자, 다른 인자) 결론은 보관본 기준으로 허용을 만들 수 있다. 이후 실행 노드가 현행 제안서로 호출을 만들고 입장 허가가 현행본에 대해 명시 제약을 재확인하므로 완전 우회는 아니지만, {사람이 본 내용}과 {실행되는 내용}이 달라질 수 있다. 미확인이 아니라 코드상 확인된 구조다.
- 정정안(최소, 새 저장소·새 동등성 함수 없음): 승인 결론에 현행 제안서(또는 그 digest)를 함께 받아 보관본과 비교하고, 불일치 시 stale 거부로 닫는다(기존 stale 경로와 동일 취급). digest는 기존 헬퍼를 재사용한다. 비교 범위는 사람 승인 의미를 바꾸는 필드(분류·동작·대상·인자·가용영향·선택 도구)이며, 실행·참여·행동 식별자는 기존 검사로 충분하다. 멱등 캐시는 승인 식별자 단독 키에 digest를 함께 묶어, 다른 내용의 두 번째 호출이 캐시된 허용을 받지 못하게 한다.
- 스킵 경계 유지: 일반 행동 기반 허용·거부(허용 분류·동작 목록, 승인 요구 목록, 위험도)는 재실행 스킵을 유지한다. 재실행하면 두 번째 승인이 생긴다. 명시 제약은 입장 허가가 현행 제안서에 대해 이미 재확인한다(범위 철회, 시간문·병렬·가용 재검사, 대상별 행동정책 거부, 외부문자 차단, duration 소진). 승인 결론에 중복 추가하지 않는다. 단 승인 결론의 허용은 입장 허가와 함께 소비해야 하므로, 계약 문서(포트 독스트링)에 {승인 결론 ALLOW 단독 실행 금지, 입장 허가와 함께 소비}를 명시한다. 운영자 제외(엔드포인트·영역·작업 억제, epoch)는 오케스트레이션 현행 검사가 담당하므로 컨트롤로 이관하지 않는다.
- 변경 경계(구현 시, 지금은 변경 없음): 포트 계약의 승인 결론 시그니처에 현행 제안서·digest와 독스트링 1단락, 판단 서비스의 보관본 대 현행본 비교 1조건과 stale 거부, 정책 노드 재개 호출부의 현행본 전달. digest·동등성은 기존 헬퍼·기존 전체 동등성과 현행 검사를 조합하고, 부분 필드 전용 새 비교 함수는 만들지 않는다.

### 10.4 정밀 변경 경계표와 최종단계 테스트 제안 (실행 안 함)

- 참여 승인 정책 +1 필드(연산자 명령 패턴 목록) / 도구 설명 계약 +1 선언(명령어 텍스트 필드) / 승인 판정 +1 분기와 사람필수 표시 1개 / 판단 서비스의 선택 기술서 직접 조회와 상한 우회 1조건과 보관·현행 비교 1조건 / 포트 승인 결론 시그니처와 독스트링 1단락 / 정책 노드 재개 전달 1줄 / 등록·실행 매니페스트 선언 기입과 검증. 중앙 이름별 분기·별도 상태·별도 저장소·신규 추상화 없음.
- 테스트 제안(최종 단계, 지금 실행 금지): 명령 매칭→사람 승인 단위, 사람필수가 상한 이하 자동승인을 이기는 단위, 미상 기술서→사람 승인이며 최대값 폴백 없음 단위, 보관·현행 불일치→stale 거부 단위(인자 1개 변경 포함), 캐시된 허용이 다른 내용에 재사용되지 않음 단위, 입장 허가의 명시 제약(범위·시간·가용·대상·외부문자·duration) 유지 단위, 일반 허용·거부 재실행 없음(두 번째 승인 미생성) 단위. 기존 승인 재개·승인 결론·차단 기록·예산 검사를 재사용 우선하고, 라이브·네트워크 검사는 제외한다.

---
검증 구분: 위는 코드·문서 읽기만. 자동 테스트·실행 검증 없음. 제품 코드 수정·커밋·푸시 없음. §10은 §2·§3의 폴백·재사용안을 정정한다.
## 11. 후속 C 범위 분할과 루트·A 전달용 필수 계약 (READ-ONLY, 미구현)

구현은 다음 차례다. 이번에는 소유권 경계와 요구 필드만 확정하고 제품 코드·테스트를 건드리지 않는다.

### 11.1 소유권 분할 (C가 손대는 범위와 손대지 않는 범위)

- A 소유: 핵심 계약 3종(참여 계약·판정 계약·도구 설명 계약). 필드 1개 추가도 A 승인이 필요하며, C는 아래 11.2의 요구사항만 전달한다.
- 루트 소유: 호출 사용량·인지 엔진·상태(호환성 유지: 요청 단위 사용량 기록, 예산 차감, 런타임 상태와 체크포인트). C는 소비만 하고 호출부를 직접 바꾸지 않는다. 정책 노드 재개부의 현행본 전달 1줄(§10.3)도 루트 호출부이므로 요구로만 전달한다.
- C 범위: 판단 뭉치 전체(선택 위험·패턴 승인·판정 의미 투영), 모델 어댑터와 포트(제공자 실패 정규화)의 소비, 진단과 보고 투영(실패·차단 구조화 투영)의 소비. 계약 정의와 상태·호출 소유물은 범위 밖이다.

### 11.2 루트·A에 전달하는 필수 필드 (C의 요구, 정의는 A)

- 도구 설명 계약 +1 선언: 명령어 텍스트 필드 목록(예: command_fields). 제공자가 선언하고 기본값은 빈 목록이며, 등록·매니페스트 자리에서 기입한다. 승인 정책에 도구 식별자 목록(tool_ids)을 두는 안은 기각한다. 중앙에 이름 수집을 금지한다는 기존 원칙과 충돌하고, 선택 도구는 이미 판단 맥락의 선택 도구 칸(판정 계약)으로 식별되기 때문이다.
- 승인 정책 계약 +1 규칙: 참여 단위 패턴 규칙 목록(예: command_rules, 원소 모양 {name, patterns}). 이름은 운영자가 붙이는 표시용 식별자이며 매칭 키가 아니다. 패턴은 리터럴 부분문자열만 허용한다. 규칙에 최소 위험도(minimum_risk)를 다는 안은 불필요하다. 명령 매칭은 위험도와 무관하게 사람필수이므로 기존 위험 임계치·자동승인 상한과 직교한다.
- 사람필수 타입화: 판정 경로에 사람필수 표시 1개를 타입으로 둔다(매칭 규칙의 고정 속성 true 또는 판정 결과의 불리언 1개, 자리 선택은 A 결정). 저장 단계는 이 표시가 있으면 자동승인 상한 검사를 건너뛰고 항상 승인을 만든다. 상한 판정 함수 자체는 손대지 않는다.

### 11.3 기존 승인 정책 직접 지정의 마이그레이션 (조용한 폐기 금지, 검토 질문)

- 기존 승인 요구 목록(능력 네임스페이스·표준 행동 키 직접 지정)은 의미를 그대로 유지한다. 새 패턴 규칙이 생겨도 기존 항목을 조용히 버리거나 패턴으로 흡수하지 않는다. 동작이 겹치면 사유를 합쳐 기록하고, 기존 소비자가 읽던 matched_rules 원소는 그대로 둔다.
- A·루트 검토 질문: 기존 항목과 새 패턴 규칙이 같은 행동에 겹칠 때 사유를 둘 다 기록하는가 우선 1개만 기록하는가. 사람필수 표시를 판정 문자열 태그로 두는가 판정 계약 불리언으로 두는가. 규칙 이름 중복 시 조립 실패로 할 것인가 선승으로 할 것인가(기존 확장 조립의 중복 실패 규율과 일치 권장). 패턴 개수·길이 상한을 두는가(빈·공백 무시는 기존 규율 유지, 상한값은 A 결정). 기존 진단·보고 소비자가 새 태그를 모를 때 무시하는가 차단하는가.

### 11.4 명령 탐지 파서 복잡도 확장 금지

- 명령 탐지는 리터럴 부분문자열 매칭을 넓히지 않는다. 정규식·셸 파싱·구문트리·인자 의미 해석을 새로 들이지 않는다. 기존 공유 좁은 검사와 동일한 근거(예측 가능성과 과다 매칭 회피)다. 선언된 필드(실행파일 문자열과 인자 배열의 문자열 요소)에만 적용하고 비문자열 스칼라는 무시한다. 패턴 저장소를 따로 두되 매칭 의미는 기존 것과 같게 둔다.

### 11.5 후속 C 범위 파일 경계 (읽기 전용 계획, 지금 변경 없음)

- 판단 뭉치: 승인 판정 함수에 명령 분기 1개(기존 목록 분기 다음 자리, 위험 분기와의 순서는 구현 시 확정)와 사람필수 표시. 판단 흐름에 선택 기술서 1회 조회(기존 외부문자 조회와 같은 형태)를 두어 위험 직접 참조(미상은 사람 승인, 최대값 폴백 없음)와 명령 매칭과 상한 우회를 잇는다. 위험 해석기의 기존 최대값 동작은 레거시 소비 전용으로 남기고 삭제하지 않으며, 선택 직접 경로는 판단 서비스의 기술서 조회로 처리해 위험 모듈에 새 추상화를 두지 않는다. 승인 결론에 보관·현행 비교 1조건(§10.3). 외부문자와 공유 좁은 검사는 손대지 않는다.
- 모델 어댑터·포트·진단·보고: C는 소비만 한다. 어댑터의 구조화 호출과 요청 단위 값·토큰 반환, 포트의 요청 단위 계약을 전제로 쓰고 변경 요구를 내지 않는다. 제공자 실패는 인지 엔진의 전송 실패 진단으로만 흐르게 유지하고, 모델명·예외형 분기를 중앙 판정에 추가하지 않는다. 진단의 안전 피드백·식별자·재시도 가능 규율을 유지하고, 보고 투영의 필수·유예 분리와 차단 목록에 새 실패 종류를 신설하지 않고 기존 투영으로 수용한다.
- 최종단계 테스트 제안(지금 실행 금지): §8·§10.4 목록에 추가한다. 기존 항목과 새 규칙 겹침 기록 단위, 사람필수의 상한 우회 단위, 미상 기술서의 사람 승인 단위, 패턴 리터럴 의미(정규식 아님) 단위, 파서 복잡도 회귀(새 import·새 의존·새 저장소 없음) 단위. 기존 승인 재개·승인 결론·차단 기록·예산 검사를 재사용 우선으로 하고 라이브·네트워크 검사는 제외한다.

---
검증 구분: 위는 코드·문서 읽기만. 자동 테스트·실행 검증 없음. 제품 코드 수정·커밋·푸시 없음. §11은 미구현 후속 범위의 소유권과 요구 필드만 확정한다.

