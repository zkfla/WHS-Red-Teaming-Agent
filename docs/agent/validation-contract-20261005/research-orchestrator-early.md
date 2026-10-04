# 검증 계약 개선 — 오케스트레이터 조기 경계 제안

상태: 조사 단계의 조기 메모다. 아래는 오케스트레이터가 직접 확인한 소스 근거와 잠정 경계 제안이다. 제품 코드·테스트·커밋은 건드리지 않았다. 루트 조사 문서(root-investigation.md)의 확정 사실과 겹치지 않는 의존성·소비자 목록에 집중한다.

## 위임 출처 정정 (필수)

- 이 세션에는 요청 모델(opencode-go/muse-spark-1.3-contributor, xhigh)을 받는 multi_agent_v1__spawn_agent 도구가 노출되지 않았다. 오케스트레이터가 사용할 수 있었던 위임 도구는 agy(mcp__agy__*)뿐이었고, 그 모델 목록에 요청 모델은 없었다.
- 그래서 오케스트레이터가 실행한 워커는 요청 모델이 아니다. 아래는 취소·실패한 실행의 정확한 handle이다.
  - v1(모델 claude-opus-5-5-high): A job 1791150272717498700-3c552e1a92e14615795cc6c4 (failed, 콘텐츠 필터), B job 1791150274825115900-acff3d12fbc5edba766a191f (failed, 할당량), C job 1791150276911589000-501a97fb465200f857481a12 (failed, 할당량)
  - v2(모델 gemini-3.1-pro-high): A job 1791150333679000500-98c2197805c17c11b1fe4f36 (cancelled), B job 1791150331593594800-180f06c4870dc58c6e7c7c80 (done, 결과 있음), C job 1791150335810765600-3f46d9b7632dc3641319097f (cancelled)
- A(v2)·C(v2)는 취소되어 결과가 없다. B(v2)의 Gemini 결과는 보존하되, 요청 모델의 검증을 대신하지 않는 미검증 대체 자료로만 취급한다. Muse 워커 결과로 재확인하기 전에는 계약 확정 근거로 쓰지 않는다.
- 오케스트레이터는 추가 agy 대체 실행을 하지 않는다. 루트가 Muse 워커를 직접 만들면 그 결과를 받아 통합한다.

## 직접 확인한 소스 사실

- engine.py:96 ActionIntent는 capability·operation·tool_id를 모두 필수로 받는다. LLM은 이미 도구를 지목하지만 행동 분류가 함께 강제된다.
- core/contracts/tool.py:76 ToolInvocation.operation은 필수이고 'ActionProposal.operation을 복사한 정식 의도'라는 주석이 붙어 있다.
- capability/catalog.py:128 MCPToolCatalog 생성자는 등록된 모든 도구에 명시적 operation 바인딩이 없으면 ValueError로 실패한다. 새 도구 등록만으로 중앙 조립이 깨지는 지점이다.
- capability/catalog.py:180 supports(tool_id, operation)와 gateway.py:233의 supports 호출이 실제 실행 거절(operation_not_allowed_for_tool)을 만든다.
- capability/server_roles.py ServerRoleSpec.operations가 서버 역할별 도구-행동 표를 데이터로 갖는다.
- nodes/execute.py:230-235는 proposal.capability·operation과 step 값 일치, 도구 네임스페이스 일치를 실행 준비 조건으로 본다.
- nodes/policy.py:96/235와 admission 거절 기록이 proposal.capability.operation 문자열을 요약에 쓴다.
- cognition.py:199 consume(stage=...)로 초안·검토·재검토가 같은 DecisionBudget을 공유한다.
- completion.py:1086은 outcome을 success/refuted/inconclusive/blocked로 검사하고, 1552는 refuted 기준으로 rejected/refuted를 요구한다.

## 작업 스트림 1 — 직접 선택 도구 실행 경로

계약 제안(최소): LLM 제안은 tool_id·target·principal·arguments·reason으로 실행 의도를 표현하고 capability/operation은 선택 필드가 된다. 별도 tool_call 마커 모델을 새로 만들기보다 ActionIntent의 기존 tool_id를 실행 정본으로 승격하고 capability/operation을 호환 메타데이터로 남기는 (b)안을 우선 검토한다. 두 안의 정확한 소비자별 준비 상태는 워커 A 결과로 확정한다.

소비자 목록(변경 필요):

1. capability/catalog.py:128 — 바인딩 누락 시 생성 실패 제거. operation이 주어진 경우에만 기존 일치 검사를 수행.
2. capability/catalog.py:180 supports + gateway.py:233 — operation 없는 호출은 도구 등록·건강·입력 규격으로 판단하는 별도 조회 경로.
3. core/contracts/tool.py:76 — operation 선택화(또는 빈 값 허용)와 그 의미 정의.
4. orchestration/candidates/compiler.py — 도구를 행동 목록에서 재탐색하는 단계에 직접 tool_id 검증 경로 추가.
5. orchestration/nodes/execute.py:230 — binding_ready의 capability·operation 일치 조건을 직접 선택 경로에서 도구 등록·네임스페이스·대상 범위 검증으로 대체.
6. orchestration/nodes/policy.py:96/235 — 요약·기록이 operation 부재 시 tool_id로 표시.
7. orchestration/candidates/catalog.py bound_operations, eligibility.py:95 applies_to.operations — operation 미선언 도구가 후보·자격에서 누락되지 않게.
8. engine.py context() 도구 노출 — 행동 연결이 없는 등록 도구도 역할 범위에서 노출.
9. core/convert.py, core/contracts/action.py:27 — 정책 승인에서 실제 호출까지 tool_id 전달, action_id/invocation_id 출처 유지.

유지: 등록·건강·입력 규격·대상 범위·호출 출처, 실행 직전 정책 검사, 기존 행동 기반 API의 기존 호출자 호환. 금지: 도구별 합성 행동 연결, 모든 행동 자동 매칭.

## 작업 스트림 2 — 완료·증거·그래프 소비 계약

- 완료: 공개 출력 모델(engine.py:147 자유 객체)과 실제 검사(completion.py:1086/1552)가 같은 최소 계약을 쓰게 한다. 담당은 완료 모듈, 소비자는 engine.py:570·paths/writing.py:57 공개 규격과 semantic_completion.py:520/829 영수증 검사.
- 미해결: 새 DB 없이 기존 가설·작업 상태·진단에 구조화된 사유를 남긴다. 미확인을 refuted나 성공으로 바꾸지 않는다.
- 그래프 기록: 새 병렬 저장소 대신 기존 공격 그래프 서비스를 워커가 부르는 공통 기록 도구 경계로 노출한다. 등록은 기존 capability manifest/registration 경로를 쓴다. orchestration/observations/* 도구별 해석기는 축소·삭제 후보.
- 출처: 원본 도구 영수증에 실제 사용한 세션 참조를 남기되 세션 참조와 계정 신원을 같은 것으로 취급하지 않는다. 실제 사용 세션 확인과 세션-계정 연결 확인은 별개 검사다.

## 작업 스트림 3 — 진단·정책 의미·위험 명령 승인

- 제공자 오류 정규화: 담당은 호출 경계(cognition.py:~405). 최소 필드는 공개 오류 유형·HTTP 상태·재시도 가능·재개 시점. 소비자는 기존 진단·보고. 모델 이름별 분기 금지.
- 정책 의미: 담당은 control/roe.py. 계획 문맥은 의미 조회 결과를 재사용하되 실행 권한을 대신하지 않는다.
- 위험 명령 승인: 기존 operator_intents·정책 판단·승인 저장/재개 연결을 재사용하고, 위험 패턴 매칭 시 사람 승인을 요구하는 최소 연결만 추가한다. 대상 범위 유지, 컨트롤 플레인 재설계 없음.
- 예산: 정상 추론 요청과 형식 보정 재시도를 구분하되 기존 전체 시간·턴·워커 요청/토큰 상한과 연결하고 초기화로 무한 루프를 만들지 않는다.

## 미확정

- compiler.py의 정확한 재탐색 지점과 도구별 바인딩 소비 목록 전체는 Muse 워커 A 결과로 확정한다.
- 자연문 판정값이 공개 규격을 통과한 정확한 경로와 완료 계약 단일화 시 유지·이동할 검사 항목은 Muse 워커 B 결과로 확정한다.
- 위험 패턴 설정의 현재 존재 여부(설정 키·기본값)와 승인 훅의 실제 확장 지점은 Muse 워커 C 결과로 확정한다.

