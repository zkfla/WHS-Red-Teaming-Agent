# Generic Core 성능·조사 효율 개선 계획

> 작성일: 2026-09-08 · 개정: R3 FINAL (자기검토 3회 반영)
> 상태: COMPLETE_OFFLINE / G0·G0-C1·G1·G2·G3·G4·G6·G7·G8·G9 PASSED_OFFLINE / G5·C1~C4 DEFERRED
> 기준 commit: `a01417cb241fc437ce0556ab0d9d5afb0381e8a2` (`redteam_core`)
> 대상: `C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent-redteam-core`
> 기존 Phase 0~14 완료 기록을 대체하지 않는 후속 계획이다. 2026-09-08 사용자 요청으로 구현에 착수했다. commit·push는 수행하지 않는다.
> 계획 완결성 평가: 20개 binary 항목 × 5점 = 100/100. 실제 성능·정확도·구현 완료 점수가 아니다. [검토 기록](GENERIC_CORE_PERFORMANCE_PLAN_REVIEW.md) 참조.

## 1. 목적과 제외 범위

목표는 domain-independent core가 더 큰 상태와 더 긴 조사에서 **동일한 의미적 신뢰도를 유지하며 CPU·메모리·문맥·호출 예산을 덜 낭비하게 하는 것**이다. 세 가지 성능을 별도로 평가한다.

1. 계산 성능: commit, recompute, Frontier, context projection, checkpoint의 시간·메모리.
2. 조사 효율: 같은 예산에서 해결한 requirement, 조사한 root, 불필요한 반복 호출.
3. 판단 품질: 올바른 UNKNOWN 유지, 지지·반증 근거 보존, 기대 결과와 실제 판정의 일치.

**이번 계획에는 CI/CD 적용·이식·benchmark가 없다.** GitHub/AWS, Financial, Mobile, Web 실제 adapter도 만들지 않는다. 기존 네 가지 synthetic reference (`static_graph`, `stateful_experiment`, `artifact_runtime`, `request_response`)와 domain-neutral fake provider로 검증한다. GitHub는 공개 설계 자료를 읽는 데만 사용했다.

제외: framework 교체, 외부 SDK 필수 의존성 추가, 실제 모델·API 호출, 분산 multi-writer, multi-agent swarm, 임의 Python 실행, 악성 third-party plugin sandbox, vector DB/장기 기억, 자동 exploit, 자동 재시도 확대. 실제 provider 검증은 별도 예산·범위 승인 전까지 NOT_RUN이다.

### 보존할 불변조건

- LLM 출력은 제안이며 Evidence/Observation/Relation verdict의 authority가 아니다.
- Candidate → investigation → validation → Finding; pathless Candidate도 유지한다.
- 모든 applicable validator를 manifest에서 결정하고 각 결과를 기록한다.
- UNKNOWN, conflict, stale, truncated는 SAFE 또는 FALSE가 아니다.
- normative Expectation과 observed state를 합치지 않는다.
- single writer, CAS, 원자적 commit, deep snapshot 격리를 보존한다.
- effect는 기존 permit/final boundary를 통한다. 시작 후 결과가 불명확하면 UNCERTAIN이며 자동 replay하지 않는다.
- raw artifact 보관·외부 전송·LLM 노출은 각각 기존 policy boundary를 통한다.
- 최적화 캐시와 telemetry는 canonical truth가 아니다.
- 속도를 위해 fsync, validator, guard, provenance, cleanup을 생략하지 않는다.

## 2. 현재 코드에서 확인한 근거

아래는 정적 소스 확인이다. 실측 병목·취약점·성능 향상으로 확정한 것이 아니다. 경로와 줄은 기준 commit에 대한 것이다.

| ID | 확인한 코드 | 개선 가설 |
|---|---|---|
| L1 | `knowledge/commit.py:122`: slot마다 전체 Observation 필터링; `:236` freshness 전체 순회 | requirement→Observation 인덱스로 반복 스캔 축소 |
| L2 | `knowledge/recompute.py:31`: effective payload 전체 조회; `:56/:84` recomputer 전체 실행·세션 복사 | 직접 ID 조회, 제한적 dependency-driven recompute |
| L3 | `investigation/candidates.py:90/:114`: deriver별 전체 세션 복사 | 읽기 계약을 정의한 컴포넌트만 좁은 snapshot 제공 |
| L4 | `graph/frontier.py:225/:288`: partial frontier마다 보조 sink BFS | 검색 내부 memoization과 보조 작업량 budget |
| L5 | `reasoning/context_projection.py:81/:135`: 전체 row 생성 후 절단, active portfolio는 별도 ID 목록 | active Candidate 중심으로 근거를 묶어 선택; bounded top-k |
| L6 | `reasoning/context_projection.py:263`: 하나씩 제거하며 JSON 재생성 | protocol/근거 묶음 단위의 budget packing |
| L7 | `investigation/resolution.py:65/:169`: resolver call count는 registry 메모리; `:320` 한 건 resolve+commit | durable reservation과 별도의 bounded batch scheduler |
| L8 | `investigation/coverage.py:71/:89`: requirement 수를 information_gain proxy로 사용 | 해결 가능성·공유 root 효과·실패 비용을 반영한 명시적 우선순위 |
| L9 | `validation/engine.py:137`: 전역 revision 기반 run 재사용; `:155` 전체 snapshot | 전역 cache를 함부로 좁히지 않고 의존성 계약 이후 판단 |
| L10 | `runtime/checkpoint.py:38/:232`: schema hash 계산, 이전 checkpoint load, 전체 상태 검증/직렬화 | 불변 hash 재사용부터 검토; 저장 구조 변경은 후순위 |
| L11 | `runtime/orchestrator.py`, `redteam_app/composition.py`: lifecycle/composition은 있지만 통합된 model-neutral decision step loop는 없음 | 기존 기능을 잇는 얇은 step engine 및 fake decision driver |
| L12 | `runtime/termination.py:35`: policy/budget/error/finish 판단; semantic no-progress 전용 판단은 없음 | action+결과+의존 상태 기준의 bounded 반복 방지 |
| L13 | `tests/integration/test_batch_f_reference_domains.py:78/:127`: core source hash 고정 | 후속 core 변경을 위한 release baseline 관리 필요 |

검토한 registry/checkpoint 경로에는 resolver별 사용량의 복원 연결이 보이지 않는다. 이것은 G3에서 독립 재현할 통합 공백이며, 아직 실서비스 예산 우회가 재현됐다는 뜻이 아니다. 기존 전역 RuntimeUsage의 영속화와 resolver별 call count는 구분한다.

## 3. 공개 소스에서 채택하는 패턴과 채택하지 않는 것

2026-09-08에 공식 문서와 GitHub main의 아래 commit을 조회했다. 문서 URL은 변할 수 있으므로 구현 근거는 고정 commit으로도 연결한다. 공개 프로젝트의 benchmark 수치를 이 core의 향상률로 전용하지 않는다. 코드 복사는 하지 않았으며 향후 복사 시 파일별 license/NOTICE를 별도 확인한다.

| 출처 | 관찰한 패턴 | 이 계획의 적용 | 그대로 가져오지 않는 부분 |
|---|---|---|---|
| LangGraph [CachePolicy 코드](https://github.com/langchain-ai/langgraph/blob/81bf17b23123e4ef8b9d5f49fa09a0122fc2edd1/libs/langgraph/langgraph/types.py#L521), [공식 node caching 문서](https://docs.langchain.com/oss/python/langgraph/use-graph-api) | 입력 key 함수와 TTL을 분리한 node cache | G1/G2의 명시적 key, 만료·무효화 계약 | 기본 pickle hash나 무기한 외부 응답 cache를 사용하지 않음 |
| LangGraph [pending write 구현](https://github.com/langchain-ai/langgraph/blob/81bf17b23123e4ef8b9d5f49fa09a0122fc2edd1/libs/langgraph/langgraph/pregel/_loop.py#L415), [task 문서](https://docs.langchain.com/oss/python/langgraph/functional-api) | 작업 결과를 저장해 재개 시 완료 작업의 재실행을 줄임 | G4의 완료된 passive 결과 receipt와 single commit | 전체 graph runtime 도입, effect의 일반 자동 재실행은 제외 |
| Pydantic AI [usage 검사 코드](https://github.com/pydantic/pydantic-ai/blob/716f2ae4a1cb2650ce4ee58f702d1f60c3c93f8c/pydantic_ai_slim/pydantic_ai/usage.py#L520), [공식 agent 문서](https://pydantic.dev/docs/ai/core-concepts/agent/) | 요청 전 검사와 tool projected usage 검사 | G3의 durable reserve-before-dispatch | 사후 token 검사만으로 엄격한 선제 예산 제한이 된다고 주장하지 않음 |
| Pydantic AI [deferred tool 문서](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/) | 요청과 외부 완료·승인을 분리 | G7에서 waiting과 done 구분; typed 외부 결과 receipt | 승인받았다는 LLM 텍스트, 임의 결과 injection은 인정하지 않음 |
| OpenHands [Agent 구조](https://docs.openhands.dev/sdk/arch/agent) | step 기반 reasoning/action loop | G7의 model-neutral 한 단계 실행 | SDK 자체·도구·provider를 core로 가져오지 않음 |
| OpenHands [stuck detector 코드](https://github.com/OpenHands/software-agent-sdk/blob/3896f186936342b99c7c5d3f89cdae888f458e7f/openhands-sdk/openhands/sdk/conversation/stuck_detector.py#L21), [문서](https://docs.openhands.dev/sdk/guides/agent-stuck-detector) | 제한된 최근 이벤트에서 반복 action/result/error 검사 | G7의 bounded semantic progress ledger | 단순 문자열 동일성만으로 정상 polling이나 증거 재검증을 중단하지 않음 |
| OpenHands [condenser 코드](https://github.com/OpenHands/software-agent-sdk/blob/3896f186936342b99c7c5d3f89cdae888f458e7f/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L38), [문서](https://docs.openhands.dev/sdk/guides/context-condenser) | 유지할 문맥과 압축할 history의 분리 | G6에서 trust/근거 묶음을 유지한 선택적 projection | LLM summary를 canonical evidence로 승격하지 않음; 이번 단계는 결정적 압축만 |
| smolagents [agent 코드](https://github.com/huggingface/smolagents/blob/30bb1161095dbae2271e6bc3cc4c219cc3897a57/src/smolagents/agents.py#L282), [공식 문서](https://huggingface.co/docs/smolagents/en/reference/agents) | max steps, planning interval, step callbacks, final checks | G7의 bounded replan trigger·receipt·finish gate | code-agent 실행, 다중 agent, 매 N회 무조건 유료 LLM 재계획은 제외 |
| Pydantic Evals [공식 concurrency 문서](https://pydantic.dev/docs/ai/evals/how-to/concurrency/), [evaluator 문서](https://pydantic.dev/docs/ai/api/pydantic_evals/evaluators/) | case별 평가와 concurrency·duration/tool 평가 | G0/G9의 case 결과, 비용/시간/품질 분리 | LLM judge를 truth oracle로 사용하지 않음; eval SDK 필수화하지 않음 |

Repository snapshot: LangGraph `81bf17b23123e4ef8b9d5f49fa09a0122fc2edd1` (MIT), Pydantic AI `716f2ae4a1cb2650ce4ee58f702d1f60c3c93f8c` (MIT), OpenHands SDK `3896f186936342b99c7c5d3f89cdae888f458e7f` (MIT), smolagents `30bb1161095dbae2271e6bc3cc4c219cc3897a57` (Apache-2.0). License 표기는 조회 당시 repository metadata 기준이다.

## 4. 설계 경계와 산출물

`src/redteam_core`는 canonical state·검증·budget·generic query/step 계약을 소유한다. `src/redteam_app`는 generic composition/step 연결만 담당한다. 외부 LLM/provider SDK는 추가하지 않는다. fake provider/decision driver, brute-force 비교 모델과 부하 생성기는 `tests/` 아래에 둔다.

새 이름은 제안 API이며 현재 존재한다는 뜻이 아니다.

| 제안 계약 | 최소 내용 | authority / persistence |
|---|---|---|
| `StateDelta` | 변경·삭제된 typed ref, old/new revision, invalidation reason | commit 결과에서만 생성; ledger로부터 재생성 가능 |
| `ReadDependencies` | exact refs, collection/range read, absence read, schema/implementation fingerprint | 컴포넌트 읽기 계약; 완전하지 않으면 full path |
| `QueryIndex` | requirement→Observation, Relation adjacency/guard/source, root→Candidate | 파생 자료; resume 시 재생성; cache miss는 truth 변화 아님 |
| `OperationalBudgetState` | scope별 used/reserved/uncertain, attempt ID, request/token 단위 | durable single-writer runtime state; registry는 등록 정보만 |
| `ResolutionBatch` / `ResolutionReceipt` | ordered item IDs, descriptor binding, dispatch intent, result/failure refs | external 결과와 commit 분리; replay 대상은 이미 저장한 결과뿐 |
| `DecisionRequest` / `DecisionProposal` | snapshot ref, allowed action IDs, typed args, expected revision | proposal은 untrusted; core validation 이후에만 실행 |
| `StepReceipt` / `ProgressState` | proposal hash, action ID, outcome class, affected refs, next state | bounded durable step/progress 기록 |
| `CompletionAssessment` | scope/search coverage, unresolved/blocked/uncertain counts, stop reason | finalization 실행 완료와 조사 완전성을 별도 표현 |
| `DecisionContextBundle` | active root, 지지·반증·guard·baseline refs, missing/truncation | LLM용 파생 view; raw payload 없음 |
| `PerformanceReceipt` | dataset/code/config hashes, counters, timings, memory, quality | 진단 자료; canonical semantic ID에 timing 포함 금지 |

### 공통 cache 계약

- run 안에서 재사용 가능한 pure result만 기본 허용한다. 외부 응답의 run 간 cache는 제외한다.
- key는 cache 종류에 맞는 전체 의존성을 포함한다: session namespace, typed input/qualifier, manifest·implementation version, 관련 state/expectation, snapshot/freshness, 권한·policy가 결과에 영향을 주면 그 binding.
- 전체 세션을 읽는 callback은 전역 revision 또는 full recompute를 유지한다. namespace/type만 같다는 이유로 재사용하지 않는다.
- TTL은 freshness를 대신하지 않는다. 시각은 명시적으로 주입한다. stale/conflicting 결과를 최신 TRUE로 재사용하지 않는다.
- 같은 payload라도 획득 시각·출처가 다르면 새 canonical Observation과 동일하다고 간주하지 않는다. cache hit는 기존 provenance를 보존한다.
- 항목 수와 bytes 상한을 모두 둔다. eviction order와 canonical 결과는 분리한다. cache off/cold/warm에서 의미 결과가 같아야 한다.
- private mutable 객체를 callback끼리 공유하지 않는다. 악성 Python plugin을 안전하게 만드는 sandbox라고 주장하지 않는다.

## 5. 작업 패키지와 실행 순서

G0/G0-C1/G1/G2/G3/G4/G6/G7/G8/G9은 PASSED_OFFLINE이며 G1/G2와 G4 concurrency 기본값은 기존/직렬 방식을 유지한다. G8 utility 기본화는 두 fixture의 품질 회귀로 보류한다. G5와 C1~C4는 선언된 profiling/설계 조건이 충족되지 않아 DEFERRED다. 계측 기준을 고정하고 한 번에 한 optimization을 비교한 최종 증거는 §13과 각 G receipt를 따른다.

| 패키지 | 선행 | 우선순위 / 기본값 | 예상 변경 영역 |
|---|---|---|---|
| G0 기준선·계측·평가 | 없음 | 필수, 가장 먼저 | `tests/benchmarks/`, `tests/reference_domains/`, 진단 hook |
| G1 Observation 조회 | G0 | 필수 후보, gate 통과 후 기본화 | `knowledge/commit.py`, `knowledge/recompute.py`, 파생 index |
| G2 Frontier 비용 통제 | G0 | 필수 후보 | `graph/frontier.py`, query/result budget metadata |
| G3 durable 운영 예산 | G0 | 필수 | `models/runtime.py`, `runtime/budget.py`, checkpoint, resolution |
| G4 bounded passive scheduler | G1, G3 | 필수, 초기 concurrency=1 | `investigation/`, runtime receipt/commit 연결 |
| G5 증분 계산·snapshot | G1, G0 실측 | 조건부, default off | `knowledge/`, deriver/read view 계약 |
| G6 근거 중심 문맥 | G0, G1 | 필수 후보 | `reasoning/context_projection.py`, context tests |
| G7 generic step/progress | G3, G4, G6 | 필수, fake decision driver만 | `redteam_app`, generic runtime/decision 계약 |
| G8 조사 우선순위 | G4, G7 | shadow 비교 후 기본화 결정 | `investigation/coverage.py`, priority provider |
| G9 통합·release 평가 | G1~G4, G6~G8 | 필수; G5는 선택 여부 기록 | regression, benchmark, compatibility receipt |

권장 순서: **G0 → G1 → G2 → G3 → G4 → G6 → G7 → G8 → G9**. G5는 계측상 전체 복사/재계산이 지배적일 때만 G9 전에 추가한다. 날짜/인일은 프로파일 없이 단정하지 않는다. 각 단계가 끝나면 gate receipt로 다음 진행 여부를 결정한다.

### G0 — 계측과 비교 기준

구현: opt-in `PerformanceObserver` 또는 동등한 no-op 진단 hook. perf clock은 의미적 RuntimeClock과 분리한다. commit/recompute/frontier/context/checkpoint/resolve/decision 각각 span·호출 수·입력 크기·bytes를 측정한다. network 및 prompt/raw payload의 telemetry 저장은 기본 금지한다.

Fixture: Observation 100/1,000/10,000/100,000; Relation 100/1,000/10,000; Candidate 10/100/1,000을 작은 조합부터 사용한다. 모든 Cartesian product를 돌리지 않는다. chain, diamond, fan-out, cycle, disconnected, context bridge, guard, pathless invariant, 동일 payload 충돌, expiry 경계, multi-bundle removal을 포함한다.

측정: fixed seed, 같은 interpreter/lock/code/config, 충분한 반복, warmup와 cold/warm 분리. quick suite는 소규모 5회, release timing은 warmup 5회+측정 최소 30회. p50/p95와 원시 표본을 보존한다. 무거운 memory 측정은 timing과 분리한다. OOM/timeout/truncated는 빠른 성공으로 기록하지 않는다.

Gate: 모든 metric에 정의·단위·누락 정책이 있고 no-op telemetry와 enabled telemetry의 canonical 결과가 같다. telemetry가 실패해도 truth를 바꾸지 않는다. audit/durable event의 실패는 이 규칙과 달리 기존 fail-closed다.

Rollback: hook 비활성화. 이 단계는 성능 향상 주장을 하지 않는다.

### G1 — 전체 스캔 제거

1. `_recompute_slot`에 requirement별 Observation lookup을 제공하고 batch commit에서 한 번 구축/갱신한다.
2. effective payload는 slot의 authoritative ID를 직접 조회하고 deterministic tie-break를 유지한다.
3. unknown requirement, duplicate, conflict, inferred/stale 분류, ID collision 검사를 유지한다.
4. mutable public session 변경을 감지할 수 없는 장수 index는 사용하지 않는다. 첫 버전은 operation-scoped index이며, 영속 reuse는 write gateway/epoch 계약 후에만 허용한다.

Gate: 기존 full-scan과 모든 ledger/Relation verdict 동일. R개의 requirement, O개의 Observation fixture에서 freshness용 global Observation scan을 R회에서 1회 이하로 줄이는 구조적 counter를 확인한다. 개별 slot 정렬 비용까지 O(R+O)라고 과장하지 않는다. 총시간·index 구축 비용·메모리도 함께 측정한다.

Rollback: full-scan 경로 선택. expiry heap은 이번 기본 구현에서 제외하고 G5와 별도로 프로파일 후 결정한다.

### G2 — Frontier memoization과 실제 작업 budget

1. 동일 검색의 `_nearest_sink(start_ref, depth)` 결과를 재사용한다. 검색 간 캐싱은 하지 않아 invalidation 위험을 줄인다.
2. source 초기화, adjacency/index 구축, edge 검사, guard 검사, queue pop, 보조 BFS를 각각 계측한다. 현재 `expansions_used`의 의미는 조용히 변경하지 않는다.
3. `max_work_units`, `max_queue_states`, `max_aux_visits` 같은 명시적 한도를 query에 추가할 경우 search/result 계약 버전을 올린다. limit 도달은 partial/truncated와 stop reason으로 노출한다.
4. 초기 source 전부 materialize하는 비용도 한도에 포함하고, `max_expansions=0`, source만 존재, 모든 guard로 차단, 높은 fan-out을 시험한다.
5. nearest sink는 structural hint일 뿐 context/authorization을 증명하지 않는다. 기존 guard·context bridge·dominance의 의미를 성능 변경에 섞지 않는다.

Gate: 같은 legacy budget에서 memo off/on의 결과·순서 동일; 보조 BFS 수행은 unique key 수 이하. 새 work limit은 queue/memory 폭증을 막으며 중단 정보를 숨기지 않는다. stronger pruning, reverse-sink multi-source index는 기본 범위 밖이다.

Rollback: memo off. query budget v2를 끄면 legacy 동작이 명확히 선택되며 결과 metadata에 mode를 남긴다.

### G3 — 요청 전 예약과 재시작 예산 보존

1. resolver registry의 사용량을 durable 운영 ledger로 연결한다. 전역·provider별·resolver별 request 수, 동시성, token reservation을 구분한다.
2. single writer가 `reserve → durable dispatch intent → call → reconcile`을 수행한다. call count의 단위는 resolver invocation이며 adapter 내부 실제 transport 횟수는 별도 capability/budget 계약 없이는 보장하지 않는다.
3. 완료가 불명확한 요청의 reservation은 사용된 것으로 보수적으로 유지한다. **미전송이 증명된 예약만** 반환한다. retry는 새 attempt와 새 예산을 소비한다.
4. 실제 token 수가 사전에 불명확하면 input estimate+maximum output을 예약한다. 엄격한 상한을 보장할 수 없는 provider mode는 hard-token-budget 실행을 거절하거나 명시적 미지원으로 처리한다. 비용 정보 없음은 0원이 아니다.
5. planner·repair·summary 등 모든 model 요청도 동일 budget에 포함하는 계약만 제공하며, 이번에는 fake adapter로만 검증한다.

Gate: restart 후 resolver별 예산이 리셋되지 않음; 동시 제출이 남은 예산을 초과하지 않음; 미전송/전송불명/완료 crash cut을 구분; registry 재생성으로 추가 호출 허용 안 됨. policy와 cleanup 예산은 독립적으로 기존 안전 규칙 유지.

Rollback: durable ledger를 유지한 채 concurrency=1. 새 ledger를 버리고 예산을 리셋하는 rollback은 금지한다.

### G4 — passive 실행과 canonical commit 분리

1. `resolve_and_commit`을 단순 gather하지 않는다. scheduler가 canonical requirement를 확인하고 admissible batch를 고정한 뒤 실행한다.
2. 독립적이며 권한이 맞는 `side_effect_free` resolver만 대상으로 한다. 같은 requirement라도 auth/snapshot/qualifier/descriptor가 다르면 합치지 않는다.
3. initial concurrency=1; 실험 profile에서 2/4/8을 비교한다. per-provider semaphore, bounded queue, deadline, cancellation을 적용한다. race-to-first-success는 하지 않는다.
4. single-flight 결과는 기다린 Candidate/root에 공통 reference로 연결한다. Observation을 복제하거나 최신 관측으로 포장하지 않는다. 실패도 structured failure receipt로 유지하며 UNKNOWN으로 둔다.
5. batch의 완료/timeout 결과를 안정된 requirement/resolver/attempt 순서로 단일 writer가 원자적으로 적용한다. 각 실제 acquired_at은 보존한다. authority commit 전에 결과 receipt를 저장한다.
6. fallback resolver 순서는 trust/cost/ID 규칙을 보존한다. 한 resolver의 실패를 영구 negative cache로 만들지 않는다. backoff/cooldown은 운영 힌트이며 truth 아님.

Gate: 서로 다른 completion 순서의 fake replay에서 semantic 결과가 동일. 모든 failure와 cancellation이 기록됨. 같은 batch의 동일 eligible 요청 N개는 실제 resolver invocation 1회. cached payload·저장 receipt와 evidence validity는 독립적으로 재검사.

Crash: durable 완료 receipt는 network 재호출 없이 idempotent commit 가능. dispatch intent만 있으면 결과 불명확으로 기록하고 자동 replay하지 않는다. 재조회는 별도 정책 아래 새 attempt로만 허용한다.

Rollback: scheduler serial mode. 저장된 결과·budget receipt는 유지한다.

### G5 — 선택적 증분 계산과 격리된 read view

착수 조건: G0에서 full-copy/recompute가 대표 workload 시간의 20% 이상이며, G1~G2 후에도 유지됨. 숫자는 사전 채택 기준이지 현재 측정값이 아니다.

1. `StateDelta`와 `ReadDependencies`를 제공한다. exact refs 외에 **없음 조회, collection 전체 읽기, 범위 조회, producer 삭제, Expectation, freshness 시간 경계**를 추적해야 한다.
2. 초기 v2는 collection 의존성 위주로 보수적 invalidation. callback이 기존 full session을 요구하면 legacy deep-copy/full-recompute 유지.
3. topological dependency 순서와 cycle 검사를 추가한다. 기존 ID 정렬 의미를 바꾸는 경우 별도 version/migration이다. 고정점 반복이 필요한 cycle은 이번 범위에서 거절한다.
4. read view는 nested container까지 mutation이 불가능하거나 독립 복사돼야 한다. 하나의 mutable snapshot을 서로 다른 callback에 공유하지 않는다.
5. changed owned Relation 집합을 staging한 뒤 검증하고 atomic replace한다. 삭제된 Relation이 남지 않아야 한다.

Gate: full/incremental differential, dependency 누락 canary, negative/range read, baseline expiry, multi-bundle isolation, snapshot mutation tests. canonical outcome 불일치 0. 최적화의 bookkeeping 비용까지 포함해 profile별 효과 측정.

Rollback: full path. 검증되지 않은 컴포넌트는 항상 full path이며 runtime에 억지로 opt-in시키지 않는다. validator result cache는 별도 C1으로 보류한다.

### G6 — 활성 조사에 필요한 근거를 묶어서 문맥에 제공

1. active portfolio/focus Candidate를 먼저 선택하고 그 Candidate의 support/contradiction/guard/requirement/Expectation을 묶는다. 불리한 반증을 먼저 버리는 ranking 금지.
2. 문맥용 전체 row를 생성한 뒤 자르지 않는다. bounded top-k/ID lookup을 활용한다. omitted 수 계산 때문에 필요한 순회와 대형 문자열 생성 비용은 구별한다.
3. system/execution policy와 trusted objective는 필수 영역. canonical bundle과 working text는 다른 채널. 문자열 byte/character ceiling과 tokenizer adapter token ceiling을 구분한다.
4. bundle이 안 들어가면 complete한 작은 bundle, 상세 재조회 ref, 또는 명시적 `CONTEXT_INCOMPLETE`를 제공한다. 일부 근거만 남겨 완전한 검증 문맥인 척하지 않는다.
5. tool call/result protocol group을 보존하고 request schema+output reserve도 token budget에 포함한다. stable prefix와 가변 state를 분리하되 provider prompt cache hit는 실측 전 주장하지 않는다.
6. 초기 압축은 결정적 omission/retrieval만. LLM 요약, vector retrieval, raw Observation projection은 추가하지 않는다.

Gate: 모든 active candidate가 full bundle 또는 명시적 unavailable 상태로 표시됨; guard·반증·baseline omission canary; Korean/Unicode/큰 schema/oversized latest group; omitted 사실을 근거로 finish 불가. token counter 미제공일 때는 character-bounded임을 명시한다.

Rollback: legacy projection. 새 step engine에서 incomplete context는 상태를 명시하고 검증 가능한 bounded 재조회로 이어져야 한다.

### G7 — 얇은 generic step loop와 무진전 제어

상태 흐름: `SNAPSHOT → PROPOSE → VALIDATE_PROPOSAL → RESERVE → DISPATCH → RECORD/COMMIT → RECONCILE → ASSESS`. waiting, policy stop, budget stop, error, finish는 별도 outcome이며 모두 기존 finalizer와 정합성을 유지한다.

1. `DecisionDriver` protocol과 deterministic fake 구현만 추가한다. typed proposal의 action ID/args/ref/revision을 canonical 상태로 재검사한다. 외부 provider SDK·prompt framework는 의존하지 않는다.
2. proposal은 조회/requirement 해결/memory patch/기존 experiment 계획 요청/finish 제안으로 제한한다. executor 우회나 validator 선택 인터페이스를 추가하지 않는다.
3. no-progress key는 action+canonical args+관련 입력 fingerprint+normalized outcome이다. 변경 없는 Observation ID/문장 수정/선택 횟수 증가만으로 진전이라 판정하지 않는다.
4. 반복 동일 결과, 반복 오류, A-B ping-pong을 bounded 최근 window에서 탐지한다. 초기 test policy: 동일 semantic no-op 3회면 replan 1회, 상태 변화 없이 재반복하면 STOP_PARTIAL. threshold는 versioned configuration이다.
5. 정상적인 deferred result 대기와 승인 대기는 stuck로 세지 않는다. freshness 만료·권한 변경·새 evidence·명시적 사용자 지시는 관련 key를 무효화한다. 무제한 polling은 허용하지 않는다.
6. replan은 필요 사건에서만: 막힌 root, dependency 충돌, 새 근거, 남은 예산 급감, no-progress. fake driver로 budget reservation과 bounded repair를 검증한다. malformed proposal repair는 최대 1회, canonical mutation은 0이어야 한다.
7. `LLM_FINISH`는 종료 요청이지 완료 증명 아님. `coverage_complete()`와 finalizer의 COMPLETE도 조사 완전성과 별개다. search truncation, unresolved requirement, blocked roots, UNCERTAIN effect, cleanup gap을 CompletionAssessment에 따로 남긴다.
8. Candidate가 0개인 경우도 acquisition/search scope가 충분하지 않으면 clean 결과가 아니다. 모든 root를 BLOCKED_EXTERNAL로 처리했더라도 조사 성공으로 세지 않는다.

Gate: provider-free end-to-end 4 reference suite, wrong ref/revision/forged evidence rejection, malformed proposal budget, no-op/ping-pong stop, 정상 waiting 유지, finish without coverage → PARTIAL, crash/resume 후 step·budget 일관성.

Rollback: 기존 API 직접 호출 경로 유지. 새 loop 사용 여부는 명시적 composition config로 결정한다.

### G8 — 예상 조사 효용에 따른 순서와 starvation 방지

1. 기존 score를 baseline provider로 보존한다. 새 `PriorityProvider`는 score components와 reason을 반환하며 truth를 바꾸지 않는다.
2. 초기에는 학습된 정보이득 확률을 발명하지 않는다. resolvable requirement 수, 공유 unresolved root 수, cached fresh availability, call cost bucket, 반복 실패, aging을 결정적으로 비교한다.
3. 이미 예산/권한상 불가능한 요청을 무한 선택하지 않고 blocked reason을 기록한다. 서로 다른 root/family의 조사 기회를 유지한다. eligible인 root는 finite stable pool에서 설정된 최대 대기 batch 이내 선택되도록 round-robin floor를 둔다.
4. stable root identity와 현재 evidence fingerprint를 분리한다. 이전 terminal decision이 어떤 근거·baseline에 bound됐는지 기록한다. 관련 상태가 바뀌면 재평가 필요성을 표시하되 무관한 변경으로 모든 root를 다시 열지 않는다. MERGED/user-final decision은 별도 정책 없이 덮어쓰지 않는다.

Gate: fixed total budget에서 baseline과 비교해 solved requirement, correct root decision, distinct root coverage, duplicate calls, time-to-first-valid-outcome을 보고한다. FALSE/충돌 발견도 유효한 진전으로 센다. UNKNOWN 감소만 최적화하거나 Finding 수를 무조건 늘리지 않는다. ordering 정책 변화는 별도 version과 ablation이다.

Rollback: baseline priority provider. priority ledger는 보존하며 재시작으로 aging/failure 정보를 초기화하지 않는다.

### G9 — 기본 활성화와 release gate

- 기존 unit/integration/architecture 회귀와 네 reference/composite를 실행한다.
- G1/G2/G6/G8을 각각 끄고 켜는 ablation, 전체 조합, serial/concurrent, cold/warm/resume를 비교한다.
- 자동화된 CI 서비스나 외부 workflow를 실행하지 않는다. 여기서 release gate는 로컬 테스트 절차다.
- 구조 최적화는 canonical 결과 동등성을 요구하고, priority/새 work budget처럼 의도적으로 바뀐 계약은 별도 expected result와 정책 version으로 비교한다.
- 기능은 `PROPOSED → TESTED_OFFLINE → DEFAULT_ENABLED`로 상태를 구분한다. fake loop 성공은 LIVE_LLM_VALIDATED가 아니다.
- 각 receipt에 source/config/fixture hash, 실행 환경, 전체 test 결과, 성능 표본, 실패·중단, 적용/보류 결정을 남긴다. 향상률을 못 입증한 최적화는 기본화하지 않는다.

## 6. 평가 수치와 기본 활성화 규칙

아래 값은 이 계획에서 사전 설정한 목표이지 관측 결과가 아니다.

| 종류 | 승인 기준 |
|---|---|
| 의미 보존 | 구조 최적화에서 canonical mismatch 0; UNKNOWN/guard/conflict/Expectation canary 모두 통과 |
| 속도 최적화 | 지정 대표 workload의 p50 20% 이상 개선, p95 10% 초과 악화 없음; 반복 표본으로 불확실성 보고 |
| 작은 입력 회귀 | p50 악화가 `max(10%, 1ms)` 초과하면 기본화 보류 |
| 메모리 | 명시적 cache/queue cap 내; reference 대비 peak 20% 초과 증가는 별도 justification 없이 기본화 금지 |
| scheduler | invocation count/예산 초과 0; fake latency에서 concurrent 이득은 별도 표기, 실제 provider latency로 일반화 금지 |
| 문맥 | 관련 큰-context fixture에서 input tokens 20% 이상 감소 목표, required context bundle omission 0 또는 명시적 incomplete |
| 조사 우선순위 | 예산별 correct decision/coverage가 fixture별 악화하지 않아야 함; aggregate만 좋아지는 변경은 기본화 보류 |
| 안전·회복 | dispatch/commit/restart fault matrix 전부 통과; side effect replay 0; 권한·출처·retention 변화 반영 |

Counter 개선만 있고 총 실행 시간 이득이 없으면 “복잡도 개선 관측, end-to-end 향상 미입증”으로 적는다. 지표·threshold는 결과를 본 뒤 통과시키려고 수정하지 않는다. 변경이 필요하면 dataset/config version을 올리고 baseline부터 다시 측정한다.

## 7. 조건부 또는 보류한 확장

- C1 Validator memoization: G5의 complete read dependency와 isolated snapshot 계약 후 별도 검토. applicable validator를 생략하지 않으며 cache receipt도 현재 run/expectation에 다시 bound해야 한다. 현 단계에는 구현하지 않는다.
- C2 Checkpoint delta/WAL/blob store: G0에서 checkpoint 비중이 20% 이상이고 작은 hash 재사용으로 해결되지 않을 때만 별도 설계. fsync ordering을 줄이지 않는다. append-only audit 보존과 retention 충돌부터 해결한다.
- C3 expiry heap / cross-search graph index / artifact search index: stale·filesystem change·budget accounting 계약을 추가로 검증해야 하므로 기본 구현에 넣지 않는다. artifact re-read 비용을 줄이는 cache도 raw retention/exposure policy를 통과해야 한다.
- C4 learned priority, LLM summary, prompt cache adapter, cross-run memory: 실제 provider 평가/예산과 별도 승인이 없으므로 보류. 일반 benchmark의 성공률을 domain-neutral core의 정확도로 치환하지 않는다.

## 8. 현재 상태와 남는 불확실성

아래 두 문단은 계획 작성 시점의 검증 기록이다. 구현 착수 후 최신 상태는 §13에 별도로 기록한다.

확인 완료: local 코드 대조, 공식 웹 문서 검색, 4개 공개 repository commit·관련 소스 확인, 후속 계획 작성. 문서 작성 후 현재 worktree의 baseline을 다시 실행해 `175 passed in 26.15s`를 확인했다(Python 3.13.14, `python -m pytest -ra -o addopts='' -p no:cacheprovider`, 현재 `src`를 명시적으로 import). 이는 회귀 검사 결과이며 부하 benchmark나 신규 기능 테스트 결과가 아니다. `src/`, `tests/` 변경은 없다.

미실행: 부하 benchmark, 새 index/scheduler/step engine, fault matrix, 실제 LLM/provider 검증. 예상 효과의 크기, 동시성의 실제 외부 이득, ranking의 실제 조사 품질은 아직 UNKNOWN이다.

현재 G0를 진행 중이며 correctness 문제를 먼저 분리했다. 기존 완료 receipt·원격 branch는 변경하지 않는다.

## 9. 자기검토 1차 수정 — 독립 정답 모델과 재현성

### 9.1 기존 구현과 같은 결과라는 것만으로는 충분하지 않다

G0의 필수 산출물에 작은 독립 oracle을 추가한다. 최적화 대상의 helper를 호출해 expected result를 만들지 않는다.

- Knowledge oracle: 작은 Observation 집합을 직접 집합 연산으로 분류한다. 동일 hash 복수 출처, inferred-only, conflicting, max_age와 not_after의 경계, future acquired_at reject를 독립 계산한다.
- Frontier oracle: 노드 최대 8개·간선 최대 12개인 작은 그래프에서 simple path를 열거하고 문서화된 guard/context 규칙을 직접 적용한다. source/context/visited-node/uncertainty가 다른 경로를 포함한다. pruning으로 제거해도 된다고 증명한 equivalence만 expected comparison에서 합친다.
- State/validation oracle: pathless invariant, normative baseline 부재/변경, negative observation에 대한 expected PASS/UNKNOWN/ANOMALY/VIOLATION을 fixture에 명시한다.
- Scheduler oracle: reservation·dispatch·result·commit 순서의 작은 상태 머신. 실제 calls/remaining budget/result visibility를 독립 계산한다.
- Completion oracle: empty candidate, truncated search, blocked-only coverage, uncertain effect, cleanup gap의 완료 판정 표를 test data로 고정한다.

작은 oracle이 legacy 결과와 다르면 **성능 변경을 멈추고 최소 반례와 correctness 변경 제안으로 분리**한다. legacy를 자동 정답으로 채택하거나 새 결과를 맞추려고 expected 값만 바꾸지 않는다. 기존 175 tests는 필요조건이지 충분조건이 아니다.

### 9.2 구조적 동등성과 실행 기록 동등성 구분

- 같은 recorded Observation/clock/decision tape를 replay한 경우에는 canonical IDs, provenance, verdict, expected coverage가 같아야 한다. 시간 필드를 지워서 비교를 통과시키지 않는다.
- 실제 시각이 다른 독립 실행은 Observation ID까지 같다고 약속할 수 없다. semantic projection과 acquisition provenance를 각각 비교하며 기록 차이를 숨기지 않는다.
- serial/concurrent 비교는 동일 fake response tape를 사용하고 response의 acquired_at도 고정한다. 완료 순서를 뒤집는 perturbation과 deadline 직전·직후 사건은 별도 사례다.
- 다른 chunk/batch 크기가 freshness 의미를 바꿀 수 있으므로 평가 시각과 commit barrier는 config에 기록한다. batch grouping 변경을 단순 hash normalization으로 덮지 않는다.
- semantic hash에 perf counters, wall-clock duration, cache hit 수를 포함하지 않는다. 테스트 실패 때 원시 receipt를 보존한다.

### 9.3 데이터·지표와 실험 절차 보강

- 규모 외에 state growth 1/10/100 commit cycles를 포함한다. index rebuild, receipt fsync, checkpoint load/write, cache invalidation의 비용을 전체 측정에 포함한다.
- `UNKNOWN 감소`는 올바른 새 근거에 의한 TRUE/FALSE 전환만 별도 집계한다. UNKNOWN→conflict, stale→UNKNOWN도 정당한 결과로 측정하고 실패로 벌점 주지 않는다.
- `correct root decision`의 denominator는 fixture의 ground truth root 집합이다. 출력한 root만 denominator로 삼지 않는다. no-finding correct result도 별도 포함한다.
- A/B는 동일 seed/input/config에서 실행 순서를 교차한다. p50 ratio의 paired bootstrap 95% 구간을 보고하고 sample 부족/구간이 넓으면 INCONCLUSIVE로 둔다. p95는 최소 30개 표본의 제한도 적고 필요하면 늘린다.
- quick run은 총 60초·단일 case 5초, release run은 총 15분·단일 case 60초를 초기 watchdog으로 둔다. 메모리 한도는 `min(2 GiB, 실행 직전 가용 메모리의 25%)`; 가장 큰 fixture가 불가능하면 자원 제한으로 표시하고 통과 수에 넣지 않는다. budget 변경 시 receipt에 남긴다.
- 설정값은 벤치마크 자체의 상한이지 제품 runtime의 보편적 기본값이 아니다. timing sample과 peak-memory sample은 분리해 계측 오염을 줄인다.
- prerequisite 오류, timeout, OOM, assertion failure, skip, inconclusive를 각각 보고한다. 실패 case를 제외한 평균만으로 승인하지 않는다.

## 10. 자기검토 1차 수정 — 호환성, recovery, rollback

### 10.1 API·저장 schema 변경 분류

| 변경 | 기본 처리 |
|---|---|
| operation-local index/memo | 기존 canonical schema/IDs 유지; config로 off 가능 |
| query work budget/result metadata | 명시적 query contract version; legacy mode 보존 |
| budget/step/receipt durable state | checkpoint·event schema version 증가; fingerprint에 반영 |
| priority/context/dependency 정책 | 정책 ID+implementation/config fingerprint; resume mismatch 거절 |
| 진짜 semantic correctness 수정 | 최적화와 분리한 계약·fixture·migration 검토 |

G3부터 `OperationalBudgetState`, G7부터 `ProgressState`와 `StepReceipt`를 durable envelope의 명시적 typed 필드로 소유한다. 임의 DomainState에 core 상태를 숨기지 않는다. 구버전 loader가 새 필드를 무시하고 실행하는 호환 모드는 제공하지 않는다.

초기 release는 **서로 다른 durable schema 간 자동 resume를 지원하지 않는다**. 구버전 checkpoint는 구버전 코드/환경으로 읽기 전용 검사할 수 있도록 보존하고, 새 runtime에서는 명확한 오류로 중단한다. migration이 필요하면 복사본에 대해 별도 tool/승인을 마련한다. 원본을 덮어쓰지 않는다.

구버전에는 resolver별 정확한 사용량이 없을 수 있다. Observation 수만 세어 호출 수를 복원하면 실패·취소 요청이 누락된다. 따라서 missing budget ledger를 0으로 초기화하거나 추정값으로 안전한 resume라고 표시하지 않는다. 이전 run의 예산을 새 run으로 몰래 리셋하는 것도 금지한다.

### 10.2 운영 receipt durability와 fault matrix

현재 journal은 모든 semantic state를 재구성하는 완전한 event store가 아니다. G4의 결과 복구를 기존 journal만으로 가능하다고 가정하지 않는다.

G3/G4는 versioned durable operational record에 reservation, dispatch intent, validated result/failure receipt를 저장하고 checkpoint와 연결한다. raw result bytes는 기존 evidence storage policy를 따르는 별도 boundary에 두며 receipt에는 허용된 typed result 또는 보호된 참조만 둔다. default scheduler receipt의 atomic write/fsync 비용도 benchmark 대상이다.

| crash 지점 | resume 규칙 |
|---|---|
| reservation 저장 전 | call은 아직 없어야 함; pending item 유지 |
| reservation 저장 후 dispatch intent 전 | 미전송을 증명할 수 있을 때만 reservation 반환 |
| dispatch intent 저장 후 call 전/중/후, 결과 receipt 전 | 외부 완료 여부 불명; 예약 보수적 유지, UNKNOWN operational outcome, 자동 replay 금지 |
| durable result receipt 후 semantic commit 전 | receipt를 현재 schema/권한/신선도와 검증 후 idempotent commit; 재호출 없음 |
| semantic commit 후 checkpoint 완료 전 | durable receipt ID로 중복 commit 억제, canonical 충돌 검사 |
| checkpoint 완료 후 | 정상 resume, index/cache만 재구축 |

corrupt receipt, 다른 run/manifest/requirement의 result injection, duplicate arrival, late arrival, deadline 후 completion, permission revocation, waiter cancellation을 추가한다. 하나의 waiter 취소가 다른 waiter의 shared result를 삭제하지 않도록 한다. mutation/effect 실행은 이 passive 복구 경로를 사용하지 않는다.

### 10.3 단계별 rollback 계약

- feature flag rollback과 durable data downgrade는 다르다. off로 전환해도 새 runtime의 ledger/recovery reader를 유지한다.
- 새 schema로 시작한 run을 이전 binary로 열지 않는다. release rollback은 기존 run 종료/안전 정지와 schema 호환성을 먼저 확인한다.
- incremental cache 오류는 full path로 fallback하되 불일치가 발생한 canonical commit은 승인하지 않는다. silent partial reuse 금지.
- schema version, manifest, input/read dependencies, permission, freshness가 불명확하면 cache miss/full recompute 또는 fail-closed다.
- 각 G receipt에는 touched files, API compatibility, enable/disable procedure, durable schema 영향, known limits를 반드시 적는다.

## 11. 자기검토 2차 수정 — 기존 freeze gate와 release 기준 관리

현재 `test_reference_domains_use_the_frozen_core_without_reference_specific_core_edits`는 Python core 파일 전체를 hash한 뒤 과거 Batch F의 상수와 비교한다. 이 테스트는 당시 reference 추가 과정에서 core를 변경하지 않았다는 증거다. 미래의 모든 core 수정을 영원히 금지하는 요구로 해석하지 않는다.

다음 절차를 G0의 첫 작업으로 둔다.

1. 기존 `a01417c`와 core SHA `53e814dcb5152fa178765201a9afa22c22eab928b3ba3b2101645b738cd1c21c`는 역사적 baseline으로 보존한다. 기존 완료 계획/receipt/hash를 덮어쓰지 않는다.
2. 기존 source hash와 테스트 결과를 먼저 검증하고 baseline receipt에 기록한다. 현재 최적화 branch와 비교할 참조 자료를 보존하되 donor를 다시 이식하거나 실행하지 않는다.
3. G0에서는 tests-only로 역사적 baseline 검증과 **현재 release core에 대한 reference 단계 no-edit 검증**의 목적을 분리한다. 이후 reference fixture 개발 전·후 core SHA가 같은지 검사하고, 독립적인 architecture/domain-neutrality 및 동작 oracle을 계속 유지한다.
4. core 변경 패키지에서는 source hash 차이가 예상됨을 기록하고, 마지막 **승인된 core 단계**의 SHA를 versioned release baseline으로 명시적으로 갱신한다. 새 hash는 전체 correctness gate와 변경 검토를 통과한 뒤에만 기록한다.
5. 테스트 실행 중 자동으로 현재 hash를 expected로 생성하거나, 테스트 삭제/상시 skip/무조건 통과 처리하지 않는다. expected와 actual을 같은 계산에서 가져오면 검증이 사라지므로 금지한다.
6. reference-only 변경은 직전 승인 core SHA를 그대로 사용해야 한다. source hash를 갱신하는 commit에 reference 전용 의미론을 몰래 추가하면 architecture 검사가 별도로 실패해야 한다.
7. byte hash는 현재 방식의 경로 순서·줄바꿈 영향을 유지하고 canonical semantic output hash와 혼동하지 않는다. OS/checkout 설정을 receipt에 기록한다.

G9의 baseline-test 결과는 위 목적 분리를 반영해 명시한다. 기존 175개의 모든 의미적 회귀 검사를 유지하고, 역사적 freeze 검사만 목적에 맞게 재구성한 내역을 보고한다. 단순히 새 숫자의 PASS만 보고하지 않는다.

## 12. 최종 실행 체크리스트와 인계

각 G의 상태는 `NOT_STARTED / IN_PROGRESS / PASSED_OFFLINE / DEFERRED / FAILED / INCONCLUSIVE` 중 하나다. G0/G0-C1/G1/G2/G3/G4/G6/G7/G8/G9은 PASSED_OFFLINE, G5와 C1~C4는 조건 미충족으로 DEFERRED다. G1의 기능 PASS와 speed gate INCONCLUSIVE를 구분한다. G2 memo는 최종 fanout p95 회귀로 기본화하지 않는다. G4 concurrency speed direction은 관찰됐지만 20% 기준 미달이라 serial default다. G8 utility는 예산별 fixture 품질 회귀로 기본화하지 않는다.

- 착수 전: 해당 worktree/HEAD/dirty state 확인; source baseline 보존; 이 계획의 최신 개정 확인.
- G0: 테스트 목적 분리, dataset·oracle·metric·watchdog·A/B gate 고정.
- G1/G2: structural counter와 end-to-end 비교를 함께 통과한 항목만 채택.
- G3/G4: durable 예산·receipt·fault matrix가 완료되기 전 concurrent mode 기본화 금지.
- G6/G7: context completeness, proposal validation, completion assessment를 하나의 fake loop로 연결.
- G8: 기존 priority 대비 shadow/ablation, starvation·stale decision 재검사.
- G5: profile trigger를 충족하지 않으면 DEFERRED로 이유를 기록. 필요하지 않은 복잡성을 completion 조건으로 강제하지 않음.
- G9: 필수 패키지 PASS, 선택 패키지 결정 기록, regression/quality/performance/compatibility receipt를 종합.
- 최종 보고: 무엇이 빨라졌는지, 무엇이 그대로인지, 어떤 품질을 검증했는지, 무엇이 UNKNOWN인지 분리.

기준선 및 G0/G0-C1/G1/G2/G3/G4/G6/G7/G8/G9 receipt는 승인됐다. `GENERIC_CORE_PERFORMANCE_ACCEPTANCE.md`는 최종 요구사항별 증거와 보류 범위를 기록한다. 문서의 존재나 조합 호환성 PASS 자체를 신규 성능 향상으로 해석하지 않는다.

최종 결론: **새 도메인 없이도 index·탐색 비용·durable scheduler·근거 중심 context·bounded step loop를 개선할 근거가 있다.** 다만 새 framework나 더 많은 agent를 붙이는 것 자체를 성능 향상으로 취급하지 않고, 측정·독립 정답·실패 복구 gate를 통과한 변경만 채택한다.

## 13. 구현 진행 — 2026-09-08 G0 첫 작업

### 최신 상태

- **2026-09-09 G9 PASSED_OFFLINE / 계획 완료**: 최종 core `7293c3e3ca79ba3177f27ee5a52c48c41719e7573996f1325a522b54b9945ce5`, runtime 0.4.1/schema 4.0. 승인 tree의 전체 1,166개 테스트가 100%/exit 0으로 통과했다. 승인 순서를 보존하는 1,164 semantic PASS와 후속 source/reference 10 PASS, seed 1/7/777 각 67 PASS, Ruff/compileall/build PASS도 별도 기록했다. 최종-source 조합 105회와 메모리 3회도 source/harness/canonical parity PASS지만 p50 개선 기준은 충족하지 못했다. 최종 fanout p95 회귀와 G8 품질 회귀를 포함해 G1/G2/G4/G8 대안은 opt-in으로 유지한다. G5/C1~C4는 선언된 조건 미충족으로 DEFERRED다. [G9 상세 기록](GENERIC_CORE_G9_RECEIPT.md), [최종 요구사항 검증표](GENERIC_CORE_PERFORMANCE_ACCEPTANCE.md). 아래 G8/G7 항목은 단계 당시 기록이다.

- **과거 G8 종료 시점(G9 착수 직후)**: G8 최종 1,118 semantic PASS(459.86s), 승인 후 source/reference 10 PASS(1.09s), Ruff/compileall/0.4.0 build PASS. 당시 승인 source는 `921c1003cc410e8a967a3d358977570ebf07f08705c6f70a12de98509e99a09e`였다. fixed-budget 20-arm 비교의 두 budget-3 fixture root coverage 회귀로 utility 기본화를 보류했다. [G8 receipt](GENERIC_CORE_G8_RECEIPT.md). 이 항목의 조합·resume·최종 기본값 gate 미완료 표시는 당시 기록이며, 현재 결론은 위 G9 최종 항목을 따른다.

### 이전 단계별 진행 기록

아래 IN_PROGRESS/NOT_STARTED 표시는 해당 기록 당시 상태다. 현재 판단에는 위 최신 상태, §12와 각 receipt의 최종 승인 절을 사용한다.

- **2026-09-09 G7 재개 점검 / IN_PROGRESS**: 토큰 초과량 보존, 과거 조회 결과의 canonical 재생성, 완료 batch 복구 시 권한·지시 binding을 보강했다. 새 회귀로 문제를 확인하고 수정했으며 최종 source 승인은 아직 하지 않았다. 최신 증거·잔여 항목은 [G7 receipt](GENERIC_CORE_G7_RECEIPT.md)의 2026-09-09 항목을 따른다.

- **G7 IN_PROGRESS**: 초기 typed step/progress/completion과 fake driver, 기존 passive scheduler 연결. 아직 response recovery·전체 budget·bounded retrieval·최종 acceptance가 남아 있다. [G7 현황과 필수 잔여 gate](GENERIC_CORE_G7_RECEIPT.md)를 우선한다. 아래 NOT_STARTED 표기는 착수 이전 기록이다.

- **G6 PASSED_OFFLINE**: active Candidate별 support/contradiction/guard/requirement/Expectation 완전 bundle 또는 명시적 `CONTEXT_INCOMPLETE` marker를 제공한다. 무관 Relation 1,000개 fixture의 paired 30회에서 합성 input token 65,614→771(98.82% 감소), required omission 0. 전체 의미 회귀 1,017 PASS 뒤 승인 후보 hash `9ce07a002c52d083a9fa72251d5deb25787eb1156aec50a892c1f4339249bcdf`; hash gate 승인을 진행한다. [G6 receipt](GENERIC_CORE_G6_RECEIPT.md).

- **G4 PASSED_OFFLINE / SERIAL_DEFAULT**: 전체 1,006 semantic tests 통과 뒤 수동 source 승인, 최종 hash `b7d5eaa69e9a4c4691dad5ee97f5516aeb059f2562c0825c0c612d13d8d2f5f9`. paired 30회 concurrent=4 p50 4.82% 개선으로 20% gate 미달, memory canary 악화 없음. G6가 다음 단계다. [G4 최종 receipt](GENERIC_CORE_G4_RECEIPT.md).

- G4 explicit fallback chain(known payload failure만), 별도 attempt/budget·실패 이력 보존, batch byte reservation, 응답 직후 auth/deadline 재검사, shared-task shutdown 추가. 관련 59 PASS. 독립 oracle/확대 fault·response boundary·30회 timing·전체/source 승인은 남아 있다. [최신 G4 receipt](GENERIC_CORE_G4_RECEIPT.md).

- G4 bounded batch 초기 연결: durable admission, 동일 요청 waiter 공유, 기본 serial/최대 8 worker, provider semaphore/deadline, batch auth 재검사와 receipt commit 연결. 초기 scheduler 13 PASS(2/4/8 기능 profile 포함). explicit fallback/byte cap/확대 oracle·fault·timing/source 승인은 남아 있다. [G4 최신 기록](GENERIC_CORE_G4_RECEIPT.md).

- G4 receipt 기반 구현의 전체 회귀 **971 PASS / 2 승인 hash 차이 FAIL**, capacity 보강 후 receipt **11 PASS**, Ruff PASS. G4 전체/source 승인 및 scheduler는 아직 미완료다. [상세 검증 기록](GENERIC_CORE_G4_RECEIPT.md).

- **G4 IN_PROGRESS**: typed response/failure receipt를 terminal 예산과 함께 저장하고, 재호출 없는 private staging → checkpoint → publish commit을 연결했다. 저장 전/후 실패 복구와 permission/CAS/mutation canary 포함 34 PASS, 기존 dispatcher/checkpoint 52 PASS. bounded scheduler/single-flight·실험 profile·확대 gate는 남아 있다. [G4 receipt](GENERIC_CORE_G4_RECEIPT.md).

- **G3 PASSED_OFFLINE**: 승인 source 기준 전체 **963 PASS (116.20s)**, Ruff/compileall/sdist/wheel PASS. runtime/package 0.2.0, checkpoint/event schema 2.0. 실제 token reconciliation·알려진 실패/불명 구분·전역 합산·limits binding·fault matrix 완료. 다음 G4는 typed result receipt, single-flight, bounded scheduler와 원자적 semantic commit이다. 아래 G3 IN_PROGRESS 항목은 과거 중간 기록이다. [G3 receipt](GENERIC_CORE_G3_RECEIPT.md).

- G3 durable passive dispatcher 초기 연결. 예약/전송의도 저장 후 adapter 실행, registry ledger 조회, 권한 재검사, fake 통합 포함 38 PASS. 기존 RuntimeUsage 합산·limits-hash binding·확대 fault matrix·전체 승인은 남아 있다. [G3 receipt](GENERIC_CORE_G3_RECEIPT.md).

- G3 RuntimeState typed budget + checkpoint/event schema 2.0 연결. actual checkpoint recovery 10 PASS, execution/validation/durable regression 36 PASS. 구버전 자동 migration 없음. 실제 resolver dispatch/registry 사용량 보존 연결은 아직 남아 있다. [G3 receipt](GENERIC_CORE_G3_RECEIPT.md).

- G2 PASSED_OFFLINE: 의미/architecture 회귀 910 PASS, 승인 후 freeze/reference 10 PASS. 단독 30회 memo A/B는 4 completed/2 TIMEOUT. shared-1,000 p50 22.15% 개선, p95/메모리 악화 없음. G9 기본화 후보이며 전체 그래프 성능 주장 아님.
- G3 OperationalBudget 모델·순수 전이 구현 시작, 11 tests PASS. durable checkpoint/registry/dispatch 연결은 아직 없다. [G3 receipt](GENERIC_CORE_G3_RECEIPT.md).
- 아래 G2 IN_PROGRESS 기록은 과거 중간 결과다.

- G2 work/queue/aux v2와 counter, partial/stop metadata, cache item/byte cap까지 구현. legacy 기본값 유지. 관련 **414 PASS**; v2 oracle 확대/성능/전체 source 승인 gate는 남아 있다. 상세 [G2 receipt](GENERIC_CORE_G2_RECEIPT.md).

- G1 기능/정확성 PASSED_OFFLINE. 전체 의미 회귀 608 PASS, 승인 후 freeze/reference 10 PASS. 30회 A/B는 7 완료/3 TIMEOUT이며 commit/freshness p50 20% 향상 미입증으로 DEFAULT_OFF 유지. payload는 별도 기본화 후보로 G9에서 평가한다. [G1 receipt](GENERIC_CORE_G1_RECEIPT.md).
- G2 검색 내부 sink memo 구현 시작. off/on 결과 동일, positive/negative cache와 search-local 재생성 확인. 관련 278 PASS. work-budget v2/counters/성능 gate는 남아 있다. [G2 receipt](GENERIC_CORE_G2_RECEIPT.md).
- 아래 G1 진행 중 기록은 이전 중간 상태다.

- G0 최종 **568 tests PASS**, Ruff PASS. Candidate scale/paired watchdog 검증까지 완료했고, release 성능/default enable gate와는 분리했다.
- G1 operation-scoped Observation index 및 direct ID lookup 구현 시작. 기본값 full_scan 유지. 관련 94 tests PASS, freshness global scans 10→1 확인. timing/memory/전체 승인 gate는 미완료. [G1 receipt](GENERIC_CORE_G1_RECEIPT.md).
- 아래 G0 진행 중 표기는 이전 중간 검증 기록이다. 현재 상태는 위 요약과 각 최신 receipt를 따른다.

- G0-C1에서 Frontier source/정상 detour 누락을 수정했다. 승인 후 전체 513 tests PASS. 상세 [G0-C1 receipt](GENERIC_CORE_G0_C1_RECEIPT.md).
- 실제 opt-in 계측, watchdog benchmark, 독립 scheduler oracle까지 추가했다. 확장 fixture 검증을 진행 중이다. [G0 receipt](GENERIC_CORE_G0_RECEIPT.md).
- 확장 후 전체 **562 tests PASS**, Ruff PASS. 단독 quick benchmark **10 PASS / 4 TIMEOUT**, 시작/종료 source fingerprint 동일. 시간 제한 도달 case는 성공/성능 향상으로 집계하지 않는다.
- 최초 quick 측정 8 PASS / 2 TIMEOUT이며 속도 향상을 주장하지 않는다. G0 전체 완료 전이며 G1~G9는 미착수다.

### 최초 재현 기록 — 아래 실패는 G0-C1 이전 상태

- 사용자 구현 시작 요청에 따라 G0의 historical/current freeze 분리, independent Knowledge/graph oracle, transition/completion specification, bounded offline telemetry wrapper를 추가했다.
- 신규 graph gate에서 하나의 dominance root cause에 의한 source 누락과 정상 detour 누락을 재현했다. 각각 모든 24/120개 입력 순서에서 실패했고 단독 source/shortcut 제거 대조군은 통과했다.
- §9.1에 따라 **성능 변경을 멈추고 정확성 수정 제안으로 분리**했다. G1 이후는 미착수다.
- 전체 248 tests: **246 PASS / 2 FAIL**. source bytes는 Batch F와 같고 기존 의미적 175 tests는 PASS. 실패를 xfail/skip하지 않았다.
- G0 남은 범위: scale/growth benchmark runner, watchdog/memory, 통계/A-B, 단계별 counters, scheduler oracle 등. **G0 미완료**다.
- [기준선·구현 receipt](GENERIC_CORE_PERFORMANCE_BASELINE_RECEIPT.md)
- [G0-C1 정확성 수정 제안](GENERIC_CORE_G0_CORRECTNESS_PROPOSAL.md)
- [기계 판독용 최소 반례](GENERIC_CORE_G0_FRONTIER_COUNTEREXAMPLES.json)
