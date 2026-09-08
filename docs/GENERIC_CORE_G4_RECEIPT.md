# G4 Passive Scheduler 구현 기록

2026-09-08 · **PASSED_OFFLINE / SERIAL_DEFAULT** · 실제 provider/domain 호출 없음

## 최종 G4 승인

- 승인 core SHA: `b7d5eaa69e9a4c4691dad5ee97f5516aeb059f2562c0825c0c612d13d8d2f5f9`.
- 최종 source 전체 회귀: **1,006 PASS / 2 expected approval-hash differences (399.33s)**. 두 실패가 위 actual hash와 직전 G3 승인 hash의 차이뿐임을 확인한 뒤 baseline을 수동 갱신했다. 승인 후 hash/reference gate를 별도 재실행한다.
- checkpoint schema 2.1, core/package version 0.2.1. 1.0/2.0 checkpoint와 runtime 0.1.0/0.1.1/0.2.0 자동 resume 없음.
- 최종 fake latency 100ms, 4개 독립 requirement, warmup 5 + paired 30: serial p50 **10,683.49ms**, concurrent=4 p50 **10,168.22ms**, ratio **0.95177**. paired bootstrap 95% ratio **0.91519~0.97734**, direction observed. p95 serial **11,473.98ms**, concurrent **11,233.58ms**. canonical mismatch 0.
- 최종 별도 프로세스 peak working set: serial **57,982,976 bytes**, concurrent=4 **57,618,432 bytes**, ratio **0.99371**. 단일 표본이므로 정밀 메모리 우월 주장이 아니라 20% 악화 canary다.
- p50 20% 개선 기준을 못 넘었다. 따라서 bounded scheduler 기능과 receipt recovery는 PASSED_OFFLINE이지만 **concurrency 기본값은 1**, 2/4/8은 opt-in 실험 설정이다. 실제 provider latency로 일반화하지 않는다.
- 최종 raw 자료: `GENERIC_CORE_G4_RELEASE_AB_FINAL.json`, `GENERIC_CORE_G4_MEMORY_SERIAL_FINAL.json`, `GENERIC_CORE_G4_MEMORY_CONCURRENT4_FINAL.json`. 접미사 없는 앞선 자료는 구 source 측정 또는 최초 JSON 실패 전후 자료이므로 최종 승인 근거가 아니다.
- G4 rollback은 serial mode이며 durable batch/budget/receipt reader를 유지한다. receipt/ledger를 삭제하거나 구 binary로 downgrade하지 않는다.

G4 범위는 완료됐다. G6/G7/G8/G9와 전체 계획 최종 acceptance는 아직 미완료다.

## 최신 추가 — fallback/byte admission/late response/shutdown

- 현재 source 전체 **1,000 PASS / 2 FAIL (333.90s)**. 실패는 예상된 G3 승인 hash 2곳뿐이며 actual core SHA는 `b94f1b0f5cda8fffb4909bc1e89fb86fb6bbdf4fe1b23de86740a46a25b295ca`였다. 의미/복구 회귀 실패는 0. 최종 fault/timing 전이므로 expected hash는 아직 갱신하지 않았다.

- shutdown 추가 후 scheduler/fallback/receipt/dispatcher/checkpoint/architecture **86 PASS (84.66s)**. receipt byte count/ledger cap 추가 후 관련 **32 PASS (84.23s)**. input 순서 6개 permutation을 production helper 없이 계산한 grouping oracle 및 canonical projection **7 PASS (17.56s)**. Ruff PASS.
- 각 receipt는 canonical payload 기준 `serialized_bytes`를 self-validate하며 단일 최대 1 MiB, 전체 receipt ledger 합계 64 MiB다. 이는 raw artifact 또는 adapter response body 저장을 허용하지 않는다. Observation은 등록된 typed payload만 담고, raw bytes/ref가 필요하면 기존 EvidenceStoragePolicy boundary를 별도로 통과해야 한다. receipt retention 삭제/compaction은 committed marker와 audit/recovery 계약이 필요해 이번 단계에서 자동 삭제하지 않는다.

- `allow_fallback=False`가 기본이며 frozen batch에서 명시적으로 켤 수 있다. trust/cost/ID 순서로 admitted descriptor별 새로운 attempt ID를 미리 저장하고 정확한 descriptor에 호출을 고정한다. 각 시도는 독립 budget reservation을 소비한다.
- fallback은 **known FAILED + INVALID_PAYLOAD**에서만 다음 admitted resolver로 진행한다. UNCERTAIN, timeout, auth 변경, 예산 부족은 자동 fallback하지 않는다. 이전 실패와 최종 성공을 모두 receipt history로 보존하고 함께 atomic commit한다. 실패를 영구 negative cache로 쓰지 않는다.
- 단일 batch serialized byte cap은 최대 1 MiB(최소 4 KiB 설정), 전체 batch ledger의 reserved byte cap은 16 MiB다. 입력을 누적 제한하고, 미래 receipt hash/outcome/cancelled waiter 증가분을 호출 전에 예약한다. 기본 1 MiB씩 예약하면 batch count 1,000보다 총 reserved byte cap이 먼저 적용된다. 이는 배치 저장 상한이며 adapter 응답 할당이나 전체 process RSS의 sandbox 제한이 아니다.
- dispatcher가 응답 직후 admission check를 다시 호출한다. in-flight auth/deadline 변경은 durable 실패로 기록하고 reusable 성공 receipt를 남기지 않는다. 따라서 receipt 이후 batch settlement 전에 중단돼도 늦은 성공을 재승격하지 않는다.
- `close(grace_seconds<=60)`는 scheduler가 소유한 shared wait task를 취소하고 완료를 기다린다. 종료 후 새 실행을 거절한다. grace 후 task가 살아 있으면 중단을 완료했다고 주장하지 않고 dispatcher를 poison하여 추가 저장을 막는다. caller가 직접 소유한 `run()` task의 lifecycle은 caller가 관리해야 한다.
- fallback 4 cases PASS; batch/receipt/dispatcher 관련 **59 PASS (80.19s)**. primary 실패 뒤 backup 호출과 별도 예산, disabled fallback, UNCERTAIN 유지, global budget 소진, 바이트 초과 admission call=0, late response→TIMEOUT failure를 확인했다. shutdown + checkpoint/architecture 묶음은 검증 중이다.

### 완료 전 남은 gate 기록

1. fallback/동시 완료 순서를 독립 oracle 및 동일 response tape로 교차 비교. chain 전체 장애와 중간 crash, late/cancel 경계, immutable terminal batch/active writer 범위 확대.
2. typed response receipt 자체의 byte/retention bound와 policy 변경, artifact 참조 boundary를 검증한다. batch byte cap을 response memory 제한이라고 주장하지 않는다.
3. shutdown grace 초과 및 실제 재시작 fault matrix, schema/manifest/auth rebind 입력 확대.
4. fsync/checkpoint 포함 30회 serial/concurrent 측정과 raw receipt, 전체 회귀·G4 source 승인. G4와 최종 계획은 계속 IN_PROGRESS.

아래 목록은 당시 구현 시점의 이력이며 최신 남은 gate는 이 절을 따른다.

## 최신 추가 — bounded batch 초기 연결

- 최신 묶음 **79 PASS (67.62s)**: scheduler/receipt/dispatcher/operational checkpoint/architecture. provider concurrency 2가 global 4보다 우선함, 새 runtime+registry의 저장 receipt 복구, waiter 취소 ID 보존을 포함한다. Ruff/compileall/whitespace PASS. schema_version 기본값 추가 후 Ruff/compileall 재확인; 최종 전체 회귀는 G4 완료 gate에서 별도로 실행한다.

- `models/resolution_batch.py`: versioned batch/item, canonical requirement snapshot, frozen eligible descriptor hash 순서, auth context/permission snapshot, waiter mapping, deadline, concurrency 설정, settled/failure/receipt binding을 저장한다. RuntimeState의 typed batch ledger는 run/composition과 receipt binding을 검증한다.
- `runtime/passive_scheduler.py`: 최대 256 input waiter, 1,000 batch, 기본 concurrency/provider concurrency 1, opt-in 최대 8. 실행 전에 admissible batch를 checkpoint한다. 동일 requirement 전체 snapshot/descriptor 후보/auth/permission만 묶으며 qualifier/snapshot이 다르면 공유하지 않는다.
- `wait()`는 공유 task를 shield한다. 한 waiter 취소는 취소 ID를 durable batch에 기록하되 다른 waiter의 호출/receipt를 취소하거나 삭제하지 않는다. completed task는 메모리 task map에서 제거한다.
- 실행 시 auth context/permissions/requirement와 admitted resolver hash를 다시 검사한다. batch deadline과 provider semaphore 대기를 적용한다. response가 없는 기존 attempt는 자동 재전송하지 않는다. durable receipt가 있으면 batch binding만 복구한다.
- dispatcher result receipt → batch settlement → 완료 batch의 single atomic commit 연결. failure receipt도 canonical failure ledger에 반영하고, 늦게 도착한 성공 응답의 semantic commit은 거절한다. admission 단계 AUTH/BUDGET/UNSUPPORTED도 batch outcome에 보존한다.
- commit 시에도 auth context를 검사한다. batch 소유 receipt를 직접 `commit_resolution_receipts()`에 넘길 때도 batch authorization callback 없이는 거절한다. permission 이름이 같더라도 다른 실행 주체의 response를 재사용하지 않는다.
- 초기 scheduler **7 PASS**, waiter cancellation/receipt recovery 포함 기존 통합 **48 PASS**, auth commit + 2/4/8 기능 profile 추가 scheduler **13 PASS**. 후속 provider limit/실제 resume/cancelled waiter persistence 묶음은 검증 중이다. 성능 A/B 수치나 G4 최종 acceptance로 계산하지 않는다.

### 현재 남은 범위

- 한 항목 내 실패 후 다른 admitted resolver를 시도하는 explicit fallback attempt chain, 새 attempt·예산/UNKNOWN 유지.
- byte cap, late/cancel race와 terminal state 보호, 독립 scheduler oracle/동일 response tape의 completion order perturbation 확대.
- owner task 종료/복구 lifecycle, durable batch fault matrix, batch freshness/snapshot/retention 및 forged binding 추가 검증.
- fsync 포함 30회 성능 비교, 전체 회귀·source 승인. checkpoint schema 2.1 개발 중이며 G3 source 승인은 그대로 유지한다.

아래 '남은 필수 작업'은 초기 receipt-only 단계의 목록이다. 현재 구현과 최신 남은 범위는 이 절을 우선한다.

## 현재 구현

- `models/resolution_receipt.py`: typed requirement + Observation 또는 안전하게 분류한 failure를 가진 hash-bound receipt. run/attempt/composition/limits/descriptor에 binding한다. hash는 integrity identifier이지 서명이나 외부 결과 신뢰 증명이 아니다.
- `models/runtime.py`: receipt와 committed receipt hash를 명시적 durable 필드로 소유한다. 중복, 다른 run/provider/resolver/예산, 잘못된 reservation 상태를 거절한다.
- `runtime/resolver_dispatch.py`: terminal operational budget와 receipt를 같은 checkpoint에 저장한 뒤 반환한다. 검증 실패와 취소/timeout도 failure receipt로 남긴다. arbitrary adapter exception text는 저장하지 않는다.
- `runtime/resolution_commit.py`: 외부 Observation 객체 대신 저장된 attempt ID만 입력받는다. disk receipt와 runtime의 일치, 현재 descriptor/권한/requirement를 검사하고 private session에 기존 atomic observation commit을 수행한다. committed marker와 session을 함께 checkpoint한 뒤 live state에 게시한다.
- 이미 반영한 receipt는 canonical result 존재/동일성까지 확인하여 idempotent 처리한다. checkpoint validator도 committed marker와 canonical ledger 간 일치를 검사한다.
- 저장 실패 후 dispatcher는 재사용 불가이며 reload/recovery가 필요하다. 저장된 결과를 읽는 과정은 resolver 호출을 수행하지 않는다. intent만 있고 receipt가 없으면 G3의 UNCERTAIN 복구를 유지한다.

## 계층·호환성

- requirement/observation 데이터 모델을 `models/requirements.py`로 옮겼다. runtime의 typed receipt가 investigation 실행 계층을 역수입하는 순환 참조를 피하기 위한 이동이며, 기존 `investigation.requirements` import는 re-export로 유지한다. 모델 필드와 semantic ID 공식은 바꾸지 않았다.
- checkpoint schema **2.1**. event schema는 새 event 추가가 없으므로 2.0 유지. 자동 migration 없음. G3의 2.0 checkpoint는 새 reader에서 거절한다.
- semantic typed Observation을 저장하며 별도 raw bytes/export/LLM projection 기능은 추가하지 않았다. raw artifact 보관 정책을 대체하거나 plugin sandbox를 제공하지 않는다.
- source 승인 baseline은 G3 hash를 유지한다. G4 작업 중 hash 차이는 예상되며, G4 전체 검증 전 자동 승인하지 않는다.

## 검증 이력

- 전체 회귀 **971 PASS / 2 FAIL (148.22s)**: 두 실패는 명시적으로 유지한 G3 승인 source hash와 G4 작업 source 차이뿐이다. 테스트를 skip하거나 expected를 자동 갱신하지 않았다. 전체 실행 중 추가한 receipt capacity 선제 차단은 별도 최신 receipt suite **11 PASS (20.24s)**로 검증했다. 따라서 이 전체 실행을 최종 G4 source 승인으로 간주하지 않는다.
- receipt는 최대 10,000개이며 진행 중 RESERVED/DISPATCHED 요청의 저장 자리까지 호출 전에 계산한다. capacity를 다 쓴 뒤 호출하고 결과 저장에 실패하는 경로를 차단했다. 확대 batch/byte cap은 scheduler 단계에서 추가 검증한다.

- dispatcher/receipt/operational checkpoint/runtime checkpoint/knowledge/architecture 묶음 **107 PASS (38.66s)**. 이후 expiry와 rehashed in-memory forgery를 추가한 receipt suite **10 PASS (16.04s)**, Ruff PASS. 전체 973개 회귀는 진행 중이며 승인 baseline은 G3 그대로다.
- 만료된 receipt는 acquired_at을 변경하지 않고 UNKNOWN/stale로 기록한다. recomputed hash를 가진 in-memory receipt도 disk receipt와 다르면 거절한다.

- 기존 durable dispatcher + checkpoint **52 PASS**.
- 신규 receipt replay/permission/CAS/mutation/failure 검사 최초 **6 PASS**.
- durable commit 뒤 응답 유실, marker만 남은 잘못된 live state canary 추가 후 receipt + architecture + operational checkpoint **34 PASS**. Ruff PASS.
- 권한 철회 시 receipt 보존/semantic mutation 0, commit 저장 전 실패 시 live mutation 0, commit 저장 후 중단 시 restart에서 committed state 복원/추가 resolver call 0을 확인했다.

## 남은 필수 작업 — G4 완료 조건

1. versioned durable ResolutionBatch와 admissible item 고정. canonical/auth/snapshot/qualifier/descriptor가 동일한 항목만 single-flight하고 waiter reference를 보존한다.
2. 초기 serial + bounded queue/per-provider semaphore/concurrency 2·4·8 실험 profile, deadline와 waiter cancellation/late completion 처리.
3. frozen trust/cost/ID fallback 순서, failure receipt 보존, no permanent negative cache. UNSUPPORTED/BUDGET/AUTH 등 dispatch 이전 failure도 batch outcome에 기록한다.
4. receipt의 현재 freshness/snapshot/retention 경계, forged-but-rehashed input, duplicate/late arrival, batch별 순서 perturbation, batch commit rollback fault matrix 확대.
5. independent scheduler oracle, 동일 fake response tape serial/concurrent 결과 비교, invocation single-flight 증명, fsync 포함 비용 측정과 전체 회귀/source 승인.

현재 receipt 기반 단일 writer commit은 구현됐지만 bounded scheduler/single-flight는 아직 아니다. G4 전체 또는 최종 계획 완료로 계산하지 않는다. commit/push 없음.
