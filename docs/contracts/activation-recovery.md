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
| entered / held / returned | 자동 재개 거절. 아래 5.6절 원본 수집 API는 재개 허가가 아님 | 기존 checkpoint·영수증 reconcile·승인 경로 |
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
실제 Mobile/LLM 검증은 별도 조건이 필요하다. fixture 실행 격리는 이후
[M5 offline fixture 계약](code-fixtures.md)으로 선택 연결했다. 이러한 보류를 해소하려고
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

### 5.4 불변 증거 append의 응답 유실 복구

2026-10-11 추가. **5.2~5.3의 `status`는 계속 읽기 전용**이다. 이 기능은 기존
전문 모드의 `resume`/승인 cohort 재개에서 선택된 실제 EvidenceStore를 사용한다.
별도 DB·작업 큐·runner·복구용 대상 도구를 만들지 않았다.

발생 원인: `PlanningEvidenceStore.append`는 외부 저장 전에 관련 참조를 fenced 상태로
만든다. 파일과 index 저장 후 응답만 유실되면 원본은 있지만 fence가 계속 남는다.
같은 Evidence ID를 다시 append하면 멱등성 검사에서 반환되므로 이를 해결하지 못한다.
그렇다고 fence가 오래됐다는 이유로 없애면 미저장·부분 저장까지 성공으로 오인한다.

새 쓰기의 계약:

1. 같은 coordinator transaction에서 참조 버전을 증가시키고 writer와 versioned
   `ReferenceUpdate`를 저장한다. 증거 wrapper가 제공하는 기대 원본 ID와 **전체
   EvidenceRecord의 hash**만 담는다. 본문·prompt·비밀값은 이 원장에 복제하지 않는다.
2. writer별 전체 참조/버전 목록을 보존한다. 일반 저장 완료도 정확한 전체 목록을
   확인해 한 transaction에서 모두 해제하고 intent를 `confirmed`로 표시한다.
   일부 참조만 해제하거나 이전 writer의 늦은 완료로 새 writer를 해제하지 않는다.
3. 재개 driver가 기존 Run 실행 소유권을 얻고, 실행 actor가 없을 때 wrapper의
   `reconcile_pending_writes`를 호출한다. 복구 중 새 actor 시작은 거절하고 driver
   종료도 복구가 끝나기 전에 Run 소유권을 반납하지 않는다.
4. wrapper는 기존 `verify_run_integrity`와 원본 `get`을 사용한다. 파일/index 무결성,
   원본 ID·Run·전체 hash, 증거 종류에 맞는 정확한 참조 목록이 모두 일치해야 한다.
   조회와 finish는 별도 저장소 transaction이므로 finish에서 기존 intent·writer·버전을
   다시 CAS 검사한다. reference 버전은 낮추거나 초기화하지 않는다.

**복구되는 것은 ‘이 증거 append가 저장됐음’이라는 확인 상태뿐이다.** 원본 Evidence,
Task/occupant/완료 조건, activation, Work, 실행 자원 marker, pending approval,
unknown/사용 예산은 수정하지 않는다. 이전 계획은 계속 stale이며 새 판단이 필요하다.
실패·unknown 도구 원본이 저장됐다는 사실은 대상 실행 성공이나 취약점 확인이 아니다.
기존 원본 영수증 처리와 Control/accounting 게이트는 이 확인 이후에도 그대로 적용된다.

원본 없음/변경, 무결성 오류, 무결성 검사 기능 없음, 읽기 실패, intent/CAS 변경,
지원하지 않는 proof kind, 구형 proof 부재는 `held`다. 깨진 journal index·누락/일부만
남은 fence를 성공한 빈 목록으로 숨기지 않는다. graph는 mutable store이므로 현재 값이
같다는 이유로 append proof를 적용하지 않는다. **구형 Graph/proof 부재 writer는 자동 복구하지
않는다.** 새 Graph 저장 영수증의 별도 확인은 5.5절을 따른다.
확인한 증거 fence만 먼저 정리됐어도 남은 held writer가 있으면 전문 cohort 재개는
checkpoint/provider 처리·모델/도구 시작·Control 계량 복원 전에 거절한다.

이 보장은 같은 coordinator·wrapper·로컬 Run 소유권을 쓰는 writer에 한정된다.
파일 index의 암호학적 인증, 임의 외부 파일/DB 조작의 차단, 여러 호스트의 분산 복구를
제공하지 않는다. EvidenceRecord 자체의 저장 확인이며 `artifact_ref`가 가리키는
외부 artifact의 내용·현재 유효성 검증은 아니다. 구형 Run의 기대 원본을 현재 파일에서
역산하거나 자동 이행하지 않는다.
관련 journal v1이 없는 interrupted writer는 자료를 보존하고 후속 검증이 필요하다.

비용 없는 재현:

```sh
./.venv/bin/pytest -q tests/unit/test_planning_write_recovery.py tests/integration/test_planning_write_resume.py
```

Memory/SQLite, 별도 프로세스의 파일 저장 직후 종료, 취소/응답 유실, 원본·index·Task·
승인 ID·공유 원장 보존, 원본 불명/손상/구형/Graph 보류, CAS 경합, 소유권 경쟁 및
driver 종료 경합을 시험한다. 실제 LLM/금융 사이트/기기/생성 코드는 사용하지 않는다.

### 5.5 Graph 변경의 저장 완료 응답 유실 복구

2026-10-11 로컬 추가. 기존 specialist `resume` 접점과 coordinator writer intent를
재사용한다. 현재 Graph 값이 기대 값과 같다는 사실은 확인 근거로 사용하지 않는다.
같은 값이 변경 전부터 있었을 수 있고, 뒤에 다른 정상 변경이 생길 수도 있기 때문이다.

저장소 소유의 선택적 `ReceiptedAttackGraphStore` 계약:

1. `prepare_graph_mutation`으로 Run·고유 mutation ID·작업 종류·분리된 입력 snapshot·
   참조 목록을 확정한다. engagement wrapper가 **이 단계에서만** 물리 ID와 namespace를
   변환한다. 소비자는 provider 비공개 DB/메모리 구조에 접근하지 않는다.
2. `PlanningGraphStore`가 준비된 요청의 hash/ID를 `graph_mutation_v1` proof로 기존
   reference fence와 같은 coordinator commit에 남긴다. 전체 node/edge 본문은 이 원장에
   복제하지 않는다. Run·작업·참조 목록이 preparation 중 바뀌면 저장 전에 거절한다.
3. provider의 `apply_graph_mutation`은 Graph 변경과 불변 receipt를 함께 저장한다.
   SQLite는 기존 Graph DB 안의 `graph_mutation_receipts`와 Graph rows를 같은
   `BEGIN IMMEDIATE` transaction에 commit한다. receipt 저장 실패 시 변경도 rollback한다.
   동일 ID의 같은 요청은 최초 receipt를 반환하고 **현재 Graph에 다시 적용하지 않는다**.
   같은 ID의 다른 요청은 거절한다. Memory는 같은 객체/이벤트 루프의 원자적 변경만
   제공하며 프로세스 재시작 내구성을 제공하지 않는다.
4. 재개 시 owning wrapper가 `read_graph_mutation_receipt`만 호출한다. SQLite 조회는
   `mode=ro`이고 setup/파일 생성/schema migration/Graph 쓰기를 하지 않는다. 원본 receipt의
   schema·물리 index·Run·mutation ID·namespace·기대 hash·참조 목록을 검사하고,
   coordinator의 전체 intent/writer/version을 CAS로 다시 검사하여 확인 상태만 정리한다.

SQLite provider의 원본 ID 키는 `(namespace, run_id, mutation_id)`다. 별도 engagement의
receipt나 다른 Run의 receipt로 원래 writer를 풀지 않는다. 기존 일반 Graph port/스냅샷
형식은 유지한다. 새 table은 정상 provider setup에서 추가되지만, 구형 writer의 proof를
소급 생성하거나 기존 Graph에서 원래 요청을 추측하지 않는다. receipt에는 hash·ID·종류·
참조·저장 반환값만 있고 Graph 본문을 중복 보관하지 않는다.

현재 Graph 값이 원본과 달라져도 정확한 원본 receipt가 있으면 **그 변경이 저장됐었다는
확인**은 가능하다. 삭제의 `False`도 요청이 저장소에 처리된 noop receipt이지 삭제 성공
주장이 아니다. 이 확인은 ToolResult의 성공, Evidence 유효성, Finding, Task/목표 완료를
뜻하지 않는다. 기존 작업/승인 ID와 claim·자원 marker·예산은 그대로 유지한다.

receipt 부재/읽기 오류/손상/상충, 저장소 기능 부재, CAS 변경은 `held`다. 누락 DB를
빈 성공 조회로 처리하거나 새 DB로 대체하지 않는다. 구형 proof 부재 writer, 지원하지
않는 custom provider, 구형 물리 edge 삭제(두 ID가 관여할 수 있음)는 자동 복구하지
않는다. 일반 쓰기/구형 edge 삭제 동작은 유지하고 미확정 fence를 보존한다.

driver는 Evidence와 Graph wrapper에 각자 지원하는 proof만 검증하도록 요청한다.
이후 원래 coordinator inventory에 남은 writer가 하나라도 있거나 검증 결과가 held이면
checkpoint/provider 복구·Control 계량 복원·모델/도구 기동 전에 거절한다. 한 wrapper가
다른 저장소의 proof를 해석하거나 새로운 실행 엔진/Task 큐를 만드는 구조가 아니다.
같은 coordinator·wrapper·로컬 Run 소유권을 사용하는 경계에 한정되며, 임의 DB 조작의
암호학적 탐지나 다중 호스트 분산 복구를 보장하지 않는다.

비용 없는 검증:

```sh
./.venv/bin/pytest -q tests/unit/test_graph_mutation_receipts.py tests/unit/test_planning_graph_recovery.py tests/integration/test_planning_write_resume.py
```

실제 SQLite transaction/reopen/별도 PID의 commit 직후 종료, Memory receipt,
오류/취소/rollback, 이후 Graph 변경과 중복 요청 무재적용, 귀속/index/CAS 상충,
원본 불명 보류와 기존 전문 승인 cohort 연결을 검사한다. LLM과 대상 도구는 명시적
scripted/합성 fixture이며 실제 금융권·Mobile·생성 코드 검증이 아니다.

### 5.6 entered + checkpoint 부재의 명시적 원본 수집

`InstanceDriver.collect_checkpointless_originals(run_id, checkpoint_instances=...)`는
이미 같은 설정으로 조립된 애플리케이션의 **명시적 수집 API**다. 기존 Run 독점
소유권과 quiescent recovery lock을 사용한다. 실행 중인 actor나 미확정 reference
writer가 있으면 거절한다. 이 API를 CLI `status`나 자동 `resume`에 연결하지 않았다.
조회 상태의 exit 0이나 이 API 반환으로 재개/claim 해제 권한을 만들지 않는다.

수집 순서:

1. `TaskActivation.checkpointless_entered`에서 version 1 initial Runtime digest,
   Run/engagement/Mission·instance·원래 Task/역할/claim·완료 계약을 확인한다.
   reserved는 기존 zero-entry 복구의 대상이며 여기서는 실행하지 않는다.
   held/returned의 역사적 참조를 읽어도 activation 상태를 launched로 바꾸지 않는다.
2. 현재 component·공유 요청 정책·조립 입력을 확인하고, 선택된 원래 instance의
   checkpoint가 실제로 없는지 검사한다. checkpoint 조회 기능 부재/실패는 absence가
   아니다. 모든 선택 actor의 preflight가 끝나기 전에 provider 원본을 수집하지 않는다.
3. 기존 Work/Evidence port로 Run/instance/Task/역할과 submission·관련 실행 receipt를
   대조한다. submission hash만 맞아도 다른 실행 문맥의 Run을 받아들이지 않는다.
   서로 다른 submission·입력·결과 식별자나 schema 오류는 `invalid`다.
4. 이미 확정된 원본이면 그 receipt를 재사용한다. 없거나 uncertain이면 저장된
   `ExecutionReference`와 제공자의 pure `prepare_reference` 공통 식별·입력 digest를
   대조하고, 기존 recovery port의 `query_execution`/`fetch_execution`만 사용한다.
   opaque handle의 실제 보관 결과 검증은 제공자의 책임이다. 조회와 수집은 원래
   invocation의 설정 timeout을 함께 쓰며 자동 재시도하지 않는다.
5. verified·non-uncertain terminal answer와 원래 invocation/action/tool에 일치하는
   결과만 기존 append-only Evidence에 `execution_resolution`으로 추가한다.
   결과의 성공 여부도 provider terminal state와 맞아야 한다. 기존 timeout/실패 원본은
   덮어쓰지 않는다. 저장 완료 응답 유실 뒤에는 저장된 같은 원본을 읽고 재수집하지 않는다.
6. 원래 activation/claim·조립 입력을 다시 검사한다. 중간 변경은 거절하되 이미
   보존한 과거 원본을 삭제하거나 현재 실행 권한으로 승격하지 않는다.

진단 DTO는 `schema_version=1`, 원래 Run/instance, invocation별
`known | held | invalid`, 안전한 reason code, 원본 receipt ID만 반환한다.
`replay_permitted=false`, `claim_release_permitted=false`다. raw output·provider handle·
예외 본문을 반환하지 않는다. running/unknown/unverified/원본 부재·오류·시간 초과는
보류한다. cancellation은 전파하며 원장/claim/자원 marker를 정리하지 않는다.

`known`은 **확정 원본이 보존되어 있음**만 의미한다. Tool 성공·Graph 반영·Work
정산·Task/목표 완료를 뜻하지 않는다. 빈 원본 목록·개별 invocation의 non-entry는
워커 전체의 non-entry 증명이 아니다. orphan Work는 기존 교차 진단에 남고, unknown
모델 호출을 환불하지 않는다. 일반 checkpoint 재개는 같은 수집 함수를 사용한 후
기존 VERIFY/UPDATE·Work/accounting 경로를 계속 적용한다.

이 단계는 actor 자동 기동·승인 변경·Control 계량 복원·모델 판단·Graph mutation·
Work/Task 정산·claim 해제·새 checkpoint를 수행하지 않는다. checkpoint 없는 전체
런타임/사용량 재구성과 사람 지시 기반의 안전한 claim handoff,
구형/orphan 원본 및 분산 복구는 아직 완료하지 않았다. 전용 operator CLI는
5.7절의 원본 수집 범위에 한정한다.

비용 없는 합성 검증:

```sh
./.venv/bin/pytest -q tests/integration/test_execution_original_collection.py tests/integration/test_checkpointless_original_collection.py
```

### 5.7 명시적 operator 원본 수집 CLI

5.6의 원본 수집 API에 다음 명령을 연결했다. 저장소 루트에서 **원래 Run의 설정**과
engagement 파일을 사용한다. 새 프로필/요청 정책을 같은 Run에 끼우는 명령은 아니다.

```sh
./.venv/bin/python -m redteam.app.cli collect-originals RUN_ID engagement.yaml --json
```

명령의 범위는 **version 1 기동 기록이 있는 entered + checkpoint 부재 actor 전체**다.
checkpoint가 있는 peer는 조립 일관성만 확인하며 재개하지 않는다. reserved actor는
기존 zero-entry recovery 경로에 남긴다. 구형/단일 legacy thread, 귀속 불명, 변경된
claim·계약·조립 입력은 거절한다. 특정 instance만 선택하거나 정책/step 한도를
override하는 옵션은 제공하지 않는다.

1. 설정을 기존 CLI loader/durable settings로 해석한다. 기존 checkpoint identity와
   `read_activation_collection_identity`의 read-only canonical snapshot을 확인한다.
   이 collection hint는 `read_activation_identity`의 zero-entry resume proof와 별개다.
2. 전체 기동 cohort의 version·원래 runtime digest·binding·공유 요청 정책·저장 조립
   입력을 확인한다. 수집 대상의 원래 Task claim·완료 계약을 같은 소유 모듈에서
   확인하며 profile/prompt fingerprint·component 등록을 조립 전에 검사한다.
3. 기존 `build_resume_application`/`build_application`을 원래 engagement ID로
   재사용한다. 설정된 MCP 연결/discovery는 열 수 있지만 대상 도구·LLM을 실행하지
   않는다. 조립 후 기존 Run exclusive ownership을 확보하고 checkpoint/cohort
   inventory를 다시 읽는다. 변경됐다면 provider query 전에 거절한다.
4. 같은 Driver collection API로 원본만 수집한다. pending writer는 자동 해제하지
   않는다. 종료 시 driver/checkpointer/연결을 정리하며 원래 오류·취소를 정리 오류로
   바꾸지 않는다. 정리 오류는 예외 본문 없이 별도로 알린다.

`--json`은 실제 수집 시도 결과를 schema version 1로 반환한다. 원래 Run,
`outcome=collected|held`, instance별 5.6의 안전한 DTO만 포함한다. preflight/설정/정리
실패는 stdout 결과 대신 stderr 오류와 실패 exit code를 반환할 수 있다.

- exit **0**: 수집된 모든 actor가 비어 있지 않은 원본 목록을 갖고, 모두 `known`이다.
  **실패한 도구의 확정 원본도 known**일 수 있다. 이는 수집 성공이지 도구/Task/목표
  성공, 최신 전체 런타임 복구나 미정산 효과 부재의 증명이 아니다.
- exit **1**: 불명/부적합 귀속, 거절, held/invalid/빈 원본 목록 또는 정리 실패.
  일부 확정 원본을 이미 저장한 뒤 실패해도 과거 원본을 삭제/재실행하지 않는다.
- exit **2**: 읽을 수 없는 실행 설정 또는 MCP 설정 오류.

`replay_permitted=false`, `claim_release_permitted=false`는 top-level과 instance DTO에
유지한다. Control 재수화, 승인 변경, Graph/Work/Task 정산, 모델 호출·환불,
claim 해제, 새 워커/새 checkpoint는 수행하지 않는다. 종료 보고서를 성공으로 다시
작성하지 않는다. 수집 후에도 entered/no-checkpoint의 자동 resume은 계속 거절한다.
`status`는 별도의 읽기 전용 진단이며 provider를 조회하지 않는다.

라이브 provider의 원본 보존/recovery 지원이 없으면 확정 결과를 새로 얻을 수 없다.
이 CLI 추가가 ZAP/Burp 등 모든 MCP의 recovery 구현 완료를 의미하지 않는다.

비용 없는 합성 CLI 검증:

```sh
./.venv/bin/pytest -q tests/integration/test_original_collection_cli.py
```

### 5.8 Task 점유 정리와 요청 원장의 무결성 경계

`InstanceDriver.release_instance_claim(runtime)`는 **canonical Task가 이미 종료된
경우의 명시적 점유 정리**에 한정한다. 호출자가 전달한 Runtime의 `finished/failed`나
수집한 `known` 결과만으로 미완료 Task를 다른 워커에게 넘기지 않는다.

- 기존 Run exclusive ownership과 quiescent recovery lock을 사용한다. 다른 driver가
  소유하거나 같은 driver에서 actor가 진행 중이면 정리하지 않는다.
- 같은 coordinator transaction에서 원래 Run/Mission/engagement binding, 실제
  Mission의 engagement, Task의 Mission·occupant owner·lifecycle을 확인한다.
- Task lifecycle 소유 모듈의 terminal 상태만 허용한다. PENDING/READY/RUNNING/
  BLOCKED는 `task_claim_handoff_unverified`로 거절한다. Runtime 단계/카운터는
  종료 증명이 아니다. task 없는 기존 호출은 no-op이고, Task를 지정한 legacy
  instance 부재 호출은 거절한다.
- 종료된 Task에서 기존 owner 점유만 정리하며, 이미 정리됐으면 상태/시간도 다시
  바꾸지 않는다. Task/Mission/Run 상태·승인·Work/resource marker·LLM 원장·Evidence·
  Graph·activation·checkpoint는 변경하지 않는다. 닫힌 Run도 다시 열지 않는다.

종료된 Task는 기존 claim 규칙으로 다시 채택할 수 없으므로 이 제한된 정리는 최신
checkpoint 복구를 요구하지 않는다. **그 사실을 미완료 Task 인계의 복구 증명으로
확대하지 않는다.** 현재 operator intent에는 검증된 claim handoff 의미가 없고,
checkpoint 없는 최신 native context/계량도 복원하지 못한다. 미완료 인계의 구현에는
명시적 사람 지시, 최신 상태·원본/처리/요청 기록 확인, 원래 actor 재개 차단과
canonical claim 변경의 원자적 연결이 필요하다. 이번 정리 API는 그 기능이 아니다.
새 CLI나 자동 정리/인계 경로를 추가하지 않았다.

공유 요청 원장은 저장소 소유 모듈 `RunRequestLedger`에서 공통 검사를 적용한다.
request key/payload ID 일치, nonblank Run/owner/kind, 지원 상태, 사용량 필드의 존재와
정수/비음수·상태 호환을 확인한 뒤 예약·계량·조회·dispatch/settle/release를 처리한다.
불명 Run의 row는 해당 inventory에서 안전하게 제외할 수 없으므로 거절하고,
귀속된 다른 Run의 기록은 이 Run의 예산에 포함하지 않는다. kind는 새 method label을
받는 열린 형식이며 역할/모델 이름 목록으로 검사하지 않는다.

손상 기록은 `llm_request_record_invalid`, 잘못된 예약 입력은
`llm_request_scope_invalid`로 거절한다. 예외에 row 본문을 출력하지 않고, 기록을
수정/삭제하거나 빈 사용량으로 바꾸지 않는다. `unknown`은 계속 과금된 요청이고,
`reserved/dispatched`는 점유된 slot이며, 복구 조회나 점유 정리가 환불하지 않는다.
이 검사는 구조적 원장 무결성이지 외부 효과 부재나 전체 런타임 복원의 증명이 아니다.

비용 없는 합성 검증:

```sh
./.venv/bin/pytest -q tests/integration/test_instance_claim_release_guard.py tests/unit/test_request_ledger_integrity.py
```
