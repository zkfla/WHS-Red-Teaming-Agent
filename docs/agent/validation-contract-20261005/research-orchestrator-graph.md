# 검증 계약 개선 — 의존성 그래프와 위험·소비자 (오케스트레이터)

상태: 조사 단계의 잠정 정리다. 앞선 research-orchestrator-early.md와 같은 세션의 조사 메모이며, 제품 코드·테스트·커밋은 건드리지 않았다. 작성 시점에 Muse 워커 research-A/B/C.md는 아직 없었다. 아래는 오케스트레이터가 직접 확인한 소스 근거이고, 워커 문서가 나오면 대조해 갱신한다.

## 제안 구현 의존성 그래프

- P0 공통 의도 계약(루트 담당): engine.py:96 ActionIntent의 기존 tool_id를 실행 정본으로 승격하고 capability·operation은 선택 메타데이터로 둔다. core/contracts/action.py:27, core/contracts/tool.py:76을 함께 조정한다. P1·P2·P4가 여기에 막힌다.
- P1 직접 선택 실행 경로(워커 A): capability/catalog.py:128 생성 실패 완화 + 도구 단독 조회 추가, gateway.py:233의 operation 없는 호출 허용, bootstrap.py:154-170의 '미바인딩 도구는 실행 불가' 규칙을 직접 선택 한정으로 대체, core/contracts/tool.py:76 operation 선택화. P0에 의존한다.
- P2 정책·위험·승인(워커 C): control/risk.py:76이 list_tools_for_action(capability, operation)으로 위험을 조회하므로 tool_id 기반 조회를 추가한다. control/policy.py의 require_approval_for는 capability 네임스페이스와 capability.operation 키만 매칭하므로 tool_id 매칭 계약을 추가한다. 위험 패턴 승인은 기존 ApprovalService(control/approval.py)와 CLI approvals list/resolve를 재사용한다. P1에 의존한다.
- P3 진단·예산(워커 C): cognition.py:405 제공자 오류 정규화, 보고의 원인 이력 보존, DecisionBudget의 정상 추론과 형식 보정 구분. P1과 독립이다.
- P4 완료·증거·그래프(워커 B): completion.py 단일 계약과 공개 규격(engine.py:147/570) 정합, 미해결 구조화 사유, 공통 그래프 기록 도구(attack_graph/service.py의 record_hypothesis·record_finding·observe·link를 부르는 도구)를 기존 manifest/profile·result_mapper 등록으로 노출. operation 없는 도구가 실행되려면 P1이 선행이다.
- P5 증거 읽기 문맥(루트): 기존 체크포인트에 참조·범위·결과 상태만 유지하고 원문 복제와 그래프 승격은 하지 않는다.

## 위험과 소비자 (구현 시 필수 확인)

1. 도구 노출 소비자 집합: compiler.py:265, candidates/catalog.py:26, agents/llm_provider.py:251, engine.py:1089, control/risk.py:76, app/methodology.py:110, tests/fakes/tool_gateway.py:36, 그리고 contract/integration 테스트. 하나라도 남으면 operation 기반 동작이 잔존한다.
2. control/risk.py:76이 위험을 operation으로 조회한다. 직접 선택 도구의 위험 조회를 tool_id로 바꾸지 않으면 HIGH 위험 도구가 승인 없이 실행될 수 있다(보안 회귀).
3. '미바인딩 도구는 실행 불가'는 의도적 보호 규칙이다. 대체 가드는 등록·건강·입력 규격·대상 범위·정책 ALLOW·호출 출처여야 하며, 아니면 발견된 모든 도구가 실행 가능해진다.
4. catalog.py:128 제거 시 명시 설정 바인딩의 충돌 검사는 남긴다. 명시 설정이 있을 때의 일치 검사는 유지한다.
5. app/runtime_components.py:apply_extensions의 지원 kind 튜플은 중앙 수정 지점이다. 새 kind 추가보다 기존 mcp_profile·result_mapper·eligibility_provider·capability.action_binding 재사용을 우선한다.
6. 재시도: operation이 비면 declares_retry_safe가 False다. 직접 선택 도구의 재시도를 자동 허용하지 않는다.
7. 기존 행동 API 호환: tests/contract/test_action_binding_consistency.py, tests/integration/test_capability_dev_integration.py, tests/integration/test_a_profile_composition.py, tests/integration/test_approval_resume.py가 list_tools_for_action을 쓴다.
8. capability/bootstrap.py:104는 예제 어댑터용 합성 바인딩을 인라인으로 만든다. 이 패턴을 일반화하지 않는다.
9. 예산을 나눌 때 delegation_decision.py의 워커 전체 요청·토큰·시간 상한을 초기화하지 않는다.
10. 완료 공개 규격을 엄격화하면 기존 제안 호환 정규화(문자열·참조 편의 입력)는 입력 경계에 남긴다.

## 실제 도구 영수증과 계정 신원 구분 (B·A 공통)

- 원본 도구 영수증에는 실제 사용한 불투명 세션 참조를 남긴다. 세션 참조와 계정 신원은 다른 의미이며, 세션 목록 선언만으로 계정 신원을 증명한 것으로 보지 않는다.
- 실제 사용 세션 확인과 세션-계정 연결 확인은 별개 검사다. 이미 있는 세션 원본 근거 처리(check_record_linkage, verify_stored_origin)를 재사용하고 중앙 검증에 비교 도구 이름 예외를 넣지 않는다.
