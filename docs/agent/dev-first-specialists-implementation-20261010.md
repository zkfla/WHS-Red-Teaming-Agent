# dev 기반 전문 에이전트 이식 — 구현·검증 현황

기록일: 2026-10-10. 기준 dev 커밋: `153265e32e029481628e8c057fe0ea476d939d64`.
작업 브랜치: 로컬 `codex/dev-first-specialists`. 공개 대상: `dev-jm-native-specialists`.
기준 PRD: `prd-dev-first-specialist-closed-loop-port-20261009.md`.

기존 dev의 native ReAct 실행기를 유지한 채 전문 판단과 후속 작업의 조립을 추가했다. 전체 PRD 완료나 실제 금융권 침투 성능을 입증한 상태는 아니다. 사용자가 기존 `dev-jm` 보존을 선택하여 별도 브랜치로 공개한다. 다른 checkout과 dev-jm의 작업은 변경하지 않는다. 아래 각 시점의 commit/push 없음 기록은 당시 검증 상태이며 최신 공개 여부는 Git 원격 참조로 확인해야 한다.

최종 로컬 현황(2026-10-11): **M5의 로컬 구현·검증 완료**, M6 미완료.
M5 완료 범위는 **15절**, 이후 M6 로컬 안전성 검수는 **16절**,
설치 프록시 실행 환경의 실제 차단 원인·사용자 준비 조건은 **17절**,
새 Linux VM의 실제 프록시 검증 통과는 **18절**을 기준으로 읽는다.
2026-10-11 이후 Mobile 제외·Web 전용 프로필과 공개 전 검증은 **19절**을 따른다.
2~15절의 “다음 작업/남음”과 검사 수는 각각 당시 기록이며 뒤의 추가를 반영하지
않은 이력이다. HEAD `3c24e28` 이후 변경도 사용자 요청에 따라 별도 공개 브랜치에
반영하는 범위이며 `dev`/기존 `dev-jm`과 다른 checkout은 변경하지 않는다. PR은 생성하지 않는다.

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
| Mobile 실행 | 등록·MD·정책이 적용된 catalog·역할 선택·readiness 진단 연결 | 실제 승인된 기기/앱 환경 검증은 미실시. catalog 준비는 실행 허가가 아님 |
| 연구·증거 | 기존 분석·claim·작업/목표 완료 판단에 같은 역할 MD 연결. 현재 유효성·관측된 만료 사건과 활성 cohort wakeup·완료/보고 소비자 연결 | 독립 상시 평가자·종료 Run timer service·실환경 평가는 없음 |
| 코드 산출물 | 별도 작성·독립 검토 stage/MD, `code.prepare`·`code.inspect`·선택적 `code.validate` 연결. 기존 blob/evidence/원장/CAS 및 격리 workroom 사용 | 작성/조회는 미실행 DATA. 검증은 합성 JSON case만 실행하며 실제 대상 실행·Finding/Task 성공 권한은 없음 |

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
| M5 전문 산출물/복구 | 증거 유효성·코드 산출물 작성/독립 검토/저장, 미시작/zero-checkpoint 복구 및 불변 증거 append 응답 유실 복구 구현. 자동 만료 기동, fixture 실행/격리, entered/orphan·Graph/구형 writer 복구, 실제 Mobile 검증은 남음 |
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

## 7. 별도 브랜치 공개와 중단 기록 진단

2026-10-10 사용자가 기존 `dev-jm` 보존과 별도 브랜치 공개를 선택했다.
기존 구현 snapshot `0dc1401`을 `dev-jm-native-specialists`에 push했다.
`dev-jm`/`dev`에 commit·merge·force-push하거나 PR을 생성하지 않았다.
`.env`, 가상환경, 실행 DB·보고서·인증 정보를 올리지 않았다. 신뢰된 역할 MD,
공통/모듈별 AGENTS, PRD와 브랜치 안내를 함께 포함했다.

이후 M5의 첫 진단 연결을 추가했다. 기존 `status --activation-db`에서 읽기 전용
canonical snapshot을 사용한다. 기동 상태·Task claim·원래 owner별 요청 상태만
제공하며 실제 checkpoint/Work/원본 Evidence/provider 교차 검증은 하지 않는다.
반환된 `replay_permitted=false`는 바뀌지 않는다. entered에서 호출 수가 0이거나
반환 기록이 있다는 이유로 원래 instance/claim/unknown 예산을 지우지 않는다.

수정 이유와 검증:

- bootstrap 거절만 보면 reserved/entered·미정산 요청의 차이를 확인하기 어렵다.
  같은 기존 미진입 증명 규칙과 RunRequestLedger의 공개 조회를 재사용했다.
  새 원장·복구 엔진·외부 실행 권한을 만들지 않았다.
- 일반 `status`는 기존 보고서 경로를 유지한다. 별도 플래그만 읽기 전용
  조회를 선택하며 LLM/MCP 조립 없이 작동한다. 읽기 실패는 unknown 부재와 구분한다.
- 검증 중 `replay_authorized`라는 신규 키가 기존 CLI의 `auth` 마스킹에 걸려
  False가 문자열로 표시되는 것을 실제 재현했다. 비밀 마스킹을 약화하지 않고
  신규 필드명을 `replay_permitted`로 정리하여 JSON의 실제 boolean을 보존했다.
- 신규 진단 시험 **18 passed (1.93초)**. 원본 상태 무변경, missing/legacy DB,
  entered + 호출 0, 모든 요청 상태, held/returned, 깨진 형제/저장 schema,
  비밀 본문 미출력, CLI parser/조립 미실행을 확인했다.
- 최종 승인·재개·원장·기동·CLI 호환 회귀 9개 파일 **171 passed (32.45초)**.
  위 신규 시험을 포함하므로 별도 합산하지 않는다. 전체 `src/tests` Ruff,
  변경 소스 3개 파일 mypy, `git diff --check`, 실제 CLI `status --help` 통과.
- 전체 수천 건과 Docker 시험을 이번 진단 변경 후 다시 돌리지는 않았다.
  유료 LLM·금융 대상·기기·생성 코드를 실행하지 않았다.

다음은 외부 Work/원본 영수증의 교차 진단·명시적 해제와 writer/orphan 복구다.
그 뒤에도 fixture 격리, 자동 만료 기동, 실제 Mobile/LLM 평가와 기존 Docker
실패 확인이 남으므로 PRD 전체 완료로 표시하지 않는다.

## 8. Work·저장 원본 교차 진단 (2026-10-11)

`status --activation-db --work-db --evidence-dir`를 연결했다. 세 경로는 원래 실행
저장소로 명시하며, LLM/MCP 조립·provider 조회 없이 동작한다. 구체 저장소가 자기
read-only snapshot을 제공하고, 판단은 기존 실행 journal 검증을 재사용한다.
다른 모듈의 내부 원장을 CLI가 직접 해석하거나 별도 복구 엔진을 만들지 않았다.

확인 가능한 내용은 submission·원본·결과 처리 seal·Work·Task/instance 연결이다.
원본 없음, unknown, 미진입, 처리 미완료/완료, 손상·상충을 구분한다.
Work 상태 상충과 미정산 자원 marker도 별도로 유지한다. timeout을 실제 실패로
확정하지 않고, `processed`를 취약점/목표 성공이나 occupant 해제로 바꾸지 않는다.

검수에서 반영한 이유:

- 일반 Work 조회는 setup/migration, 일반 Evidence 생성자는 mkdir을 동반한다.
  진단이 원본 저장소를 바꾸지 않도록 provider-owned 읽기 전용 조회를 추가했다.
- index 필터만 쓰면 잘못된 run/instance index가 행을 숨길 수 있다. payload와
  물리 index를 필터 전 대조하고 구형 귀속 불명·부분 schema를 안전 부재로 취급하지 않는다.
- 파일 index와 Run 파일의 중복/누락/변경 검사를 기존 integrity 검사와 공유하여
  규칙 복제를 줄였다. snapshot 포트는 deep copy와 append 거절로 원본을 유지한다.
- 여러 저장소의 조회를 하나의 원자적 proof라고 표시할 수 없다. 확장 출력은
  version 2 envelope이고 `cross_store_atomic=false/replay_permitted=false`다.
  기동 표시 투영의 전후 변경만 별도 표시하며 전체 state CAS나 writer 부재로 주장하지 않는다.
- 조회 실패의 raw exception은 원본 비밀값을 포함할 수 있다. CLI는 클래스만
  출력하고 JSON에는 ID/구조화된 상태·고정 원인 코드만 담는다.

이 교차 진단 단계까지는 명시적 해제·writer/orphan 복구가 미구현이었다. 9절에서
불변 증거 append writer만 추가로 복구한다. 위 진단은 필요한 근거를
읽을 수 있게 한 단계이며, marker 삭제·재호출·unknown 예산 환불은 추가하지 않았다.
M5/M6 및 실제 기기/모델/격리 검증 전체 완료를 주장하지 않는다.

검증 결과:

- 관련 회귀 16개 파일 **287 passed (37.77초)**. native 영수증 복구·Work lifecycle·
  Evidence integrity·승인/cohort 재개·공유 원장·기동·CLI 호환을 포함한다.
- 마지막 진단 표시/원인 코드 정리 후 신규 파일 **38 passed (3.29초)**를 재확인했다.
  위 회귀 집합에도 포함되는 시험이므로 287과 합산하지 않는다.
- 전체 `src/tests` Ruff, 변경 소스 6개 파일 mypy, `git diff --check`, 실제
  `status --help` 통과. 실제 전문 합성 실행/CLI 시험은 원본 DB·Evidence 내용의
  무변경과 추가 모델/대상 호출 없음도 확인했다.
- 이번 변경 뒤 전체 수천 건·Docker 시험은 다시 실행하지 않았다. 실제 LLM,
  금융 대상, Mobile 기기와 생성 코드는 호출/실행하지 않았다.

## 9. 원본 증거 append 응답 유실 복구 (2026-10-11)

작업 checkout은 `WHS-Red-Teaming-Agent-dev-specialists-20261010`이며 기준 dev
`153265e`의 native 실행 구조를 유지한다. 8절의 읽기 전용 진단을 재실행 허가로
쓰지 않고, 기존 전문 cohort 재개 접점에 **확인 가능한 저장 완료만** 연결했다.
이번 변경은 M5의 일부이며 PRD 전체 완료·실환경 검증 완료를 의미하지 않는다.

직접 확인한 문제:

- 기존 `test_unknown_append_retains_fence_across_reopen`은 Memory/SQLite 양쪽에서
  파일 저장 후 OSError/취소가 발생하면 원본은 남지만 계획 참조가 계속 blocked임을
  재현한다. 수정 전 기존 파일 8건을 먼저 실행해 이 동작을 확인했다.
- 기존 append 멱등성은 같은 원본이 이미 있으면 반환한다. writer intent가 없어서
  저장 완료만 끊긴 경우와 부분 저장을 구분하지 못하므로 fence를 자동으로 지울 수 없었다.

구현과 책임:

- `PlanningAdmission`: 기존 coordinator records 안에 writer v1 intent와 전체 참조/버전을
  같은 commit으로 저장한다. 원본 내용은 복제하지 않는다. 완료 시 전체 inventory와
  recovery snapshot을 CAS 검사하고, confirmed ack는 새 writer를 건드리지 않는다.
- `PlanningEvidenceStore`: append 입력을 deep copy한 뒤 기대 ID/전체 hash를 기록한다.
  기존 store의 무결성과 원본 조회로 실제 일치하는 append만 확인한다. legacy·Graph·
  원본 부재/상충·읽기 실패는 보류한다. 도구/모델/Graph 호출과 재append는 하지 않는다.
- `InstanceDriver`: 기존 exclusive Run ownership과 actor 수명을 소유한다. 원본 checkpoint
  처리 전에 복구하고, 실행 중 actor가 있거나 미확정 writer가 있으면 거절한다. 복구
  중 actor 시작과 소유권의 조기 반납을 막는다. 기존 승인·Task·LLM 원장은 유지한다.
- CLI: 같은 `_drive_cohort_guarded`에 연결했다. 일반 상태 진단과 legacy 실행 경로는
  바꾸지 않으며, 공개 port를 사용해 다른 저장소의 비공개 구현을 직접 읽지 않는다.

검수에서 보완한 이유:

1. 현재 파일만 비교하면 어떤 write를 하려던 것인지 역산하게 된다. 기대 ID/hash를
   외부 저장 **전에** fence와 함께 저장하고, 구형 intent 부재는 추측하지 않는다.
2. 일부 fence 해제와 새로운 writer의 늦은 ack가 혼합되면 stale 계획이 채택될 수 있다.
   전체 참조/버전·intent를 원자적으로 재검사하고 원래 reference 버전은 보존한다.
3. journal 또는 reference 한쪽 index만 보면 부분 삭제/잘못된 귀속을 빈 조회로 숨긴다.
   양쪽 inventory를 검사하고 bool을 정수 version과 같다고 인정하지 않는다.
4. EvidenceRecord는 가변 객체다. 대기 중 호출자가 metadata를 바꾸면 기대 proof와
   실제 append가 달라질 수 있으므로 입력 snapshot을 고정했다.
5. Run 소유권만으로 같은 driver 안의 actor/close 경합은 막지 못한다. 복구 수명에만
   작은 lock을 사용하고 actor 시작과 close를 기존 driver 경계에서 검증했다.
6. 저장된 실패·unknown 기록은 성공 근거가 아니다. storage 확인만 정리하고 실행
   영수증·Work·resource marker·claim·예산·Task/목표 완료는 기존 별도 게이트에 남겼다.

검증 결과와 미완료 범위는 아래에 기록한다. 새 private engine·Task queue, 특정 랩
주소/계정/경로·모델 하드코딩, Control 완화나 생성 코드 실행 권한을 추가하지 않았다.

검증 기록:

- 신규 두 파일 **62 passed (2.84초)**. 별도 PID의 실제 append 직후 종료와 SQLite
  재연결, 오류/취소, proof와 fence의 원자적 rollback, 양방향 index 손상, CAS 경합,
  가변 입력 snapshot, 승인/cohort 연결과 driver 시작/종료 경합을 포함한다.
- unit/integration 전체 회귀 **3,857 passed / 76 skipped (131.82초)**.
  기존 Docker 실패가 있는 `test_gated_install_docker.py`·`test_shell_workroom_docker.py`,
  localhost가 필요한 `tests/unit/test_install_proxy_route.py`와 integration의
  `test_page_text_mcp.py`·`test_page_text_transport.py`·`test_principal_mcp_transport_e2e.py`·
  `test_approved_mcp_deployment.py`는 이 명령에서 제외했다.
- 위 localhost 5개 파일은 별도 localhost 허용 실행에서 **26 passed (19.64초)**.
  처음 제한된 실행의 포트 bind `PermissionError` 1건은 여기서 재확인했다.
  제품 ROE/Control 정책을 변경한 것이 아니며 외부 금융 사이트를 사용하지 않았다.
- 전체 `src/tests` Ruff, 변경 소스 4개 파일 mypy, `git diff --check` 통과.
  전체 회귀 이후 CLI의 실패 안내 prefix만 `recovery inventory unreadable`로 정리하고
  관련 10개 파일 **151 passed (13.35초)**를 추가 확인했다. 집중 시험은 전체 회귀와
  중복되며 합산하지 않는다.
- 76개 skip, 제외한 기존 Docker 격리 실패 3건, 실제 LLM/Mobile/금융 대상과 생성 코드
  실행을 통과로 계산하지 않는다. 큰 저장소/여러 호스트의 성능은 측정하지 않았다.

남은 작업: entered+checkpoint 부재 및 실행 provider의 원본 확인/명시적 claim 해제,
Graph/구형/불명 writer 복구(새 Graph receipt는 아래 10절에서 보완), 합성 코드
artifact 검증·격리, 자동 만료 기동, 승인된
실제 Mobile/LLM 평가, 기존 Docker 격리 실패 확인과 최종 인계. 이번 복구는
EvidenceRecord 저장 완료만 증명하며 artifact 파일·증거의 현재 유효성·실제 취약점이나
목표 달성을 증명하지 않는다. 구형 기록을 현재 파일에서 역산하거나 새 Run/예산으로
대체하지 않는다. 이번 변경은 로컬에 반영했으며 추가 commit/push/PR은 하지 않았다.

## 10. Graph 변경의 원자적 저장 영수증 복구 (2026-10-11)

같은 checkout·고정 dev 기준에서 M5 후속 작업을 진행했다. 별도 fetch/pull, 구형
`dev-jm` 교체, commit/push/PR은 하지 않았다. M5/PRD 전체 완료로 표시하지 않는다.

직접 확인한 실패 접점은 `PlanningGraphStore._write`다. 외부 Graph commit이 성공하고
응답만 유실되면 coordinator의 reference fence는 남는다. 9절의 Evidence append proof는
불변 원본에 관한 것이므로 mutable Graph에 그대로 적용할 수 없다. 현재 값 비교로
해제하면 저장 전부터 같던 값을 새 변경의 성공으로 오인하거나 뒤의 정상 변경을
무시할 수 있다. 이 두 상황을 별도 시험으로 확인했다.

구현:

- 소유 모듈 `attack_graph/stores/mutations.py`에 선택적 prepare/apply/read receipt
  port와 versioned DTO를 추가했다. core Graph port/GraphNode/GraphEdge는 변경하지 않았다.
- Memory와 SQLite provider가 원본 변경+receipt의 원자성을 제공한다. SQLite는 기존
  Graph DB 안에서 같은 transaction을 사용한다. 일반 쓰기와 새 쓰기는 같은 SQL
  statement builder를 재사용하여 node/edge·cascade 삭제 의미를 복제하지 않았다.
- engagement wrapper가 physical ID/namespace를 확정하고 다른 engagement의 요청과
  receipt를 거절한다. 구형 edge 삭제의 기존 두-ID 동작은 보존하며 새 자동 복구로
  덮어쓰지 않는다. 구형/custom store도 일반 쓰기는 유지한다.
- planning wrapper가 deep copy한 입력의 ID/hash를 기존 reference intent에 먼저
  저장한다. 원본 receipt 조회와 기존 전체 inventory CAS에 성공한 경우에만 fence
  확인 상태를 정리한다. 원본 Graph 재적용과 대상 도구/모델 호출은 없다.
- 기존 driver의 exclusive/quiescent 경계에서 Evidence/Graph owning wrapper를 연결하고,
  모든 writer inventory를 마지막에 검사한다. 모르는 proof를 다른 wrapper가 해석하거나
  known writer만 확인했다고 나머지 unknown을 숨기지 않는다. Control·Task·claim·
  activation·예산·기존 native ReAct/SETTLE 경로는 유지한다.

검수에서 보완한 사항과 확실한 이유:

1. Graph와 확인 상태가 다른 DB이므로 Graph 값/시간만으로 commit을 확정할 수 없다.
   Graph와 receipt를 provider가 같은 transaction에 저장하도록 했다. receipt insert
   실패 때 Graph 변경도 rollback하는 node/edge 추가·삭제 4종 시험을 통과했다.
2. 저장 뒤 응답 유실과 저장 전 실패를 구분해야 한다. 값이 같아도 receipt가 없으면
   held이고, 값이 뒤에 달라져도 정확한 원본 receipt가 있으면 저장 ack만 확인한다.
3. 같은 요청의 재전달이 현재 Graph를 덮어쓰면 진전/원본이 왜곡된다. 같은 ID는 최초
   receipt만 반환하며, 상충 요청은 거절한다. noop 삭제도 뒤에 생긴 객체를 지우지 않는다.
4. namespace 변환은 hash 확정 전에 수행해야 한다. provider의 물리 ID/receipt key를
   확정한 뒤 proof를 만들고, 다른 engagement/Run/index는 거절한다.
5. mutable payload와 malformed 준비/receipt는 증명으로 쓰지 않는다. 입력 snapshot,
   준비 단계의 Run/작업/참조 동일성, receipt schema/반환값/원본 hash를 검증했다.
6. Graph와 Evidence를 한 중앙 kind 분기로 처리하면 저장소 책임이 섞인다. 각 wrapper는
   자기 proof만 해석하고 driver는 마지막에 전체 pending/held 상태를 확인한다.

검증: 관련 9개 파일 **218 passed (11.06초)**. 실제 별도 PID에서 SQLite Graph commit
직후 종료한 뒤 coordinator와 Graph를 다시 연결하는 시험, 응답 오류/취소, 미저장,
원본·index·namespace·CAS 상충, 가변 입력, legacy/custom 호환, scripted 전문 loop 및
실제 승인 cohort 연결을 포함한다. 같은 승인/action/instance/Task와 공유 예산을 유지하고
추가 모델·대상 요청이 없음을 확인했다. 집중 시험은 아래 전체 회귀에 포함되므로
합산하지 않는다.

최종 검증:

- unit/integration **3,948 passed / 76 skipped (133.87초)**. 9절과 같은 제외 목록을
  사용했다: 기존 실패가 있는 Docker 2개 파일과 localhost 5개 파일이다.
- localhost 5개 파일은 별도 로컬 포트 허용 실행에서 **26 passed (19.10초)**.
  외부 금융 사이트/LLM을 호출하거나 제품 scope·Control을 변경하지 않았다.
- 전체 `src/tests` Ruff, 변경 소스 11개 파일 mypy, `git diff --check` 통과.
  검사 이후에는 결과 문서만 갱신했다. 새 코드·문서는 로컬에만 있으며 HEAD는
  `3c24e28` 그대로다. GitHub push/PR이나 다른 checkout 변경은 하지 않았다.
- Docker 격리의 기존 실패 3건은 이번에 다시 실행/수정하지 않았다. skip·제외·
  실제 환경 미실행은 성공으로 계산하지 않는다.

한계: Memory receipt는 같은 프로세스 수명 내에서만 유효하다. 구형 intent/receipt 부재,
원본 불명·손상은 계속 held이며 자동 claim 해제/재실행으로 우회하지 않는다. Graph
본문을 receipt에 복제하거나 현재 Graph와 비교하지 않는다. 임의 DB 조작에 대한 인증,
여러 호스트의 분산 복구, 큰 저장소 성능은 검증하지 않았다. 생성 코드는 계속 미실행이며
실제 LLM/금융권/Mobile 검증은 이번 scripted/합성 시험의 통과에 포함되지 않는다.

다음 남은 범위는 entered+checkpoint 부재의 원본 실행 검증/명시적 claim 해제,
구형·불명 writer/orphan, 합성 code artifact 검증·격리, 자동 만료 기동, 승인된
실환경 평가, 기존 Docker 격리 실패의 안전성 확인 및 M6 최종 인계다.

## 11. 활성 cohort의 관측된 증거 만료 기동 (2026-10-11)

같은 checkout·고정 dev 기준에서 M5의 다음 접점을 연결했다. 10절 마지막의 자동
만료 항목은 아래 **활성 cohort 범위**로 보완했다. held/terminal Run의 독립 기동이나
M5 전체 완료를 뜻하지 않는다. 별도 fetch/pull·commit·push·PR은 하지 않았다.

직접 확인한 문제: `evidence_validity.refresh_validity`와 완료 게이트는 관측된 만료를
판정하지만, refresh는 주로 explicit assess/기존 planner 진입 시 호출됐다. 기존
`InstanceDriver`는 TaskCreated 또는 actor 반환을 기다리므로 독립 워커가 장시간
실행 중이면 만료 변화의 후속 판단 전달이 늦어질 수 있었다.

구현:

- 증거 의미 소유 모듈이 `ValiditySchedule`/`validity_schedule`을 제공한다. 기존
  coordinator의 projection records에서 같은 Run/engagement의 실제 valid deadline만
  조회한다. Run/Mission/lifecycle·pending operator 상태와 schema/identity/timezone를
  확인한다. projection은 notification hint이며 완료/실행 허가가 아니다.
- 기존 driver에 선택적 `on_wakeup`/`wake_events` 접점을 붙였다. actor 결과·coalesced
  event·관측된 다음 만료 중 먼저 도착한 것을 기다린다. model을 event handler에서
  호출하지 않고 기존 드라이버의 직렬 결과/기동 경계에서 application callback을
  소비한다. 다른 actor들은 동일 driver 소유 아래 계속 실행한다.
- `SpecialistLifecycle.evidence_wakeup`이 만료 시 현재 원본과 grounding을 다시 읽고
  기존 outbox로 유효성 변화만 기록한다. 실제 변화만 기존 planner → atomizer → Task
  admission/activation에 전달한다. 새로운 조사 engine·queue·저장소·role ID 분기는 없다.
- fresh start와 검증된 full-cohort resume가 같은 접점을 사용한다. 부분/unknown 재개는
  callback을 켜지 않는다. active resume의 overdue stored-valid deadline은 fresh
  원본으로 확인한다. 판단 중 변경과 추가 만료도 재검사하고 기존 final CAS를 유지한다.
- event payload는 canonical 상태를 확인하라는 힌트다. 같은 상태의 알림은 추가 모델
  요청이 없고, 기존 meaningful-input dedup·유한 전략/기동 제한·공유 Run 원장을 유지한다.
  빈 cohort는 미래 만료를 기다리지 않고 반환한다. 완료·승인 대기 actor를 초기화하거나
  Task/claim·Control·예산·원본 Evidence/Graph를 만료 이벤트로 변경하지 않는다.
- 읽기/정합성/fence 실패는 `evidence_validity_wakeup_held`와 안전한 예외 유형으로
  남긴다. 현재 wakeup을 보류하고 즉시 timer retry를 예약하지 않는다. 취소는 숨기지
  않고 기존 driver teardown으로 전달한다. 기존 사건/명시적 recovery가 후속 확인 경계다.

검수 수정 이유:

1. 증거 생성 시각에 임의 TTL을 더하면 유효성을 추측한다. 원본에서 측정된 aware
   timestamp가 있는 등록 조건만 wakeup 시각으로 사용했다.
2. publisher 안에서 모델을 호출하면 outbox delivery/lock 경계를 침범하고 다른
   사건 전달을 막을 수 있다. 기존 subscribe는 알림만 보내고 driver가 callback을 호출한다.
3. projection/event를 원본 판단으로 사용하면 과거 상태를 승인할 수 있다. 만료 시
   original/grounding을 재검사하고 기존 계획·완료 gate는 별도로 fresh/CAS를 검사한다.
4. 모델 판단 뒤의 상태를 이미 검토한 것으로 표시하면 판단 중 변화를 잃는다.
   판단 전에 관측한 digest만 소비하며 이후 변화·판단 도중 경과한 만료는 다시 확인한다.
5. 매 만료마다 새 예산/Task/actor를 무조건 만들면 중복 실행·과금·작업 부활 위험이 있다.
   기존 planner/admission/activation/공유 원장을 쓰고 inactive/partial/empty 경계를 지켰다.
6. 저장 오류를 no-expiry로 숨기거나 지난 시각을 계속 반환하면 안전 판단 유실 또는
   zero-timeout spin이 생긴다. bounded diagnostic/보류로 구분하고 무변경 반복을 막았다.

검증:

- 신규 2개 시험 파일 **38 passed (2.28초)**. memory/SQLite, known/unknown/naive 만료,
  inactive·pending operator, 원본 보존·변경·중복, overdue 확인, 판단 중 변경, cancellation,
  writer fence·조회 오류, empty/partial의 비기동을 검증했다.
- 통합 사례는 실제 planner/atomizer·Task 계약/admission·outbox·activation·공유 요청
  원장과 scripted provider를 사용한다. 독립 synthetic actor들이 대기 중일 때 만료
  후속 Task가 기동되고 판단 2회를 같은 Run에 계량했으며 대상 도구 호출은 없었다.
- 앞선 관련 7개 파일 회귀 **98 passed (10.49초)** 이후 최종 전체 회귀를 수행했다.
  집중 시험은 전체 결과에 포함되므로 합산하지 않는다.
- 최종 unit/integration **3,986 passed / 76 skipped (139.69초)**. 9~10절과 동일하게
  기존 Docker 실패 2개 파일 및 localhost 5개 파일은 이 명령에서 제외했다.
  localhost 5개 파일은 별도 로컬 포트 허용 실행에서 **26 passed (19.59초)**.
- 전체 `src/tests` Ruff, 누적 변경 소스 12개 파일 mypy, `git diff --check` 통과.
  skip·제외한 기존 Docker 3건·미실행 실환경을 성공으로 계산하지 않는다.

한계와 다음 작업: 종료/보류 Run의 independent timer service, 분산 event delivery,
큰 binding 기록의 성능은 구현/측정 범위 밖이다. 이번 변경은 대상 재검사 자체를
자동 허가하거나 실제 LLM·금융권·Mobile·생성 코드 성능을 입증하지 않는다.
entered+checkpoint 부재의 원본 실행/provider 확인·명시적 claim 해제, 구형·불명
writer/orphan, 합성 코드 artifact 검증·격리, 승인된 실환경 평가, 기존 Docker 안전성
확인 및 M6 최종 인계가 남았다. HEAD는 `3c24e28`이며 새 변경은 로컬에만 있다.

## 12. checkpoint 없는 entered actor의 원본 수집 분리 (2026-10-11)

11절 뒤의 M5 후속으로 **원본 수집 단계**를 구현했다. 동일 checkout·고정 dev 기준
`153265e32e029481628e8c057fe0ea476d939d64`를 유지한다. 원격 최신화·commit·push·PR은
하지 않았다. 이 단계만으로 M5/M6나 checkpoint 없는 actor 재개가 완료되지는 않는다.

실제 확인한 접점:

- 기존 `reconcile_execution_receipts`에는 provider query/fetch·원본 저장과
  VERIFY/UPDATE·Work 정산·계량 투영이 함께 있었다. checkpoint 없는 actor에 초기
  Runtime을 넣어 이 전체 함수를 실행하면 최신 런타임을 재구성했다고 잘못 해석할
  여지가 있다. 원본 수집만 따로 호출할 공개 경로가 필요했다.
- `TaskActivation.unstarted`는 entered이면 정확히 거절한다. 이 검사 삭제나
  reserved로 바꾸는 방식은 실제 실행을 중복 제출할 수 있으므로 유지했다.
- 검수 재현 시험에서 receipt Run/proposal/reference가 같더라도 내부
  `invocation.execution_context.run_id`가 다른 기록이 `known`으로 받아들여졌다.
  수정 전 해당 시험은 **1 failed**였다. 이 내부 Run 귀속 누락은 기존 journal 소유
  검증에 추가했고, 수집/일반 재개/읽기 진단이 같은 검사를 사용한다.

구현:

1. `orchestration/execution_collection.py`가 원본 수집을 소유한다. 기존
   `ExecutionRecoveryProvider`와 `execution_journal`을 사용하며 별도 provider
   registry·실행 큐·원장·Graph/Work 복사본을 만들지 않았다.
2. 단일 submission의 불변 hash·전체 invocation/proposal/step/Work/instance binding을
   확인한다. 보존된 원본이 있으면 재사용한다. 없다면 저장된 reference와 provider가
   pure prepare한 공통 식별/승인 입력 digest를 비교한다. opaque handle의 실제 보관
   결과 검증은 제공자의 기존 port 책임으로 남긴다.
3. verified·non-uncertain terminal 상태와 원래 invocation/action/tool에 일치하는
   결과만 기존 append-only Evidence에 resolution으로 추가한다. query/fetch 전체는
   원래 invocation timeout 안의 1회 시도다. unknown/running/오류/timeout은 보류하고
   cancellation은 전파한다. ToolGateway invoke와 자동 재시도는 하지 않는다.
4. batch 수집은 기존 Work/receipt inventory를 검사하고 안전한 invocation별
   `known/held/invalid`·원본 receipt ID만 반환한다. 내용/handle/예외 본문이 진단에
   들어가지 않는다. DTO의 replay/claim release 허가 값은 항상 false다.
5. `TaskActivation.checkpointless_entered`는 원래 version 1 activation digest와
   Run/engagement/Mission/Task/role/claim/완료 계약만 증명한다. returned/held를 다시
   launched로 바꾸지 않는다. 이 initial Runtime은 collection identity이지 checkpoint나
   현재 사용량의 복구값이 아니다. reserved의 zero-entry 경로는 별도로 그대로 유지한다.
6. `InstanceDriver.collect_checkpointless_originals`는 명시적 application API다.
   기존 로컬 Run 소유권·공통 quiescent recovery lock·현재 조립 입력/component/요청
   정책·실제 checkpoint absence를 확인한다. 모든 actor preflight가 끝난 후 provider를
   읽고, 조회 후 activation/claim/입력도 재확인한다. 미확정 writer는 먼저 해결해야 한다.
   진행 중인 actor·다른 driver와 동시에 수집하지 않는다.
7. 기존 `reconcile_execution_receipts`의 수집 코드를 같은 함수 호출로 대체했다.
   일반 checkpoint 재개에서는 원본 확인 후 기존 VERIFY/UPDATE·Work 정산·accounting
   투영이 계속 적용된다. 새 API에서는 그 후속 처리를 실행하지 않는다.

검수 수정 이유:

| 결정 | 확인한 이유 |
| --- | --- |
| 원본 수집과 해석/정산 분리 | activation의 초기 Runtime만으로 최신 대화·승인·전체 사용량 상태를 복원할 수 없음 |
| 기존 zero-entry 거절 유지 | entered 기록·빈 receipt 목록은 미실행 증명이 아니며 재전송하면 중복 효과 가능 |
| 공통 식별 필드까지 prepare 비교 | 기존 수집 코드는 input digest만 비교했음. 다른 Run/instance/action의 reference는 query 전에 거절해야 함 |
| 내부 실행 문맥 Run 검사 추가 | 실제로 다른 내부 Run이 known으로 받아들여지는 수정 전 실패를 재현함 |
| immutable resolution과 동일 원본 재조회 | 저장 응답 유실을 재실행/과거 실패 덮어쓰기/두 번째 provider fetch로 처리하면 원본 계보가 왜곡됨 |
| 원래 timeout으로 query/fetch 한 번만 수행 | provider 조회가 멈추면 Run 소유권/정리도 멈출 수 있음. 새로운 고정 timeout이나 무한 retry를 만들 필요 없음 |
| 별도 CLI 자동 fallback을 추가하지 않음 | 원본 수집 성공은 현재 런타임·모든 pending 효과·공유 계량·claim handoff의 증명이 아님 |

검증:

- 신규 2개 합성 integration 파일 **45 passed (2.97초)**. 확정 원본 보존·중복 수집,
  저장 응답 유실, provider 부재/running/unknown/unverified/원본 부재, query/fetch 오류와
  실제 deadline cancellation, 잘못된 결과/reference·digest/Work/Task/Run, 원본
  cancellation, resource execution marker 보존, memory/SQLite actor identity·claim·
  공유 원장 보존, 설정/실제 checkpoint/Run 소유권 상충, 동시에 drive하지 못하는
  경계, 수집 후 일반 재개 연결 및 entered/no-checkpoint의 계속된 보류를 검사했다.
- 최종 unit/integration **4,031 passed / 76 skipped (140.38초)**. 11절과 같은 기존
  Docker 2개 파일과 localhost 5개 파일을 이 명령에서 제외했다. 원본 수집의 최종
  내부 Run 귀속 수정과 resource marker 회귀도 포함한 결과다.
- localhost 5개 파일은 별도 로컬 포트 허용 실행에서 **26 passed (19.94초)**.
  이번 단계의 수집/영수증 변경 후 실시했다. 기존 Docker 실패 3건은 이번에
  재실행/수정하지 않았으며 성공으로 계산하지 않는다.
- 전체 `src/tests` Ruff, 누적 변경 소스 16개 파일 mypy, `git diff --check` 통과.
  집중 시험은 넓은 결과와 겹치므로 합산하지 않는다. 실제 LLM/Mobile/금융권
  대상·생성 코드는 호출하지 않았다. skip/제외를 통과로 계산하지 않는다.

한계/다음 작업: collection API를 위한 operator CLI, checkpoint 없는 전체 런타임과
사용량 재구성·사람 지시에 연결된 안전한 claim handoff, 구형 writer/orphan 및 원본
부재, 코드 격리·승인된 실환경 평가·기존 Docker 안전성 확인·M6 인계가 남았다.
`status`의 읽기 전용/무-provider 동작과 `resume`의 entered/no-checkpoint 거절은
유지한다. `known`은 보존된 원본이 있다는 뜻이며 Task 성공·목표 달성을 뜻하지 않는다.

## 13. 명시적 원본 수집 operator CLI (2026-10-11)

12절 API의 후속 접점을 `collect-originals` CLI로 구현했다. 동일 checkout과 기준
dev SHA를 유지하며 GitHub 최신화·commit·push·PR을 하지 않았다. 기존 resume/status
명령을 자동 수집이나 entered actor 재실행으로 바꾸지 않았다.

```sh
./.venv/bin/python -m redteam.app.cli collect-originals RUN_ID engagement.yaml --json
```

구현과 검수 근거:

- `TaskActivation` 소유 모듈에 collection 전용 historical assembly identity 조회를
  추가했다. 기존 read-only SQLite snapshot을 재사용하고 저장 runtime/hash·Run/
  engagement/Mission/Task/role·component·공유 정책을 전체 cohort에서 확인한다.
  원래 claim 확인은 기존 API와 같은 owner 메서드를 공유한다. CLI가 저장소 내부
  row나 provider handle을 직접 해석하는 별도 복구 규칙은 만들지 않았다.
- 기존 zero-entry identity는 entered actor를 계속 거절한다. 새로운 collection hint는
  historical assembly 식별일 뿐 최신 runtime/checkpoint/accounting이나 실행 허가가
  아니므로 타입과 조회 경로를 구분했다. 이전 목표 변경/종료가 과거 원본을 지우지는
  않지만 이 명령으로 Run을 부활시키지도 않는다.
- CLI는 원래 specialist MD/profile fingerprint·공유 요청 정책·등록 component를
  조립 전에 확인한다. 원래 engagement ID와 기존 durable assembler를 재사용하며
  별도 runtime/DI/provider registry를 만들지 않았다. 조립은 MCP discovery를 열 수
  있지만 도구/모델 실행은 하지 않는다. Run 소유권 확보 후 전체 checkpoint/activation
  inventory를 다시 검사하고 이후 기존 driver collection API만 호출한다.
- 전역/per-instance `replay_permitted=false`, `claim_release_permitted=false`와
  bounded invocation DTO만 출력한다. 모든 원본이 known이고 비어 있지 않을 때만
  수집 exit 0이며, 실패 원본의 known도 포함할 수 있다. Task/목표 성공과 구분한다.
  빈 목록·unknown·invalid·설정/claim 변화·pending writer는 실패/보류로 남긴다.
- 오류 내용은 coordinator의 code-owned 거절 또는 예외 class만 표시한다. raw output,
  불변 runtime 본문, provider handle, 설정/연결 예외의 민감한 문구는 출력하지 않는다.
- 검수에서 **원본 조회 취소 뒤 MCP 연결 정리 오류가 발생하면 취소가 일반 오류로
  바뀌는 문제를 재현**했다. 수정 전 회귀 시험은 1 failed였다. driver와 transport를
  모두 정리하되 원래 취소/오류는 유지하고 정리 오류는 class-only로 별도 표시하도록
  수정했다. 기존 engine·Graph·Control 규격을 변경하지 않았다.

검증:

- 신규 합성 CLI integration 파일 **30 passed (5.56초)**. parser 등록, 원래 설정/ID
  조립, 중복 원본 수집, shared ledger·claim·승인/도구/모델 비호출, provider 부재/
  running/invalid/빈 inventory, 전체 cohort 설정/claim/runtime/checkpoint 거절,
  조립 사이 변경·Run 소유권·pending writer, read-only identity, zero-checkpoint
  빈 목록, text/JSON 출력, 민감한 오류 미출력과 취소/정리 오류를 검사했다.
- canonical `build_application`을 실제로 다시 조립하는 사례는 synthetic 모델/도구/
  recovery provider를 명시하고 기존 SQLite 및 Filesystem Evidence를 읽었다.
  resolution 저장·원본 integrity·동일 component binding·원장 불변을 확인했다.
  라이브 MCP가 실제로 원본 recovery를 지원한다는 시험은 아니다.
- 최종 unit/integration **4,061 passed / 76 skipped (145.12초)**. 12절과 같은
  기존 Docker 실패 2개 파일 및 localhost 5개 파일은 이 명령에서 제외했다.
  집중 시험은 이 넓은 결과에 포함되므로 합산하지 않는다.
- localhost 5개 파일은 별도의 로컬 포트 허용 실행에서 **26 passed (19.62초)**.
  기존 Docker 실패 3건은 이번에 재실행/수정하지 않았고 성공으로 계산하지 않는다.
- CI 대상 `src/tests/examples`와 Docker 예제 서버 2개 Ruff, 누적 변경 소스
  **18개 파일 mypy**, `git diff --check`, CLI 실제 `--help` 확인 통과.
  실제 LLM·금융권·Mobile 대상·생성 코드 실행은 하지 않았다.

남은 범위: checkpoint 없는 전체 최신 런타임/계량 재구성과 사람 지시 기반의 안전한
claim handoff, 구형 writer/orphan·원본 부재, 생성 코드 격리·승인된 실환경 평가·
기존 Docker 안전성 확인·M6 인계다. 이 CLI는 원본 수집 접점의 완료이며 M5/M6 전체
완료가 아니다. HEAD `3c24e28`은 그대로이고 새 구현·테스트·문서는 로컬에만 있다.

## 14. 점유 정리 및 공유 요청 원장 경계 검수 (2026-10-11)

13절 이후 전체 복구/인계에 필요한 조건을 점검했다. 최신 native context와 계량을
초기 activation에서 추측해 재구성하지 않았고, 기존 dev 실행 구조를 교체하지 않았다.
이번 범위는 다음 두 실제 빈틈의 보강이다. 미완료 Task handoff 구현 완료는 아니다.

| 확인한 문제 | 재현 근거 | 수정 이유/방식 |
| --- | --- | --- |
| `release_instance_claim`이 호출자의 Task/owner만으로 미완료 점유를 해제 | 올바른 fixture로 수정 전 memory/SQLite × 미완료 상태 4종의 거절 시험 **8 failed**. terminal Runtime을 넘겨도 실제 Task는 미완료인데 해제됨 | 반환 Runtime은 canonical 완료 판정이 아님. 기존 Run 소유권/quiescence와 transaction에서 실제 binding/Mission/Task/owner/lifecycle 확인. 종료 Task 정리만 허용 |
| 요청 원장의 index/payload·owner/kind·state/usage가 공통 검증되지 않음 | 수정 전 malformed row 예약 14건과 index mismatch 정산 2건, 총 **16 failed**. 요구한 거절이 발생하지 않음 | recovery의 owner activity/slot/usage 조회와 실행 admission이 같은 소유 검사를 사용해야 함. 공통 row 검사 후 예약·조회·dispatch/settle/release 처리 |

구현과 검수 결정:

1. Task lifecycle 판단은 `task_graph.claim`이 기존 `rules.TRANSITIONS`를 사용한다.
   중앙 Driver에 종료 상태 목록이나 역할/도구/시험 대상 이름을 복제하지 않았다.
2. Driver는 await 전에 호출자의 identity만 복사하고 기존 Run exclusive ownership과
   quiescent recovery lock을 사용한다. 같은 transaction에서 scope와 종료 상태를
   재확인하고 기존 release helper로 occupant만 정리한다. 다른 owner/missing Task는
   거절하며, 이미 정리된 종료 Task는 변경하지 않는다. task 없는 기존 no-op은 유지한다.
3. 미완료 Task 인계는 `task_claim_handoff_unverified`로 명확히 거절한다. 현재
   operator intent enum에는 claim handoff 계약이 없다. GUIDANCE/RESUME나 모델의
   추정을 사람의 인계 지시로 재해석하지 않았고, 새로운 자동 호출/CLI도 만들지 않았다.
4. 종료 Task는 기존 claim 규칙상 다시 채택할 수 없어 이 제한된 정리는 checkpoint
   재구성 없이 가능하다. 반면 미완료 인계에는 최신 상태/처리 증명, 명시적 사람 지시,
   원래 actor의 재개 차단과 claim 변경을 원자적으로 연결해야 한다. 그 기능은 남았다.
   초기 Runtime, 원본 known, 사용량 0만으로 인계 허가를 주지 않는다.
5. 점유 정리는 Work execution/resource marker, 요청 slot/비용, 승인, Task/Mission/Run
   상태, Graph/Evidence, activation/checkpoint를 바꾸지 않는다. 닫힌 Run을 열거나
   미완료 Task를 성공 처리하지 않는다. 종료 Task에 남은 불명 효과도 별도로 계속 보류한다.
6. 요청 기록 검사는 index/payload ID, nonblank scope/kind, 상태, 사용량 필드 및 정수/
   비음수·상태 호환을 확인한다. Boolean 토큰/없는 사용량/임의 상태도 안전한 고정 코드로
   거절한다. 다른 owner 필터를 적용하기 전에 해당 Run의 row를 검사한다.
7. 새 호출 kind는 열린 문자열이며 새 역할별 분기를 만들지 않는다. 귀속된 다른 Run은
   분리하고, Run 자체가 불명인 row는 안전하게 inventory에서 제외할 수 없어 거절한다.
   구조 검사를 외부 효과 부재·최신 전체 런타임 복구·암호학적 이력 증명으로 주장하지 않는다.
8. 손상 기록을 자동 수정/삭제/환불하지 않는다. 취소로 unknown 처리된 요청은 기존 charged
   의미를 유지한다. request/owner/body를 오류에 출력하지 않는다.

검증:

- 최종 집중 9개 파일 **222 passed (19.17초)**. 신규 2개 파일의 76개 합성 사례를
  포함한다. memory/SQLite 미완료 거절·종료 정리 멱등성·scope/owner 충돌·다른 driver/
  live actor·닫힌 Run·정리 전 취소, Work marker와 dispatched 비용 보존, 손상/누락
  원장·공통 소비자·Run 격리·확장 method와 기존 unknown 환불 거절을 확인했다.
  기존 activation/원본 수집/API/CLI 및 shared requests/Driver/Task claim 회귀도 포함한다.
- 신규 2개 파일만 최종 다시 실행한 결과도 **76 passed (1.92초)**다.
- 넓은 unit/integration **4,136 passed / 76 skipped (154.36초)**. 13절과 같은
  기존 Docker 실패 2개 파일 및 localhost 5개 파일을 이 명령에서 제외했다.
  넓은 검사 수집 이후 추가한 정리 전 취소 1건은 위 최종 집중/신규 파일 검사에서
  검증했다. 겹치는 집중 시험을 합산하거나 skip/제외를 통과로 계산하지 않는다.
- localhost 5개 파일 별도 허용 실행 **26 passed (19.92초)**. 기존 Docker 실패
  3건은 이번에 재실행/수정하지 않았으며 전체 원본 시험 집합이 green인 것은 아니다.
- CI 대상 Ruff, 누적 변경 소스 20개 및 관련 테스트 3개 mypy, `git diff --check`
  통과. 실제 LLM·금융권/Mobile 대상·생성 코드 실행은 하지 않았다.

범위/한계: 로컬 변경만 했다. 전체 최신 상태/계량 재구성·사람 지시 기반 미완료
handoff, 구형 writer/orphan, 코드 격리 및 승인된 실환경 평가·M6는 계속 미완료다.
이번 변경으로 checkpoint 없는 actor가 재개되거나 실제 대상 성능이 증명된 것은 아니다.

## 15. M5 최종 로컬 구현·검증 — offline fixture와 readiness (2026-10-11)

작업 위치는 기존 `WHS-Red-Teaming-Agent-dev-specialists-20261010` checkout,
로컬 브랜치는 `codex/dev-first-specialists`, HEAD는 `3c24e28`로 유지했다.
기준 dev는 `153265e32e029481628e8c057fe0ea476d939d64`이며 이번 요청에서 원격
최신화·다른 checkout 수정·commit·push·PR을 하지 않았다. 이전 로컬 변경을 보존했다.

### 완료 판단

PRD M5는 복구 회귀·유효성 사건·코드 산출물·Mobile readiness와
“중단/재개 중 중복 실행 없음, 실패/unknown으로 성공 불가”가 기준이다.
G8도 “복구 **또는 명확한 보류**”를 허용한다. 이번 추가로 그 **로컬 구현·검증
범위를 완료**했다. 모든 orphan의 자동 복원이나 실제 대상 성능까지 완료한 것은 아니다.
기존 gate를 수정하거나, 미완료 Task handoff/실환경 평가를 완료로 바꾸지 않았다.

| 범위 | 실제 연결·검증 근거 | 유지한 제한 |
| --- | --- | --- |
| 실행 복구 | 9~14절의 기존 reconciliation, 원본 수집, 응답 유실 proof, shared ledger/claim 경계와 이번 전체 회귀 | unsafe/원본 불명은 held. TTL·빈 원장·초기 activation으로 재실행/인계하지 않음 |
| 증거 유효성 | 원본/현재 조건 검토·관련 참조 CAS·활성 cohort의 관측된 만료 wakeup 회귀 | 종료 Run 부활·추정 세션 만료·독립 timer service 없음 |
| 코드 산출물 | 독립 작성/검토/immutable 보존 + `code.validate` → core port → 기존 supervised Docker workroom → append-only fixture 결과 | 실제 대상 실행은 비활성. 합성 성공은 Finding·Task/목표 완료가 아님 |
| Mobile readiness | 실제 조립 catalog의 get/전체 descriptor/선택/기동 적격성/진단이 기존 정책 view를 공유 | 실제 승인된 기기·앱 미준비. health 관측과 실행 허가/기기 검증을 구분 |

### 구현과 검수 수정 이유

| 확인한 접점 | 수정 이유 | 반영 방식 |
| --- | --- | --- |
| 코드 작성/검토는 연결됐지만 합성 case 실행 경로 없음 | artifact 생성 성공만으로 테스트됐다고 볼 수 없음 | 기존 local registry에 `code.validate` 추가. 기존 sandbox를 port로 주입; 새 shell 엔진/큐/네트워크 정책 없음 |
| interrupted fixture 재호출 | 실행 원본 없이 두 번 제출하면 중복 효과를 알 수 없음 | 기존 coordinator에 정확한 한 번의 admission을 기록. 원본 없으면 held, 있으면 exact immutable 결과 재사용 |
| admission callback의 기존 증거 유효성 조회 | 외부 store 조회가 같은 coordinator를 다시 잡으면 교착됨 | 트랜잭션 밖에서 원본/조건을 캡처하고 내부에서는 staged Task/scope/reference CAS만 검사. 중첩 coordinator/외부 I/O 제거 |
| 정책이 달라진 artifact/Run 재사용 | 다른 이미지/한도를 같은 검증으로 취급하면 복구 의미가 바뀜 | mechanism+검증된 정책 fingerprint를 component/planning/artifact binding에 보존. 미선택 null 필드는 직렬화에서 생략해 구형 hash 형태 유지 |
| child에 예상 결과를 함께 전달 | 출력에 정답을 복사한 것을 정상 동작으로 오해할 수 있음 | source/input만 전달, expected는 호스트 비교. 이는 self-supplied case agreement이지 독립 보안 검증은 아님 |
| fixture 종료·취소·부분 생성 | 성공 후 정리 불명이거나 취소된 child가 남을 수 있음 | fixture 전용 deterministic key, 기존 ownership 검사 후 전체 컨테이너 제거. unverified cleanup은 held, 취소는 전파, foreign는 삭제하지 않음 |
| Mobile `all_descriptors`의 raw gateway 위임 | get/list/invoke는 정책 거절인데 카드/선택이 미승인 도구를 노출할 수 있음 | descriptor 전체 조회도 기존 Mobile eligibility로 필터. 원본 discovery/health와 provider metadata는 수정하지 않음 |
| UNKNOWN health를 HEALTHY와 같은 준비로 표시 | 기존 dev는 UNKNOWN을 선택 가능하게 둘 수 있지만 관측 건강을 뜻하지 않음 | 기존 `ready_role_ids` 의미 유지. 새 projection의 `activation_eligible`와 `catalog_ready`를 분리 |

등록·조립:

- `core/ports/code_fixtures.py`가 bounded JSON request/result/runner를 소유한다.
  사례 schema는 기존 CodeProgram에서도 같은 타입을 사용한다.
- `capability/code_fixtures.py`는 `ShellWorkroomPolicy` / `ShellWorkroomManager`를
  재사용한다. content-pinned image만 허용하며 network gate를 붙이지 않는다.
  호스트 `exec`/shell fallback·image pull·package 설치를 하지 않는다.
- `orchestration/code_fixture_validation.py`는 검토된 evidence ID만 입력받는다.
  source/path/URL/command를 직접 받는 새 실행 API가 아니다. current Run/Mission/
  Task/instance/claim/원본/조건/prompt/blob을 검사한다. STOP 후 역사적 결과는
  보존하되 현재 결과 채택은 거절한다. 실패/불명/원본 없는 중단은 재제출하지 않는다.
- `app.bootstrap`의 두 공개 조립 접점이 optional port를 주입한다.
  `orchestration.code_fixture_policy_path` /
  `REDTEAM_CODE_FIXTURE_POLICY_PATH`는 기존 typed 설정 경로를 사용한다.
  기본은 비활성이고, 선택된 독립 code roles가 필요하다. 잘못된 파일·unpinned·
  모호한 injection+설정은 실행 전에 거절한다. 사용자 경로/모델/시험 계정/포트는
  일반 실행 코드에 고정하지 않았다.
- `agent.readiness`와 planner payload는 역할 등록·기존 selector/eligibility/
  실제 catalog를 읽는다. device probing·새 도구·실행 허가를 생성하지 않는다.

### 최종 검증

- 신규 집중 4개 파일: **51 passed (1.46초)**.
  registry/조립·공통 port·기본 비활성/잘못된 설정·정확한 binding·case JSON/byte 한도·
  입력 escaping·expected 비전달·정수/boolean 구별·invalid/truncated/nonzero/unknown/
  timeout·cleanup·foreign/취소·중복/STOP/손상·저장 응답 유실을 확인했다.
  SQLite canonical coordinator와 Filesystem Evidence를 실제 재개방해 원본 재사용과
  원본 없는 consumed admission의 보류, claim/공유 사용량 보존을 확인했다.
  이 결과는 **명시적 scripted 모델/runner**이며 라이브 LLM/Mobile 시험이 아니다.
- 실제 로컬 Docker fixture 1개 파일: **4 passed (4.20초)**.
  이미 로컬에 있던 고정 이미지로 실제 Python 합성 case, demoted UID, capability 0,
  no-new-privileges, host 파일 미기록, network-none 연결 거절, 무한 loop timeout 및
  취소 뒤 컨테이너 제거를 검사했다. 새 이미지 다운로드·실제 대상 통신은 없었다.
  생성한 임시 fixture 컨테이너만 ownership 확인 후 제거했으며 저장된 작업/사용자
  파일이나 다른 컨테이너는 삭제하지 않았다. 컨테이너 격리의 완전한 보안 인증은 아니다.
- 최종 unit/integration: **4,188 passed / 80 skipped (145.71초)**.
  마지막 byte-bound/key 및 durable reopen/fixture 완료 거절 시험까지 포함한다.
  기존 Docker 실패 2개 파일과 localhost 5개 파일은 이 명령에서 제외했다.
  localhost 5개 파일은 별도 로컬 포트 허용 실행에서 **26 passed (19.32초)**.
  겹치는 집중 시험은 합산하지 않고 skip/제외를 통과로 계산하지 않는다.
- 전체 CI 대상 `src/tests/examples`와 실제 Docker 예제 서버 2개 Ruff, 이번 추가/
  변경 소스 **14개 파일 mypy**, `git diff --check` 통과.
  실제 Mobile·금융권 대상·라이브/유료 LLM을 호출하지 않았다.

### 여전히 보류/미검증인 범위 — M6와 별도 운영 작업

1. 실제 승인된 Mobile serial/앱·APK/서버·원본 수집/실행 검증. 현재 준비 정보 없음.
2. 명시적으로 승인된 실제 LLM/대상 평가 및 최종 운영 인계. 합성 결과로 대체하지 않음.
3. 수정 전 dev에서도 재현된 기존 Docker 실패 3건: gated install pip proxy의 OPEN/DENY,
   장기 shell의 loopback-only 기대와 실제 상태 불일치, 취소된 setsid 자식 정리.
   이번 fixture의 전체 컨테이너 정리 시험 통과를 기존 장기 shell의 수정으로 주장하지
   않는다. 이 2개 파일은 이번에 재실행/수정하지 않아 전체 원본 suite가 green은 아니다.
4. checkpoint 없는 전체 최신 Runtime/계량 재구성과 사람 지시 기반 미완료 handoff,
   구형 writer/원본 불명 orphan의 자동 복원. 기존 contract에 따라 계속 명확한 held.
   원본 known·readiness healthy·시간 경과는 재실행/claim 이관 허가가 아니다.
5. 실제 대상 생성 코드 실행. 공통 operation·허용 target/path·요청 한도·격리·Control
   규격이 미확정이므로 비활성이다. 무제한 LLM 요청 모드도 이 권한을 열지 않는다.

세부 계약: [코드 산출물](../contracts/code-artifacts.md),
[offline fixture / readiness](../contracts/code-fixtures.md),
[복구](../contracts/activation-recovery.md),
[증거 유효성](../contracts/evidence-reuse-validity.md).

## 16. M6 로컬 Docker 안전성 검수 (2026-10-11)

동일 checkout/브랜치/HEAD에서 기존 로컬 변경을 보존했다. Git 원격 갱신·commit·
push·PR, 실제 Mobile/금융 대상·라이브 LLM 실행은 하지 않았다. M5 이후 요청에
따라 기존 dev에서도 재현한 Docker 실패를 점검했다. **M6 전체 완료는 아니다.**
앞 절의 “Docker 3건 미해결”은 그 당시 기록이며 아래 결과로 구분해서 갱신한다.

### 원인과 수정 근거

| 접점 | 직접 확인한 근거 | 수정/판정 |
| --- | --- | --- |
| 취소 후 env-scrubbed/setsid 자식 생존 | 수정 전 취소·네트워크 2건 모두 실패(41.21초). 취소는 verified인데 실제 자식 PID는 살아 있음. host가 1초 후 subreaper를 KILL하지만 supervisor의 기본 grace는 2초 | 기존 manager에서 supervisor의 유한 grace/reap을 기다린다. 강제 KILL은 자식 group/carrier에만 적용. 보호된 terminal 원본 + clean scan이 없으면 cleanup-unknown이며 원본을 삭제하지 않음 |
| 정리/상태 조회의 잘림·빈 응답 | 이전 `workroom_container_state`는 exit 0의 빈/잘못된 응답도 stopped로 처리. tag scan과 record 조회도 outer timeout/truncation을 무시 | 정확한 true/false/부재만 확정. metadata 조회를 bounded하고 partial/읽기 불명은 unknown. 기존 budget/slot 보류를 유지하며 false-clean으로 환불하지 않음 |
| 소유권 없는 동일 이름 컨테이너 | 기존 취소는 단순 running 조회 뒤 signal을 시도 | 기존 image/label/isolation ownership 검증을 먼저 사용. foreign/unknown은 signal하지 않음. authoritative gone 또는 소유권 검증된 stopped만 정리 확인 |
| 보존된 과거 PGID의 숫자 재사용 | 추가 Docker 회귀에서 root-only fixture로 PGID 재사용을 모의. 과거 completed 기록에 대한 취소 확인이 살아 있는 형제 child를 죽이는 실패를 직접 재현(1 failed, 16.20초) | live supervisor가 없는 PGID는 역사적 숫자이므로 signal/clean 판정의 권한으로 쓰지 않음. terminal 원본은 보존. 모델은 protected 파일을 수정할 수 없으며 재현은 시험 소유 컨테이너의 trusted fixture 조작임 |
| loopback-only 시험의 오탐 | network-none 실제 namespace에 추가 터널이 있지만 모두 DOWN·IPv4 주소 없음. IPv4 경로 없음·IPv6 경로는 loopback뿐. 기존 시험은 `/sys/class/net` 이름 수만 검사 | 시험을 실제 namespace flags·IPv4/IPv6 주소·경로 + 기존 ownership 검사로 변경. 이름별 tunnel 예외 목록이나 제품 네트워크 허용 확장은 없음 |
| gated install 시험 사전 조건 | 로컬에 `shell-workroom:t003-draft`와 `t005-gate-sidecar` 이미지가 없음. 기존 시험은 다른 gated 시험과 달리 사전 점검 없이 자원을 생성해 implicit pull 가능 | 기존 `image_available` helper로 3개 필수 이미지를 자원 생성 전에 검사. 없으면 명확한 skip. 실제 OPEN/DENY 안전성이나 기존 `gated_open_failed` 원인이 해결됐다고 판정하지 않음 |
| fixture 문서의 read-only root 주장 | 기존 `create_argv`에는 `--read-only`가 없고 writable workroom/container layer를 사용 | [fixture 계약](../contracts/code-fixtures.md)을 정정. network-none/no host mounts·non-root/자식 caps·no-new-privileges·CPU/memory/PIDs/tmpfs/output/time 한도와 별개로 root read-only/전체 disk quota는 보장하지 않음 |

기존 `ShellWorkroomManager`와 supervisor/ownership/결과 계약 안에서 수정했다.
새 scheduler·shell engine·도구/역할별 예외나 Lab 정답은 추가하지 않았다.
Control/ROE/network gate를 완화하지 않았다. 따로 종료한 Task/Run을 부활시키거나
unknown 결과를 성공·Task 완료로 바꾸지 않는다. 일반 장기 workroom은 유지하며
형제 작업을 죽이는 전체 컨테이너 제거를 취소의 우회 수단으로 사용하지 않는다.

### 검증 범위

- 최종 unit/integration 회귀: **4,220 passed / 80 skipped (173.65초)**.
  마지막 PGID 보호 수정과 신규 사전 조건 시험까지 반영한 코드로 재실행했다.
  Docker 2개 파일(`test_gated_install_docker.py`, `test_shell_workroom_docker.py`)과
  localhost 5개 파일은 이 명령에서 제외하고 아래처럼 별도 실행했다.
  localhost 파일은 `test_install_proxy_route.py`, `test_page_text_mcp.py`,
  `test_page_text_transport.py`, `test_principal_mcp_transport_e2e.py`,
  `test_approved_mcp_deployment.py`다. skip/제외가 있어 전체 시험 집합의
  모든 환경을 통과했다는 뜻은 아니다.
- 최종 관련 unit 5개 파일: **174 passed (38.99초)**. 정리 원본의 보호/누락/손상,
  읽기 잘림·빈 상태 응답·foreign 소유권, grace/reap·기존 sync/job/recovery와
  fixture 실행·gated 사전 조건을 검증했다. 새로운 사전 조건·취소 단위 시험은
  scripted Docker 응답이며 아래 실제 Docker 결과와 구분한다.
- 최종 실제 Docker 3개 파일: **29 passed / 7 skipped (41.53초)**.
  장기 shell 25건과 고정 이미지 fixture 4건을 통과했다. scrubbed/setsid child
  실제 소멸·cancel 원본 보존·과거 PGID가 형제 child를 종료하지 않음·workroom
  상태/ownership/네트워크·UID/caps·timeout을 확인했다. skip은 Kali 미선택 5건,
  별도 비교 이미지 부재 1건, gated 설치 이미지 부재 1건이다.
  로컬에 이미 있던 고정 Python 이미지로 수행했으며 이미지 다운로드는 없었다.
- localhost 5개 파일 별도 허용 실행: **26 passed (26.66초)**. 로컬 합성 서버이며
  외부 금융 대상 접속/실제 LLM 호출이 아니다.
- 최종 전체 `src/tests/examples` 및 CI Docker 예제 2개 Ruff,
  shell manager/fixture 관련 생산 소스 4개 mypy, `git diff --check` 통과.
  테스트 소유 리소스는 각 fixture의 finally에서 정리했고 마지막 shell-label
  조회에 잔존 컨테이너가 없었다. 다른 사용자 컨테이너·저장 파일을 삭제하지 않았다.

위 집중/Docker 시험은 전체 회귀와 겹치므로 수를 단순 합산하지 않는다.
skip·제외·미준비 실환경을 성공으로 계산하지 않는다.

### 남은 검증 조건

- gated 설치 프록시의 실제 OPEN, proxy 경유 허용, 직접 경로 차단, 계량·종료·
  회복은 필요한 로컬 fixture 이미지를 준비한 뒤 재시험해야 한다. 이번에는
  Docker image pull/build/apt 설치를 하지 않았고 미검증을 통과로 세지 않는다.
- 실제 승인된 Mobile 기기/앱과 실제 LLM/대상 평가·최종 운영 인계는 여전히 필요하다.
- writable container layer의 disk-byte quota와 운영 이미지/runtime 신뢰는 이 회귀가
  인증하지 않는다. bounded 합성 case 검증을 범용 적대 코드 실행 보증으로 확대하지 않는다.
- checkpoint 없는 전체 Runtime/계량 재구성과 미완료 claim handoff, 구형/불명
  orphan 자동 복원은 계속 명확한 held다. 실제 대상 생성 코드 실행은 비활성이다.

## 17. 설치 프록시·실환경 준비 점검 — 2026-10-11

사용자 확인: **Mobile은 준비 점검까지만**, LLM은 기존 **100회·0달러** 조건 유지.
실제 기기·앱·유료 모델을 호출하지 않았다. 이번 차이는 네트워크 허가를 받은 뒤
누락된 로컬 Docker fixture 이미지 2개를 기존 Dockerfile로 빌드한 것이다.
기준 checkout/HEAD `3c24e28`과 dev 기준 SHA는 그대로이며 commit/push/PR은 하지 않았다.

### 사용자가 준비해야 하는 것

| 검증 | 현재 상태 | 다음에 필요한 사용자 준비 |
| --- | --- | --- |
| 합성 specialist/준비 진단·offline shell/코드 fixture | 실제 로컬 검사 가능 | 현재 추가 키·금융 계정·Mobile 정보 필요 없음 |
| 설치 프록시의 kernel gate OPEN/계량/차단 | 필수 이미지 빌드 완료. 커널 quota 부재로 OPEN 전 실패 | named quota/NFQUEUE를 지원하는 승인된 Linux Docker 호스트 또는 VM 선택. GCP는 필수가 아님 |
| 실제 Mobile | 사용자 답변상 serial/앱·APK/MCP 정보 없음 | 나중에 승인된 기기 참조·serial, package/bundle ID 또는 APK 위치·해시, 서버/정책 파일과 scope 필요. 지금 임의 다운로드·기동하지 않음 |
| 실제 LLM 비교 | 유료 호출 비허용 | 현 조건에서는 미실행 유지. 나중에 공급자·모델과 명시적 허용 호출/비용 또는 토큰 한도가 필요. 무제한 요청 기능은 무료가 아님 |
| 운영 설치 경로 | 이번에는 합성 mirror/proxy만 사용 | 운영 시에는 pinned 이미지·승인된 mirror/proxy IP/포트·별도 INSTALL/TARGET 한도 필요. 합성 설정을 운영 권한으로 복사하지 않음 |

현재 도구 프로세스에서는 LiteLLM/모델 설정과 Mobile serial/앱 설정이 관측되지
않았다. 이는 별도 Mac 터미널의 환경까지 없다고 증명하는 것은 아니다.
비밀키는 읽어 출력하거나 채팅으로 요청하지 않았다. 실제 대상 생성 코드 실행은
규격 미확정·비활성 상태를 유지한다.

### 실제 gate 실패 원인과 근거

- 실제 Docker 5건: **1 passed / 4 failed (12.47초)**. 4건은 모두
  `_apply_ruleset`의 `quota qb`/`quota name qb drop`에서 `No such file or directory`.
  fixture 이미지 부재 skip이 아니라 실제 실패다.
- 네트워크 없는 일회용 컨테이너에서 관측한 커널은 `6.12.54-linuxkit`,
  nft CLI는 `1.0.6`. `/proc/config.gz`에는 `# CONFIG_NFT_QUOTA is not set`,
  `CONFIG_NFT_QUEUE=y`, `CONFIG_NETFILTER_NETLINK_QUEUE=y`가 기록돼 있었다.
- 올바른 다중 행 문법의 `nft --check`로 default-drop과 NFQUEUE는 exit 0,
  named/anonymous quota는 exit 1을 확인했다. 초기 한 줄 진단의 문법 오류는
  커널 지원 여부의 근거로 쓰지 않고 올바른 문법으로 재확인했다.
- 그러므로 이 Docker daemon의 필수 커널 기능 부재가 현재 차단 원인이다.
  모든 macOS/Docker 버전에 일반화하지 않는다. 컨테이너 userspace 설치·이미지
  재빌드·ZAP/Burp 키로 커널 옵션을 활성화할 수 있는 문제가 아니다.
- 규칙 설치 뒤에 network transition을 하는 기존 순서는 유지했다. quota를
  제거하거나 HTTP_PROXY만으로 보안을 대신하거나 `--privileged`로 바꾸지 않았다.
  실제 프록시 경유 허용·직접 우회 차단·종료 계량은 아직 입증하지 못했다.

운영자가 다른 승인된 daemon을 선택했을 때 재검사할 명령은
[gate 준비 안내](../../deploy/docker/gate-sidecar/README.md)에 기록했다.
단순 rule-check 통과와 실제 OPEN/계량·격리 proof 통과는 다르다.

### 근거가 있는 최소 보완

| 확인된 문제 | 보완과 이유 |
| --- | --- |
| 새 이미지가 기존 manifest와 달라 `shell_image_manifest_mismatch` | 기존 generator에 명시적 `--offline`만 추가. 실제 이미지 ID/설치 버전/실행 경로에서 새 임시 manifest를 생성하고 integration test의 선택 입력으로 전달. 이전 목록의 digest만 바꾸거나 검증을 완화하지 않음 |
| 로컬 목록 생성에도 외부 unsigned mirror index 조회 | offline 선택 시 조회하지 않고 component origin을 unknown으로 남김. 기본 온라인 동작은 보존. 실제 이미지 관측을 패키지 공급망 신뢰 인증으로 과장하지 않음 |
| gate 시험의 OPEN 실패가 내부 cleanup 전에 발생 | 가장 바깥 finally에서 이 시험 UUID의 container/mock을 먼저 정리한 다음 bridge 정리. 실패한 fixture 자원이 다음 global cap 시험을 막는 문제만 수정 |
| quota 오류가 일반 실행 성공으로 잘못 승격될 가능성을 검증할 필요 | 실제 SQLite 원장 + scripted Docker 실패의 INSTALL/TARGET 회귀 추가. 연결·RPC 미시작, 정리 시도, full charge·quarantine 유지와 새 window 거절을 검사. production gate/Control 로직 변경 없음 |

이미지의 실제 `docker image inspect .Id`(Linux arm64)는 다음과 같다. 빌드 로그의
config blob digest와 혼동하지 않으며, 컨테이너 실행/manifest 검증에는 이 ID를 썼다.

- shell: `sha256:f2da09753c1ebacdec135addb3f1a1df698bb656331f40ba17587b25a8965854`
- sidecar: `sha256:63ebd2980b661a066dc58aff5e0a0f642359487bfbf9fffd614261eef77b95a2`
- shell base: `sha256:ed99295a386abde2fb31e01a441b7c2800d9bcf19a20028b77d642c3ef068363`

기존 Dockerfile의 apt 서명 검증을 유지했다. 임시 manifest는 19개 설치 package/
427개 실행 경로를 관측했고 offline origin 19개는 미확인으로 남겼다.
이 저장소의 추적된 `program-manifest.json`은 덮어쓰지 않았다.
시험 이미지와 임시 manifest는 로컬 재검사 용도로 남겼다.

### 최종 검증과 남은 조건

- 관련 unit/합성 integration 13개 파일: **76 passed (2.25초)**. 이 중 실제
  Mobile 분석/동작 품질은 검증하지 않았다. 초기의 62/30/75건 집중 시험과
  겹치므로 합산하지 않는다.
- 실제 Docker shell/fixture 2개 파일: **34 passed / 1 skipped (46.02초)**.
  새 Kali 이미지의 5건과 pinned Python 이미지의 코드 fixture 4건을 포함한다.
  생략 1건은 별도 비교 이미지 `python:3.12-slim-bookworm` 부재이며 통과가 아니다.
- 수정 전 Kali 부분 시험은 **4 passed / 1 failed (5.07초)**로 manifest 불일치를
  확인했다. 실제 이미지에서 생성한 별도 파일로 대조한 뒤 5건 모두 통과했다.
  전체 Docker 중간 시험의 **29 passed / 1 failed / 5 skipped (43.16초)**는
  gate 실패 fixture의 잔존 자원으로 cap=1 기동 둘 다 거절된 상태와 fixture 이미지
  opt-in 누락을 포함한다. 정확한 시험 UUID의 container 2개와 bridge 1개만 정리했다.
  그 임시 데이터는 삭제됐고 이미지·manifest·원장·사용자 컨테이너는 보존했다.
- cleanup 수정 후 실제 gate 재시험: **1 passed / 4 failed (12.92초)**.
  4건은 여전히 동일 quota 커널 부재이며 숨기거나 skip으로 바꾸지 않았다.
  시험 종료 후 managed shell/gate container·gate bridge와 t005 mock 조회는 비었다.
  해당 임시 자원 정리 누락 때문에 다음 시험이 실패하는 경로는 해소했다.
- Python 범위 Ruff·compileall·`git diff --check` 검사. 이 단계에서 전체 4,000여
  건 회귀를 새로 돌렸다고 주장하지 않는다. 16절의 전체 검사 수는 이전 시점의 결과다.

이 변경은 빌드/검증 도구·실패 fixture 정리·안전성 회귀와 안내에 한정했다.
generic Agent loop/Graph/Control의 의미, 운영 정책, 실행 예산·허용 범위는 변경하지
않았다. M5 로컬 완료 판단은 유지하며, **M6 전체 완료는 계속 보류**다.
다음 실제 gate 검증은 지원 커널이 준비될 때 수행하고, Mobile/LLM 실환경 평가는
사용자 준비·명시적 허용 후에만 한다. 환경 조건을 기다리는 동안 미확인 성공이나
불필요한 새 실행 엔진을 구현하지 않는다.

## 18. Lima Linux VM의 실제 설치 프록시 검증 — 2026-10-11

사용자가 준비한 `whs-proxy` VM으로 기존 로컬 이미지 3개를 복사했고,
현재 checkout의 미커밋 변경을 포함한 소스로 별도 시험 복사본을 만들었다.
GitHub clone/pull, 브랜치 전환, commit/push/PR은 하지 않았다. Mac 원본과
기본 Docker context `desktop-linux`는 보존했다. VM 경로는 이번 시험 산출물
위치이지 일반 실행 코드의 고정 경로가 아니다.

### 환경과 변경 범위

- VM: Linux `7.0.0-34-generic`, `CONFIG_NFT_QUOTA=m`, 실제 로드된 `nft_quota`.
  Docker `29.9.0`, Python `3.14.4`. VM의 별도 venv에 `.[dev]`만 설치했다.
- 17절의 shell/sidecar 이미지 ID와 pinned Python 이미지
  `sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534`가
  VM에서도 일치했다. 이미지는 재빌드/원격 pull하지 않았다.
- VM 시험 폴더: `/var/tmp/whs-m6-proof.Kij3mf`.
  `.env`, 비밀키, 기존 Run 데이터, Mac `.venv`와 `.git`은 복사하지 않았다.
  gate/manager 및 설치·window 시험/fixture의 5개 접점 파일 SHA-256을
  Mac 원본과 VM에서 대조해 일치함을 확인했다.
- 기존 window 시험은 버전 관리되지 않는 `data/t005-gated/mock.py`와
  `prober.py`를 참조해 깨끗한 복사본에서 실행할 수 없었다. 이를 버전 관리되는
  `tests/integration/fixtures/gated_network.py` 하나로 교체했다. 주소/포트/로그
  위치는 시험이 인자로 제공하며, literal IP·횟수/크기 제한과 UDP 응답 확인을
  적용한다. 도메인 이름 조회나 send 성공만으로 전달 성공을 주장하지 않는다.
  quiet 시험도 필요한 출력 폴더를 직접 만든다. 이 변경은 시험 입력/준비에만
  한정하며 Agent, Graph, Control, gate 규칙과 운영 예산은 변경하지 않았다.

초기 archive에 존재하지 않는 data 파일을 지정한 문제는 명시적인 소스 선택과
pipefail로 정정했다. 최초 실행의 basetemp 부모 부재도 시험 폴더에서 정리했다.
unit 중간 결과 **62 passed / 1 failed**는 VM 복사본의 `examples/app.yaml` 누락이었고,
비밀정보 없는 해당 예제 파일을 복사한 뒤 아래 최종 결과로 재확인했다.
이 준비 실패를 제품 동작 실패나 성공으로 계산하지 않는다.

### 최종 실제 검증

| 검사 | 결과 | 실제로 확인한 범위 |
| --- | --- | --- |
| VM 격리 netns의 기존 ruleset `nft --check` | exit 0 | 필수 커널 규칙 지원. 전체 네트워크 안전성 인증은 아님 |
| 설치/window Docker integration 2개 파일 | **5 passed, 54.09초; skip 없음** | 합성 패키지 프록시 경유 설치, 직접 mirror 접속 차단, INSTALL 0 capacity 거절, quiet 종료, listener 없는 NFQUEUE 차단·DNS/IPv6 거절, live daemon의 허용 요청과 종료 정산 |
| gate 관련 unit 10개 파일 | **63 passed, 0.81초** | fixture 입력 제한·UDP 응답, cleanup, ruleset 오류 fail-closed, 원장·인자/정책·등록 조립. scripted 시험을 실환경 모델 평가로 세지 않음 |
| Mac의 새 fixture/cleanup unit | **8 passed, 0.23초** | VM 결과와 겹치므로 합산하지 않음 |
| 전체 src/tests/examples 및 manifest generator Ruff; diff 검사 | 통과 | 정적 검사. 전체 4천여 회귀는 이번에 재실행하지 않음 |

실제 원본 정산에는 quiet의 `released/closed_verified`(0 bytes/0 deliveries),
live window의 `released/closed_verified`(3 deliveries, 816 outbound bytes,
12 outbound packets)가 기록됐다. 3개 요청의 verdict는 모두 `committed`,
probe도 3개 모두 `OK`였다. sidecar를 고의로 잃은 별도 window는
`quarantined/unknown_fully_charged`로 예약 30 deliveries/20,000 bytes 전체를
유지하고 환급하지 않았다. unknown 복구를 강제 허용하지 않았다.

시험 종료 뒤 VM의 `docker ps -a`는 비었고 네트워크는 기본 `bridge/host/none`만
남았다. 시험이 만든 container/bridge만 finally로 정리했고 기존 사용자 자원을
삭제하거나 전역 prune하지 않았다. VM 시험 폴더·원장·이미지·결과는 보존했다.

최종 JUnit 원본 2개를 로컬
`runtime/whs-m6-linux-proof.htrQGa/gated-final.xml` 및
`runtime/whs-m6-linux-proof.htrQGa/gated-unit-final.xml`에 보존했다
(경로 기준: checkout 상위의 `new-chat` 작업공간). 운영 데이터가 아닌 합성 결과다.

Mac 터미널에서 동일 VM 복사본을 다시 시험할 명령:

```sh
limactl shell --workdir /var/tmp/whs-m6-proof.Kij3mf whs-proxy -- \
  ./.venv/bin/python -m pytest -q -rs \
  tests/integration/test_gated_install_docker.py \
  tests/integration/test_gated_window_docker.py
```

VM을 재부팅하면 실제 모듈/규칙 지원을 다시 점검한다. 이 VM에서 합성 설치 경로는
검증됐지만 CLI 운영 설정을 이 VM으로 자동 전환한 것은 아니다. 실제 mirror의
pin/포트·이미지 공급망·별도 INSTALL/TARGET 한도는 운영자 설정이 여전히 필요하다.
실제 Mobile은 미준비, LLM은 100회·0달러 조건으로 유료 호출 없음, 실제 대상
생성 코드 실행은 비활성이다. 따라서 **프록시 합성 검증 차단은 해소했지만 M6 전체
완료는 아니다**. 17절의 Docker Desktop 커널 실패는 당시 daemon의 사실로 보존한다.

## 19. 공개 준비와 Web 전용 후속 범위 — 2026-10-11

### 요청과 공개 대상

사용자가 누적 작업의 GitHub 공개, 동료용 설명, 이후 Mobile 제외를 요청했다.
원격 조회 결과 기존 `dev-jm`은 `8af7a6a`, 별도 `dev-jm-native-specialists`는
`3c24e28`이므로 기존 공개 브랜치에 정상 fast-forward로 반영한다. 강제 push,
`dev`/`dev-jm` 변경, PR 생성은 하지 않는다. 비밀 설정, Run 데이터, venv와
검증 XML은 공개 대상에서 제외한다. 공개 여부와 커밋은 실제 원격 참조를 기준으로 확인한다.

### 다음 동료가 읽어야 할 소유 모듈

| 접점 | 소유 모듈/문서 | 이번 누적 변경의 의미 |
| --- | --- | --- |
| 역할과 새 조사 | `app/agent_profile.py`, `app/specialist_run.py`, `config/agents/` | 기존 native 엔진 위에 역할·전문 판단·Ready 기동 조립 |
| 계획 채택 | `orchestration/planning_admission.py`, `planning_evidence.py`, `planning_graph.py` | 현재 관련 입력 CAS와 저장 영수증으로 확인된 writer 응답 유실 복구 |
| 실제 Graph 저장 | `attack_graph/stores/mutations.py`, `memory.py`, `sqlite.py`, `namespaced.py` | Graph 변경+영수증 원자 저장. 현재 값이 같다는 이유로 재적용하지 않음 |
| 만료와 후속 작업 | `orchestration/evidence_validity.py`, 기존 `instance_driver.py` | 활성 cohort의 관측된 유효성 변경만 기존 driver에서 처리 |
| checkpoint 부재 원본 | `orchestration/execution_collection.py`, `app/cli/commands/collect_originals.py` | 기존 원본만 보존. 전체 상태 재구성·미완료 claim 인계·재실행 허가는 아님 |
| 코드 fixture | `core/ports/code_fixtures.py`, `capability/code_fixtures.py`, `orchestration/code_fixture_validation.py` | 명시적 정책을 선택한 offline JSON 시험. 대상 증거나 취약점 성공으로 인정하지 않음 |
| shell 안전성 | `capability/shell_workroom.py`, shell/gate Docker 시험 | supervisor 생존·신뢰 원본·자식 정리 및 unknown full-charge 유지 |

### Mobile을 제외하는 방식과 수정 이유

새 `config/agents/native-specialists-web.yaml`은 대화·플래너·분해·Web 실행·연구/증거·
코드 작성의 **여섯 논리 역할**을 같은 기존 loader/MD/Task/InstanceDriver로 조립한다.
Web만 schedulable하며 Web 인스턴스 수는 기존 설정 한도를 따른다. 기존 전체
프로필과 Mobile 구현·회귀는 보존한다. 새 scheduler, 프로필 상속 framework,
중앙 Mobile 이름 비교, Finance Lab 전용 행동열을 추가하지 않았다.

수정 이유는 Mobile이 아직 준비되지 않았기 때문이라는 추측이 아니라 **이번 사용자
범위 변경**이다. Mobile 기기/앱 실행은 이후 작업 및 현재 완료 차단 조건에서 제외한다.
프로필 선택은 MCP 서버 목록 필터가 아니므로 운영자 설정의 `capability.mcp_servers`/
`REDTEAM_MCP_SERVERS`도 필요한 승인 공급자만 선택한다. 기존 Run의 fingerprint와
다르므로 새 Run에서 선택하며, 이전 checkpoint의 역할을 강제로 바꾸지 않는다.

실제 public `build_application`/`run_engagement`의 scripted closed-loop 시험을
전체·Web 전용 프로필 둘 다 통과하도록 확장했다. Web 전용 registry에 Mobile 역할이
없고 동일 완료 조건·후속 두 Task·공통 LLM 원장이 유지됨을 검사한다. 역할 수를
제품 코드의 조건으로 고정하지 않는다. Mac 관련 profile/closed-loop **16 passed**.

### 공개 전 발견한 Python 3.14 실패와 좁은 수정

Linux VM에서 전체 회귀를 처음 실행했을 때 **204 failed / 4,166 passed /
62 skipped**였다. 이 결과를 통과로 표시하거나 감추지 않는다. 원인을 분리했다.

- Python 3.12 이상 runtime Protocol의 정적 속성 검사가 `PlanningGraphStore`와
  `__getattr__` 위임 writer의 실제 기능을 보지 못했다. 보고서/관측은
  `StoreCapabilityError`로 실패하고 원자 영수증 writer는 구형 경로로 분류됐다.
  Python 3.11에서는 통과하므로 기존 Mac 검사만으로 놓친 실제 호환성 오류다.
- Graph 소유 `stores/base.py`의 세 optional-port 검사만 **필수 메서드 전체의
  callable 검사**로 바꿨다. 반환값은 원래 wrapper이며 inner로 우회하지 않는다.
  빠진/비호출 가능 메서드는 계속 거절하고 영수증 내용·CAS·권한 검증은 유지한다.
  역할/제품/대상별 분기나 그래프 엔진 교체가 아니다.
- Linux의 기본 파일 생성 권한 때문에 합성 Workspace/VDB 입력도 private-file
  정책에서 거절됐다. 제품 정책을 낮추지 않고 시험 실행과 CI pytest step에
  `umask 077`을 적용했다. 이미 노출된 파일의 권한을 자동 수리하는 코드가 아니며
  runtime 허용 대상/경로를 늘리지 않는다.

동적 위임의 실제 callable 전체 검사·부분 기능 거절·wrapper 보존·보고서 경계
회귀를 추가했다. Mac Graph/복구 관련 **95 passed**, Linux Graph/복구 **81 passed**,
Linux private Workspace/유효성 wakeup/두 프로필 closed-loop **69 passed**를 확인했다.
검사 집합이 겹치므로 총합으로 합산하지 않는다. 중간에 존재하지 않는 시험 파일명을
지정한 준비 명령은 시험이 실행되지 않았으며 유효한 결과로 계산하지 않았다.

### 최종 공개 전 검증과 재현

| 검사 | 관측 결과 | 구분 |
| --- | --- | --- |
| 수정 후 Linux Python 3.14 전체 pytest, 제한 이미지 fixture 포함 | **4,379 passed / 1 failed / 62 skipped**, 294.00초 | 실패 1건은 시험 복사본에 `.env.example` 누락. 제품 코드 오류가 아닌 준비 오류 |
| 변경 없는 Git 추적 `.env.example` 복사 후 `pytest --lf` | **1 passed**, 0.51초 | 나머지를 다시 실행하거나 실패를 삭제하지 않고 누락 사례만 재확인 |
| 최종 소스의 통과 사례 집합 | **4,380개 통과 사례, 62개 미실시** | 단일 명령의 “4380 passed” 출력으로 꾸미지 않음. 겹치는 focused 검사 수를 더하지 않음 |
| Mac 전체 src/tests mypy | **743 files, no issues** | Python 3.10 대상 정적 검사이지 3.10 실제 실행은 아님 |
| src/tests/examples, Docker 예제/manifest generator Ruff; diff 검사 | 통과 | 이 과정에서 Control/ROE 완화 없음 |
| CI YAML 및 Compose 구성 두 파일 | 통과 | 구성 구문 검사. preview push는 dev 전용 CI를 자동 기동하지 않음 |

최종 전체 집합에는 실제 gate 5건, network-none code fixture, shell Docker와
localhost MCP 시험이 포함된다. 62개 미실시는 외부 provider/LLM/VDB 입력,
별도 실행 opt-in/추가 이미지 등 사전 조건 부재이며 성공으로 계산하지 않는다.
Mobile live 시험도 실행하지 않았다. 수정 전 XML/수정 후 전체 XML/마지막 실패
재확인 XML을 VM `proof-results/publish-final.xml`, `publish-fixed.xml`,
`publish-lastfailed.xml`에 보존한다. 로컬 원본 보존 위치는
`runtime/whs-m6-linux-proof.htrQGa/`이며 Git 공개 대상은 아니다.

저장소 전체가 있는 준비된 Linux Docker 환경의 재현 명령:

```sh
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
umask 077
# 이미 존재하는 승인된 workroom 이미지의 실제 불변 image ID만 지정한다.
# 환경에 이미지가 없으면 fixture 검사는 skip이며 설치 성공이 아니다.
export REDTEAM_CODE_FIXTURE_TEST_IMAGE="$(docker image inspect --format '{{.Id}}' shell-workroom:t003-draft)"
./.venv/bin/python -m pytest -q -rs --tb=short
```

시험 VM의 container 목록은 비었고 network는 `bridge/host/none`만 남은 것을
확인했다. 전역 Docker context·Mac CLI 설정·다른 checkout·사용자 컨테이너는
변경하지 않았다. 재현에 실제 LLM 키/금융 계정이 필요하지 않으며 새 유료 호출은 없다.

### 현재 남은 경계

실제 Web LLM 비교는 100회·0달러 조건을 유지해 유료 호출하지 않는다. 운영 mirror
pin·신뢰 이미지·INSTALL/TARGET 정책은 실제 운영자 결정/검증이 남는다. 입력/원본이
불명확한 전체 상태 복구와 미완료 claim 인계는 기존 held를 유지한다. 생성 코드의 실제
대상 실행은 규격 미확정·비활성이며 무제한 요청 mode로 이 제한을 우회하지 않는다.
Mobile은 이번 후속 작업에서 제외다. 안전한 보류를 임의로 완료 처리하지 않는다.
