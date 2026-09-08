# G3 Operational Budget 구현 기록

## 최신 상태 — token reconciliation 보강

**G3 PASSED_OFFLINE**. 승인 hash 적용 후 전체 **963 passed in 116.20s**. Ruff, compileall, sdist/wheel 0.2.0 모두 PASS. 아래 '진행 중' 문장은 이전 검증 시점의 이력이다. G4 이후와 전체 계획의 최종 release gate는 미완료다.

활성화: 새 run에서 `DurableResolverDispatcher.initialize()` 후 frozen registry를 dispatcher에 연결한다. legacy `resolve_and_commit`은 기존 API로 유지하지만 durable usage 보장을 제공하지 않는다. rollback 시 저장 ledger를 버리지 않고 새 reader/serial 실행을 유지한다. 구버전 binary로 새 checkpoint를 열지 않는다. 외부 provider, 실제 모델, 도메인 adapter, commit/push는 수행하지 않았다.

- source 승인: `569bcadd15259024feab474e960789897f1e1006238cec34fd5b3baca5a1f234`. 전체 958 PASS/4 FAIL에서 두 실패는 G2 승인 hash 차이, 나머지는 신규 fixture 생성 오류와 의도적 runtime version 변경의 기존 assertion이었다. fixture/compatibility 수정 후 dispatcher 28 PASS, compatibility 2 PASS와 Ruff PASS를 확인하고 G3 hash를 명시 승인했다. 승인 후 전체 회귀는 재실행한다. 역사적 Batch F hash는 유지했다.

- `ResolverOutput.actual_tokens`에 nonnegative integer 또는 None만 허용한다. None은 미측정이며 0으로 치환하지 않는다. planner/repair/summary adapter도 같은 reservation/transition 계약을 사용해야 한다; 실제 model loop 연결은 G7이다.
- 반환된 응답의 token 사용량은 semantic payload 검증 전에 수집한다. schema가 잘못된 응답은 FAILED로 저장하고 사용량을 유지하며, 응답이 확인되지 않은 timeout/cancel/exception은 UNCERTAIN으로 유지한다.
- 성공/알려진 실패의 measured usage는 terminal checkpoint에 함께 저장된다. resume에서 중복 합산하지 않는다. reported overrun은 잘라내지 않고 다음 dispatch를 차단한다.
- checkpoint/event schema 2.0 변경에 맞춰 package/runtime version을 0.2.0으로 올렸다. 0.1.0/0.1.1 자동 resume 및 migration은 지원하지 않는다.
- 보강된 dispatcher suite **28 PASS**. 최초 신규 canary의 잘못된 BaseModel 직접 생성은 미등록 payload subclass로 수정했다. 전체 회귀와 source 승인은 진행 중이다.
- 아래 기록은 단계별 이력이며, 과거의 '남은 작업'은 해당 시점 기준이다. G4 typed response receipt·batch commit·single-flight는 아직 구현되지 않았다.

2026-09-08 · IN_PROGRESS · durable passive dispatch 초기 연결, 최종 gate 전

- `models/operational_budget.py`: frozen limits/scope/reservation/state. 사용량은 ledger에서 계산하여 별도 used counter 불일치를 피한다. attempt ID는 RELEASED 후에도 재사용 금지.
- `runtime/operational_budget.py`: revision 검사, 전역/provider/resolver request·token·동시성 projected reservation, 제한된 상태 전이, recovery 변환을 구현했다. 외부 I/O/authorization 기능은 없다.
- RESERVED→DISPATCHED/RELEASED, DISPATCHED→SUCCEEDED/FAILED/UNCERTAIN만 허용한다. UNCERTAIN 자동 재실행/반환/완료 추측은 금지한다. concurrency도 보수적으로 유지한다.
- hard-token mode에서 bound 보장 불가능한 요청은 거절한다. 완료 시 실제 token 수로 조정하되 보고된 overrun을 잘라내지 않는다. guaranteed bound 위반이 발견되면 추가 예약을 차단한다.
- 순수 recovery에서 RESERVED는 RELEASED, DISPATCHED는 UNCERTAIN으로 바꾼다. **이는 호출자가 durable intent ordering을 지켰다는 전제**다. 현재 함수 자체는 파일을 읽거나 fsync하지 않으며 이것만으로 crash safety가 완성되지 않는다.
- 현재 11 tests PASS: 독립 scheduler oracle의 crash cut 비교, JSON roundtrip, scope/request/token 제한, uncertain slot 보존, 실제 token 조정/초과 기록, 중복 attempt/revision 거절.

## 남은 필수 작업

1. RuntimeState typed 필드와 checkpoint/event schema 2.0 연결 완료. active limits/descriptor와 dispatch 경계의 binding은 후속 작업.
2. reserve/dispatch intent의 durable 저장과 final dispatch 직전 재검사. run/composition/limits hash와 registry descriptor binding.
3. ResolverRuntimeRegistry 재생성에도 사용량 보존. 기존 메모리 call count만 사용하는 경로와 durable 경로의 계약 분리/통합.
4. single-writer 동시 예약, fsync 실패, restart의 미전송/전송불명/완료 fault matrix. G4 receipt/scheduler와 실제 연결.
5. 전체 회귀, source 승인과 G3 최종 receipt. 현재 승인 core hash는 G2 단계이므로 작업 중 source 차이는 예상된다.

fake passive dispatch까지 연결했다. 외부 provider·live call·도메인·commit/push는 수행하지 않았다.

## checkpoint 연결 진행

- `RuntimeState.operational_budget`를 명시적 typed 필드로 추가했다. 다른 run의 ledger는 RuntimeState validation에서 거절하고, 다른 composition ledger는 checkpoint envelope validation에서 거절한다.
- checkpoint/event schema를 **2.0**으로 올렸다. 과거 1.0 파일을 변환하거나 missing ledger를 0으로 추정하지 않는다. 원본 파일을 덮어쓰는 migration은 구현하지 않았다.
- field의 None은 operational budget 미구성 상태이지 0원/0회 ledger가 아니다. durable dispatch 경계는 이를 명시적으로 거절해야 하며 해당 연결은 아직 남아 있다.
- 재개 시 persisted RESERVED만 RELEASED로 복구한다. DISPATCHED는 UNCERTAIN으로 복구하고 완료 상태/실제 token은 그대로 보존한다.
- checkpoint 이후 journal dispatch intent가 있으면 RESERVED도 DISPATCHED로 투영한 뒤 UNCERTAIN으로 보존한다. unknown/duplicate attempt, provider/resolver/composition 재결합은 거절한다.
- settlement marker만으로 typed result나 token 사용량을 만들지 않는다. known dispatch 없는 settlement는 fail-closed이며, 이 marker로 RESERVED 예산을 반환할 수 없다. 완료 receipt의 semantic 복구는 G4 범위다.
- 모델+기존 checkpoint 검증 35 PASS. 신규 checkpoint 검증 포함 당시 44 PASS. 이후 settlement-only 거절 canary를 추가한 실제 checkpoint suite **10 PASS**. execution policy/validation/기존 durable integration을 포함한 추가 **36 PASS**. Ruff 및 whitespace 검사 PASS.

남은 핵심: reserve/dispatch 전 checkpoint fsync와 journal ordering을 실제 call에 강제하고, 해당 경계에서 limits hash·descriptor·권한을 재검사해야 한다. 현재 resolver 메모리 call count가 재시작으로 보존된다는 주장은 아직 하지 않는다.

## durable dispatcher 초기 구현 — 위 연결 전 기록의 후속

- `runtime/resolver_dispatch.py`의 DurableResolverDispatcher를 추가했다. 초기 예산 설정은 새 uncheckpointed run에서만 가능하다. resume/missing budget을 0으로 초기화하는 경로는 없다.
- reserve checkpoint → DISPATCHED checkpoint → journal dispatch intent → 실제 adapter 경계에서 재검사 → call → terminal budget checkpoint → settlement marker 순서다. checkpoint/journal 예외 시 dispatcher를 재사용 불가 상태로 만든다.
- adapter 입구에서 canonical requirement·frozen descriptor composition·현재 permissions·고정 limits를 재검사한다. async scheduling gap을 강제로 만든 권한 변경 canary를 검증했다. Python wait_for가 즉시 진입하는 경우에도 적용되며, 테스트의 잘못된 scheduling 가정을 강제 gap으로 수정했다.
- bound ResolverRuntimeRegistry는 사용량을 현재 durable ledger에서 읽고 기존 메모리 reserve_call 경로를 거절한다. 새 registry+resume에서도 사용량을 읽는 테스트 PASS. legacy unbound registry는 여전히 legacy API용으로 존재하며 durable 보장을 주장하지 않는다.
- ResolverDescriptor.max_tokens_per_call을 추가했다. None은 상한 미보장으로 hard-token mode에서 거절되며 0은 명시적인 0-token contract다. 실제 transport 횟수는 resolver invocation과 별개다.
- 성공 Observation을 반환하지만 semantic commit은 하지 않는다. durable typed result receipt/single-flight/batch commit은 G4에서 연결해야 한다. 응답 후 commit 전 crash recovery가 완성됐다고 주장하지 않는다.
- fake dispatch 7 cases + 기존 resolver/checkpoint suite **38 PASS**, Ruff PASS. 실제 adapter 진입 시 저장된 DISPATCHED와 journal intent 확인, registry 재생성/resume, legacy bypass 거절, checkpoint 실패 call=0, async permission revocation, last-budget 동시 요청, remote failure→UNCERTAIN 유지.

### 남은 승인 필수 항목

1. 기존 RuntimeUsage/RuntimeBudgetLimits와 operational ledger 요청·토큰 합산을 명시적으로 연결. 현재 dispatcher는 OperationalBudgetLimits를 적용하며 기존 전역 usage가 자동 증가한다고 주장하지 않는다.
2. limits hash를 durable intent/활성 설정에 명시적으로 binding, descriptor별 max_calls와 provider/global 제한을 각각 독립 canary로 검증.
3. journal append/fsync 실패, terminal checkpoint 실패, timeout/cancellation, descriptor/requirement 교체, poisoned dispatcher 재생성의 fault matrix 확대. 실제 data receipt는 G4.
4. 전체 회귀와 schema/compatibility/G3 source 승인. 이 구현은 아직 release/default 승인 전이다.

## 전역 합산·binding·fault matrix 보강

- `operational_accounted_requests/tokens`로 이미 RuntimeUsage에 반영한 양을 저장한다. DISPATCHED 이상 intent는 requests에 정확히 한 번 합산하며, 토큰은 실제 reported value만 RuntimeUsage에 추가한다. 미측정 예약은 ledger에 남겨 projected global limit에 더한다.
- global projected charge = RuntimeUsage + ledger charge − 이미 합산한 operational usage. 다른 runtime action의 사용량과 pending reservation을 빠뜨리거나 중복 계산하지 않는다. RuntimeBudgetLimits와 OperationalBudgetLimits 중 어느 하나라도 초과하면 호출 전 거절한다.
- 재개 시 동일 동기화 함수를 사용한다. 이미 합산한 요청은 다시 더하지 않는다. accounted 값이 실제 ledger를 넘거나 RuntimeUsage가 accounted floor보다 작으면 validation 실패다. dispatcher는 저장된 usage보다 낮은 상태로 attach하거나 실행 중 usage가 역행하는 것도 거절한다.
- ledger의 runtime_limits_hash와 operational scope limits를 합친 limits_hash를 intent/settlement에 기록·검사한다. 활성 runtime limits 변경, 다른 limits_hash의 journal intent는 fail-closed다. scope tuple 순서는 hash 계산에서 정렬한다.
- 이전 관련 36 tests PASS 후 journal 실패/terminal 저장 실패/cancel 추가 suite 40 PASS. 기존 checkpoint 포함 64 PASS. 이어 timeout과 descriptor/requirement 교체 검사를 추가한 **dispatcher 21 tests PASS**, Ruff PASS.
- fault cases: intent append 전/후 실패는 call=0, terminal checkpoint 실패는 call=1+UNCERTAIN 유지, cancellation/timeout은 request 및 concurrency 유지, descriptor/requirement/permission 변경은 adapter 진입 전 차단. provider/resolver limits와 descriptor max_calls를 전역 limit과 독립적으로 검사했다.
- 전체 회귀와 명시적 source 승인은 현재 진행 중이다. typed response receipt 저장과 단일 semantic commit은 여전히 G4 범위다.
