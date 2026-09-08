# G0-C1 — Frontier correctness receipt

2026-09-08 · correctness 수정 (성능 최적화 아님)

## 변경

- `_dominance_key`가 entry/source, blocked/influence, continuation, guard/missing/requirement/unknown/evidence 상태를 보존한다.
- 동일 상태라도 visited-node 집합이 포함되지 않으면 prune하지 않는다. incomparable states를 하나의 tuple로 덮어쓰지 않는다.
- enqueue/dequeue에 동일한 strict `_dominates` 비교를 사용한다. 같은 상태의 deterministic tie-break와 기존 shorter-equivalent-route pruning은 유지한다.
- private `_PathState`에 evidence/continuation 정보를 추가했다. 공개 Frontier schema와 bundle manifest 형식은 바꾸지 않았다.
- core runtime/package version을 0.1.1로 올려 0.1.0 checkpoint를 자동 resume하지 않도록 했다. 새 의미론에 과거 파생 결과를 그대로 적용하지 않는다. `uv.lock`의 local package version만 동기화했으며 dependency 변경은 없다.

## 검증과 승인 근거

- 과거 strict 반례 24/24와 120/120 입력 순서가 모두 PASS로 전환.
- 기존 Frontier 12 tests와 작은 oracle 64 tests PASS.
- 추가 dominance 검증 264 tests PASS: 250 seeded exhaustive comparisons, 12 noninterchangeable state cases, visited/tie test, distinct evidence path test.
- 0.1.0 checkpoint 거절 test PASS.
- 등록 전 전체 513 tests 중 **511 PASS**. 실패 2개는 승인 source hash가 아직 이전 값인 것만 확인했다. correctness/architecture/기존 의미적 회귀 실패는 없었다.
- Ruff PASS. 변경 diff를 읽어 source·blocked·visited·provenance 판정, enqueue/dequeue 일치, version binding을 검토했다.
- 이에 current approved release를 `g0-c1`로 명시적으로 등록한다. historical Batch F commit/hash는 그대로 둔다. 테스트 실행 중 자동 hash 갱신은 없다.

새 core exact-byte SHA256:
`957244f96871c62bebecca7509353cbe243e260946f364a7145559ad91d70b47`

등록 후 전체 회귀: **513 passed in 13.21s**. G0 전체/G1~G9 완료를 뜻하지 않는다. root cause 재현 당시 receipt는 덮어쓰지 않는다.

## 한계

보수적으로 더 많은 경로를 보존하므로 성능 향상을 주장하지 않는다. 작은 독립 oracle의 지원 어휘 밖 의미론에 대한 완전성 증명은 아니다. 새로운 work/memory budget과 계측은 G2에서 처리한다. 외부 provider·도메인·commit·push는 수행하지 않았다.
