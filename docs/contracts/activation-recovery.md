# 전문 워커 기동 예약·중단 복구 계약

기록일: 2026-10-10. 로컬 `codex/dev-first-specialists`, 기준 dev `153265e`.
PRD R3-A와 M5의 **기동 예약 → 실행 시작 → checkpoint** 사이 연결을 보완한다.
별도 작업 큐·실행기·claim TTL을 추가하지 않는다.

## 1. 수정 이유

기존 CLI는 checkpoint가 있는 instance만 전체 cohort로 열거했다. 하지만
Task claim과 initial Runtime을 저장한 직후, 첫 checkpoint 전에 프로세스가
종료될 수 있다. 이 경우 누락된 워커를 무시한 채 형제만 재개하면 전체
사용량·완료 범위를 확인했다는 판단이 틀릴 수 있다.

수정 전 합성 회귀 시험에서 이미 진입한 것으로 표시된 누락 워커를 두고도
재개가 성공하는 것을 확인했다. 현재는 실행 시작 가능성을 부정할 수 없는
누락 워커가 있으면 **모델·도구·Control 계량 복원 전에 전체 재개를 거절**한다.

## 2. 같은 기록, 두 가지 진입 상태

기존 coordinator의 `task_activation` row에 다음 연결 정보만 추가한다.

- `launch_proof_version=1`, `launch_stage=reserved`.
- 원래 initial Runtime의 digest. 같은 run/task/instance/role/시작 시각/정책을 유지한다.
- 목표 관련 입력 hash, operator generation, 조립 입력 hash, Task 관련 입력 hash.
- Task 완료 조건 hash와 occupant는 기존 계약/claim을 계속 사용한다.

claim과 예약 기록은 같은 change에서 저장한다. `AgentLoopRunner`는 전문
instance의 graph 진입 **전에** `launch_stage=entered`를 저장한다. 이후 graph
구성 실패·취소·프로세스 종료가 발생해도 marker를 자동으로 되돌리지 않는다.
marker는 실행 허가가 아니라 “진입했을 수 있음”을 나타내는 기록이다.

| 저장 상태 | checkpoint 없음 | checkpoint 있음 |
| --- | --- | --- |
| version 1 reserved + 현재 증명 통과 | 전체 재개에서 같은 원래 instance로 시작 가능 | 목록 불일치로 거절; 기존 checkpoint 경로 필요 |
| entered / held / returned | 사용량·효과를 증명할 수 없으므로 거절 | 기존 checkpoint·영수증 reconcile·승인 경로 |
| 구형 또는 불완전 기록 | 미실행으로 추측하지 않고 거절 | 기존 검증된 재개 경로 유지 |

구형 기록에 reserved 증명을 자동으로 만들어 넣지 않는다. 도구 또는 모델을
호출한 후 기록/claim/checkpoint를 지워 새 워커로 대체하지 않는다.

## 3. 미시작 증명의 검증

`TaskActivation.unstarted`는 canonical 예약 전체와 checkpoint 목록을 대조한다.
없는 워커를 반환하려면 다음이 모두 맞아야 한다.

1. version·reserved·launched 상태와 initial Runtime digest가 일치한다.
2. Run/engagement/Mission binding이 같고 ACTIVE이며, 미처리 변경 지시가 없다.
3. 목표·operator generation·조립 입력·Task 입력이 그대로다.
4. Task가 RUNNING이고 원래 occupant/실행 역할/완료 계약이 일치한다.
5. 해당 실행 역할이 등록·schedulable이고, 의존 Task가 계속 완료 상태다.
6. 해당 owner의 LLM 원장 행이 없다. 전송 전 환불된 행도 “미진입”과 상충한다.

`InstanceDriver.recover_unstarted_activations`는 먼저 기존 Run 실행 소유권을
확보한다. 외부 port 조회는 DB transaction 밖에서 한다.

- 원래 instance의 checkpoint가 **정말 없는지** saver의 정확한 thread를 검사한다.
- 같은 Run의 Work와 instance metadata가 연결된 Evidence가 없는지 검사한다.
- 현재 선택 component·요청 정책·전문 profile/scope 입력이 원래 값과 일치하는지 검사한다.
- 조회 후 canonical 예약을 다시 대조한다. 도중 entry/목표/claim이 바뀌면 거절한다.

조회 실패·읽을 수 없는 데이터·비활성 checkpoint를 “없음”으로 취급하지 않는다.
진단에는 고정 실패 이유를 사용하며 prompt·원본 비밀값을 출력하지 않는다.
이는 같은 조립/원장/로컬 Run 소유권을 사용하는 writer의 보장이다.
wrapper를 우회한 외부 조작과 여러 호스트의 분산 복구를 보장하지 않는다.

## 4. 재개와 전체 완료

기존 CLI cohort 재개에서, 누락 예약도 검증된 initial Runtime으로 전체 계량에
포함한다. 요청/도구 사용량은 미진입이 증명된 경우에만 0이고, 시작 시각은
원래 공유 clock이므로 경과 시간 제한을 초기화하지 않는다. 공통 LLM 원장은
그대로 유지하며, 다른 담당의 사용량이나 unknown을 환불하지 않는다.

전체 선택이고 unknown 형제가 없을 때만 기존 `InstanceDriver`에 미시작
원래 Runtime을 전달한다. checkpoint 재개와 함께 같은 runner/checkpointer/
취소 수명으로 처리한다. 새로운 instance·Task claim·activation allowance를
만들지 않는다. 실행 시작 직전에도 예약 증명을 재검사한다.

명시적 `--instance` 부분 재개는 누락 예약을 시작하지 않는다. 다른 형제를
무시하고 후속 계획·새 워커·전체 완료로 넘어가지 않는다. 승인 대기는 기존
checkpoint의 같은 action/approval을 사용하고 Control을 우회하지 않는다.

전문 lifecycle의 quiescent 전환은 미완성 activation과 미전달 outbox가
없을 때만 가능하다. 목표 완료도 모델 검토 전과 **Mission 완료 commit의 같은
transaction 안**에서 다시 검사한다. 늦게 생긴 예약/이벤트가 전체 완료에
누락되지 않도록 하며, 이 검사 자체는 모델 호출이나 강제 정리를 하지 않는다.

## 5. 검증과 남은 범위

신규 두 파일의 로컬 합성 시험 55건:

- Memory/SQLite의 동일 instance·claim·clock·공통 원장 보존.
- 별도 PID의 예약 직후/진입 직후 강제 종료와 SQLite 재연결.
- 바뀐 목적/역할/범위 입력/claim, 구형 기록, 미처리 stop, owner 요청의 거절.
- 실제 native 첫 모델 호출 전에 entered commit 확인.
- 전체/부분 cohort, Work/Evidence/checkpoint 상충, 읽기 실패, 조회 중 진입 경합.
- 미전달 outbox/예약의 quiescence 차단과 완료 검토 도중 변경의 commit 거절.

이 시험은 유료 모델·실제 금융 사이트·기기·생성 코드 실행을 사용하지 않는다.
외부 요청 성공이나 침투 성능을 입증하는 시험이 아니다.

### 5.1 추가 연결 — checkpoint가 하나도 없는 Run

전체 `resume`에서 checkpoint 열거가 정상적으로 빈 목록을 반환하면,
설정된 기존 SQLite Task/coordination 저장소를 읽기 전용으로 조회한다.
`inspect_sqlite_state`는 `mode=ro`와 하나의 read transaction을 사용한다.
새 canonical DB·부모 디렉터리·테이블을 만들거나 migration/claim/budget를 변경하지 않는다.
구형 저장소에 예약 기록이 없으면 기존 missing-checkpoint 거절을 유지한다.
읽기 실패·부분 schema·미지원 저장 버전을 빈 결과로 바꾸지 않는다.

`TaskActivation.bootstrap_identity`는 **해당 Run의 모든 예약**에 기존 미진입
증명 검사를 적용한다. engagement/Mission/component/요청 정책이 같은 경우에만
조립용 identity를 반환한다. 기록의 index와 Run/Task 귀속도 검증하므로, 잘못된
payload가 다른 Run인 것처럼 보이는 누락을 허용하지 않는다. 이 정보는 checkpoint
thread가 아니며, 같은 runner의 replay 허가로 해석하지 않는다.

등록된 component 선택·현재 MD fingerprint·요청 정책을 확인하고 기존
`build_resume_application → InstanceDriver → _drive_cohort_guarded`로 전달한다.
실행 소유권을 얻은 driver가 checkpoint/Work/Evidence 부재, 현재 scope/정책/
역할 selector와 canonical 예약을 다시 검증한다. identity 조회 후 entered 상태로
바뀌거나 다른 실행자가 소유 중이면 dispatch와 Control 계량 복원을 거절한다.
성공한 복구는 원래 instance/claim/Run 원장/clock으로 시작하고, 이후에는 생성된
checkpoint와 원래 action/approval로 재개한다. 새 초기 계획을 만들지 않는다.

`--instance`는 계속 **실제로 저장된 checkpoint thread**의 부분 선택이다.
checkpoint 없는 예약을 하나만 골라 실행하는 새 의미를 추가하지 않았다.
모든 예약이 미진입임을 증명한 전체 Run 복구만 제공한다.

조립 입력 projection v2는 `Engagement.created_at`만 비교에서 제외한다.
CLI 조립 때 새로 배정되는 시각이므로 scope/policy 변경이 아니며, 이 값을 포함하면
동일 설정으로 새 프로세스에서 복구해도 거절된다. engagement ID와 실제 범위·ROE·
승인/실행 정책·component·전문 profile은 계속 비교하고, 시간 예산은 기존 Run의
`started_at`으로 복원한다. **구형 projection의 hash를 자동 변환하지 않는다.**
구형 hash와 맞지 않으면 `activation_configuration_changed_new_run_required`로
거절하며 기존 자료를 보존한다. 새 Run이 필요하다.

추가 로컬 합성 시험 두 파일 **28건**은 실제 CLI 재조립·같은 ID 승인 대기/재개,
범위/selector/정책 변경, 실행 소유권 경합, 조회 이후 entry 변경, 혼합/구형/잘못된
예약, 읽기 전용 snapshot과 저장소 실패를 확인한다. 유료 모델·외부 대상은 사용하지 않는다.

### 5.2 읽기 전용 기동·호출 원장 진단

기존 `status`에 명시적 `--activation-db`를 추가했다. 지정한 **기존 Task/coordination
SQLite DB**에서 `read_activation_diagnostics`를 호출하며, 일반 `status`의 보고서
표시는 변경하지 않는다. LLM 공급자·engagement 재조립·MCP·checkpoint saver를
열지 않는다. 기존 읽기 전용 composite snapshot을 사용하여 schema migration,
부모 디렉터리 생성, claim·예산·activation 수정이 없다.

- 모든 기동 index/Runtime identity/digest를 검증한다. 깨진 형제를 빈 목록으로 숨기지 않는다.
- reserved 기록은 기존 canonical 미진입 증명 검사를 공유한다. 현재 역할·설정·외부
  저장소를 검사한 것은 아니므로 `reserved_proof_consistent`도 실행 허가가 아니다.
- entered는 원래 checkpoint/영수증이 필요하다고 표시한다. 요청 행이 0개여도
  미실행으로 바꾸지 않는다. held/returned와 Task 현재 claim도 별도로 표시한다.
- 기존 RunRequestLedger가 같은 snapshot의 `reserved/dispatched/settled/unknown/released`
  수를 Run/owner별로 제공한다. 상태가 깨지면 조회 실패이며 unknown이나 reserved를
  환불하거나, 시간 경과만으로 과거 요청을 정산하지 않는다.
- JSON은 `schema_version=1`, `replay_permitted=false`, `external_stores=not_inspected`를
  명시한다. 성공 exit code는 **조회 성공**이지 재개·완료·해제 승인이나 whole-Run
  실행 상태 검증이 아니다. 실제 Work/원본 Evidence/외부 provider 조회는 하지 않는다.
- Runtime 본문·Task 목적·인자·응답·비밀값을 출력하지 않는다. 저장소 오류는 클래스만
  출력한다. 전체 Run 소유권을 잡아 실행자를 중단시키지 않으며 시점별 read snapshot이다.

```sh
./.venv/bin/python -m redteam.app.cli status RUN_ID --activation-db data/orchestration/task_graph.sqlite3 --json
```

`RUN_ID`는 실제 값으로 바꾸고 DB 경로도 원래 실행의 설정을 사용한다. 위 경로는
기존 durable 설정의 기본값 예시일 뿐 다른 DB/checkout을 자동 탐색하지 않는다.
읽을 수 없거나 기록이 없으면 unknown/실패로 표시하고 새 DB를 만들지 않는다.

이 연결은 canonical 진단의 첫 부분이다. Work·저장된 원본 영수증의 추가 조회는
아래 5.3절에서 제공한다. provider 확인·명시적 해제, interrupted writer/orphan 정리,
fixture 실행 격리와 실제 Mobile/LLM 검증은 남아 있다. 이러한 보류를 해소하려고
marker를 지우거나 Control을 완화하지 않는다.

### 5.3 추가 연결 — Work와 저장된 원본 실행 교차 진단

2026-10-11 추가. 아래 세 경로를 모두 명시할 때만 확장 진단을 선택한다.
일반 `status`와 `--activation-db` 단독의 기존 사용법은 유지한다.

```sh
./.venv/bin/python -m redteam.app.cli status RUN_ID \
  --activation-db data/orchestration/task_graph.sqlite3 \
  --work-db data/orchestration/work.sqlite3 \
  --evidence-dir data/evidence --json
```

위는 기본 설정 경로의 예시다. 반드시 **원래 Run에서 사용한 저장소 경로**를 넣는다.
다른 checkout·최근 실행 저장소를 자동 탐색하지 않는다. 두 추가 플래그가 일부만
있거나 `--activation-db` 없이 지정하면 설정 오류(exit 2)로 거절한다.

읽기 책임과 수정 이유:

- `SQLiteWorkStore.inspect_run`은 `mode=ro`, `query_only`, read transaction으로
  기존 행을 읽는다. `get/list_items`의 setup/migration을 진단에서 호출하지 않는다.
  물리 Work index와 typed payload를 필터 **전에** 대조하여 잘못된 run/instance/status
  index가 대상 행을 숨기지 않게 한다. 구형·귀속 불명 Work는 자동 이행하지 않는다.
- `FilesystemEvidenceStore.inspect_run`은 constructor의 mkdir 없이 기존 Run 파일과
  index를 비교한다. 기존 `verify_run_integrity`와 같은 소유 모듈의 검사 함수를
  공유하고, 중복 index도 거절한다. 손상·누락·잘린 append를 성공한 빈 조회로
  바꾸지 않는다. 이 검사 자체는 index의 인증/서명을 제공하지 않는다.
- detached Evidence snapshot은 기존 EvidenceStore port의 읽기만 제공한다.
  조회 결과를 바꿔도 snapshot 원본은 바뀌지 않고 append는 거절한다.
  별도 영속 원장·작업 큐·실행기가 아니다.
- `inspect_execution_diagnostics`는 기존 `checked_receipt/preserved_result/
  execution_processed`를 재사용한다. submission과 원본·Work의 Run/instance/작업
  연결, 후보·역할·Task 연결을 대조하고, 없는 submission과 상충 원본도 표시한다.
  선택된 activation의 engagement/역할/Task/entered 연결은 별도 boolean이다.

| state | 확인한 내용 | 해석하면 안 되는 내용 |
| --- | --- | --- |
| `unknown` | 저장소 불명 또는 영수증 없는 Work | 미실행·자동 재요청 가능 |
| `missing_original` | submission은 있으나 원본이 없음 | 실제 대상 요청이 실패함 |
| `uncertain` | 원본 envelope가 결과 불명으로 표시됨 | 실제 성공/실패 확정·예산 환불 |
| `no_entry` | 검증한 원본의 실행 미진입 marker | Task 완료·모든 워커 미실행 |
| `unprocessed` | 원본은 있으나 처리 seal이 없음 | 원본 대상 도구 재호출 |
| `processed` | 원본과 연결된 결과 Evidence/처리 seal 확인 | 취약점 확정·목표 완료·claim 해제 |
| `invalid` | digest/schema/귀속/원본 상충 등 검사 실패 | 오류 기록 무시·새 워커로 대체 |

Work 상태 일치는 `work_result_matches`, 미정산 자원 marker는
`resource_execution_pending`으로 별도 출력한다. 처리 seal이 있다고 Work의 상충
상태나 자원 점유를 숨기지 않는다. timeout envelope의 `result_success`는 null이며
실제 성공/실패를 추측하지 않는다. 진단은 이 값을 수정하거나 결과를 재처리하지 않는다.

확장 JSON은 `schema_version=2` envelope의 `activation`/`execution`에 각각 검증된
진단을 담는다. `--activation-db` 단독 JSON은 version 1을 유지한다. 기동 진단에는
engagement ID를 추가했지만 저장된 Runtime/Task/실행 계약을 이행하거나 재작성하지 않는다.
`replay_permitted=false`는 두 모드 모두 유지한다.

각 저장소는 독립적으로 읽으므로 `cross_store_atomic=false`다.
외부 조회 전후 **기동·요청 표시 투영**이 달라지면 `activation_projection_changed=true`다.
false가 전체 canonical state 불변·writer 부재·CAS 통과를 증명하지는 않는다.
checkpoint/provider 조회·Run 실행 소유권 확보·해제 proof는 이번 기능에 없다.

exit 0은 읽기/분류 성공이지 복구 승인이다. 원본 부재/unknown/invalid는 결과 안에
남기고, 저장소 손상·불완전 schema·조회 오류는 클래스만 stderr에 표시하여 exit 1로
종료한다. 모델·MCP 호출, Graph 적용, claim 해제, 예산 정산·환불은 하지 않는다.

비용 없는 로컬 검증:

```sh
./.venv/bin/pytest -q tests/integration/test_execution_diagnostics.py
```

실제 native 루프가 남긴 합성 journal의 publication loss·미진입·timeout·원본
회수 후 처리, 실제 전문 플래너/분해/워커/검토/CLI 연결, 누락·상충·손상·구형
저장소·경로 탈출·시점 변경과 원본 무변경을 검사한다. scripted 모델과 합성 도구를
사용한 시험이며 실제 금융권 공격 성능 검증은 아니다.
