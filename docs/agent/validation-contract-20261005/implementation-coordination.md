# 구현 조정 — 의존 인터페이스와 인접 소비 경로 누락 감지

상태: 조사 전용 조정 메모다. 제품 코드·테스트·커밋은 건드리지 않았고 테스트를 실행하지 않았다. 근거는 현재 작업 폴더 소스와 plan-review.md·research-A/B/C.md·implementation-plan.md다. 모델·에이전트를 새로 만들지 않았다.

## 소유권 (구현 착수 당시 기록)

아래 초기 소유권·행번호·미확인 항목은 당시 조사 기록이다. 현재 코드 사실이나 리뷰 수정 지시로 재사용하지 않는다. 리뷰 수정의 최신 소유권과 의존성은 문서 마지막 절을 따른다. 특히 `analysis.py`·`completion.py`는 이번 리뷰 수정에서 B가 소유하고, 명령 실행 제공자 선언은 A가 소유한다.

- A: core 실행/선택 계약과 선택 manifest. `core/contracts/action.py`, `core/contracts/tool.py`, `core/contracts/policy.py`, `core/convert.py`, `core/ports/tool_catalog.py`, `core/ports/tool_gateway.py`, `core/provider_contracts.py`, `core/config.py`, `capability/` 중 `servers/` 제외 전부.
- B: local registry 기여, 그래프 기록, 사후 주장 검토. `attack_graph/*`, `evidence/*`(가산), 새 로컬 기록 도구 모듈, `observations/mapper.py`는 폴백 유지.
- C: `core/contracts/engagement.py`와 `control/*`만. 선택 도구 위험과 명령 패턴 승인.
- Root: `orchestration/engine.py`, `nodes/*`, `cognition.py`, `completion.py`, `semantic_completion.py`, `analysis.py`, `update.py`, `delegated_runtime.py`, `state.py`, `runner.py`, `reporting.py`, `diagnostics.py`, `component_binding.py`, `paths/*`, `app/bootstrap.py`, `app/runtime_components.py`, LLM 포트·어댑터.
- D: 세션 참조 생산자. `capability/servers/web_http.py`, `network.py`, `execution.py`.

규칙: `app/runtime_components.py`·`app/bootstrap.py`·`orchestration/analysis.py`·`completion.py`·`component_binding.py`는 Root 단독 작성자다. A는 manifest 파서 API와 설정 필드만, B는 기여 클래스와 공개 함수 시그니처만 제공한다. A는 `capability/servers/*`, D는 그 외 `capability/*`를 건드리지 않는다.

## 의존 인터페이스별 인접 소비 경로 (누락 감지 목록)

A0 실행/선택 계약(binding_mode, tool_id 선택, operation 선택화):
- 즉시 영향: `core/convert.py` `build_tool_invocation`, `orchestration/nodes/execute.py:230-242`, `orchestration/nodes/policy.py:96`·`:129-166`, `orchestration/engine.py:2824`·`:2841`, `:579`·`:673`·`:704` 노출, `candidates/compiler.py:265`, `candidates/catalog.py:26`, `agents/llm_provider.py:251`, `app/methodology.py:110`, `control/risk.py:76`, `control/policy.py:93`, `state.py:100`, `runner.py` `register_pending_proposal`, `component_binding.py` `binding_for_resume`.
- 자주 빠지는 소비자: 대상 의미가 아직 operation 키다 — `orchestration/context.py:106`과 `core/provider_contracts.py:19-36`. 재시도도 행동 키다 — `core/provider_contracts.py:72` `RetryPolicy.permits`, `nodes/execute.py:257-259`, `capability/bootstrap.py:168` `declares_retry_safe`(operation이 비면 False).
- 계약 테스트: `tests/contract/test_action_binding_consistency.py`(미바인딩 도구 비실행·`no_tool_for_operation` 단언), `tests/fakes/tool_gateway.py:36`, `tests/integration/test_approval_resume.py:141`.
- 승인 지문: `core/contracts/action.py:21`에 tool_id가 없고 `core/contracts/policy.py:27`의 선택 도구는 일시 필드다. 영속하지 않으면 승인-재개가 action_id로만 결속된다.

A1 선택 manifest:
- 영향: `core/config.py` `CapabilityConfig`, `app/bootstrap.py` 로드 경로, `app/runtime_components.py` `resolve_selection`·`instantiate`, `agents/packages.py` `RolePackage`, `component_binding.py` `RuntimeComponentBinding`, `delegated_runtime.py:339` 워커 문맥.
- 누락 감지: `capability/settings.py:202`가 allowed_tools와 tool_operations 키 집합을 같게 요구하고, `capability/catalog.py:126-128`이 명시 매핑 누락 시 구성 오류를 낸다. 파일은 기능 선택만 담당하고 역할 정의·도구 입력/위험/대상은 기존 모듈이 소유한다. 지문은 값 생산만 A, 바인딩 필드와 비교는 Root.

B local registry·그래프 기록:
- 영향: `capability/gateway.py:218`(어댑터 없으면 `unknown_server`), `app/runtime_components.py:214` `apply_extensions`의 `validate_kinds`(새 kind 추가는 Root), `nodes/update.py`(실제 그래프 쓰기), `observations/mapper.py`(등록), `attack_graph/service.py` `record_hypothesis`·`record_finding`·`observe`·`link`, `evidence/service.py`, `completion.py:386`·`:532`, `analysis.py:844`, `delegated_runtime.py:323`·`:466`.
- 누락 감지: 로컬 기록 도구를 원격 MCP 대상 도구로 위장하면 대상 작업 예산·승인 대상이 된다. 로컬 호출은 모델 판단 예산 안에서 제한되고 대상 예산에는 포함되지 않아야 하며, 예산 정본은 Root(`cognition.py`, `delegation.py`)이다. B는 예산 영역을 수정하지 않는다.
- B1 기록 handler 자체는 A0/B0 source API로 구현 가능하다. A1은 전체 직접 실행 E2E의 선행이며, B1 전체를 A1 완료까지 대기시키지 않는다.

C control 정책·승인:
- 영향: `control/service.py:146`·`:203`·`:212`·`:258`, `control/policy.py:86-119`, `control/risk.py:75-80`, `control/approval.py`, `core/contracts/engagement.py` `ApprovalPolicy`, `nodes/policy.py:35` `_OUTCOME`.
- 누락 감지: 외부 전송 금지(DENY)와 사람 승인(REQUIRE_APPROVAL)을 같은 설정 의미로 합치지 않는다. `policy.py:72` `may_auto_approve` 우회 금지. 승인 후에도 scope·시간 재확인 경계(`service.py:902` `admit_invocation`, `:700` `_current_admission_check`)을 유지한다. 위험 해소는 선택 도구 기술서 우선, 선택 없을 때만 기존 최대값 폴백.

D 세션 참조 생산자:
- 영향: `capability/servers/web_http.py`(익명·인증 비교 반환에 세션 참조 없음), `network.py` 요약. 소비자는 `analysis.py:844`와 `completion.py:1395`.
- 누락 감지: 세션 참조와 계정 신원은 다른 의미다. 중앙 검증에 비교 도구 이름 예외를 추가하지 않는다.

## 선행 CLI dirty 변경 보존과 checkpoint 범위

- 현재 dirty: `README.md`, `app/cli/commands/interactive.py`, `app/cli/engagement_form.py`, `intake/models.py`, `intake/serializer.py`, `orchestration/engagement_planner.py`, `orchestration/engine.py`, 테스트 4개, 그리고 미추적 `intake/targets.py`, `tests/unit/test_intake_targets.py`. 원본은 `data/validation-contract-20261005/preexisting.patch`에 보존되어 있다.
- 구분 필요: `orchestration/engine.py`가 선행 dirty 목록과 Root 구현 대상에 동시에 속한다. 선행 변경을 먼저 별도 커밋으로 분리하지 않고 구현 커밋에 섞으면 선행 CLI 입력 개선과 이번 계약 변경이 한 커밋에 혼입된다. checkpoint는 선행 분리 → A0 계약 → 슬라이스별로 나누고, 테스트 없는 checkpoint를 검증 완료로 표현하지 않는다.
- `docs/agent/validation-contract-20261005/`는 미추적 조사 산출물이다. 제품 구현 커밋 범위에 포함할지는 Root가 정한다.

## 미확인·주의

- A0가 보고하는 core 계약 확정 내용은 Root를 통해 공유받아야 하며, C1과 Root 소비 경로가 그 확정 전에 시작되면 같은 의미를 두 번 정의하게 된다.
- `core/contracts/engagement.py`(C)와 `core/contracts/policy.py`(A)는 파일이 달라 충돌하지 않지만, ApprovalPolicy 필드 추가가 `control/policy.py` 판정 순서에 미치는 영향은 C가 명시해야 한다.
- 기존 실제 MCP 도구 하나를 tool_operations 부분 설정에서 빼면 unbound로 등록되므로 A1 직접 실행은 실제 LLM·MCP로 검증할 수 있다. fake 전용이라는 단정은 사실과 다르다. 관련 테스트는 아직 없다.
- 과거 BOLA 실행의 세션 연결 누락은 원본 자료가 없어 미확인으로 남는다.

## 독립 전체 코드리뷰 수정 조정 — 최신 소유권

기준은 `code-review.md`의 CR01~CR12(P1 7건/P2 5건)와 이번 루트 지시다. 12건 모두 수정 대상으로 채택하며, 아래 판단은 수정 완료 판정이 아니다. 이 조정에서는 코드 읽기와 본 문서 수정만 수행했다. 제품 수정·테스트·라이브 실행·커밋·추가 에이전트/agy 실행은 하지 않았다. 동시 수정 중인 현재 소스는 리뷰 당시 행번호와 달라질 수 있으므로 함수와 소비 경로도 함께 적는다.

| 항목 | 판단 | 수정 소유자·파일 | 근거와 처리 경계 |
|---|---|---|---|
| CR01 (P1) | 반영 | C: `control/policy.py`, `control/service.py` | 일반 승인 조기 반환이 명령 사람 승인 표식을 누락시킨다. 적용되는 근거를 보존하고 사람 필수 의미를 자동 승인보다 우선시한다. 외부 전송 DENY 의미는 유지한다. |
| CR02 (P1) | 반영 | A: `capability/execution_manifest.py`, 명령 어댑터/`servers/execution.py`, `core/contracts/tool.py`의 계약 설명 | 내장 실행 제공자의 빈 명령 필드 선언 때문에 검사 입력이 생기지 않는다. 제공자→발견 기술서까지 선언을 연결한다. CR03과 같은 기존 목록 계약을 쓴다. |
| CR03 (P2) | 반영 | C: `control/policy.py` | 실행 파일과 인자 배열을 독립 검사하면 `rm -rf` 같은 설정 패턴이 누락된다. A가 선언한 순서대로 한 명령 표현을 구성하여 검사한다. |
| CR04 (P1) | 반영 | B: `orchestration/graph_recording.py`, `analysis.py` | 함께 제출하거나 참조한 가설에도 대상/주장 연결 확인 후 이번 검수 결과를 반영한다. 최종 승격의 확정 상태 검사는 유지한다. |
| CR05 (P1) | 반영 | B: `analysis.py`, `graph_recording.py` | 반박/지원 거짓을 위반으로 바꾸는 판정 변환을 고친다. CR04의 상태 반영과 함께 소비하여 반박된 주장이 발견 사항으로 승격되지 않게 한다. |
| CR06 (P1) | 반영 | B: `graph_recording.py`, `analysis.py` | 명시 대상도 실제 원본 대상 또는 근거로 입증된 관련 연결을 코드가 검사한다. 단순 동일 ID 강제나 모델 판정 위임으로 대체하지 않는다. |
| CR07 (P1) | 반영 | B: `analysis.py`, `completion.py`, `graph_recording.py` | 사후 가설→원본 연결을 최종 영수증에서 읽고 기존 원본 정합성을 검사한다. 가설만 검토하는 경로도 영수증 생산과 연결한다. 원본 제안/가설 참조를 소급 변경하지 않는다. |
| CR08 (P2) | 반영 | B: `completion.py`, `analysis.py` | 실행/호출만으로 최신 영수증을 고르면 다른 주장이 덮인다. 기존 가설/주장 식별 의미로 재검토와 별도 주장을 구분한다. 오래된 판정을 무조건 되살리지 않는다. |
| CR09 (P1) | 대안 반영 | D: `orchestration/session_proof.py`; B: `analysis.py`, `completion.py` 소비 연결 | 옛 자료 읽기 호환은 유지하되 요청 echo는 옛/현재 어느 자료에서도 실제 사용 proof가 아니다. actual-only 공개 helper를 소비하고 부재는 미확인으로 남긴다. 별도 버전별 echo 인정 경로를 만들지 않는다. |
| CR10 (P2) | 반영 | Root: `cognition.py`, `delegated_runtime.py` | 정상/보정 상한을 유지하고 전체 잔여량을 별도 총상한으로 적용한다. 정상 판단에 잔여량을 먼저 배분해 보정을 0으로 만드는 계산을 고친다. 기존 단독 총횟수 계약과 실제 사용량 누적을 유지한다. |
| CR11 (P2) | 반영 | Root: `delegated_runtime.py`, `delegation_decision.py`, 기존 진단/미해결·보고 연결 | 예외 클래스 이름으로 축소하거나 일회 결과에만 두지 않는다. 공개 진단을 기존 정본에 보존하고 다음 판단/보고에는 동일 원인 참조·요약을 전달한다. 비재시도 원인과 한도 종료를 구분한다. |
| CR12 (P2) | 반영 | B: `graph_recording.py` | 문자열 원본도 전체 값 또는 명시한 위치/구간의 정확한 일치로 측정 사실을 등록할 수 있게 한다. 도구별 문자열 파서는 추가하지 않는다. |

## 수정 의존 그래프와 인접 소비 확인

- **A CR02 → C CR03 통합:** 기존 `command_fields: list[str]`는 **한 명령의 순서 있는 구성요소**다. 실행 파일 필드 다음 인자 배열 필드를 제공자가 순서대로 선언하고 C는 문자열 배열 원소의 순서를 유지해 합친다. A가 제공자 선언·전달·계약 설명을 맡고 C가 평가를 맡는다. 새 필드 레지스트리, 도구 이름별 분기, 무관한 입력의 추측 결합은 추가하지 않는다. 현재 C의 `_command_text_candidates`에도 이 순서 결합 설명이 보이지만 CR02의 실제 제공자 전달까지 확인해야 연결이 끝난다.
- **C CR01 ∥ A CR02:** 승인 조기 반환 수정은 제공자 선언 작업과 독립적으로 진행 가능하다. 사람 승인 표식이 `evaluate_policy`에서 `service`의 자동 승인 처리까지 보존되어야 한다. CR01~03의 통합 소비자는 실제 승인 대기·해소·동일 입력 재개 경로다.
- **D CR09 → B 실제 사용 소비:** D는 `session_proof.py` 공개 함수 이름을 유지한다: `collect_actual_sessions`, `has_actual_receipt`, `actual_receipt_conflict`. B는 이 함수를 그대로 받아 `analysis.py`와 `completion.py`의 계정/세션 검사를 actual-only 의미로 통일한다. 부재와 명시적 null을 구분하며, 실제 사용 참조와 세션-계정 신원 근거를 별개로 검사한다. 현재 `completion.py`는 `collect_actual_sessions`, `analysis.py`는 세 함수를 이미 가져오므로 새 proof 저장소나 별도 helper 복제 없이 소비를 맞춘다. D helper 준비 전에도 B의 다른 수정은 진행 가능하다.
- **B CR04 + CR05 → 승격 소비:** 이번 검수의 가설 상태와 확인/반박/지원 여부가 함께 일치해야 한다. 한 항목만 수정한 결과를 전체 승격 해결로 취급하지 않는다. 기존 `findings.py`의 최종 승격 조건을 소비 기준으로 유지한다.
- **B CR06 → 사후 주장 검토:** 대상 관련성 admission은 의미 검수 전에 코드가 적용한다. 실제 호출 대상과 원본에 새로 등장한 대상의 입증 관계를 구분하고 계정 proof도 이어서 소비한다.
- **B CR07 + CR08 → 완료·보고:** 영수증 생산(`append_posthoc_claim_receipt`)과 조회(`_load_final_receipts`)가 같은 주장 식별/원본 참조 의미를 써야 한다. 동일 원본의 복수 주장 및 같은 주장의 재검토를 구분한 뒤, 가설 확인/반박 완료가 최신 관련 판정을 읽도록 연결한다. 원본 자체의 실행 출처 검사는 유지한다.
- **B CR12 ∥ 다른 B 수정:** 문자열 위치/값 계약은 독립 구현 가능하다. 이후 원본 조회→워커 기록 요청→원본 일치 admission→그래프 저장→응답 반환까지 동일 계약을 소비한다.
- **Root CR10 ∥ CR11:** 예산 계산과 진단 전달은 독립 수정하되 `delegated_runtime.py`는 Root 단독 소유다. 판단 실패에서도 실제 요청/토큰 계측을 보존하고 공개 원인을 기존 진단/미해결 정본으로 전달해야 한다. 워커 상태에는 정본 참조/요약을 연결하며 새 저장소를 만들지 않는다.

이번 리뷰 수정에서는 B가 `graph_recording.py`·`analysis.py`·`completion.py`, D가 `session_proof.py`, A가 명령 제공자 선언, C가 정책 평가/사람 승인, Root가 예산/진단 전달을 소유한다. 앞 절의 초기 Root 단독 `analysis.py`·`completion.py` 규칙과 A의 `servers/*` 제외 규칙은 이번 할당에 적용하지 않는다. 인접 파일은 담당자의 공개 계약을 소비하고 공동 편집하지 않는다.

범위는 12건의 실제 리뷰 실패 조건에 한정한다. 새로운 field registry/store/tool-name case는 추가하지 않는다. 선행 CLI 변경과 기존 checkpoint 경계는 보존하고 이번 조정에서 커밋하거나 정리하지 않는다. 수정 완료·재리뷰·최종 검증 결과는 아직 이 문서에서 선언하지 않는다.

