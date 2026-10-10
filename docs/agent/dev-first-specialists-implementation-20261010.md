# dev 기반 전문 에이전트 이식 — 구현·검증 현황

기록일: 2026-10-10. 기준 dev 커밋: `153265e32e029481628e8c057fe0ea476d939d64`.
작업 브랜치: 로컬 `codex/dev-first-specialists`. 공개 대상: `dev-jm-native-specialists`.
기준 PRD: `prd-dev-first-specialist-closed-loop-port-20261009.md`.

기존 dev의 native ReAct 실행기를 유지한 채 전문 판단과 후속 작업의 조립을 추가했다. 전체 PRD 완료나 실제 금융권 침투 성능을 입증한 상태는 아니다. 사용자가 기존 `dev-jm` 보존을 선택하여 별도 브랜치로 공개한다. 다른 checkout과 dev-jm의 작업은 변경하지 않는다. 아래 각 시점의 commit/push 없음 기록은 당시 검증 상태이며 최신 공개 여부는 Git 원격 참조로 확인해야 한다.

## 1. 지금 연결된 구조

```text
사용자 목표 → 기존 Mission
  → 전략 판단 → 작업 분해 → 참조·조건 검증 → 현재 입력 CAS
  → 기존 MissionTask + outbox
  → Ready 상태 재확인 + 원자적 claim → 기존 InstanceDriver
  → 역할별 native MODEL → TOOLS → SETTLE
  → 기존 Control · MCP · 원본 영수증 · Graph · 증거/완료 검토
  → 승인 대기 시 원래 checkpoint/instance/action 유지 → 전체 cohort 복구·정산 후 재개
  → 종료한 작업을 근거로 후속 전략/Task → 새 워커 instance
  → 후속 판단과 실행이 멈춘 뒤 기존 목표 정산
```

별도 canonical 작업 DB, 새 실행 엔진, 랩 전용 행동열은 만들지 않았다. 준비 상태와 실행 허가는 다르며, 역할·도구 등록이나 프롬프트만으로 Control을 통과할 수 없다.

| 역할 | 실제 연결 | 제한 |
| --- | --- | --- |
| 대화 | 기존 CLI의 Run 귀속 대화 판단에 역할 MD 적용 | 승인·도구 실행 권한을 생성하지 않음 |
| 전략 플래너 | 현재 목표·도구·관측을 읽어 조사 질문과 완료 조건 제안 | 대상·principal을 임의 생성할 수 없음 |
| 작업 분해 | 질문을 typed Task로 변환, 조건·대상·의존성 보존 검증 | 등록된 실행 역할만 배정 가능 |
| Web 실행 | 기존 native 실행기로 Task 수행 | 현재 역할의 실제 catalog/eligibility와 Control 적용 |
| Mobile 실행 | 등록·MD·역할별 catalog 선택 경로 준비 | 실제 기기/앱/서버 환경 검증은 미실시 |
| 연구·증거 | 기존 분석·claim·작업/목표 완료 판단에 같은 역할 MD 연결. 명시적 재사용 조건·현재 유효성·중복 없는 사건과 완료/보고 소비자 추가 | 독립 상시 평가자·자동 만료 타이머와 실제 환경 평가는 미완료 |
| 코드 산출물 | 별도 작성·독립 검토 stage/MD와 `code.prepare`·`code.inspect` 연결. 기존 blob/evidence 저장소·공통 호출 원장·관련 입력 CAS 사용 | 코드는 미실행 DATA. 합성 case 실행·격리 검증·대상 실행은 하지 않음 |

역할 ID·역할 수는 설정과 registry에서 결정한다. 위 이름을 중앙 루프의 분기 조건으로 고정하지 않았다.

## 2. 팀 피드백 네 가지에 대한 구현

### 완료 조건을 끝까지 유지

- 공유 `CompletionCriterion`을 Task 계약 v2에서 사용한다. v1은 기존 저장 데이터 호환용으로 유지한다.
- 플래너의 조건을 분해 과정에서 누락하면 등록하지 않는다. v2 작업 완료는 저장된 조건 전체의 AND 검사를 통과해야 한다.
- 동일 원본 `(action_id, invocation_id)`의 중복·상충 사본과 실패 결과를 먼저 제외한 뒤, 증거 수와 principal별 비교 조건을 검사한다.
- 완료 영수증에는 조건 ID와 계약 hash를 연결한다. 검토 중 목적이 달라지면 늦은 완료 판단을 채택하지 않는다.
- 워커가 완료하면서 `active_task_id`를 비워도 기동 기록의 원래 Task 참조로 후속 플래너의 문맥을 복원한다. 소유권 회수나 재실행을 뜻하지 않는다.

### 필요한 워커를 다시 움직임

- 기존 coordinator에 claim과 instance 기동 기록을 함께 저장한다. 중복 이벤트는 새 기동의 근거가 아니라 현재 Ready 상태를 조회하는 신호다.
- 정상 종료 후 후속 Task는 새 instance로 수행한다. 승인 대기·실행 불명확·중단 상태의 기존 instance를 새 instance로 대체하지 않는다.
- 역할 준비 상태, 의존 Task 완료, Run/Mission 상태와 공통 호출 한도를 확인한다. 중단·제외·한도 소진을 새 워커가 우회하지 못한다.
- 다른 워커가 실행 중이어도 Ready 이벤트를 기존 드라이버가 처리하는 시험을 추가했다.
- 전문 모드의 승인과 전체 cohort 재개를 같은 후속 플래너/Ready 드라이버에 연결했다. 부분 선택과 unknown 형제가 있는 경우 새 Task 기동·후속 판단·전체 완료 정산을 보류한다.
- 재개 actor도 기존 기동 기록의 실행 슬롯을 차지한다. 새 activation ID·Task claim·호출 allowance를 만들지 않으며, 원래 Task 조건 hash/역할/소유권이 바뀌면 실행 전 거절한다.
- 재개용 Runtime에 다른 워커의 카운터 합계를 덮어쓰지 않는다. Runtime은 해당 instance의 상태이고 전체 LLM 사용량은 공통 원장이다. 전체 도구 실행 통계를 CLI에서 별도로 투영·표시하는 정리는 남아 있다.

### 중복 처리와 오래된 계획을 따로 검사

- 같은 proposal ID와 내용의 재전달은 기존 결과를 반환한다. 같은 ID의 다른 내용은 거절한다.
- 목표 내용, 상태 generation, 전략 revision, 선택 구성, 읽은 Task 계약/상태, 관련 Graph 참조를 채택 직전에 비교한다.
- Graph 쓰기 전에 fence를 세우고 저장 후 해제한다. 쓰기 실패 시 fence를 자동 환불하지 않는다.
- 현재 플래너는 전체 Graph를 문맥으로 사용하므로 전체 Graph 참조를 읽는다. 무관한 형제의 Task-only 변경은 계획을 폐기하지 않는다.
- Graph fence는 같은 조립의 Run scope를 가진 writer가 검증 범위다. 이번 M5 조각에서는 EvidenceStore append와 유효성 binding도 같은 coordinator의 query/관련 참조 fence에 연결했다. 외부 writer가 wrapper를 우회하는 변경이나 여러 호스트의 분산 쓰기까지 보장하지 않는다.

### 모든 판단의 공통 호출 한도와 명시적 무제한

- 첫 플래너 호출 전 공통 Run 원장에 정책을 bind한다. 플래너·분해·워커·분석·완료·Run 귀속 대화가 같은 원장을 사용한다.
- 요청은 `reserved → dispatched → settled/unknown`으로 기록한다. 외부 전송 이후 취소/오류/사용량 미확인은 환불하지 않는다. SDK 자동 재시도는 끈다.
- `bounded`는 양의 `max_requests`, `unlimited_requests`는 명시적 mode와 null 상한을 요구한다. 누락·0·-1을 무제한으로 해석하지 않는다.
- 무제한은 **LLM 요청 총횟수만** 무제한이다. 동시성, 판단 repair, 연속 429, Task 기동/전략 개정, 실행 시간, Control/ROE 제한은 유지한다. 무료 실행을 보장하지 않는다.
- Retry-After와 연속 429 상태를 Run에 보존한다. 소진 후 늦게 도착한 형제의 성공 응답도 제한을 초기화하지 못하도록 수정했다.
- 원장은 요청 수·알려진 토큰·미확인 사용량을 보고한다. 이번 새 정책이 별도 금액 상한 계산까지 제공하는 것은 아니다.

### 공유 자원 충돌과 원래 요청 재개

- [공유 자원 계약](../contracts/tool-resources.md)의 typed provider metadata를 Registry → Compiler → 기존 WorkStore로 전달한다. 서버/ToolId 이름 또는 `_ref` 인자명으로 공유 자원을 추측하지 않는다.
- 운영자 `TOOL_RESOURCES_JSON`은 provider lock에 추가만 한다. 발견되지 않은 도구, 잘못된 metadata/schema/자원 인자는 등록 또는 컴파일에서 거절한다.
- 같은 프로필/기기/작업공간을 쓰는 서로 다른 대상·Run/instance 요청도 같은 key를 사용한다. 독립 key는 기존 실행 동시성 한도 안에서 병렬 진행한다.
- Workspace read/write 서버는 같은 canonical root key를 제공한다. Mobile diagnostics는 정책의 device ref 별칭을 serial hash로 연결한다. 외부 브라우저의 실제 프로필 등록/로그인이나 실제 기기 실행은 이번 검증에 포함하지 않는다.
- 도구 진입 전에 기존 owned/live lease로 durable 실행 marker를 기록한다. 결과 unknown, 취소, 만료, abandon만으로 자원을 풀지 않는다. 원래 실행 영수증의 verified reconcile가 필요하다.
- native TOOLS는 동일 원래 call/Work를 보존한 유한 대기를 사용한다. 대기 중 새 LLM turn/claim/ID를 만들지 않는다. 상한·unresolved original이면 `resource_wait`와 구체적 진단을 남긴다.
- `resource_wait`는 Task 실패/완료가 아니다. 전문 lifecycle/activation은 held로 유지하고, 안전한 원래 pending checkpoint batch만 기존 runner/cohort resume에서 복원한다. 취소된 요청과 unknown 실행의 재제출은 거절한다.
- 이 연결은 같은 WorkStore를 쓰는 실행에 한정된다. 별도 WorkStore/호스트와 외부 수동 조작을 자동으로 잠그거나 전체 전문 루프의 다중 프로세스 소유권을 보증하지 않는다.

### 전문 Run의 실행 소유권

- 기존 저장소 backend의 선택적 `exclusive_run` port와 `InstanceDriver.own_run`을 연결했다.
  Task/Work claim을 대체하는 새 큐·실행기를 만들지 않았다.
- fresh 전문 판단과 cohort reconcile/Control 복원 전에 획득한다. iterator가 종료돼도
  원래 actor drain/checkpointer close까지 유지하여 동시 재개를 거절한다.
- Memory는 같은 backend 내부, SQLite는 같은 로컬 canonical DB의 POSIX companion inode로
  독립 연결·프로세스 사이에서 적용한다. 모델/도구 I/O 동안 DB transaction을 잡지 않는다.
- 프로세스 종료 뒤 새 소유권을 얻어도 원래 instance·조건·계량·미확정 자원 marker를
  변경하지 않는다. 영수증 reconcile가 별도로 필요하다. 미지원 플랫폼/저장소는 거절한다.
- 상세 범위·운영 조건은 [실행 소유권 계약](../contracts/run-execution-ownership.md)에 정리했다.

## 3. 이번 검수에서 수정한 이유

| 확인된 문제 | 수정 및 근거 |
| --- | --- |
| 형식은 맞지만 역할/대상/조건이 틀린 모델 응답이 일반 예외로 빠질 수 있음 | 소유 모듈의 typed validation을 기존 decision client에 연결. schema 수정과 같은 유한 repair 예산을 사용하고 실패 원인을 저장. 나쁜 분해로 Task/outbox를 만들지 않는 실제 시작 경로 시험 통과 |
| 완료 후 후속 플래너에 원래 Task가 빠질 수 있음 | 기존 activation의 불변 Task 참조를 공개 조회로 복원. 완료 조건·영수증을 후속 payload에서 실제 확인 |
| 계획 사유를 단순 길이 제한만 하여 저장 | 기존 `safe_feedback`으로 마스킹 후 저장. 비밀값이 포함된 합성 사유 저장 시험 통과 |
| 구형 주입 객체에 전문 모드 필드가 없으면 일반 모드가 깨짐 | 추가 설정이 없을 때 기존 일반 경로 유지. 전체 회귀에서 발견한 `AttributeError` 원인 수정 |
| 연속 429 소진 후 늦은 성공으로 제한이 초기화됨 | 수정 전 Memory/SQLite에서 재현, 소진 상태를 유지하도록 수정 후 두 backend 시험 통과 |
| 새 wrapper와 CLI 인자에서 정적 타입 불일치 | 소유 port 타입·명시적 선택 인자를 사용. 새로운 기능이나 권한 우회를 추가하지 않고 타입 검사 통과 |
| 승인 대기 Runtime에 형제 카운터 합계가 섞임 | 재개용 Runtime의 instance 카운터를 그대로 유지. 먼저 끝난 형제는 실행 1건이고 대기 워커는 0건인 경우 실제 SQLite checkpoint와 반환값의 action/model 카운터 일치 검증 |
| cohort 재개 후 native 워커가 먼저 전체 완료를 시도할 수 있음 | 재개 전 전문 lifecycle을 active로 유지하고, 후속 Task 실행까지 끝나야 quiescent로 전환. 부분/unknown/승인/일시정지는 held로 유지 |
| 재개와 새 Ready 기동이 다른 실행 소유권을 가질 위험 | 기존 InstanceDriver에 검증된 resume builders와 Ready 보충을 함께 전달. runner/checkpointer/취소는 같은 드라이버가 소유 |
| 승인 대기 tick을 사용자 resume으로 오해할 위험 | 명시적 CLI resume과 자동 승인 watch를 구분. 자동 watch는 pause 해제 지시를 작성하지 않음. 실제 operator pause/stop 적용 후 승인해도 실행·모델 호출이 늘지 않는 시험 통과 |
| 서로 다른 대상 요청이 같은 프로필/기기/작업공간을 동시에 사용 | 제공자 선언을 기존 Work conflict에 추가. 별도 lock 서비스·도구명 중앙 목록 없이 같은 자원은 직렬, 독립 자원은 병렬 시험 통과 |
| lease 만료를 원본 실행 종료로 착각할 위험 | 진입 marker를 원장에 먼저 저장. unknown/취소/만료/abandon 뒤에도 보존하며 correlated original reconcile 후 해제 |
| 자원 대기 종료를 일반 fresh MODEL 재개/Task 정산으로 처리 | 명시적 resource_wait와 원래 미실행 batch 복원 연결. 실제 SQLite 전문 start→held→cohort resume→후속 완료까지 계약 hash·instance 유지 검증 |
| 두 프로세스가 동일 occupant/checkpoint로 동시에 재개 | 드라이버 객체 내부 중복 검사만으로는 부족함. 기존 backend가 Run 소유권을 제공하고, 전문 계획/복구보다 먼저 확보하여 중복 판단·정산·checkpoint writer를 차단 |
| durable 시험이 checkpoint만 SQLite를 사용 | 전문 루프 시험의 durable 설정을 Task/Path/Work/Graph/Intent 저장까지 SQLite로 변경. 독립 facade/connection과 기존 승인·자원 재개를 실제 영속 backend에서 다시 검증 |
| `close()` 정리 자체가 취소되면 Run 소유권이 반환되지 않음 | 수정 전 Memory/SQLite에서 2건 실패 재현. 기존 driver-owned 정리 하나를 shield하고 drain/SDK/소유권 반환 뒤 호출자 취소 전달. 정리 중 reopen도 거절하며 같은 시험 통과 |

## 4. 검증 결과와 한계

모델은 scripted/fake provider만 사용했다. 실제 유료 LLM, 금융권 대상, 사용자 계좌·인증서·거래는 호출하지 않았다. 전송 시험은 테스트가 만든 합성 localhost HTTP/HTTPS와 로컬 MCP를 대상으로 한다.

- 신규 집중 시험 8개 파일: **87 passed**.
- 마지막 타입/CLI 인자 정리 후 대화·승인·계량·완료 접점 6개 파일: **77 passed**. 위 집중 시험과 중복되므로 별도 합산하지 않는다.
- 전체 `src/tests` Ruff: 통과. `git diff --check`: 통과.
- 신규 접점 11개 파일, 변경 접점 17개 파일의 mypy: 통과. 기존 untyped 함수 본문까지 모두 엄격히 타입 검증했다는 뜻은 아니다.
- 전체 unit/integration 회귀: **3,551 passed / 65 skipped / 3 failed**. 마지막 429 보완은 위 집중 시험으로 다시 검증했다.
- 제한된 실행에서는 localhost bind/Docker 접근 실패가 발생하여 승인된 로컬 시험 실행으로 다시 확인했다. 이 권한 해제는 제품 Control 정책 완화가 아니다.

전체 회귀에서 남은 시험은 다음과 같다. 통과했다고 표시하거나 제품 격리를 완화하지 않았다.

1. `test_gated_install_docker.py::test_real_pip_install_uses_proxy_and_direct_mirror_is_blocked`: `gated_open_failed`.
2. `test_shell_workroom_docker.py::test_container_has_loopback_only`: `lo` 외 인터페이스가 관측됨.
3. `test_shell_workroom_docker.py::test_cancel_kills_env_scrubbed_setsid_child`: 취소 후 자식 PID 소멸 검증 실패.

수정 전 dev SHA `153265e`를 격리된 임시 디렉터리에 그대로 추출하여 위 3건만 같은 환경에서 재실행했다. **3건 모두 같은 실패가 재현됐다(3 failed, 41.84초).** 실제 import 경로도 임시 기준 소스의 `src/redteam/capability/gated_strategy.py`임을 확인했다. 따라서 이식에서 새로 생긴 실패로 분류하지 않는다. 다만 기존 제품 버그인지 Docker/kernel 환경 문제인지의 세부 원인은 아직 확정하지 않았고, 격리 안전성의 최종 검증도 미완료다.

주요 신규 시험:

- `test_native_specialist_closed_loop.py`: public build/start → 플래너 → 분해 → native 실행 → v2 검토 → 후속 새 워커 → 목표 정산. 잘못된 분해, stale Graph, 사유 마스킹, 첫 판단의 총량 소진도 검증.
- `test_specialist_tool_view.py`: 역할별 카드/상세/native 후보 일치, 구형 일반 모드 union 유지, 미등록/준비 안 된 역할 거절.
- `test_ready_task_activation.py`: Memory/SQLite 중복 claim, 종료한 Task 문맥 복원, 중단·승인·불명확 상태 보존, 독립 실행 중 후속 기동.
- `test_shared_llm_requests.py`: 모든 역할/호출 형태의 공통 100회 상한, 무제한 fake 120회, 취소/unknown/재시작/429.
- `test_task_completion_contract_v2.py`: 원본 하나·두 principal 미충족·실패/중복 원본의 완료 거절, 충분한 원본의 완료 허용, v1 저장 호환.

### 승인·재개 후속 구현 검증

이번 후속에서는 fresh 시작과 재개가 같은 `SpecialistLifecycle`을 사용하도록 연결했다. `redteam resume`의 기존 전체 cohort 복구/영수증 reconcile/Control 집계 복원은 유지했다. 터미널의 전문 모드 승인 watch도 같은 경로를 사용하며, 현재 watch는 전체 cohort를 열거할 수 있는 SQLite checkpoint를 요구한다. memory/disabled checkpoint를 운영용 재개로 조용히 대체하지 않는다.

- `test_specialist_resume.py`: scripted 모델 + 실제 native/checkpoint 경로. 승인된 원래 action 실행, 후속 새 instance, 전체 공통 LLM 원장 유지, pending Runtime 카운터 보존, 완료한 형제 미재실행, 부분/unknown cohort 정산 보류, 미승인/거절/pause/stop 검증.
- 같은 파일에서 **실제 DefaultControlPlane**도 사용했다. 첫 승인 후 새 후속 action은 두 번째 독립 승인을 요구하며, 두 번째 전체 재개에서 첫 요청을 재실행하지 않고 완료 조건을 검토한다. 대상 요청은 합성 gateway뿐이다.
- Memory/SQLite의 재개 슬롯/중복 activation 방지, 잘못된 역할 선택의 원자적 거절, 바뀐 Task 완료 계약/소유권 거절 시험을 추가했다.
- 위 시험은 실제 LLM 판단 품질, 브라우저 세션/기기 자원 경쟁 또는 다중 프로세스 전문 루프의 소유권을 인증하는 시험이 아니다.

승인/재개 후속 시점 검사: 신규 집중 9개 파일과 기존 재개·영수증·driver·native·CLI 승인 회귀 16개 파일을 한 번에 실행하여 **275 passed (19.64초)**를 확인했다. 신규 집중 부분은 106건, 기존 관련 회귀는 169건이며 위 과거 전체 회귀 수와 별도 합산하지 않는다. 전체 `src/tests` Ruff, 변경 접점 5개 파일의 mypy, `git diff --check`도 통과했다. 이 시점에는 전체 3천여 건과 Docker 시험을 다시 실행하지 않았다.

### 공유 자원 후속 구현 검증

2026-10-10 추가 검사. 실제 LLM·금융 사이트·ADB·외부 브라우저를 실행하지 않았다.

- 자원 unit/native integration/specialist resume 3개 파일의 최종 검사: **61 passed (7.49초)**.
  Memory/SQLite 직렬·독립 병렬·다중 자원 원자성, 진입 소유권/중복 거절,
  unknown/취소/만료/재시작 보존, 미실행 승인 release, 실제 로컬 MCP discovery,
  원래 checkpoint 재개 및 전문 후속 Task 완료를 검증했다.
- 관련 회귀 32개 파일의 검사: **434 passed (23.93초)**.
  이후 마지막 진입 guard 보완은 자원·실행·job·영수증 접점 8개 파일에서 **138 passed (7.80초)**,
  누락 Work guard는 별도 2건 및 위 최종 61건에서 다시 확인했다. 중복 시험 수는 합산하지 않는다.
- 자원/Capability/runner/bootstrap 변경 접점 24개 파일, 설정/환경/state/delegation 5개 파일의 mypy 통과.
  전체 `src/tests` Ruff와 `git diff --check` 통과.
- `tests/unit tests/integration`을 다시 실행하되 기존 Docker 실패가 있는 2개 파일을 제외했다.
  제한된 실행 환경에서 **3,571 passed / 76 skipped / 9 failed / 3 errors (119.26초)**였다.
  나머지 12건은 `127.0.0.1` 테스트 서버 bind의 `PermissionError`였다.
  해당 5개 파일만 localhost 허용 실행으로 다시 검사하여 **26 passed (19.40초)**를 확인했다.
  제품 Control/ROE나 테스트 기대값을 완화해 통과시킨 것이 아니다.
- Docker 격리 2개 파일은 이번 전체 검사에서 제외했으므로 앞서 확인한 기존 실패 3건을
  해결 또는 통과로 판정하지 않는다. 전체 원본 시험 집합의 최종 green을 주장하지 않는다.

### Run 실행 소유권 후속 구현 검증

2026-10-10 로컬 구현. 실제 모델·금융 대상·Mobile 기기는 호출하지 않았다.

- 신규 소유권 시험은 **26건**이다. 별도 PID의 정상 종료/SIGTERM/SIGKILL, Memory/SQLite
  경쟁, unsafe 파일 거절, 독립 Run, start/Ready/CLI resume의 진입 guard와 종료 취소를 포함한다.
- 최종 소유권/driver/실제 native/전문 루프/자원/일반 cohort 재개 접점 10개 파일:
  **111 passed (23.03초)**. 이전 관련 28개 파일 검사 **344 passed (32.39초)** 후
  발견한 종료 취소 경로 2건은 수정 전 실패, 수정 후 위 최종 검사와 전체 회귀에서 통과했다.
- 전체 `tests/unit tests/integration` 검사에서 기존 Docker 실패가 있는 2개 파일을 제외했다.
  제한된 환경 결과는 **3,597 passed / 76 skipped / 9 failed / 3 errors (126.31초)**였다.
  나머지 12건은 모두 합성 `127.0.0.1` 서버 bind의 `PermissionError`였다.
  해당 5개 파일만 localhost 허용 실행으로 다시 검사하여 **26 passed (19.39초)**를 확인했다.
  위 수치는 중복 시험이 포함되므로 합산하지 않는다.
- 최종 전체 `src/tests` Ruff, 변경 접점 5개 소스 파일의 mypy, `git diff --check` 통과.
  브랜치/HEAD는 `codex/dev-first-specialists` / `153265e` 그대로이며 stage/commit/push/PR 없음.
- 기존 Docker 격리 실패 3건은 이 검사에서 제외했고 해결됐다고 판정하지 않는다.
  NFS/여러 호스트의 분산 소유권, checkpoint 없는 활성 예약의 전체 crash 복구,
  실제 세션·기기 실행은 완료 주장에 포함하지 않는다.

### M5 — 증거 유효성 소비자 연결

수정 전 malformed 유효성 binding을 넣은 합성 시험에서 단계·작업·목표
완료가 모두 허용되는 문제를 재현했다(3 failed). 현재는 모델 의미 검토
이전에 거절하며, 원본은 그대로 보존한다.

- `evidence.assess`를 전문 프로필의 기존 local tool registry에 등록했다.
  별도 MCP/실행 엔진이나 중앙 tool-name 분기를 만들지 않았다.
- 모델은 조건만 제안하고, 코드는 기존 measured claim reader에서 현재
  scalar·원본·같은 Run/engagement를 확인한다. 선언값·실패·fixture·모르는
  만료 시각은 실제 재사용 근거로 승격하지 않는다.
- binding은 기존 EvidenceStore에 덧붙인다. `valid/stale/invalid/inconclusive`
  현재 상태와 `unbound` 호환 의미를 원본 관측/취약점/완료와 분리했다.
- planner/native/관련 의미 검토와 완료 gate가 현재 상태를 읽는다. 유효성
  사건은 기존 coordinator/outbox로 저장·중복 제거한다. 같은 상태 조회로
  Task·모델 호출이 자동 추가되거나 사용자 중단이 해제되지 않는다.
- 관련 binding/node 버전과 관측된 deadline을 완료 commit에서 재검사한다.
  값이 바뀌었다 되돌아와도 오래된 검토를 거절하고, 무관한 node 변경은
  해당 완료를 거절하지 않는 시험을 추가했다.
- 보고서는 과거 completed 상태를 유지하면서 현재 재사용 상태를 별도
  표시한다. 현재 목표의 근거가 무효이면 release를 보류하며, 쓰지 않은
  형제 증거만 무효인 경우 전체 release를 차단하지 않는다.
- profile/common/evidence MD version은 `2`다. fingerprint가 바뀌므로
  **새 Run으로 사용**한다. 기존 Run에 새 MD를 강제로 끼우지 않는다.

추가 검수에서 event refresh와 모델 입력 스냅샷 사이에 새 유효기간이
등록되는 경합을 재현했다. 수정 전에는 모델이 새 조건을 읽어도 계획의
deadline은 이전 refresh 값이어서 만료 후 Task를 등록할 수 있었다.
현재는 **이미 확보한 read-set 안의 실제 모델 입력**으로 deadline을
연결한다. 반대로 모델이 처음부터 stale 상태를 읽었다면 제한된 재검증
작업을 제안할 수 있다. 두 경우 모두 고정 clock 합성 시험으로 검사하며,
유효성 검사 때문에 모든 재조사를 막거나 Control을 완화하지 않는다.

계약·파일별 접점과 제한: [증거 재사용 유효성](../contracts/evidence-reuse-validity.md).
자동 만료 타이머, 독립 background 평가자, interrupted writer 자동 복구,
코드 artifact 생산/검토와 실제 Mobile 시험은 이번 구현 범위가 아니다.

최종 검증 기록:

- 최종 코드의 unit/integration 회귀: **3,619 passed / 76 skipped**, 124.03초.
  `test_gated_install_docker.py`, `test_shell_workroom_docker.py`의 기존
  Docker 실패 접점과 아래 localhost 5개 파일은 이 회귀 명령에서 제외했다.
- 합성 localhost가 필요한 `test_install_proxy_route.py`,
  `test_page_text_mcp.py`, `test_page_text_transport.py`,
  `test_principal_mcp_transport_e2e.py`, `test_approved_mcp_deployment.py`는
  별도 검사에서 **26 passed**, 19.93초였다. 최종 planner의 snapshot
  보완 이전에 실행한 별도 결과이며, 수정 후 전체 재실행으로 주장하지 않는다.
- `src/tests` 전체 Ruff, 변경 접점 10개 소스 파일의 mypy,
  `git diff --check` 통과. 집중 시험 수치는 위 회귀와 중복되므로 합산하지 않는다.
- 76건 skip과 기존 Docker 실패 3건을 통과로 계산하지 않는다. 실제 LLM
  판단 품질, 금융 대상, 실제 Mobile, 대규모 binding의 성능은 검증하지 않았다.
- 브랜치/HEAD `codex/dev-first-specialists` / `153265e` 유지.
  stage/commit/push/PR 및 실제 모델 호출 없음.

### M5 — 코드 산출물 작성·독립 검토·보존

기존 dev의 generic local-tool 등록/발견/실행 접점에 `code.prepare`와
`code.inspect`를 연결했다. 새 실행 엔진·작업 큐·랩 정답 분기는 없다.

- `code_generation`과 `code_review`를 서로 다른 등록 역할에 명시적으로
  bind한다. 두 요청은 같은 공급자를 쓸 수 있으므로 역할별 독립 검토이지
  서로 다른 모델이나 보안 인증을 의미하지 않는다. 코드 역할은 대상 워커로
  기동하지 않고, 현재 실행 워커의 요청·예산으로 호출된다.
- 기존 artifact backend를 `core/ports`의 공통 port로 주입하고 기존
  filesystem 구현을 재사용했다. 별도 저장 엔진은 없다. 기존 import는
  compatibility re-export로 유지한다. generic 모드는 자동 활성화하지 않는다.
- 원본의 동일 Run/engagement/Mission 실행 연계·성공·현재 재사용 상태를
  확인한다. 작성/검토/최종 read에서 목표·Task 조건·현재 instance claim·
  prompt hash·원본 hash·관련 유효성 read-set를 재검사한다.
  무관한 형제나 strategy-only 변경으로 artifact를 폐기하지 않는다.
- 동일 caller 정상/repair budget과 기존 Run 호출 원장을 사용한다.
  잘못된 syntax/digest 검토 응답을 typed validation 오류로 연결했다.
  첫 시험에서 보통 ValueError는 기존 correction hook의 대상이 아니어서
  재시도가 빠지는 것을 확인했고, 별도 outer loop 없이 기존 finite repair로
  수정했다. 예산 거절이나 사용자 중단을 repair로 우회하지 않는다.
- `ast.parse`는 syntax 검사일 뿐이다. 프로그램을 import/compile/exec하거나
  Mac shell로 실행하지 않는다. `fixture_validation=not_run`,
  `target_execution_enabled=false`를 항상 반환한다. 생성 코드가 호스트에
  파일을 쓰도록 작성한 합성 trap도 실제 실행되지 않는 것을 확인했다.
- blob과 SYSTEM artifact record는 digest·검토·원본 참조에 묶는다.
  동일 결과 재생은 record를 중복 추가하지 않는다. 중간 실패 시 남은 bytes는
  역사적 자료일 뿐이며 현재 인자/상태가 달라진 read는 거절한다.
  artifact는 원본 응답/Task 완료/취약점으로 자동 승격하지 않는다.

이번 변경 후 profile version은 **3**, common MD는 **2**, evidence MD는 **3**,
code MD는 **2**다. fingerprint가 바뀌므로 **새 Run**으로 사용한다.
기존 Run에 변경된 프로필을 억지로 적용하거나 재개 검사를 해제하지 않는다.

최종 로컬 검증:

- 신규 code-artifact 시험 **34건**, profile 추가 시험 **3건**.
  public assembly/registry detail/generic dispatcher와 requester 계량,
  실패·fixture·잘못된 연계의 사전 거절, 두 역할 prompt, 취소 후 과금 원장,
  finite correction, goal/claim/Task/source/ABA 변경, 원본 보존·blob read를 검사했다.
- 관련 7개 파일 집중 시험: **115 passed (2.03초)**. 아래 전체 회귀와
  중복되므로 합산하지 않는다.
- unit/integration 회귀: **3,656 passed / 76 skipped (128.22초)**.
  이전과 동일한 기존 Docker 실패 2개 파일 및 localhost 5개 파일을 제외했다.
- localhost 5개 파일은 최종 변경 후 로컬 서버 허용 실행으로 따로 검사:
  **26 passed (20.20초)**. 외부 금융 대상이나 실제 LLM을 호출하지 않았다.
- 전체 `src/tests` Ruff, 변경 접점 7개 소스 파일의 mypy,
  `git diff --check` 통과. 기존 Docker 실패 3건과 76 skip은 통과로 계산하지 않는다.
- `codex/dev-first-specialists` / `153265e` 유지. stage/commit/push/PR 없음.

상세 계약·입력·제한: [코드 산출물](../contracts/code-artifacts.md).
안전한 fixture 실행/격리, orphan 정리와 전체 crash 복구, 실제 Mobile/LLM
품질 검증은 남아 있으므로 M5/M6 전체 완료로 판정하지 않는다.

## 5. PRD 단계와 다음 순서

### M5 추가 — checkpoint 이전 예약 복구와 전체 종료 검사

기존 checkpoint 목록에서 누락된 activation을 무시하고 재개하는 문제를
수정 전 시험으로 재현했다. 기동 기록을 별도 큐로 바꾸지 않고 versioned
reserved/entered 연결 정보와 현재 입력 검사를 기존 coordinator에 추가했다.

- claim과 initial Runtime 증명은 원자적으로 저장하고 graph/model/tool 진입 전에
  entered marker를 기록한다. 예외/프로세스 종료로 marker를 되돌리지 않는다.
- 전체 cohort 재개 전에 checkpoint 없는 activation도 대조한다. 현재 미진입을
  증명한 예약만 같은 원래 ID로 복구한다. legacy·진입 가능·claim/조건 변경·저장소
  오류는 모델/도구/Control 계량 복원 전에 거절한다.
- 공통 호출 원장·원래 시작 시각·Task occupant를 보존한다. 별도 프로세스 강제
  종료 뒤에도 unknown 요청이 과금 상태로 남고 ID/claim이 유지되는 것을 검사했다.
- 부분 선택과 unknown 형제는 새 예약 시작/후속 기동/전체 완료를 보류한다.
- 종료 검사는 미완성 activation·미전달 outbox를 포함한다. 목표 완료 commit에서
  다시 검사하여 검토 도중 생긴 미소비 작업을 누락하지 않는다.
- 신규 두 파일 집중 시험 **55 passed (5.45초)**. 실제 LLM/금융 대상/기기 호출 없음.

해당 단계 당시 코드 검증:

- unit/integration 회귀 **3,711 passed / 76 skipped (128.20초)**.
  기존 Docker 실패의 `test_gated_install_docker.py`,
  `test_shell_workroom_docker.py` 및 위 localhost 5개 파일을 제외했다.
- localhost 5개 파일은 별도 로컬 서버 시험에서 **26 passed (19.87초)**.
- 복구·기동·승인 접점 4개 파일 **87 passed (11.46초)**. 전체 회귀와 중복이다.
- 전체 `src/tests` Ruff, 변경 접점 10개 소스 파일의 mypy,
  `git diff --check` 통과. 기존 Docker 실패 3건과 skip은 통과로 계산하지 않는다.
- 브랜치/HEAD `codex/dev-first-specialists` / `153265e` 유지.
  stage/commit/push/PR, 유료 모델·실제 금융권/기기 호출 없음.

상세 계약: [기동 예약 복구](../contracts/activation-recovery.md).
위 검증 당시 checkpoint가 하나도 없는 Run의 CLI bootstrap은 미완료였으며,
아래 추가 작업으로 연결했다. entered인데 checkpoint가 없는 경우의 진단·명시적
해제와 전체 orphan/writer 복구는 여전히 미완료다. M5 전체 완료로 표시하지 않는다.

### M5 추가 — zero-checkpoint CLI 복구

수정 전 실제 CLI 접점 합성 시험에서, 원래 예약이 저장돼 있어도
`no checkpoint ... Refusing to resume`로 종료하는 것을 재현했다.
기존 composite SQLite snapshot/예약 증명을 재사용하여 다음을 연결했다.

- 읽기 전용 기존 저장소에서 모든 예약을 검증하고 원래 조립 identity를 얻는다.
  누락 DB는 생성하지 않고, 미래/부분 schema·깨진 기록은 거절한다.
- 이를 checkpoint로 가장하지 않고 같은 CLI cohort/InstanceDriver 경로로 전달한다.
  원래 Run/instance/claim/clock/공통 계량을 유지하며 초기 계획을 새로 만들지 않는다.
- 조립 전 profile/MD/정책을 검증하고, driver 소유권 아래 현재 scope/selector 및
  실제 checkpoint/Work/Evidence 부재를 다시 확인한다. 진입한 기록과 구형 예약은
  자동 시작하지 않는다. 명시적 부분 선택의 의미도 변경하지 않았다.
- 재조립 `created_at`만 제외하는 조립 입력 projection v2를 사용한다.
  시각을 제외하지 않으면 같은 scope/policy도 새 프로세스의 조립 시각 때문에 거절된다.
  실제 정책과 원래 실행 시간 예산은 유지한다. 구형 hash는 자동 변환하지 않으며
  불일치 시 기존 자료를 보존하고 새 Run을 요구한다.
- 추가 두 파일 **28 passed (2.23초)**. 테스트는 합성 모델/도구이며,
  실제 CLI → public build_application 재조립 → 원래 승인 대기 → checkpoint 재개를
  검사했다. 유료 LLM/실제 금융권/기기/생성 코드 실행 없음.

이번 최종 코드 검증:

- unit/integration **3,739 passed / 76 skipped (128.35초)**.
  이전 검증과 같은 Docker 2개 파일 및 localhost 5개 파일을 제외했다.
- localhost 5개 파일은 별도로 **26 passed (19.12초)**.
- 전체 `src/tests` Ruff, 이번 변경 소스 4개 파일 mypy, `git diff --check` 통과.
- 기존 Docker 실패 3건은 이번에 재검증/수정하지 않았고, skip도 통과로 계산하지 않는다.
- HEAD `153265e`, 로컬 브랜치 유지. stage/commit/push/PR 없음.
  새 checkpoint 이전 복구 연결까지 구현했지만 M5 전체 완료 판정은 아니다.

| 단계 | 현재 판단 |
| --- | --- |
| M0 기준선 | dev SHA 고정. Docker 실패 3건이 수정 전 dev에서도 동일하게 재현됨을 확인 |
| M1 역할 MD/호출 정책 | 설정·native SYSTEM·공통 원장·명시적 무제한·429의 로컬 구현/시험 진행됨 |
| M2 전략→Task | 조건 보존·typed validation·CAS·outbox·완료 영수증의 합성 검증 통과 |
| M3 전체 순환 | 단일 Web 실행 역할의 fresh-start/후속 Task/목표 정산을 fake 모델과 실제 dev 실행 경로로 검증 |
| M4 다중 경쟁 | 원자적 기동·독립 드라이버·공통 한도·승인/cohort 재개·metadata 자원 경쟁과 로컬 Run 실행 소유권 구현. 독립 프로세스 종료 후 계량/unknown 보존 검증. 실제 공유 세션/기기와 여러 호스트 분산 실행 검증은 남음 |
| M5 전문 산출물/복구 | 증거 유효성·코드 산출물 작성/독립 검토/저장, cohort의 미시작 예약 및 zero-checkpoint CLI 복구 구현. 자동 만료 기동, fixture 실행/격리, entered/orphan/writer 복구, 실제 Mobile 검증은 남음 |
| M6 최종 인계 | 전체 완료로 판정하지 않음. 기존 Docker 실패 3건의 원인/안전성 확인과 위 남은 연결·승인된 실환경 평가 필요 |

다음 구현은 **M5 안전한 fixture 검증 경계와 나머지 crash 복구**다.
특히 entered/불완전 writer의 원래 영수증 기반
진단·명시적 해제는 자동 fresh 재실행으로 대체하지 않는다.
기존 원장 대신 별도 작업 큐를 만들지 않는다. 별도 단일 워커 pause watch와 cohort 통계의
CLI 표시도 정리할 접점이다. 현재 남은 작업을 감추기 위해 Control을 풀거나 특정
Finance Lab 계정·경로를 성공 조건에 넣지 않는다.

## 6. 명시적 설정과 로컬 재현

전문 모드는 자동으로 최신 P0/P1/P2를 선택하지 않는다. [프로필](../../config/agents/native-specialists.yaml)의 MD 경로는 프로필 파일 디렉터리를 기준으로 해석한다. 실행을 재개할 때 선택 구성/MD hash가 다르면 같은 Run에 몰래 적용하지 않는다.

```sh
export REDTEAM_SPECIALIST_PROFILE="$PWD/config/agents/native-specialists.yaml"
export REDTEAM_LLM_REQUEST_POLICY_JSON='{"mode":"bounded","max_requests":100,"max_concurrent_requests":2}'
```

명시적 요청 횟수 무제한 정책의 형태는 아래와 같다. 설정 형식 설명이지 이번 작업의 유료 무제한 실행 승인 또는 실행 기록이 아니다.

```json
{"mode":"unlimited_requests","max_requests":null,"max_concurrent_requests":2}
```

대상/도구 허용 설정·모델 공급자·키는 기존 승인된 실행 설정이 별도로 필요하다. 현재 checkout의 `.venv`는 개발·합성 검증용이며 실제 LLM 공급자까지 실행 검증한 환경이 아니다. 승인 후 전체 재개는 로컬 합성 검증됐지만 M4/M5/M6 전체 완료나 운영용 완성판으로 안내하지 않는다.

비용 없이 추가한 연결을 재현하려면 해당 checkout에서 아래 명령을 사용한다.

```sh
./.venv/bin/pytest -q tests/integration/test_native_specialist_closed_loop.py tests/integration/test_specialist_tool_view.py tests/integration/test_specialist_resume.py
```

공유 자원 후속 시험은 다음과 같다. 기본 `.venv`를 사용한 합성 시험이며 실제 MCP 제품 설치나 기기 실행 명령이 아니다.

```sh
./.venv/bin/pytest -q tests/unit/test_resource_conflicts.py tests/integration/test_native_resource_conflicts.py tests/integration/test_specialist_resume.py
```

증거 유효성 접점만 재현하려면 다음 합성 시험을 사용한다.

```sh
./.venv/bin/pytest -q tests/integration/test_evidence_validity_consumers.py tests/unit/test_planning_evidence.py
```

코드 작성·검토·저장 연결의 비용 없는 scripted 검증은 다음과 같다.
모델 키·실제 대상·도구 실행 권한은 필요하지 않는다.

```sh
./.venv/bin/pytest -q tests/integration/test_code_artifacts.py tests/unit/test_native_specialist_profile.py tests/unit/test_artifact_backend.py
```
