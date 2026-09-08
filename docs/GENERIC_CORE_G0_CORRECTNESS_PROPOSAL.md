# G0-C1 — Frontier dominance 정확성 수정 제안

> 상태: **REPRODUCED / FIX_PROPOSED / NOT_IMPLEMENTED**
> 한 root cause의 두 manifestation. 별개의 발견 두 건으로 세지 않는다.
> 후속 성능 계획 §9.1에 따라 최적화와 분리한다.

## 결론

서로 대체할 수 없는 path state를 dominance key로 합쳐 유효한 source/sink 도달성을 잃는다. 충분한 budget의 작은 graph에서 재현했고 동시에 `search_complete=True`, `truncated=False`를 반환한다. downstream Candidate에 필요한 경로가 누락될 수 있다. 실제 domain/서비스의 취약점이나 live false-negative까지 검증한 것은 아니다.

## 반례 A — 서로 다른 source 누락

```text
source:one ─┐
            ├→ a → b → z
source:two ─┘
```

5 nodes/4 edges, 모든 verdict TRUE. 정답은 두 source가 각각 z에 도달하지만 실제는 source:one만 반환한다. 입력 순서 **24/24**에서 동일. 각 source 단독 대조군은 각각 **6/6 PASS**다.

## 반례 B — 막힌 shortcut이 정상 detour 제거

```text
s → a ── FALSE ──→ b → z
    └── TRUE → c ── TRUE ──┘
```

5 nodes/5 edges. `s → a → c → b → z`가 정답 경로지만 실제 도달 frontier는 0개다. 순서 **120/120**에서 누락. FALSE shortcut 제거 대조군은 **24/24 PASS**이고, shortcut을 TRUE로 바꾼 test도 120개 순서 모두 PASS다.

두 반례 모두 max_depth=8/max_expansions=100000/max_frontiers=100000, 실제 expansions_used=2다. budget 고갈로 설명되지 않는다. oracle은 production dominance/context helper를 호출하지 않는다.

## 코드 원인

- `src/redteam_core/graph/frontier.py:193`의 `_dominance_key`는 현재 node/context/source tags/mechanism tags/sink tags를 쓰지만 실제 source identity와 blocked 상태는 쓰지 않는다.
- `:485` 이후 insertion pruning은 길이와 uncertainty 포함관계로 dominance를 판단한다.
- FALSE relation은 blocked이지만 uncertainty가 비어 있을 수 있어 정상 detour보다 더 좋은 state처럼 취급된다.
- `:410` 이후 dequeue pruning도 같은 축약 state를 사용한다.

source만 key에 추가하면 B가 남고, blocked만 추가하면 A가 남는다. enqueue/dequeue 한쪽만 고쳐도 판정이 불일치할 수 있다.

## 별도 수정 제안

1. `dominates(left, right)` 계약을 명시하고 enqueue/dequeue에 동일하게 사용.
2. 최소 동일 source/current node/context/semantic tags/blocked/influence 상태에서 비교. guard/missing requirement/provenance의 차이도 교환 가능성을 검증.
3. visited nodes가 다르면 후속 simple-path 가능성도 다르다. 길이만으로 지배하지 말고, 허용 후속 확장의 포함관계를 검증하거나 보수적으로 둘 다 유지.
4. 비교 불가능한 state를 하나의 길이/uncertainty tuple로 덮어쓰지 않도록 필요한 경우 bounded nondominated state 집합 사용.
5. 기존 shorter-equivalent-route pruning test는 보존. 전부 prune-off하고 성능 개선이라고 표현하지 않음.
6. source/blocked/unknown/guard/context/visited-node 교차 fixture를 독립 oracle로 확인. 아직 재현하지 않은 추가 오류를 확정 사실로 보고하지 않음.

## 수정 gate

- strict failure tests 2개와 재현 명령이 PASS/exit 0으로 전환.
- 모든 negative control, 기존 175개 의미적 tests, 신규 oracle·telemetry·historical baseline tests 통과.
- 정당한 equivalent-route pruning, deterministic ordering, UNKNOWN ceiling, partial metadata 보존.
- API/manifest 영향과 correctness 차이를 별도 receipt에 기록.
- source 변경의 current approved hash 차이는 예상됨. 동작 검증/검토 후에만 새 승인 SHA/receipt를 명시적으로 등록하고 historical SHA는 보존. 자동 expected 갱신 금지.
- 해결 전 G1 이후 최적화·release 승인·G0 완료 선언 금지.

## 증거

- [반례·대조군 JSON](GENERIC_CORE_G0_FRONTIER_COUNTEREXAMPLES.json)
- receipt SHA256: `ec3fdcc221f34d08e303c542f585b53ae9c9bac08ca03ce54e13aa5b9dc210ef`
- core SHA256: `53e814dcb5152fa178765201a9afa22c22eab928b3ba3b2101645b738cd1c21c`
- offline synthetic 검사이며 core 수정·외부 호출·exploit 실행은 없다.
