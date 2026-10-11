# ReAct 잔여 작업 보완 계획 — 2026-10-07

현재 소스 기준: dev-orchestration / 6a2cba4c, 기존 미커밋 변경 유지. 기본 native 루프를 다시 구현하지 않는다.

## 계약과 책임

- 대상 없는 실행은 실제 선택 descriptor.requires_target=False만 허용한다. target_types/binds_target_arguments에서 추론하지 않는다. selected_tool 실행 운반체는 target=None을 표현할 수 있지만 compiler, 현재 admission, Control은 등록 descriptor를 확인한다. legacy target/bind 규칙은 유지한다.
- CandidateDraft/WorkItem은 native 실행의 빈 대상 참조를 표현한다. 기존 대상 있는 호출은 실제 target/ref 일치를 유지한다. 별도 targetless 상태나 가짜 대상을 만들지 않는다. 대상 없는 호출은 인자 자동 주입/대상 graph 충돌 키를 만들지 않는다.
- Control scope는 선택 descriptor로 대상 부재의 적법성을 확인한다. 나머지 outbound/ROE/policy/승인/예산 검사는 그대로 실행한다. fingerprint는 원래 도구/None 대상/인자를 비교하며 승인 재개 내용을 바꾸지 않는다.
- admission은 대상 관련 조회만 조건부로 하고 작업/목적/역할/등록/주체/세션/operator epoch/입력 schema는 유지한다. eligibility 및 operator exclusion 소비도 대상 부재를 정직하게 처리한다.
- verify/update는 원본과 실행 이벤트를 보존하되 없는 대상 노드/시도를 만들지 않는다. mapper의 대상 요구에 따라 not_applicable 상태를 보존한다. 대상에 대한 최종 주장에는 기존 원본 연결 검사를 유지한다.
- completion/recovery는 보존 proposal의 실제 target 유무와 work refs를 대조한다. native 작업/목적/call/action/invocation/tool 연결을 유지하고, 대상 없는 호출의 원본 보존/재처리/정산을 허용한다. 보고의 실행/관측/확정 의미는 유지한다.

## 구현 단위

독립 DeepSeek Max/Opus High의 조건부 승인 보완을 반영한다. 첫 구현은 verify.py137의 중단 구문과 소비자 지역 mapper 분기 제거다. PlanStep은 legacy exactly-one target/bind를 유지하고 native selected_tool만 None을 표현한다. resolved_target 접근자와 compiler/react/engine 소비 계약을 함께 맞춘다. 빈 candidate/work refs는 native 계보에 한정하며 targeted 참조 일치를 유지한다.

mapper registry의 map_result/map_result_with_outcome은 None을 받아 not_applicable을 공통 반환한다. verify/update/recovery는 같은 API를 소비한다. Control evaluate와 _current_admission_check 모두 check_scope에 선택 descriptor를 전달하며 미등록/다른 tool/requires_target=True의 None은 거절한다. 대상 없는 호출은 대상 selector에 매칭되지 않지만 outbound/ROE/policy/승인/예산을 유지한다.

engine의 supplied-or-observed/argument-target/node 조회만 대상 유무에 따라 처리한다. operator area/task 억제 및 role/tool visibility/eligibility는 유지한다. provider가 제한 asset_scope를 선언한 경우 target=None으로 그 제한을 면제하지 않는다.

update observe_target/record_attempt, verify의 session principal/credential/CAN_ACCESS와 finding about, recovery의 finding about에서 없는 대상 노드를 만들지 않는다. completion의 빈 work refs 허용은 보존된 유효 native selected proposal의 명시 target=None, 같은 work/call/purpose와 실제 raw action/invocation/tool 연결이 모두 맞는 경우다. 대상 있는 proposal의 빈 work refs나 누락 target metadata는 여전히 거절한다. 정산은 이 owner 판정을 재사용하며 특정 대상 주장은 별도 grounding을 유지한다.

1. 실행 운반체/compiler/Control/scope: Muse High. 공유 계약 변경은 root 통합.
2. verify/update/원본 재처리 소비: Muse High, 다른 담당 파일 수정 금지.
3. root: engine admission/native binding/완료 출처/정산/보고와 통합.
4. DeepSeek High 조정: ARTEX/job/복구/팀 접점 및 실제 등록 도구 기반 라이브 준비 조사.

계획은 DeepSeek Max와 Opus High 독립 검토 후 구현한다. 수정 후 정확성/책임 경계/불필요한 모델 제약 재도입을 같은 관점으로 검토한다. agy Gemini 보조 리뷰는 호출 가능 여부를 확인하고 사용 사실만 기록한다.

## 검증과 완료

ARTEX 소비 추가 확인: native 모델은 legacy TurnDecision의 reconcile_job에 도달하지 않는다. 기존 어댑터 주입 뒤 polling timeout으로 등록 job이 남는 경우를 위해 독립 runtime contribution job.reconcile로 현재 run의 job 조회/기존 engine 회수를 공개한다. backend/queue는 추가하지 않는다. native binding은 기존 has_ambiguous_execution을 재사용해 결과 불명확인 동일 입력의 새 call_id 재제출을 차단한다. 일반 성공/명확 실패의 방법 변경은 유지한다. 회수 이벤트 인과는 원래 local context에 연결한다.

독립 검토 처리: DeepSeek Max/Opus High의 구문 복구, PlanStep/consumer 범위, mapper owner, Control 두 지점, eligibility 유지, graph 대상 부재, 완료 출처 지적은 반영했다. provider asset scope를 None일 때 면제하는 제안은 제한 의미를 유지하는 대안으로 반영했다. Gemini3.8FlashHigh 보조 결과의 동일 차단 목록은 이미 반영된 범위로 수용했고, 단순 accessor 대신 step.target 직접 접근 제안은 공개 resolved_target 계약 확장으로 대안 반영했다. 보조 결과가 계획을 mapper/Control 두 변경으로만 해석한 판단은 전체 계획 및 보완 범위와 달라 기각했다. 이 결과는 보조 의견이며 구현/라이브 통과 근거가 아니다.

query-only 검색 native 실행→원본→결과 소비→작업/목적 출처→정산/JSON·Markdown 보고를 확인한다. 승인·재개/형제 성공/실행 불명확/기존 targeted 호출은 관련 기존 회귀를 우선한다. 구현/리뷰 뒤 최종 소스로 필수 검사 및 실제 모델·실제 등록 MCP 일부 도구 라이브를 수행한다. 대역 테스트는 라이브로 표시하지 않는다.

기존 지원 job/자원 충돌/회수의 미완료만 보완한다. 신규 큐/원장/영구 모드 및 비밀 저장소는 범위 밖이다. pending 입력 보존 한계는 승인 재개 정합성을 유지한 채 보고한다. 검증 완료 뒤 이번 관련 변경만 dev-orchestration 커밋·푸시하며 dev 통합하지 않는다.
