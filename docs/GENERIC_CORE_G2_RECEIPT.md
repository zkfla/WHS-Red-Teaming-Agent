# G2 Frontier 비용 통제 구현 기록

2026-09-08 · PASSED_OFFLINE · DEFAULT_OFF (G9 조합 승인 전)

- 검색 호출 내부의 `(start_ref, max_depth)` sink-hint memo를 추가했다. `None` 결과도 cache하며 검색 간 공유하지 않는다.
- `memoize_sink=True`로 opt-in, 기본 False. `potential_sink_ref`는 여전히 structural hint이며 guard/context/authorization proof가 아니다.
- 모든 `_to_frontier` 경로에 같은 operation-local cache를 전달한다. cache 내용은 canonical session/semantic ID에 넣지 않는다.
- 두 source가 동일 노드에 모이는 blocked partial fixture에서 보조 BFS 2→1을 확인했다. sink가 없는 negative-cache, 검색 재호출의 cache 재생성, off/on 결과·순서 동일성을 검증했다.
- 신규 memo + 기존 Frontier + seeded dominance 검증: **278 PASS**, Ruff PASS.

## v2 작업량 계약 구현

- `FrontierQuery.budget_version="v2"`를 명시적으로 선택한다. 기본 `legacy`에서는 기존 expansion 의미를 유지하고 v2 한도를 적용하지 않는다. `expansions_used`를 work units로 바꿔치기하지 않았다.
- v2 `max_work_units=100000`, `max_queue_states=5000`, `max_aux_visits=10000` (명시적으로 0 가능). query는 공개 경계에서 다시 검증한다.
- 전체 Relation 수를 work budget에 선승인한 뒤 정렬/인덱스를 구축한다. 일부 입력만 몰래 인덱싱하고 완전한 검색으로 보고하지 않는다.
- counted stages: relation_input, adjacency_index, guard_index, source_scan, source_initialization, queue_pop, edge_check, guard_check, dominance_check, hint_edge, aux_pop, aux_edge, frontier_output. `work_units_used == sum(work_counters)` 검증.
- main queue와 auxiliary BFS queue의 합계가 같은 `max_queue_states`를 공유한다. max_frontiers는 v2에서 생성 결과 buffer에도 적용한다.
- 한도 초과 전 검사한다. 중단 시 완성된 frontier만 보존하여 bounded diversity ranking하고 `search_complete=False`, `truncated=True`, 명시적 stop reason을 반환한다. source 미탐색 수를 확정할 수 없으면 `unexplored_source_count_known=False`를 함께 기록한다.
- stop reasons: MAX_WORK_UNITS, MAX_QUEUE_STATES, MAX_AUX_VISITS, MAX_FRONTIERS, MAX_EXPANSIONS, MAX_DEPTH.
- work unit은 방문/검사 건수다. wall time/CPU instruction/heap byte 제한은 아니다. bounded input sorting과 종료 시 bounded output ranking은 개별 비교 연산을 세지 않는다. 기존 runtime/상위 watchdog을 대체하지 않는다.

## cache 상한

- `max_sink_cache_entries=4096`, `max_sink_cache_bytes=1048576`을 기본 상한으로 둔다. bytes는 UTF-8 ref 내용과 depth 8 bytes이며 Python object heap 전체가 아니다. object 수는 item cap으로 함께 제한한다.
- admission 거절 시 원래 BFS로 fallback하고 `sink_memo_capacity_reached=True`를 남긴다. canonical frontier가 달라지거나 search를 성공으로 축소하지 않는다.
- **unique key당 BFS 최대 1회는 cache에 수용된 key에 대한 보장**이다. capacity 초과 key는 재조회할 수 있다. 이를 cache 무한 성장으로 해결하거나 전체 key 보장으로 보고하지 않는다. G2 성능 gate에서 capacity가 충분한 대표 workload와 overflow fallback을 구분해서 검증해야 한다.

## 최신 검증

관련 Frontier/independent oracle/seeded dominance/한도 matrix **414 PASS**, Ruff PASS. work budget 0..34 sweep, 입력 선승인 거절, zero-expansion의 source queue 상한, main+aux 합계, guard/aux charge, fanout, completed-result cap, permutation determinism, negative cache와 byte/item admission fallback, explicit legacy mode를 포함한다. 원본 canonical session 불변도 확인했다.

미완료: 더 넓은 v2 oracle differential 및 recovery/schema 영향 점검, 30회 memo 성능/메모리 비교, 전체 회귀와 G2 source 승인. 현재 release baseline은 마지막 승인 G1 source를 유지하므로 G2 작업 중 hash 불일치는 예상된다.

## 확대 검증·호환성 점검

- 같은 250 seeded graph를 legacy/v2 모두에서 독립 exhaustive oracle과 대조했다. cycle/context bridge/TRUE/FALSE/UNKNOWN/guard/두 source를 포함한 관련 562 tests PASS.
- cache admission 거절 뒤 auxiliary budget 중단 시에도 `sink_memo_capacity_reached`를 보존하도록 보강했다. 이 canary와 paired runner 포함 관련 57 tests PASS.
- 현재 CoreSession/RuntimeState/checkpoint schema에는 FrontierQuery/FrontierSearchResult가 저장되지 않는다. G2는 query/result 계약을 명시적 budget_version으로 확장하지만 durable schema를 변경하지 않는다. G7에서 resume 가능한 실행 설정을 만들 때 query 정책을 composition fingerprint에 반드시 포함해야 한다. 설정 자동 resume가 지금 구현됐다고 주장하지 않는다.

## 30회 단독 memo 비교

[원시 receipt](GENERIC_CORE_G2_RELEASE_AB.json): source_integrity PASS, source `16bd68260e28d7598f11dfa66944841fdc6632a363fabc6632a27d2fb81f04cb`, warmup 5 + paired 30, 4 completed / 2 TIMEOUT. 두 arm은 legacy budget로 같은 semantics를 비교하며 v2 budget 변경 효과와 혼합하지 않는다.

| fixture / Relations | p50 off→on ms | p50 ratio (95% bootstrap) | p95 ratio | separate Python peak ratio |
|---|---:|---|---:|---:|
| fanout control / 100 | 9.262→8.316 | 0.898 (0.781..1.082) | 0.784 | 0.999 |
| fanout control / 1,000 | 157.927→147.758 | 0.936 (0.868..1.115) | 0.889 | 1.000 |
| shared partial suffix / 100 | 12.784→10.525 | 0.823 (0.719..0.968) | 0.900 | 0.999 |
| shared partial suffix / 1,000 | 152.874→119.008 | 0.778 (0.672..0.890) | 0.861 | 1.000 |

partial fixture는 여러 blocked source에서 공유하는 17-relation structural suffix를 반복 조회한다. 이는 캐시 재사용 fixture이며 모든 그래프나 실제 domain의 성능으로 일반화하지 않는다. control은 sink에 곧바로 도달하여 보조 BFS 재사용 이득이 없다. control의 작은 차이는 CI가 1을 포함하므로 향상으로 확정하지 않는다.

10,000 Relation의 두 case는 60초 전체 case watchdog에 걸렸다. 원시 sample을 성공으로 구제하지 않았다. 메모리 cap 177,654,784 bytes. 대표 shared-1,000 case의 p50 개선은 22.15%, p95 악화 없음, Python peak 증가 없음으로 사전 수치 gate를 충족했다. G9 ablation/default enable 후보이며 현재 기본값은 여전히 memo off / legacy budget이다. v2의 장점은 명시적 비용 상한이며 속도 향상이라고 주장하지 않는다.

강한 pruning이나 reverse multi-source search로 의미를 바꾸지 않는다. 아래 최종 승인 이전의 중간 기록과 현재 완료 상태를 구분한다. 기본 활성화는 아직 아니다.

## G2 최종 기능 gate

전체 912 tests 중 **910 PASS / 2 FAIL**, 128.70s. 실패는 승인 baseline이 이전 G1 source를 가리키는 hash 검사 2개뿐이었다. 의미 회귀·architecture·새 v2 oracle/budget tests가 통과한 후 source diff와 기본값/중단 메타데이터를 검토했다. `16bd6826...1f04cb`를 G2 approved source로 명시적으로 등록하며 historical baseline은 보존한다. 등록 후 freeze/reference는 별도 재검증한다.

기능과 bounded-work 계약은 PASSED_OFFLINE, memo 성능은 shared-1,000에서 사전 gate를 충족했다. 10,000 TIMEOUT은 미입증으로 유지하고 G9에서 최종 조합/default 결정을 한다. G3 이후 durable scheduler/step 정책이 완성됐다고 주장하지 않는다.

G2 등록 후 freeze/reference 재검증 **10 PASS**, Ruff PASS. 910개 의미/architecture 회귀와 후속 hash 검증을 구분하여 기록했다. 912개를 한 번에 모두 통과했다고 바꿔 적지 않는다.
