# 검증 계약 개편 작업 기록

## 조사와 초기 계획

- 기준 dev: `ce7893a2a15f448e977b4643b68b18972c743e02`. 원격 fetch 후 로컬/원격 dev의 ahead/behind 0/0 확인.
- 원래 작업 폴더의 `.codex-remote-attachments/`, `docs/agent/current-agent-loop-20261004.md`는 이번 변경 범위 밖의 기존 미추적 파일로 보존.
- 별도 작업 공간 `C:/Users/qkrgu/.codex/worktrees/validation-contracts/WHS-Red-Teaming-Agent`, 브랜치 `codex/validation-contracts` 생성.
- 기존 제안서·연구 자료 재사용. 실제 계획 입력/등록, 행동 편집과 실행 직전, 단계·작업·목표 완료 소비 경로 추가 확인.
- DeepSeek 4.1 Flash max 오케스트레이터에 공개 조회, 폐쇄 루프, 자율 위임 조사 3개를 Muse 1.3 Contributor xhigh 워커로 분담하도록 배정.
- 초기 구현 계획 작성. 별도 검토·문서 확정 전에 제품 구현과 테스트를 시작하지 않음.
- 워커 3명(Muse 1.3 Contributor xhigh) 조사 완료. 산출물: `no-git/validation-contract-implementation-20261004/worker1-public-lookup-policy.md`, `worker2-closed-loop-ae.md`, `worker3-autonomous-subagent-contract.md`. 루트 핵심 계약 조사는 `root-core-contract.md`.
- 루트 통합 판정: 참조 후보 대상 3상태(referenced·unconfirmed) 편입, 민감 검색어 공통 좁은 판별 + 첫 영속 상태 이전 처리, 대상별 selector·allowed_action_keys 계약, 폐쇄 루프 호출별 결과·처리 기록, 자율 위임 공통 worker subgraph(부모 checkpointer 상속·호출별 child 체크포인트·WorkBudget/DecisionBudget 구분·정리 여유 예약·stop 이후 LLM 금지). 기각 6건 기록: `no-git/validation-contract-implementation-20261004/root-integration-final.md`.
- 최초 계획과 소스 대조 검토 1 완료: `no-git/validation-contract-implementation-20261004/plan-review-1.md`(R1~R8). 조사 통합으로 계획 3.5절·소유권을 수정 반영.

## 검토와 확정

조사 통합 및 독립 재검토 완료. 구현 기준 계획 확정.

- 루트가 R1~R8을 실제 입력 변환 시점, semantic 조건과 legacy unknown 구분, 목적 해시/원본 참조를 가진 작업 완료 영수증, 무효화 의미, 첫 저장 전 Control 거절, 대상 selector/제한 교집합, 부분 이동 응답과 참조 TargetRef 해결까지 구체화.
- 자율 위임은 조사 통합 보고의 하위 그래프 안 대신 기존 상위 그래프의 실제 호출별 저장·승인 재개를 사용하는 대안으로 변경. 위임 상태가 별도 역할 맥락의 다음 LLM 판단을 소유하고, 각 실제 행동은 기존 원장·분배·정책·실행·합산으로 진행한다. 큰 async 노드 내부 반복이나 루트가 매 행동을 다시 선택하는 흐름이 아님. 새로운 중첩 승인/continuation을 추가하지 않기 위한 선택이며 수정 계획 3.5.5가 조사 보고보다 우선.
- 페이지 자동 이동 설정의 의미 변경(한 요청 결과 반환, 전역/위임 예산으로 후속 이동 제한), 실제 tool의 외부 텍스트 선언과 ControlContext 연결, 종료 정산의 같은 완료 영수증 소비·중지 뒤 모델 호출 금지를 계획에 명시.

## 구현·통합·리뷰·최종 검증

독립 검토 2에서 R1~R8의 계획 수준 해결과 구현 착수 가능 판정. 최종 사용량 포트 문단도 기존 LLM 계약·어댑터·재시도 경계와 별도 대조하여 필수 문제 없음 확인. 점수 목표나 추가 기능 확대 없이 검토 종료.

공통 계약(A)부터 착수한다. 전체 코드리뷰와 테스트는 아직 수행하지 않았다.

### 공통 계약 checkpoint

- 운영 설정과 Engagement에 대상 selector/허용·거절 행동 및 외부 텍스트 금지 패턴을 같은 타입으로 연결.
- 등록 도구가 외부 전송 텍스트 인자를 선언하고 ControlContext가 실제 선택 도구를 참조하는 필드 추가.
- MissionTask의 완료 영수증 참조 추가. 실제 원자적 기록/소비는 B에서 연결 예정.
- 새로운 자연어 조건의 semantic 종류 추가. 기존 unknown 저장 값은 유지하고 작성 입력 정규화/완료 소비는 B에서 연결 예정.
- 요청별 JSON/nullable 토큰 사용량을 반환하는 선택적 LLM 포트 추가. 기존 JSON-only 포트는 유지하고 어댑터/판단 소비는 F에서 연결 예정.
- 위임 제안/상태 타입과 runtime/실제 행동/원장의 위임 참조 추가. 기존 단일 호출 기본값과 저장 기록은 읽을 수 있게 기본값 사용.
- 계획 범위·공통 타입·명백한 연결 오류와 diff의 형식만 최소 확인. 실행/테스트는 하지 않았으며 위 선언만으로 제품 기능이 완성된 것은 아님.
