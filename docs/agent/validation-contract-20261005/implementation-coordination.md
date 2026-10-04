# 구현 조정 — 의존 인터페이스와 인접 소비 경로 누락 감지

상태: 조사 전용 조정 메모다. 제품 코드·테스트·커밋은 건드리지 않았고 테스트를 실행하지 않았다. 근거는 현재 작업 폴더 소스와 plan-review.md·research-A/B/C.md·implementation-plan.md다. 모델·에이전트를 새로 만들지 않았다.

## 소유권 (구현 착수 기준)

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

