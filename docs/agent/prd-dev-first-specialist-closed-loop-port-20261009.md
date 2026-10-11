# PRD — 최신 dev 기반 전문 에이전트·폐쇄루프 통합

- 작성일: 2026-10-09
- 문서 개정: v1.2 — v1.1의 계약 유지, 2026-10-11 사용자 요청으로 이후 Mobile 작업 제외.
- 상태: 코드 조사에 근거한 구현 계획. 이 문서는 구현 완료·병합 가능·운영 검증 완료를 의미하지 않는다.
- 기준 dev: `153265e32e029481628e8c057fe0ea476d939d64`
- 기능 이식 원본 dev-jm: `8af7a6a2a9d2e773147c673e909917152d51856c`
- 공통 조상: `6a2cba4cb219f0101c8e4f4f4ef8a28a8ed2dc58`
- 저장소: `zkfla/WHS-Red-Teaming-Agent`
- 최초 문서 작업 범위: 고정 커밋 조사·비교·PRD 검수만 수행했다. 이후 구현·검증과 공개 현황은 [구현 기록](dev-first-specialists-implementation-20261010.md)과 [브랜치 안내](native-specialists-branch-guide.md)를 따른다. 원래 설계 설명을 현재 구현 완료의 근거로 읽지 않는다.

### 현재 범위 변경 — 2026-10-11

사용자 요청에 따라 **앞으로 Mobile 구현·기기/앱 설치·실환경 평가는 제외**한다.
기존 Mobile 코드/계약/시험은 호환성 유지를 위해 보존하며 기존 dev 회귀는 유지한다.
새 Web 전용 선택은 `config/agents/native-specialists-web.yaml`로 명시한다. 같은
프로필 loader와 MD를 사용하며 중앙 루프에 Mobile 제외 분기를 추가하지 않는다.
기존 Run과 fingerprint가 다르므로 새 Run에서 선택한다.

M6의 현재 필수 범위는 Web 전문 폐쇄루프·기존 dev 회귀·재현 가능한 Linux
프록시/격리 검증·운영 인계다. 과거 본문의 Mobile 평가는 이번 범위의 완료 조건이나
차단 사유로 계산하지 않는다. 승인 없는 생성 코드 대상 실행은 비활성 유지한다.
실제 LLM 평가는 기존 100회·0달러 조건을 유지하므로 유료 호출하지 않는다.
합성/로컬 통과와 실제 LLM 성능 입증은 분리해서 보고한다.

공개 전 Linux Python 3.14 회귀에서 드러난 동적 Graph wrapper의 optional-port
검사 실패도 M6 기본 호환 범위에 포함한다. Graph 소유 기능 검사만 수정하고,
wrapper fence를 우회하거나 private 파일 정책을 완화하지 않는다. 이유·재현·최종
검증은 [구현 기록 19절](dev-first-specialists-implementation-20261010.md)에 기록한다.

## 1. 결론

**dev의 실행 구조를 기준으로 유지하고, dev-jm의 전문 역할·프롬프트·전략 분해·증거 평가 기능을 선택적으로 이식한다.** dev-jm 전체를 병합한 뒤 충돌 파일을 한쪽으로 선택하는 방식은 적합하지 않다.

최신 dev에는 이미 인스턴스별 작업 소유권, 독립 실행 드라이버, 실행 영수증 복구, 여러 인스턴스의 재개 기능이 있다. 따라서 별도의 서브에이전트 실행 엔진이나 두 번째 큐를 만들 필요가 없다. 부족한 것은 이 실행 기반 위에 전문 역할을 조립하고, 계획 → 작업 → 실행 → 증거 평가 → 계획 수정의 의미 있는 피드백을 연결하는 부분이다.

쉽게 말하면 **dev의 엔진·브레이크·운행 기록은 유지하고, dev-jm의 직무와 업무 분배 방식을 연결**하는 작업이다. 프롬프트 파일을 복사하거나 모든 역할을 동시에 실행시키는 것만으로는 완성되지 않는다.

이번 개정에서 다음 네 결정을 확정한다. 근거·비교는 13절, 검수 수정 이유는 14절에 기록한다.

1. 완료 조건은 버전 있는 Task 계약으로 저장하고, 동일 계약을 워커·완료 검토·receipt 검증이 소비한다(R2-A).
2. 후속 Task의 기동은 기존 coordinator/outbox와 InstanceDriver를 연결하는 Run 단위 조립자가 담당한다. 정상 종료 워커의 새 작업은 새 instance로, 승인/복구 대기는 원래 instance로 처리한다(R3-A).
3. 동일 제안 중복 방지와 오래된 제안 거절을 분리하고, 관련 입력 stamp와 현재 전략 개정을 채택 트랜잭션에서 검사한다(R2-B).
4. 모든 조사 LLM 요청은 공통 원장에 기록한다. `bounded`와 명시적 `unlimited_requests`를 지원하되, 무제한은 총 요청 수만 무제한이며 무한 재시도나 실행 무제한이 아니다(R6).

## 2. 조사 기준과 한계

### 2.1 이전 분석과 달라진 점

이전 `prd-native-react-specialists-integration-20261008.md`는 dev `571cef0`을 기준으로 했다. 현재 dev에는 다음 변경이 추가되어 실행·병합 설계를 갱신해야 한다.

| 커밋 | 확인한 변경 | 이번 설계에 미치는 영향 |
| --- | --- | --- |
| `c352359` | subscription 테스트의 선택적 SDK 의존성 분리 | 최신 테스트 기준을 사용해야 함 |
| `c968760` | 인스턴스별 작업 소유권 | 역할 ID와 실제 실행 주체를 분리해서 연결 |
| `153265e` | 독립 실행 및 durable recovery | 기존 InstanceDriver와 실행 영수증 복구를 재사용 |

현재 로컬의 `origin/dev`는 `571cef0`이므로 그것을 최신으로 간주하지 않았다. 원격 dev의 SHA를 확인하고 정확한 커밋 객체를 받아 `git show`로 조사했다. 로컬 작업 브랜치와 추적 파일을 교체하지 않았다.

공통 조상 대비 양쪽에서 수정된 `src/` 파일은 24개다. 이는 **수정 중첩 파일 수이지 실제 충돌 수가 아니다.** 최신 조합의 병합 시뮬레이션과 통합 테스트는 이번 문서 작업에서 실행하지 않았다. 이전 보고서의 충돌 개수·테스트 통과 수를 최신 조합의 결과로 재사용하지 않는다.

### 2.2 직접 확인한 범위

새 조사 진입, 기본 그래프, native tool 선택, 인스턴스 드라이버, 작업 claim, 도구 실행 접점, 영수증 복구, CLI cohort 재개, 작업·목표 완료 검증, dev-jm 전문 역할 및 프롬프트·분해·예산·증거 유효성 모듈을 확인했다.

이는 통합 접점에 대한 정적 소스 조사다. 전체 shell/provider 구현에 대한 독립 보안 감사나 실제 LLM·모바일·외부 대상 실행 검증은 아니다. 기존 테스트 파일을 확인했지만 최신 dev와 통합본의 테스트를 실행하여 통과를 확인한 것은 아니다.

v1.1에서도 제품 기준 SHA는 위와 같다. 이 개정을 위해 제품 브랜치를 갱신하지 않았다. 외부 비교는 사용자가 지정한 `Doyoun510/ARTEX_ko`와 Shannon의 고정 커밋 소스를 직접 읽었다. 다른 동명 한국어 fork를 지정 저장소로 대체하지 않았다. PentAGI는 공식 저장소 구조·소개까지 확인했으나 소스 복제 승인 절차의 서비스 한도로 상세 검증을 완료하지 못했다. 확인하지 못한 구현을 설계의 입증 근거로 쓰지 않는다.

## 3. 현재 dev의 실제 구조

### 3.1 기본 실행 흐름

정상적인 새 실행의 그래프는 `MODEL → TOOLS → SETTLE → MODEL/END`다. 정책·실행·결과 처리는 TOOLS 안에서 기존 소유 모듈을 사용한다. 별도 legacy 노드는 예전 승인 checkpoint의 처리를 이어가기 위해 남아 있다.

| 영역 | 실제 구현 | 유지해야 하는 책임 |
| --- | --- | --- |
| 새 조사 조립 | `app/bootstrap.py::run_engagement` | Run/Mission 생성, 구성 검증, 실행 수명 관리 |
| 초기 작업 생성 | `orchestration/mission_bootstrap.py::bootstrap_mission` | 현재는 일반 READY 작업 한 개 생성 |
| native ReAct | `orchestration/graph.py`, `nodes/react.py` | 모델의 다음 도구 선택과 결과 기반 반복 |
| 여러 인스턴스 실행 | `orchestration/instance_driver.py::InstanceDriver` | 인스턴스별 runner, 독립 진행, 종료·취소 수명 관리 |
| 작업 소유권 | `task_graph/claim.py`, `nodes/observe.py` | coordinator 트랜잭션 안에서 Task claim |
| 실행 승인 | `engine.py`, `nodes/policy.py`, `nodes/execute.py` | 현재 목적·소유권 검증, Control 승인, 실행 전 재검증 |
| 실행·복구 | `runner.py`, `execution_reconciliation.py`, execution journal | 실제 실행과 결과 영수증의 연결, 불명확한 실행 재전송 방지 |
| 증거·결과 | verify/update 및 `semantic_completion.py` | 원본 결과, Graph 반영, 작업 완료와 목표 완료 구분 |
| 여러 인스턴스 재개 | `app/cli/commands/resume.py` | 전체 cohort 복구·예산 복원 후 선택된 인스턴스 재개 |

**중요한 미연결 지점:** InstanceDriver는 존재하지만 `run_engagement()`는 현재 RuntimeState와 AgentLoopRunner를 하나씩 생성한다. 초기 작업도 일반 작업 하나다. 전문 프로필을 선택했을 때 여러 역할을 준비하고 기동하는 새 조사 조립은 별도 연결이 필요하다.

### 3.2 혼동하면 안 되는 식별자

| 구분 | 의미 | 통합 원칙 |
| --- | --- | --- |
| 역할 / agent ID | Web 실행자, Mobile 실행자 등 직무 | 프롬프트·도구 적격성·작업 배정에 사용 |
| instance ID | 그 역할을 실제로 수행하는 실행 주체 | checkpoint·claim·승인·실행 복구의 소유권에 사용 |
| MissionTask | 현재는 목적·의존성·배정·완료 receipt를 가진 조사 작업 | 별도 typed 완료 조건은 R2-A에서 추가할 요구사항. 실행 목적과 진행 상태의 단일 기준 |
| WorkItem | 구체적 도구 호출을 수행하는 작업 항목 | 기존 WorkStore의 lease·충돌·실행 수명 유지 |
| action / invocation | 승인 및 실제 호출의 식별자 | 원본 결과·실행 영수증과 정확히 연결 |
| 전략 산출물 | 조사 질문·공격경로 가설·개정 이력 | 설명과 작업 분해의 출처이며 별도 실행 큐가 아님 |

dev는 `current_agent`와 `instance_id`를 구분한다. `active_task_id`도 인스턴스 상태에 속한다. checkpoint는 인스턴스별이고 native call 식별에도 인스턴스가 반영된다. 이 부분을 과거의 Run 단위 공유 상태로 되돌리지 않는다.

### 3.3 도구 탐색 방식

dev는 카드 우선 탐색을 사용한다. 기본적인 흐름은 `tool.list_cards → tool.get_detail → 실제 도구 호출`이며 program/reference도 각 상세 조회가 있다. 상세를 읽은 도구의 규격을 모델에 제공하는 방식이다.

다만 **상세 조회는 권한 부여가 아니며 모든 호출의 강제 선행 승인 절차도 아니다.** 적격한 등록 도구를 이미 아는 호출의 처리와 기존 admission 검증을 보존해야 한다. program 카드는 실행 프로그램의 설명이지 독립 MCP 서버가 아니다.

현재 `model_node`는 역할별 적격 도구의 합집합을 다루고, 상세 조회 handler는 공유 catalog/local 도구를 조회한다. 전문 역할을 추가할 때 현재 인스턴스의 역할·작업과 일관되는 카드/상세/호출 접점 검증이 필요하다. 이 사실만으로 현재 Control 우회가 확인되었다는 뜻은 아니다.

### 3.4 이미 있는 복구·종료 규칙

- Task의 `occupant_owner`에는 자동 만료 TTL이 없다. WorkItem의 실행 lease와 다른 개념이다. Task 이관은 명시적으로 해야 한다.
- 실행 전 submission과 실행 후 result receipt를 남기는 경로가 있다. 재개 시 원본 영수증 또는 provider 조회 결과로 복구하고, 불명확하면 보류한다.
- unsafe checkpoint를 단순 재실행하여 동일한 외부 효과를 다시 발생시키지 않는다. 영수증이 없는 옛 미완료 작업도 자동 안전 복구로 단정하지 않는다.
- 인스턴스 하나의 unresolved는 해당 작업 범위로 처리하고, 목표 완료는 모든 인스턴스의 미정산 실행을 확인한다.
- CLI cohort 재개는 선택되지 않은 인스턴스도 복구·예산 계산에 포함한다. 선택된 인스턴스만 보고 전체 예산을 초기화하지 않는다.
- `InstanceDriver`는 개별 결과를 준비되는 대로 전달하지만 전략을 생성하거나 전역 Mission을 스스로 완료시키지는 않는다.

## 4. 목표와 비목표

### 목표

1. dev의 native ReAct 및 승인·증거·복구 경로를 유지한다.
2. 전문 역할별 목적·프롬프트·도구 문맥을 적용한다.
3. 플래너의 전략을 검증된 MissionTask 의존 그래프로 변환한다.
4. 실행 결과와 증거 부족·유효성 변화가 후속 작업 또는 전략 개정으로 이어지게 한다.
5. 독립적인 준비 완료 작업은 병렬 수행하되, 공유 자원·예산·완료 판단은 일관되게 유지한다.
6. 단일 역할 모드와 전문 역할 모드를 명시적 설정으로 선택하고 저장된 실행은 같은 구성으로 재개한다.

### 비목표

- dev-jm의 과거 PLAN 중심 실행 엔진 복원, 새로운 범용 scheduler/DB/큐 도입.
- 모든 역할을 대상 접근 권한을 가진 실행자로 만들기.
- Finance Lab 경로·계정·정답·특정 도구 이름에 따른 분기 추가.
- 승인 완화, 임의 대상 확대, 실패한 결과를 성공 증거로 바꾸기.
- 네트워크 생성 코드 실행 권한의 자동 개방, 준비되지 않은 Mobile 실환경 검증을 완료로 표시하기.
- P0/P1/P2라는 파일명이나 버전 번호의 최댓값으로 런타임 구성을 자동 선택하기.

## 5. 목표 역할 구성

사용자 요청의 취지는 상하 관계의 거대 코어가 모든 행동을 지시하는 구조보다 **책임이 분명한 전문 역할의 협업**이다. 실행 수명 관리는 기존 드라이버가 담당하고, 각 역할은 정해진 계약에 따른 판단을 제공한다.

| 역할 | 입력과 책임 | 출력·권한 경계 |
| --- | --- | --- |
| 대화/접수 | 사용자 목표·범위·변경 지시 기록 | 정규화된 사용자 요구. 대화 자체로 승인 추정 금지 |
| 플래너 | 목표·Graph·조사 자료·증거 평가 | 조사 질문과 전략 개정. 도구 호출 강제 목록이 아님 |
| 작업 분해자 | 검증된 전략·적격 역할·대상 참조 | 원자 작업·의존성·성공 조건 제안 |
| Web 실행자 | 자신이 claim한 준비 완료 작업 | native ReAct를 통한 승인된 Web/API/Recon 작업 |
| Mobile 실행자 | 자신이 claim한 작업과 준비된 기기·앱 | native ReAct를 통한 승인된 Mobile 작업 |
| 리서치·증거 평가자 | 공개 자료·원본 결과·현재 Graph | 가설·근거·반증·유효성·미해결 조건. 완료 상태 직접 우회 변경 금지 |
| 코드 작성자 | 검증할 가설·참조 증거·허용 artifact 문맥 | 코드 artifact 및 제한된 fixture 검증. 대상 실행 허가와 별개 |

이는 **7개 논리 역할**이지 항상 7개 프로세스나 7개의 침투 워커를 동시에 띄운다는 뜻이 아니다. 1차 이식에서는 Web/Mobile을 대상 실행 인스턴스로 연결하고, 나머지는 명시적으로 호출되는 전문 판단·작업 처리 역할로 연결한다. dev-jm에서도 모든 역할이 schedulable 대상 실행자로 설정된 것은 아니다.

전략·분해 역할의 구조화 응답과 실행자의 native tool calling은 구분한다. 동일한 JSON 경로 응답 규격을 모든 역할에 강제하지 않는다.

## 6. 이식 원칙과 모듈 선택

| dev-jm 작업물 | 처리 | 이유 |
| --- | --- | --- |
| 역할 프로필, PromptSnapshot/PromptBinding, 역할 MD | 계약을 맞춰 이식 | dev native SYSTEM 접점과 저장·재개 binding에 연결 필요 |
| `specialist_planning.py`의 전략·분해·개정 검증 | 검증 로직 재사용, 출력 adapter 작성 | 현재 출력이 PathPlan/PathStep 중심이므로 dev MissionTask에 변환 필요 |
| `SerialSpecialistPlanningProvider` 전체 실행 방식 | 통째로 연결하지 않음 | 기존 PLAN/worker 의사결정 경로를 native 루프와 중복시키지 않기 위함 |
| `parallel_reasoning.py` | 공유 예산 예약 정책을 분리 재사용 | 과거 배치 fan-out을 InstanceDriver 위에 다시 씌우지 않음 |
| `resource_conflicts.py` | 현재 대상·도구 metadata에 맞춰 이식 | 서버/자원 충돌을 기존 WorkStore의 conflict key에 연결 |
| 증거 유효성·코드 artifact | 기존 증거 저장·local tool contribution에 연결 | 원본·평가·실행 결과의 분리를 유지 |
| 과거 EvidenceExpiryWaiter/path event 처리 | Task·instance·전략 개정 기준으로 적응 | 전역 active path/delegation 전제는 새 실행 모델과 다름 |
| ProviderBackoff | native/structured 공통 공급자 접점에 적용 | native 호출에도 동일한 제한·재시도 정책 필요 |
| 과거 engine/runner/state/bootstrap 파일 전체 | dev 파일을 기준으로 접점만 수정 | 인스턴스·영수증·cohort 기능 손실 방지 |

core ToolDescriptor/ExecutionContext도 dev를 기준으로 한다. `command_fields`, `approved_target`, instance 식별, execution recovery 규격을 보존한다. `selected_tool` 모드의 operation은 비어 있어야 하는 현재 계약과 legacy action 모드를 구분한다. 예전 고정 operation 목록 검증을 native 호출에 다시 강제하지 않는다.

## 7. 기능 요구사항

### R1. 구성과 프롬프트

- 기존 프로필/registry 조립 경계를 사용한다. 중앙 engine에 `if role == ...` 분기를 늘리지 않는다.
- 역할·프롬프트 버전·해시·출력 계약·사용 기능을 실행 binding에 저장한다. resume 중 파일이 바뀌었다고 조용히 다른 역할로 재해석하지 않는다.
- 공통 안전/데이터 취급 규칙, 역할 지시, 현재 작업 문맥을 분리한다. 공개 문서·도구 출력의 지시문을 시스템 권한으로 승격하지 않는다.
- native 실행자에게는 native 호출 규격을, 플래너·분해자에게는 해당 산출물 schema를 제공한다. 기존 `response_schema` 중심 compose를 무조건 공유하지 않는다.
- 운영값은 검증되는 설정으로 제공한다. dev-jm의 동시성 2, pool 64 등 특정 프로필 값은 복사할 필수 상수가 아니다.

### R2. 전략에서 MissionTask로 연결

- 플래너는 알려진 대상/사용자 참조와 증거를 바탕으로 조사 질문을 만든다. 추정 principal을 실제 등록 principal로 간주하지 않는다.
- 분해자는 질문별 작업, 배정 역할, 의존성, 성공 조건, 출처를 제안한다. 기존 strategy/atomization 검증을 먼저 수행한다.
- 검증된 결과만 coordinator를 통해 MissionTask로 반영한다. 의존성 순환, 알려지지 않은 역할/대상, 누락된 질문 커버리지를 거부한다.
- 동일 전략 개정과 동일 분해 출처의 재처리는 작업을 중복 생성하지 않는다. 저장된 출처와 개정 식별을 이용해 멱등성을 보장한다.
- **실행 진행 상태의 단일 기준은 MissionTask다.** 전략/Path 산출물은 설명·검증·개정 추적용이며 Task 상태와 경쟁하는 두 번째 실행 계획이 아니다.
- 전문 모드에서는 Mission을 먼저 만들고 검증된 원자 작업을 초기 Task로 등록한다. 일반 bootstrap의 포괄 READY 작업을 중복 생성하지 않는다. 일반 모드의 기존 단일 작업 생성은 보존한다. 사용자 전체 목표는 Mission에 유지하며 빈 작업 목록을 완료로 처리하지 않는다.
- 전략 수정 시 이미 실행된 결과와 완료 작업의 출처를 삭제하지 않는다. 관련된 미실행 작업만 검증된 개정으로 대체한다. 진행 중 호출은 영수증 정산 없이 취소된 것으로 간주하지 않는다.

### R2-A. 완료 조건의 단일 저장·검증 계약 — 피드백 1

**확인한 간극:** dev-jm의 `InvestigationQuestion`/PathStep에는 `completion_criteria`가 있다. 반면 기준 dev의 `MissionTask`에는 이 필드가 없고, `semantic_completion._task_review_payload()`는 `conditions=[]`를 전달한다. `purpose_hash()`는 objective만 해시한다. 따라서 제목·목적 복사만으로는 별도 조건을 끝까지 보존할 수 없다. 이는 현재 모든 Task가 잘못 완료된다는 주장이 아니라 이식 시 실제로 메워야 할 계약 차이다.

**선택한 방법:** 임의 provenance 문자열에 조건을 숨기지 않고 MissionTask에 최소한의 versioned 완료 계약을 둔다.

| 항목 | 구현 결정 |
| --- | --- |
| 조건 정의 | 기존 `orchestration/paths/models.py::CompletionCriterion`의 공유 가능한 DTO·기본 검증을 core 계약으로 추출하고 기존 경로에서는 재수출. core가 orchestration을 import하지 않음 |
| Task 저장 | `completion_contract_version`과 `completion_criteria` 추가. 새 전문 Task는 v2와 비어 있지 않은 known criterion 목록 필수. legacy는 v1/조건 미기재 상태로 구분 |
| 의미 보존 | description, kind, min_evidence, principal_refs, hypothesis_id 및 Task 대상 바인딩을 변환에서 보존. 전체 조건은 AND로 해석하며 묵시적 OR·조건 축소 금지 |
| 조건 식별 | 저장된 정규화 조건별 안정적 ID를 코드가 계산. 검토자는 조건 ID별 충족 여부와 원본 evidence 참조를 반환 |
| 읽기 경로 | worker의 active_task projection, `_task_review_subject`, `_task_review_payload`, 의미 검토 모두 저장 Task의 동일 조건 사용. 모델이 반환한 조건을 검사 기준으로 역수입하지 않음 |
| 해시·receipt | objective·조건·대상/주체 바인딩·계약 버전을 묶은 `completion_contract_hash`를 공개 helper로 계산하고 v2 task receipt에 저장. 기존 objective용 purpose_hash를 모든 경로에서 무작정 재정의하지 않음 |
| 저장·재개 | memory/SQLite Task 저장, codec, checkpoint binding, 보고 projection을 함께 변경. 조건 변경은 검증된 개정이며 과거 receipt가 새 계약을 증명하지 못함 |

완료 검사는 **원본 검증 → 조건별 구조 검증 → 의미 검토 → 현재 계약 재확인 → 기존 atomic Task 완료** 순서로 수행한다. 최소 증거 수는 원본 호출/결과 identity로 중복 제거해 센다. 같은 응답을 요약·복사한 레코드 두 개는 두 증거가 아니다. 요구 principal마다 실제 원본에 확인되는 바인딩이 있어야 하며, LLM이 붙인 계정 이름만으로 충족시키지 않는다. 데이터가 부족하면 inconclusive로 남긴다.

조건에 적힌 “검증을 수행했다”와 “취약점이 존재한다”도 구분한다. 두 계정의 정상 거절 응답은 비교 조사 완료의 근거일 수 있지만 취약점 입증은 아니다. 의미 검토가 accepted를 반환해도 원본·주체·최소 증거 조건을 통과하지 못하면 완료할 수 없다.

호환 정책: 기존 v1 Task/receipt는 원래 검증 경로에서 읽을 수 있다. v2 Task에 v1 receipt를 사용하거나 v2 조건을 알 수 없는데 빈 배열로 보정하는 것은 거절한다. 이미 저장된 v1 계약에 없던 조건을 추측해 채우지 않는다.

**필수 테스트:** 두 등록 principal의 비교가 요구되는 합성 Task에서 응답 하나, 같은 principal의 응답 둘, 같은 원본의 별칭 둘은 거절한다. 두 principal의 관련 원본과 모든 의미 조건이 충족되면 완료한다. resume 후에도 동일하며, objective는 그대로 두고 조건만 바꾸면 이전 receipt는 거절한다.

### R2-B. 중복 방지와 stale proposal 차단 — 피드백 3

**확인한 기반:** coordinator에 generation, pending operator intent, atomic change, outbox가 있다. 다만 `ChangeSet.token()`은 Mission/전체 Task/active path를 포함하므로 새 전략의 관련성 판정에 그대로 사용하면 무관한 형제 변경도 결과를 폐기할 수 있다. 기존 다른 소비자의 token 의미를 일괄 변경하지 않는다.

플래너 입력마다 코드가 다음 `PlanningInputStamp`를 생성한다. 이름은 계약 제안이며 새 범용 버전 DB를 만들라는 의미가 아니다.

- Run/Mission binding, 사용자 목표·범위·제약의 의미 fingerprint, stop/pause 등 admission generation.
- 기준 전략의 ID/개정. 적용하려는 branch의 expected base이며 단순 출력 일련번호가 아님.
- 판단에 실제 사용한 Task 완료 계약/의존 상태와 evidence·subject·validity 참조 및 버전.
- profile/prompt/tool eligibility binding. 모델이 이 stamp를 수정하거나 직접 발급할 수 없음.

적용 절차를 고정한다.

1. 불변 입력과 stamp를 캡처하고 LLM은 트랜잭션 밖에서 호출한다.
2. schema·참조·조건 검증 후 coordinator change 안에서 현재 Run admission, 목표/범위 fingerprint, 전략 expected revision, 관련 read-set을 다시 확인한다.
3. 동일 `proposal_id`의 동일 digest는 기존 적용 결과를 반환하고 작업·outbox를 다시 만들지 않는다. 같은 ID의 다른 digest는 충돌이다.
4. 새로운 proposal이라도 기반이 오래됐으면 `stale_plan`으로 거절한다. 자동 번호 증가나 최신 revision 덮어쓰기로 채택하지 않는다. 새 판단이 필요하면 현재 상태를 사용하며 호출도 같은 예산에 포함한다.
5. 통과했을 때에만 전략 채택·Task 변경·applied proposal 기록·outbox를 하나의 기존 atomic change로 반영한다.

증거/Graph는 다른 저장소일 수 있으므로 **외부 read-set 조회 뒤 Task 저장 사이의 경쟁**을 별도로 막아야 한다. coordinator callback 안에서 원격/모델 I/O를 수행하지 않는다. 공유 저장소가 제공하는 일관된 revision/CAS를 우선 사용한다. 그것이 없는 mutable subject/validity 갱신은 기존 writer 접점에서 관련 참조의 변경 fence를 coordinator에 먼저 올리고, 저장 완료 후 해제·버전을 확정한다. 변경 중·복구 미완료이면 그 참조를 읽은 제안만 보류한다. 이 fence와 제안 채택은 같은 coordinator로 직렬화한다. 단순 사전 hash 비교만으로 원자성 완료라고 판단하지 않는다.

원본 Evidence 자체는 불변 참조로 검증하고, 현재 유효성/정책 변경을 원본 삭제와 혼동하지 않는다. 전략에 입력한 목록의 “없음”을 근거로 판단했다면 관련 조회 집합의 변경도 read-set에 포함해 새 근거 누락을 막는다. 모든 Graph 갱신을 전역 무효화 사유로 만들지는 않는다.

**필수 테스트:** A 판단 중 목표 변경 → B 채택 → A 늦은 도착 시 A가 Task/outbox를 쓰지 못함. 같은 기반의 제안 두 개가 경쟁하면 하나만 채택. 동일 제안 재전달은 멱등. 무관한 형제 결과 변화는 허용. 관련 validity 변경이 최종 적용 직전에 발생하는 barrier 테스트에서는 거절/보류. 전체 Mission revision 숫자 변화만으로 의미가 같은 형제 작업을 거절하지 않음.

### R3. 새 조사와 독립 실행 드라이버 연결

- 전문 모드의 새 조사 진입은 하나의 Run/Mission/RuntimeDeps/Control을 조립하고 InstanceDriver를 사용한다.
- 역할 구성과 도구·기기 readiness로 실행 인스턴스를 선택한다. 실행 인스턴스에는 `current_agent`를, 실행 작업에는 `assigned_agent`를 명시한다.
- 동일 역할의 여러 인스턴스도 다른 instance ID를 사용한다. 기존 Task claim으로 하나의 작업을 중복 claim하지 못하게 한다.
- Web/Mobile가 준비되지 않았다는 이유로 가짜 대상이나 도구를 생성하지 않는다. 필요한 조건을 기록하고 실행 가능한 다른 작업은 계속한다.
- 공유 RuntimeState/Engine을 복제 없이 `gather`로 동시 수정하지 않는다. 이미 있는 인스턴스별 runner/history/checkpoint 격리를 사용한다.
- 한 실행자의 승인 대기나 늦은 응답이 다른 독립 실행자의 정산을 배치 장벽으로 막지 않게 한다.
- 새 조사와 재개에서 같은 cohort 수명·정산 규칙을 사용한다. 필요한 공통 조립 부분만 app 계층의 작은 helper로 추출하며 CLI 전용 로직을 복제하지 않는다.

### R3-A. 후속 작업의 기동 계약 — 피드백 2

**소유자:** app은 Run 수명을 조립하고, orchestration의 작은 cohort reconciliation 모듈이 기존 coordinator/outbox/EventWakeup/InstanceDriver를 연결한다. 이 모듈은 LLM 계획을 직접 만들거나 별도 Work 큐를 소유하지 않는다.

Task 생성·의존 해소·증거 평가 반영·명시적 승인/재개가 commit되면 기존 outbox로 wakeup을 전달한다. 소비자는 이벤트를 명령처럼 그대로 실행하지 않고 **현재 canonical Task와 Run 상태를 재조회**한다. 시작/재개 시 한 번 backlog를 대조하고, 전송 누락은 outbox 재전달 및 제한된 상태 대조로 회복한다. 변화 없는 주기 tick마다 LLM을 호출하지 않는다.

| 현재 상태 | 후속 READY Task가 생겼을 때의 결정 |
| --- | --- |
| 적격 인스턴스가 실행 중 | 기존 runner의 Task 선택 경로를 사용. 추가 슬롯이 있고 독립 작업이면 새 instance 가능 |
| 정상 종료했고 미정산 호출이 없음 | 종료 checkpoint를 다시 run하지 않고 같은 역할의 새 instance를 생성. 목적/증거는 canonical 저장소에서 받음 |
| 승인 대기 | 해당 요청은 원래 instance/action으로만 재개. 그 Task용 대체 instance 생성 금지. 다른 독립 작업은 별도 슬롯으로 가능 |
| 호출 결과 unknown/복구 대기 | 원래 instance의 receipt reconciliation 우선. unresolved 효과를 새 worker로 재실행하지 않음 |
| Run의 모델/실행 예산 소진 | 새 instance 생성으로 예산을 회피하지 않음. 명시적 정책 변경/새 조사 없이는 보류 |
| no-progress/repair 소진 | 같은 작업·근거로 재기동하지 않음. 검증된 새로운 입력/후속 작업이 있을 때만 상태 재평가 |
| 사용자 stop/pause 또는 terminal Run | 늦게 도착한 이벤트로 기동하지 않음. 허용된 명시적 resume 또는 새 Run 필요 |
| readiness/자원/슬롯 부족 | 원래 Task를 보존하고 이유 기록. 해당 조건 변경 시 다시 대조 |

사용자 중단은 요청의 적용 범위(Run 전체인지, 특정 Task/실행 목적인지)를 기존 intent 기록으로 보존한다. 특정 워커를 중단했는데 동일 Task를 새 instance에 재배정하여 중단을 우회하지 않는다. 관련 없는 다른 Task까지 전역 중단한 것으로 확대 해석하지도 않는다.

기동 예약도 기존 coordinator records에 최소한으로 영속화한다. `(run, task, completion_contract_hash, activation_generation)`을 키로 selected role/instance와 상태를 저장하고, 같은 작업의 중복 wakeup은 동일 예약을 조회한다. 이는 Task/Work 복제 큐가 아니라 **driver 기동과 crash 복구의 연결 기록**이다. 여러 실행 프로세스를 지원하는 구성에서는 기존 owner/lease 방식으로 단일 활성 Run 조립자를 보장한다.

예약할 때 readiness·동시성·목적 계약을 검사하고 기존 `claim_mission_task`로 선택한 instance의 Task claim을 같은 coordinator change에서 잡는다. initial runtime도 그 Task에 바인딩한다. 시작 시 Task가 바뀌었다고 임의의 다른 Task로 교체하지 않는다. 예약과 claim의 역할은 다르며, 실행 admission 때 현재 claim 재검증은 여전히 필요하다.

예약 후 실제 실행 전 중단되면 같은 예약을 복구한다. checkpoint/receipt가 있으면 기존 복구 경로를 쓰고, 이미 실행됐을 수 있는데 새 instance를 만드는 것을 금지한다. 시작 실패 시에도 예약 상태·원래 claim·미정산 호출을 함께 확인한 뒤 명시적으로 해제한다.

기존 `drive()`는 전달된 목록의 결과를 소모하는 API이지 READY Task 자동 감시 API가 아니다. 후속 기동을 최초 cohort 전체 종료 뒤에만 수행하지 않는다. 구현은 같은 InstanceDriver가 모든 실제 runner를 소유한 상태에서 새 batch의 iterator를 추가 소비하도록 최소 연결한다. app에 두 번째 runner task map을 만들지 않는다. 소비기 multiplexing이 기존 API로 수명 관리를 보장하지 못하면 driver 내부에 작은 submit/result-stream API를 추가하되, 실행 엔진·큐를 복제하지 않는다.

**필수 테스트:** Web 종료 후 Mobile 결과로 새 Web Task 생성 → Mobile의 나머지 장기 작업 종료를 기다리지 않고 Web 시작. 중복 이벤트·프로세스 재시작에도 한 Task를 한 번만 claim. 승인 대기는 원래 ID 보존. stop 이후 이벤트 재전달과 예산 소진 이후 새 작업은 기동하지 않음. 전체 종료 검사는 미소비 outbox와 기동 예약까지 포함.

### R4. 역할별 도구 탐색·실행 일관성

- 현재 인스턴스의 역할·작업·readiness를 기준으로 카드 목록, 상세 규격, native binding, local handler 실행의 적격성을 일치시킨다.
- 도구 이름을 프롬프트에 하드코딩하지 않는다. 등록 metadata와 실제 discovery schema를 사용한다.
- 역할별 읽기·작성·실행 권한 차이를 유지한다. 상세 조회 성공을 Control ALLOW로 해석하지 않는다.
- targetless local 도구에는 가짜 HostTarget을 채우지 않는다. 대상 실행 도구는 검증된 대상과 현재 승인 문맥을 요구한다.
- dev의 shell/program/gated execution은 유지한다. 코드 작성자에게 기존 shell 전체 권한을 자동 부여하거나, 반대로 기존 dev shell 기능을 통째로 비활성화하지 않는다.
- 자원 충돌 metadata는 기존 WorkStore에 전달한다. 동일 기기·세션·workspace에 대한 동시 변경은 선언된 자원 식별로 제어하고, 증명되지 않은 독립성을 가정하지 않는다.

### R5. 결과에서 후속 조사로 이어지는 폐쇄루프

목표 흐름은 다음과 같다. 화살표는 사용자 입력 또는 검증된 상태 변화를 통해 연결되며 모든 단계가 매 도구 호출마다 LLM을 호출한다는 뜻은 아니다.

`사용자 목표 → 전략 → 검증된 Task DAG → 준비된 역할 인스턴스 → native 실행 → 원본 결과/Graph → 증거 평가 → 작업 정산 → 후속 작업·전략 개정 또는 목표 정산`

- 증거 평가자는 원본 관측, 연구 자료, 가설, 검증된 취약점, 완료 근거를 구분한다. 실패 결과나 fixture 성공만으로 실제 대상 성공을 선언하지 않는다.
- 다음 판단은 현재 작업·관련 대상·근거의 버전을 기준으로 한다. 관련 없는 인스턴스의 Graph 변화가 모든 판단을 폐기하는 전역 stale 조건을 추가하지 않는다.
- 목적을 충족하는 새 증거, 유의미한 실행 실패, 유효성 변화, 사용자 목표 변경 등 관련 이벤트에서 후속 판단을 요청한다. 같은 이벤트·같은 상태의 중복 처리는 새 작업이나 새 LLM 호출을 반복 생성하지 않는다.
- 기존 coordinator/outbox와 wakeup을 사용한다. 모든 이벤트마다 전략 전체를 재생성하는 별도 polling 엔진을 만들지 않는다.
- 모르는 세션 만료 시간을 추측해 반복 타이머를 만들지 않는다. 유효성 정보가 있을 때만 범위가 제한된 재평가를 예약한다.
- 추가 도구·승인·기기·자격정보가 필요하면 구체적인 차단 조건을 기록한다. 이를 무의미한 재시도로 바꾸지 않는다.

### R6. 예산·429·중단 정책

**현재 거부 지점:** dev-jm의 `app/agent_profile.py`, `orchestration/settings.py`, `parallel_reasoning.reserve()`는 병렬 reasoning에 finite pool을 요구한다. `None` 검사 한 줄만 지우면 나머지 설정 검증과 `limit - spent - reserved` 산술이 깨진다. 이것은 dev의 모든 실행이 무제한을 거부한다는 의미는 아니다.

#### R6-A. 두 모드와 설정 이행 — 피드백 4

기존 설정 소유자에 versioned 요청 정책을 추가한다. 아래는 **구현할 설정 계약 예시**이며 현재 YAML에 복사하면 작동한다는 실행 안내가 아니다.

```yaml
llm_request_policy:
  mode: unlimited_requests
  max_requests: null
```

| 모드 | 유효 설정 | 동작 |
| --- | --- | --- |
| `bounded` | 양의 정수 max_requests 필수 | 모든 조사 역할이 한 Run 한도를 공유 |
| `unlimited_requests` | 명시적 mode와 max_requests=null | 누적 LLM 요청 수로는 중단하지 않되 기록·동시성·취소·기타 제한 유지 |
| mode 누락 / bounded의 null / 모순된 조합 | 기존 legacy 변환이 적용되지 않으면 오류 | 누락·0·음수·매우 큰 정수를 무제한의 별칭으로 쓰지 않음 |

기존 finite 설정은 bounded로 손실 없이 변환한다. 기존 순차 legacy 프로필은 기존 동작을 유지하며, 새 공유 정책을 쓰는 전문 모드에 진입할 때 정책을 명시한다. profile → orchestration settings → 실행 binding → 예약 모듈 → resume/CLI/report까지 함께 변경한다. 새 모드로의 암묵적 fallback은 없다.

무제한 요청은 **무제한 비용, 무료 공급자, 무제한 대상 요청, 무한 오류 반복**을 뜻하지 않는다. 별도 token/cost cap이 설정돼 있으면 그대로 적용한다. max_steps, 개별 판단/repair 상한, 동시성, provider rate limit, timeout, no-progress, ROE 시간창, Control action quota, stop/kill switch를 자동 해제하지 않는다. 사용자가 요청 수 제한만 풀었는데 다른 제한 때문에 멈추면 정확한 제한 이름을 표시한다.

사용자/운영자가 명시적으로 선택한 정책을 Run에 저장하고 CLI에 모드·누적 호출·unknown usage를 표시한다. resume은 원래 정책과 원장을 사용한다. env를 바꿔 bounded 실행을 무제한으로 조용히 재개하지 않으며, 첫 버전에서는 모드 변경 시 새 Run을 요구한다.

#### R6-B. 모든 요청의 공통 예약·계량

공통 LLM port의 실제 provider dispatch 경계에 한 개의 요청 계량 경로를 연결한다. native/structured/text, planner/atomizer/worker/research/code/completion, repair, provider fallback, 복구 의미 검토를 모두 포함한다. 해당 Run을 위해 수행하는 대화 모델 호출도 같은 Run에 귀속한다. Run 없는 설정 대화는 별도 계량 대상으로 표시하고 조사 호출을 그 경로로 우회시키지 않는다.

- 첫 planner 호출 **이전부터** 기존 coordinator 원장에 Run 정책을 bind한다. Mission 생성 전 호출이 필요하면 Run scope의 준비 기록을 만들고 같은 원장을 Mission binding으로 이어 붙이며 새 pool을 만들지 않는다.
- 실제 외부 요청 한 번마다 고유 request ID로 `reserved → dispatched → settled/unknown`을 기록한다. 같은 요청을 structured wrapper와 내부 dispatch에서 두 번 세지 않는다.
- bounded는 atomic change 안에서 `spent + reserved + 1 <= max_requests`일 때만 한 슬롯을 예약한다. unlimited_requests도 request 기록을 만들되 잔여 총량 산술만 생략한다. 별도 동시성 슬롯은 두 모드 모두 필요하다.
- dispatched 미정산 요청은 reserved에 남기고, settled 또는 전송 여부 unknown으로 정산할 때 한 번만 spent로 옮긴다. 같은 요청을 양쪽에서 중복 계산하거나 어느 쪽에서도 제외하지 않는다. 토큰 사용량 unknown과 전송 여부 unknown은 구분해 기록한다.
- durable dispatched 기록 후 provider에 요청한다. 호출 전 로컬 검증/중단으로 미전송이 증명된 reserved는 해제할 수 있다. dispatched 이후 취소·네트워크 오류·응답 유실은 요청 수에서 지우지 않는다.
- unknown token/cost는 0으로 간주하지 않는다. 요청 횟수는 소비된 것으로 기록하고 토큰/비용은 unknown으로 보존한다. 엄격한 비용 상한을 증명할 수 없으면 해당 정책의 다음 admission을 보류한다.
- 응답 오류로 repair하거나 provider를 바꾸는 다음 요청은 새 ID로 다시 계량한다. SDK 내부 자동 재시도는 보이지 않는 초과 요청을 만들 수 있으므로 원칙적으로 끄고 공통 retry 소유자에서 매 시도 계량한다. 실제 dispatch마다 계량되는 adapter일 때만 내부 재시도를 허용한다.
- 원장 저장 실패 시 새 외부 호출을 시작하지 않는다. 응답 후 저장 실패는 기존 reservation을 유지해 복구하고, 사용량 반환 오류를 성공·미사용으로 바꾸지 않는다.
- 모든 과거 instance/stage/예약을 합쳐 resume하며 새 instance와 새 전략 개정이 총량을 초기화하지 않는다. 통계는 원장의 투영으로 계산하며 역할별로 두 번째 authoritative 총량을 두지 않는다.

동시성 lease 회수와 요청 예산 환불도 구분한다. 중단된 프로세스의 실행 슬롯을 복구하더라도 dispatched/unknown 요청의 계량을 되돌리지 않는다. 장기 무제한 모드는 요청 원장/trace의 보존 정책과 저장 용량 오류를 처리해야 하며, 저장 장애에서 무계량 호출로 전환하지 않는다.

#### R6-C. 재시도와 검증

ProviderBackoff는 native/structured/text에 동일한 소유 경로로 적용한다. Retry-After는 취소 가능한 대기로 처리하며 기다리는 동안 Run stop/정책 유효성을 재확인한다. 일시적 429와 재시도 불가능한 quota/config 오류를 구분한다. 총량이 무제한이어도 한 판단의 repair 횟수·연속 오류·대기 시간에는 유한한 상한이 있다. 새 instance로 그 실패를 무한 초기화하지 않는다.

**필수 테스트:** bounded=100에서 여러 역할을 섞어 동시에 요청해도 실제 dispatch는 100회를 넘지 않는다. repair/cancel/unknown/fallback과 재개에도 동일하다. unlimited_requests에서는 100회를 넘는 **fake provider** 요청이 총량 때문에는 거부되지 않으며 모두 계량된다. 무제한에서도 stop·연속 429 상한·Control 거절·ROE 만료는 작동한다. 첫 planner와 완료 검토만으로도 총량 소진이 가능한 테스트를 포함한다. 실제 무제한 유료 실행은 문서 검수의 일부가 아니다.

### R7. 승인·실행 영수증·복구 보존

- 승인 요청과 표시에는 Run/instance/action 및 실제 대상을 연결한다. 마지막 이벤트의 승인 ID를 전체 cohort의 승인 ID로 취급하지 않는다.
- 저장된 승인 대상과 현재 요청이 다르면 변경하지 않고 차이를 표시한다. 다른 ID로 임의 대체하지 않는다.
- native 호출도 기존 admission → Control → submission journal → gateway → receipt → verify/update를 통과한다.
- `ExecutionReference`, invocation digest, approved_target, provider recovery를 dev 계약 그대로 유지한다. 등록/프롬프트/상세 조회로 실행 권한을 생성하지 않는다.
- 불명확한 외부 실행은 unknown/held로 보존한다. 실패한 인스턴스의 실행을 새 인스턴스가 다시 제출해 성공처럼 보이게 하지 않는다.
- Task 소유권의 명시적 해제/이관은 미정산 실행과 검증되어야 한다. heartbeat 만료만으로 Task occupant를 지우지 않는다.

### R8. 부분 종료와 전체 종료

- 인스턴스 종료, Task 완료, Mission 완료를 별개의 사건으로 처리한다.
- 한 인스턴스의 no-progress/unresolved가 독립 실행 중인 다른 작업을 전역 BLOCKED로 만들지 않는다.
- 전역 종료 판단은 준비된 후속 작업, 진행 중 작업, 승인 대기, 예약된 관련 판단, 미정산 영수증 및 전체 cohort를 확인한다. 아무 인스턴스도 현재 도구를 실행하지 않는다는 이유만으로 완료시키지 않는다.
- 최종 성공은 기존 semantic completion의 목표 커버리지·원본 증거 검증을 통과해야 한다. Task 목록이 비거나 모든 워커가 멈췄다는 사실만으로 성공하지 않는다.
- 모든 진행이 외부 조건/예산/정책에 막혔다면 이유와 미완료 작업을 보고하고 보류·종료한다. 안전 제한을 없애서 계속 돌리는 것을 폐쇄루프 완성으로 보지 않는다.

## 8. 구현 순서와 완료 조건

| 단계 | 작업과 주요 접점 | 단계 통과 조건 |
| --- | --- | --- |
| M0 기준선 고정 | 최신 dev 별도 통합 브랜치, dev/jm 정확한 SHA 기록, 기본 테스트·fixture 기준선 확보 | 기존 실패와 이식 회귀를 구분할 수 있음 |
| M1 역할/프롬프트·요청 정책 | agent_profile, prompt ports/loader, native SYSTEM, 공통 요청 계량/binding | MD/native schema 보존. bounded/unlimited_requests의 설정·계량·재개와 429 검증. 실제 모델을 쓰는 후속 단계보다 선행 |
| M2 전략→작업 계약 | specialist 검증, Task 완료 계약/receipt, 관련 입력 stamp/CAS, bootstrap/coordinator/outbox | 조건 손실·중복·stale 제안 거절. 일반 모드 호환. 1개/2개 원본의 완료 거절·허용 검증 |
| M3 단일 실행 역할 전체 순환 | 전문 fresh-start와 후속 activation, 역할별 도구 view, 증거 평가 | 첫 planner부터 모든 stage가 공통 원장 사용. Web 종료 후 새 Web Task가 실제 기동되어 목표 정산까지 연결 |
| M4 다중 인스턴스 경쟁 검증 | 기존 Driver/claim/resource lock, activation dedup, approval/cohort settlement | M1의 공통 예산을 재구현하지 않고 동시 요청·취소·부분 종료·늦은 계획 경쟁 검증 |
| M5 복구 및 전문 산출물 완성 | execution reconciliation 회귀, 유효성 이벤트, code artifact, Mobile readiness | 중단/재개 중 중복 실행 없음, 실패/unknown 증거로 성공 불가 |
| M6 통합 검수·인계 | dev 회귀 + 이식 회귀 + 명시적 동의된 실환경 평가, 운영 문서 | 아래 G1–G12 충족 및 미검증 환경 별도 표시 |

2026-10-11 로컬 진행 기록: M5의 Evidence append 및 새 Graph mutation 저장 완료
응답 유실 복구는 기존 writer/coordinator와 provider 원자적 receipt 접점으로 구현했다.
현재 Graph 값이나 TTL을 근거로 구형 writer를 해제하지 않는다. 활성 cohort의 관측된
증거 만료는 기존 Driver 대기 → 현재 원본 재검사 → 기존 planner/Task 기동으로 연결했다.
종료·보류 Run을 깨우는 별도 timer service는 아니다. entered+checkpoint 부재의 명시적
원본 수집 API는 기존 recovery port/append-only receipt로 구현했다. 일반 재개도 같은
수집 검증을 사용한다. 원본 수집은 checkpoint 없는 actor 재개나 claim 해제가 아니다.
명시적 `collect-originals` operator CLI도 같은 API/원래 설정/Run 소유권에 연결했다.
원본 보존·수집은 checkpoint 없는 전체 상태/계량 복구가 아니다.
기존 점유 해제 API의 미완료 Task 인계는 거절하도록 보강했고, canonical 종료 Task의
소유권 확인 정리만 허용한다. 공유 요청 원장도 동일한 identity/state/usage 검사를
예약·조회·정산에 적용한다. 이는 원본 수집/예산 기록이 자동 인계 권한으로 바뀌는
것을 막는 경계이며, 미완료 인계 구현 완료는 아니다. 미완료 인계에는 명시적 사람
지시와 검증된 최신 상태·원본/처리/계량, 원래 actor 재개 차단·claim 변경의 원자적
연결을 함께 설계/검증한다. 초기 activation을 최신 checkpoint로 승격하지 않는다.
전체 상태/계량 재구성·명시적 claim handoff, 구형·불명 writer/orphan,
실환경 평가 및 M6는 완료로 판정하지 않는다. 구현 사실·검수 수정 이유와
시험 범위는 [구현 기록 9~16절](dev-first-specialists-implementation-20261010.md)과
[복구 계약 5.4~5.8절](../contracts/activation-recovery.md)에 구분한다.
관측된 만료 기동의 적용 범위와 보류 조건은
[증거 유효성 계약](../contracts/evidence-reuse-validity.md)에 기록한다.

2026-10-11 M5 최종 로컬 추가: 검토된 artifact의 `code.validate`를 기존
network-none supervised workroom에 선택 연결했다. 호스트/실제 대상 실행 없이
bounded JSON case·격리·정리·immutable 원본·한 번의 admission을 검증한다.
Mobile을 포함한 역할 readiness는 실제 조립 catalog/기존 eligibility에 연결했고,
descriptor 전체 조회가 Mobile 승인/hash 정책을 우회하던 접점을 수정했다.
UNKNOWN health의 기존 기동 의미는 보존하고 관측된 HEALTHY와 구분한다.

| M5 필수 범위 | 로컬 완료 판단과 남은 경계 |
| --- | --- |
| 실행 복구 | 기존 checkpoint/provider/receipt 기반 복구와 원본 수집, 증거/Graph 응답 유실 검증. 증명 없는 경우 명확한 held이며 중복 외부 실행·미완료 claim 해제 없음 |
| 유효성 사건 | 현재 원본·관측된 조건/만료 소비와 활성 cohort wakeup 연결. 종료 Run 부활이나 추정 만료 timer는 없음 |
| 코드 산출물 | 작성/독립 검토/불변 보존 + 명시적 offline fixture 실행·기존 Docker 격리/timeout/cancel 정리 검증. 합성 성공은 대상 증거/Task 완료가 아님 |
| Mobile readiness | 역할 카드·선택·진단이 같은 실제 정책 catalog를 사용. 건강/적격성 관측만 완료했으며 실제 승인된 기기·앱 검증은 미실시 |

위 범위의 **M5 로컬 구현·검증을 완료**로 기록한다. 완료 기준을 낮춘 것이 아니라
기존 M5/G8의 “복구 또는 명확한 보류”를 적용한다. checkpoint 없는 전체 최신 상태
재구성·명시적 미완료 handoff·원본 불명 orphan을 해결했다고 주장하지 않는다.
그 자동화에는 현재 없는 사람 지시/최신 상태/재개 차단 증명이 필요하므로 held를
유지한다. 실제 Mobile/LLM 평가와 기존 dev Docker 안전성 실패 3건 및 최종 인계는
M6/운영 검수에서 별도 해결·검증해야 한다. 실제 대상 코드 실행 규격은 미확정·비활성이다.
설정·port·현재 바인딩·중단/재시작 결과는
[offline fixture 계약](../contracts/code-fixtures.md)에 명시한다.

2026-10-11 후속 M6 로컬 검수: 기존 shell 취소의 supervisor 조기 KILL과
불완전한 정리/상태 조회를 수정하고, 원본 보존 때 과거 PGID로 형제 작업을 죽이는
실패도 직접 재현·수정했다. network-none 시험은 장치 이름 수가 아닌 실제
활성 interface/주소/경로를 검사하며 네트워크 허용을 넓히지 않는다. gated install은
필수 로컬 이미지 없음을 자원 생성 전에 확인해 skip하며 실제 OPEN/DENY는 미검증이다.
fixture의 read-only root/전체 disk quota를 보장한다는 부정확한 설명도 정정했다.
실환경 평가·운영 인계를 포함한 M6 전체 완료로 표시하지 않는다. 최종 검사 수와
수정 근거/보류 조건은 [구현 기록 16절](dev-first-specialists-implementation-20261010.md)에 남긴다.

2026-10-11 설치 환경 후속 확인: 필수 fixture 이미지는 준비했고 오프라인
shell/fixture 검증은 34 passed / 1 skipped다. 실제 gate 4건은 현재 Docker
커널 `6.12.54-linuxkit`의 `CONFIG_NFT_QUOTA` 부재로 OPEN 전에 실패했다.
코드 이식 회귀나 사용자 보안키 누락으로 분류하지 않으며, 규칙 설치 실패 시
network transition/RPC 미시작과 full-charge quarantine 유지의 회귀를 추가했다.
검증 실패 fixture의 자원 누락도 정정했다. 같은 실패를 숨기거나 kernel quota를
삭제해 G1을 통과시키지 않는다. 지원 kernel에서 실제 OPEN/proxy/DENY/계량·회복
검증이 필요하다. Mobile은 준비 점검까지만, LLM은 기존 100회·0달러 조건 유지로
유료 호출하지 않았다. 이들 실환경 평가와 운영 인계는 M6 미완료 조건으로
유지한다. 근거·최종 검사 범위·사용자 준비는
[구현 기록 17절](dev-first-specialists-implementation-20261010.md)에 명시했다.

2026-10-11 후속 Linux VM 검증: 운영자가 준비한 Lima VM에서 quota 모듈과 실제
기존 ruleset 지원을 확인했고, 동일 로컬 이미지·현재 소스의 합성 gate integration
**5 passed(생략 없음)**, 관련 unit **63 passed**를 확인했다. 버전 관리 밖의
mock/prober 입력 의존성을 시험 전용 fixture로 정리했으며 운영 권한/규칙은
완화하지 않았다. 프록시 경유 설치·직접 접속/DNS/IPv6 차단과 허용 요청의 종료
계량, unknown full-charge quarantine을 입증했다. 실제 Mobile/LLM 평가·운영 pin/
정책과 최종 인계는 남고 M6 전체 완료로 판정하지 않는다. 근거와 재실행 방법은
[구현 기록 18절](dev-first-specialists-implementation-20261010.md)에 기록했다.

M1에서 공통 요청 정책을 완성하고 M2/M3의 첫 모델 요청부터 사용한다. 워커가 하나여도 planner/atomizer/reviewer 호출은 같은 원장에 포함된다. unlimited_requests 지원 검증은 fake provider로 가능하며 실제 무제한 실행을 요구하지 않는다. M6의 실제 모델 호출·비용·대상 시험은 별도 승인 범위 안에서 실행한다. 이전 대화의 특정 평가 횟수를 새 통합 실험의 무제한 권한으로 해석하지 않는다.

이식 시 기존 dev 파일을 먼저 기준으로 열고 최소 접점만 변경한다. 신규 모듈도 의존 계약이 바뀌었는지 확인 후 옮긴다. 충돌을 피하기 위해 core/app/control의 기존 계약 전체를 dev-jm 버전으로 교체하지 않는다. 최신 dev에서는 sqlite 지원 모듈 위치도 바뀌었으므로 과거 import를 복원하지 않는다.

## 9. 검증 게이트

| 게이트 | 반드시 입증할 내용 |
| --- | --- |
| G1 기본 호환 | 전문 프로필을 사용하지 않는 dev의 native loop, targetless 도구, shell/gated 기능과 기존 설정이 유지됨 |
| G2 프롬프트/격리 | 역할별 MD가 실제 모델 요청에 적용되고, 인스턴스별 history·목적·binding이 섞이지 않음 |
| G3 Task 일관성 | 질문 커버리지·DAG·역할 배정, 완료 조건 보존·조건별 검증·v1/v2 receipt 호환, 중복 및 오래된 제안 거절 |
| G4 도구 계약 | 카드/상세/native 호출의 역할 적격성 일치, 실제 MCP schema 일치, selected_tool와 legacy action 계약 모두 유지 |
| G5 독립 진행 | 느린/승인 대기 인스턴스와 별개로 진행·정산하며, 먼저 종료한 역할의 후속 Task가 전체 batch 종료 전 기동됨 |
| G6 소유권/자원 | 같은 Task 중복 claim 방지, 다른 인스턴스의 claim 침범 거부, 공유 세션/기기/파일 변경 충돌 제어 |
| G7 예산/429 | bounded의 전체 요청 상한, unlimited_requests의 상한 없는 계량, 두 모드의 취소·unknown·유한 재시도·재개 일관성. 첫 planner/repair/완료 검토 포함 |
| G8 복구 | 실행 후 결과 저장 전/후 장애, unsafe checkpoint, unknown receipt에서 중복 외부 실행 없이 복구 또는 명확한 보류 |
| G9 승인 | Run/instance/action 별 요청 표시·승인·재개 일치, stale 승인 거부, 다른 인스턴스 승인 혼용 불가 |
| G10 증거/완료 | 실패 원본·연구자료·fixture만으로 취약점/목표 성공 불가, 한 Task unresolved가 전역 조기 종료로 확산하지 않음 |
| G11 폐쇄루프 | 결과→후속 Task/전략 개정→새 기동이 연결됨. 중복 wakeup·stale 계획·stop 직후 이벤트가 재실행/작업 부활을 만들지 않음 |
| G12 인계 | 실제 검증한 역할/환경/기능과 준비만 된 기능을 분리해 기록하고, 비밀정보 없는 trace로 재현 가능 |

기존 회귀 대상으로 확인한 테스트 파일은 다음과 같다. 아래 목록은 이번 조사에서 통과시켰다는 뜻이 아니다.

- `tests/integration/test_native_react_loop.py`
- `tests/integration/test_targetless_native_flow.py`
- `tests/integration/test_instance_driver_react.py`
- `tests/integration/test_instance_task_claim.py`
- `tests/integration/test_instance_admission_race.py`
- `tests/integration/test_execution_receipt_reconciliation.py`
- `tests/integration/test_resume_cli_cohort.py`
- `tests/integration/test_resume_reconcile.py`
- `tests/integration/test_shell_native_pipeline.py`
- `tests/unit/test_tool_discovery.py`
- `tests/unit/test_instance_settlement_binding.py`
- `tests/unit/test_semantic_completion_receipts.py`

추가 통합 시나리오는 합성 대상에서 먼저 검증한다: 목표 접수 → 전략·분해 → 독립 작업 두 개 claim → 역할별 도구 조회 → 승인된 실행 → 한 작업 보류/다른 작업 정산 → 증거 평가 → 후속 작업 → 최종 성공 또는 구체적 unresolved. 이 전체 연결을 확인하기 전에는 모듈별 테스트 수만으로 통합 완료라고 판정하지 않는다.

이미 dev에서 인스턴스별로 분리한 provider call ID/checkpoint를 보존하는 회귀도 포함한다. 아직 재현하지 않은 같은 인스턴스 내 후속 턴 ID 충돌 등을 추정하여 새 전역 식별 체계를 미리 추가하지 않는다.

## 10. 병합 방식과 역할별 인계

실제 구현 승인 후 **고정한 최신 dev에서 별도 통합 브랜치를 시작**한다. dev-jm은 기능 원본으로만 사용한다. 단계별 작은 커밋으로 이식하고 각 단계에서 dev 기준선 회귀를 확인한다. 원격 dev가 이후 바뀌면 기준 SHA와 중첩 접점을 다시 확인한다.

| 담당 영역 | 작업 | 변경해서는 안 되는 경계 |
| --- | --- | --- |
| Agent/Orchestration | 전문 판단 계약, Task 분해 adapter, 인스턴스 기동, 피드백 이벤트, 전체 정산 | Control 우회·가짜 대상·두 번째 canonical 작업 상태 |
| Capability/MCP | 역할별 catalog projection 접점, 도구 schema/readiness, 선언적 자원 metadata, 복구 계약 호환 | 모델이 상세를 조회했다는 이유로 실행 허가 부여 |
| Control | 새 조립이 기존 admission/예산/승인/approved_target을 유지하는지 검수 | 역할 추가를 이유로 기본 범위·고위험 승인 완화 |
| CLI/App | 새 조사와 cohort 재개의 공통 조립, instance별 승인·부분 결과 표시 | 첫 인스턴스 종료를 전체 Run 완료로 표시 |

작업 등록과 도구 권한의 담당을 섞지 않는다. 새로운 공통 의미/계약이 실제 필요할 때만 소유 팀과 확정하고, 전문 역할 추가만으로 기존 core enums나 Control 정책을 확장하지 않는다.

## 11. 코드 근거 색인

다음 경로는 저장소 상대 경로이며 line 번호는 표시한 고정 커밋 기준이다. 후속 커밋에서는 함수명을 함께 사용해 확인한다.

| 커밋 | 파일·심볼 | 판단 근거 |
| --- | --- | --- |
| dev `153265e` | `src/redteam/orchestration/graph.py::build_graph` | 정상 MODEL→TOOLS→SETTLE 및 legacy resume 노드 구분 |
| dev `153265e` | `src/redteam/app/bootstrap.py:909`, `run_engagement` | 새 실행이 단일 runtime/runner로 조립됨 |
| dev `153265e` | `src/redteam/orchestration/mission_bootstrap.py:15` | 일반 초기 READY 작업 한 개 |
| dev `153265e` | `src/redteam/orchestration/instance_driver.py:74` | 기존 독립 인스턴스 드라이버 |
| dev `153265e` | `src/redteam/orchestration/task_graph/claim.py:27` | 트랜잭션 내 Task occupant 소유권 |
| dev `153265e` | `src/redteam/orchestration/nodes/react.py:330` | native 판단, 카드 기반 상세 노출, 인스턴스 호출 문맥 |
| dev `153265e` | `src/redteam/orchestration/tool_discovery.py:520`, `:601` | 카드 목록과 상세 조회 handler |
| dev `153265e` | `src/redteam/orchestration/runner.py:863` | checkpoint와 receipt 기반 복구 구분 |
| dev `153265e` | `src/redteam/orchestration/execution_reconciliation.py:45` | 영수증 검증 및 정상 결과 처리 경로를 통한 재반영 |
| dev `153265e` | `src/redteam/app/cli/commands/resume.py:638` | 전체 cohort 정산과 선택적 재개 |
| dev `153265e` | `src/redteam/orchestration/semantic_completion.py:803`, `:1312` | Task 완료와 전체 목표 완료의 서로 다른 검증 범위 |
| dev `153265e` | `src/redteam/core/contracts/tool.py`, `execution.py` | selected_tool·approved_target·execution recovery 계약 |
| dev-jm `8af7a6a` | `src/redteam/orchestration/specialist_planning.py:75`, `:110`, `:167`, `:432` | 전략/분해/개정 검증과 기존 serial provider 의존 구조 |
| dev-jm `8af7a6a` | `src/redteam/orchestration/prompts.py`, `src/redteam/core/ports/agent_prompts.py` | 역할별 프롬프트와 snapshot/binding |
| dev-jm `8af7a6a` | `src/redteam/orchestration/parallel_reasoning.py:49` | 기존 공유 reasoning 예산 예약 |
| dev-jm `8af7a6a` | `src/redteam/orchestration/resource_conflicts.py:15` | metadata 기반 실행 충돌 키 |
| dev-jm `8af7a6a` | `src/redteam/llm/provider_backoff.py:15` | 공급자 제한 대응 모듈 |
| dev-jm `8af7a6a` | `config/agents/specialists-p2.yaml` | 논리 역할·실행 역할·기능 설정의 구분 |

## 12. 최종 완료 판정

**역할 정의 파일을 복사한 시점도, 병합 충돌이 사라진 시점도 완료가 아니다.** dev의 기존 실행/복구 회귀를 유지한 상태에서 전문 역할의 전략·분해가 실제 Task로 저장되고, 인스턴스가 승인된 도구로 실행하며, 결과 평가가 다음 작업 또는 검증된 종료로 이어지는 전체 순환을 입증해야 한다.

Mobile 실환경이나 생성 코드 대상 실행처럼 준비·승인이 없는 항목은 별도 미검증으로 남긴다. 합성 검증 통과를 실제 금융권 침투 성능이나 취약점 발견 보장으로 표현하지 않는다.

## 13. 외부 에이전트 조사와 채택·비채택 근거

조사일은 2026-10-09이며 아래는 소스 정적 확인이다. 외부 프로젝트를 실행하거나 성능·보안성을 인증한 결과가 아니다. 원본 `Autumn-27/ARTEX` 페이지는 이번 웹 조회에서 내용을 확인하지 못했다. 삭제·비공개 전환 여부까지 단정하지 않고 사용자가 지정한 fork의 고정 SHA만 근거로 사용한다.

### 13.1 지정 ARTEX_ko

저장소: [Doyoun510/ARTEX_ko](https://github.com/Doyoun510/ARTEX_ko), 확인 SHA `934d3dcad0cb2a2e792f7ca8561648119963fbe2`.

| 관찰한 실제 코드 | 우리 프로젝트에 채택 | 그대로 가져오지 않는 부분 |
| --- | --- | --- |
| `Engine.Run`, plannerLoop/workerLoop, `NotifyDone`: 지속 워커가 작업을 가져가며 결과 이벤트는 planner를 깨움. pause/terminal gate 존재 | 결과 이벤트 → 현재 READY 상태 대조 → 역할 기동이라는 연결 | 새 Go 엔진·DB 큐, 고정 주기 worker polling, heartbeat마다 LLM 계획하는 동작 |
| `ClaimIntent`: open→running 조건부 DB update | 기존 dev Task claim을 통한 단일 소유권 | 이미 있는 dev claim을 다른 저장소로 교체 |
| `prove_goal`: fact/finding 참조를 goal과 연결하고 met 상태를 기록 | 목표와 증거의 명시적 연결 | 해당 handler에서 두 principal·min_evidence 충족까지 검증한다고 보지 않음. dev의 원본·조건별 완료 gate 유지 |
| worker/planner가 `MaxTurns=0`을 unlimited로 SDK에 전달 | 총량 정책과 timeout/취소/사용량 기록을 분리한다는 아이디어 | 0이라는 암묵 sentinel이나 SDK 내부 무제한 보장까지 추정. 우리 쪽은 explicit mode 사용 |
| `llm_usage`: 성공·오류의 호출 계량과 역할별 집계. 저장은 best-effort | 원문/내부 추론 없이 호출 ID·역할·사용량·오류를 기록 | best-effort 통계 테이블을 엄격한 총량 admission 원장으로 간주하지 않음 |

소스: [실행·기동 루프](https://github.com/Doyoun510/ARTEX_ko/blob/934d3dcad0cb2a2e792f7ca8561648119963fbe2/server/engine.go#L748), [원자적 claim](https://github.com/Doyoun510/ARTEX_ko/blob/934d3dcad0cb2a2e792f7ca8561648119963fbe2/db/exploration.go#L1172), [목표 증명 handler](https://github.com/Doyoun510/ARTEX_ko/blob/934d3dcad0cb2a2e792f7ca8561648119963fbe2/agent/tools.go#L1157), [worker 옵션](https://github.com/Doyoun510/ARTEX_ko/blob/934d3dcad0cb2a2e792f7ca8561648119963fbe2/agent/worker.go#L455), [계량 테이블](https://github.com/Doyoun510/ARTEX_ko/blob/934d3dcad0cb2a2e792f7ca8561648119963fbe2/db/llm_usage.go).

**해석 주의:** 이벤트 기동과 intent CAS가 있다고 해서 “사용자 목표가 바뀐 뒤 도착한 옛 전략의 전체 read-set 검증”까지 입증된 것은 아니다. R2-B는 확인한 dev coordinator 기능과 팀원 요구를 바탕으로 정한 우리 설계다. ARTEX_ko가 이미 동일하게 구현했다고 주장하지 않는다.

### 13.2 Shannon

저장소: [KeygraphHQ/shannon](https://github.com/KeygraphHQ/shannon), 확인 SHA `2468b67d9e40d70d0b189462c5041c24e1dc886c`.

- 구조화된 제출 도구는 schema와 그룹 간 검증을 통과한 첫 결과만 채택하고, 두 번째 accepted 제출은 거절한다. **제안과 채택의 분리**를 참고한다. 이는 현재 버전과의 CAS나 목표 충족 검증 전체를 대신하지 않는다. [submit-validation](https://github.com/KeygraphHQ/shannon/blob/2468b67d9e40d70d0b189462c5041c24e1dc886c/apps/worker/src/ai/reconciliation/submit-validation.ts)
- queue validation은 필수 산출물/큐 누락을 검사하고 재시도 가능 오류를 구분한다. **산출물 검증을 다음 단계의 진입 조건으로 사용**하는 방식을 참고한다. 파일 존재가 우리 원본 증거 검증을 대신하지 않으며 고정 취약점 분류 목록도 이식하지 않는다. [queue-validation](https://github.com/KeygraphHQ/shannon/blob/2468b67d9e40d70d0b189462c5041c24e1dc886c/apps/worker/src/services/queue-validation.ts)
- retry 설정은 에이전트 재시작과 provider 재시도를 구분하여 중첩 재시도를 줄인다. **재시도 소유자 분리**를 참고하되, 코드에 있는 8회/120초를 우리 기본값으로 복사하지 않는다. SDK retry가 내부 요청 횟수를 숨길 수 있어 우리 총량 정책은 dispatch별 계량을 요구한다. [retry-settings](https://github.com/KeygraphHQ/shannon/blob/2468b67d9e40d70d0b189462c5041c24e1dc886c/apps/worker/src/ai/pi/retry-settings.ts)
- workflow는 재개 시 완료된 agent 및 보고 진행 상태를 읽는다. **부분 완료 상태의 영속화**를 참고하되 dev의 coordinator/checkpoint 대신 Temporal을 추가하지 않는다. [workflows](https://github.com/KeygraphHQ/shannon/blob/2468b67d9e40d70d0b189462c5041c24e1dc886c/apps/worker/src/temporal/workflows.ts)

### 13.3 PentAGI 및 조사 한계

[PentAGI 공식 저장소](https://github.com/vxcontrol/pentagi)는 다중 에이전트 침투 테스트 시스템으로 소개되며, 원격 트리에서 task/subtask/controller 및 provider limits 모듈의 존재를 확인했다. 그러나 임시 소스 복제의 자동 승인 검토가 서비스 사용 한도로 실행되지 않았고, 웹의 해당 고정 소스 조회도 실패했다. 이 제한은 코드의 안전성 판정이 아니다. 그 결과 네 쟁점의 원자성·상한 강제·복구 동작을 확인했다고 주장하지 않으며 설계 채택의 핵심 근거로 삼지 않는다.

ARTEX_ko와 Shannon의 위 설계 비교는 소스를 직접 확인했다. 두 저장소의 라이선스 파일은 AGPL-3.0으로 표시되어 있다. 이번에는 코드·프롬프트를 복사하지 않고 아이디어를 비교했으며, 향후 실제 코드 재사용 여부는 별도 라이선스 검토 대상이다.

### 13.4 효율성에 대한 최종 선택

| 피드백 | 선택한 방식 | 효율적인 이유와 검증 범위 |
| --- | --- | --- |
| 1 조건 전달 | 기존 criterion DTO를 공유 계약으로 추출 + Task/receipt에 versioned 계약 | 별도 조건 DB나 프롬프트 문자열 파싱 불필요. 자동 검사는 증거 구조를, 의미 검토는 내용 충족을 담당 |
| 2 워커 기동 | event/outbox reconciliation + 기존 Driver + 최소 activation 기록 | 대기만 하는 LLM 호출·두 번째 실행 엔진 불필요. 정상 종료 후 새 instance는 숨은 세션 상태 혼합을 줄임 |
| 3 늦은 계획 | proposal 멱등 key와 관련 입력 stamp/CAS를 분리 | 무관한 형제 변경 때문에 정상 모델 출력을 버리는 비용을 줄이면서 옛 계획 채택은 차단 |
| 4 사용량 | dispatch별 공통 원장 + bounded/unlimited_requests 정책 분기 | 단계별 pool 복제와 호출 누락 방지. 무제한에서도 동일 계량·재개 구현을 사용 |

이는 복잡도와 불필요한 호출을 줄이기 위한 설계 판단이며 실제 비용/속도 향상을 측정한 주장은 아니다.

## 14. 설계 검수 및 수정 이유

팀원 피드백은 원래 방향을 폐기하라는 지적이 아니라 구현 계약을 명확히 하라는 요구로 반영했다. 업데이트 초안 작성 뒤 생산자→저장→소비자→재개→완료 경로와 경쟁·오류 경로를 재검수해 다음을 수정했다.

| 검수 항목 | 수정 내용 | 확실한 수정 이유/근거 |
| --- | --- | --- |
| 현재 구현과 요구사항 혼동 | MissionTask 설명에서 별도 완료 조건이 이미 있는 것처럼 보이던 문구 수정 | 실제 dev MissionTask에는 필드가 없고 task review가 conditions=[]를 사용 |
| 조건 저장만으로 충분하다는 오해 | worker/reviewer/receipt 소비 경로, 조건별 결과·원본 중복 제거·principal 커버리지 검증 추가 | min_evidence만 저장하거나 LLM accepted만 믿으면 두 계정 비교가 응답 하나로 끝날 수 있음 |
| 기존 receipt 회귀 위험 | objective purpose_hash를 전역 재정의하지 않고 v2 completion_contract_hash를 추가, v1 호환 명시 | 기존 receipt 검증과 action 목적 binding이 같은 objective hash를 소비하므로 무조건 변경하면 과거 복구를 훼손 |
| 기동 목록만으로 폐쇄루프 완성 불가 | 정상 종료/승인/unknown/stop/예산별 전이 및 activation-claim 원자 연결 추가 | Driver는 주어진 목록을 수행할 뿐 새 READY 작업을 자동 감시하지 않음. 예약과 claim이 어긋나면 새 작업이 유실/중복될 수 있음 |
| 중복 방지와 stale 방지 혼동 | proposal digest 멱등 처리와 관련 입력/expected revision CAS를 분리 | A와 B는 서로 다른 proposal이므로 중복 key만으로 늦은 A의 적용을 막을 수 없음 |
| read-set 검사 직후 경쟁 | mutable writer의 변경 fence와 채택을 같은 coordinator에 연결하도록 명시 | 별도 Graph/Evidence 조회와 Task commit 사이 변경을 단순 hash 사전 확인으로는 차단하지 못함 |
| 항상 finite만 허용하던 문구 | explicit unlimited_requests 계약·산술·설정 이행·재개 검증으로 교체 | 사용자 요구이며 기존 None 거부는 세 지점에 존재. 한 줄 삭제는 타입/산술/원장 일관성을 해결하지 못함 |
| 공유 총량 도입 시점 | M4가 아니라 M1에서 요청 정책 구현, M2/M3 첫 planner부터 사용 | 단일 워커여도 계획·분해·검토가 호출하므로 첫 전체 순환부터 누락 가능 |
| 재시도·unknown 계량 | 실제 dispatch 단위 계량, hidden SDK retry 통제, dispatched 취소는 환불 금지 | 함수 호출 한 번과 실제 provider 요청 수는 다를 수 있고 unknown 사용량은 무료가 아님 |
| 예약 이중 계산·부분 중단 | reserved→spent 단일 이동, 동시성 lease와 환불 분리, Task 범위 stop 유지 | 두 카운터에 중복 포함하면 조기 소진, 둘 다 제외하면 초과 호출. instance 교체로 사용자 중단을 우회해서도 안 됨 |
| 외부 구현 과신 | ARTEX의 maxTurns/계량과 전체 Run 예산 강제를 구분, 미확인 PentAGI 명시 | 호출 통계는 atomic admission과 다르며 읽지 못한 코드를 구현 근거로 삼을 수 없음 |

검수 결과: 위 결정·호환 정책·부정/긍정/경쟁 테스트를 통과하는 것을 작업 조건으로 명시했다. 단, 문서 자체 검수가 구현 검증을 대신하지 않는다. 이번에 수행한 검증은 파일 구조·참조·요구사항 일관성 확인이며, 제품 코드 테스트·외부 에이전트 실행·실제 무제한 호출 시험은 수행하지 않는다.
