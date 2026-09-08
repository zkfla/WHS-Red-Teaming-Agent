# G6 Focused Decision Context 구현 기록

2026-09-08 · **PASSED_OFFLINE** · 실제 LLM/provider/domain 호출 없음

## 승인 대상

- 승인 후보 core SHA: `9ce07a002c52d083a9fa72251d5deb25787eb1156aec50a892c1f4339249bcdf`.
- hash gate 2개를 제외한 전체 의미 회귀: **1,017 / 1,017 PASS**. 전체 수집 1,019개 중 제외한 두 검사는 직전 G4 승인 hash를 고정하는 검사다. 이 receipt와 raw benchmark를 확인한 뒤 baseline을 수동 승인하고 두 hash gate를 재실행한다.
- core/package version `0.2.2`, checkpoint schema `2.2`. `SecurityCandidate.expectation_ids`가 checkpointed canonical state에 추가됐으므로 2.1/0.2.1 자동 resume은 거절한다. event schema에는 새 event가 없어 2.0을 유지한다.

## 구현 결과

- `project_focused_decision_context()`는 bounded nonempty active Candidate portfolio만 받는다. 각 Candidate의 support, contradiction, guard, requirement, Observation metadata, Expectation metadata를 하나의 직접 조회 bundle로 만든다.
- Candidate에 명시적 `expectation_ids`를 추가했다. 기존 `grounding_refs`의 `exp_` 참조도 호환 경로로 합치되, 없는 relation/requirement/Expectation은 숨기지 않고 전체 bundle을 `CONTEXT_INCOMPLETE` marker로 바꾼다. 부분 근거를 COMPLETE로 표시하지 않는다.
- Observation payload와 Expectation payload 원문은 projection하지 않는다. content hash/type/trust/freshness/evidence metadata만 노출한다. trusted objective redaction 및 SYSTEM_POLICY/EXECUTION_POLICY/CANONICAL_STATE/UNTRUSTED_ARTIFACT 채널 분리를 유지한다.
- 전역 row 문자열을 먼저 만들지 않는다. relation/requirement/Expectation은 ID lookup하며 Observation은 operation-local requirement index를 한 번만 만든다. active Candidate 각각은 COMPLETE bundle 또는 retrieval ref가 포함된 명시적 incomplete marker를 가진다.
- character와 UTF-8 byte ceiling을 동시에 검사한다. token ceiling을 쓰려면 명시적 tokenizer adapter가 필수이며 output/schema reserve를 선차감한다. adapter가 없으면 결과에 character/byte-bounded임을 명시한다.
- legacy `project_decision_context()`는 rollback API로 유지한다. 신규 focused API는 G7 step engine 연결 전까지 독립 opt-in이며, G9에서 최종 기본 경로를 결정한다.

## 검증과 측정

- focused/legacy/history/working-memory/compatibility/checkpoint 묶음 32 PASS 이후 확대 검증을 수행했다. Korean UTF-8 byte overflow, token adapter 부재/invalid 결과, large schema/output reserve, duplicate/unknown active ID, 무관 전역 row 배제, missing contradiction의 전체 incomplete 전환, oversized latest protocol group fail-closed를 포함한다.
- 고정 fixture: active Candidate 1, required support/contradiction/guard/requirement/Expectation 각 보존, 무관 전역 Relation 1,000. warmup 5 + 교대 paired 30회.
- 합성 `UTF-8 bytes / 4` 카운터(실제 provider tokenizer 아님): legacy **65,614**, focused **771**, **98.82% 감소**, 사전 목표 20% 통과. 출력은 **262,456 → 3,082 bytes**. required bundle omission 0, raw payload 포함 0.
- 보조 timing: legacy p50 **11.045ms**, focused p50 **0.650ms**, ratio **0.05882**, bootstrap 95% ratio **0.05105~0.06814**. 이는 해당 synthetic projection fixture의 방향성일 뿐 provider latency나 전체 agent 성능으로 일반화하지 않는다. timing sample에서 process memory는 측정하지 않았다.
- raw receipt: `docs/GENERIC_CORE_G6_RELEASE_AB.json`. source hash가 위 승인 후보와 일치한다.

## 결정

승인 후 hash/reference/compatibility 묶음 15 PASS, Ruff/compileall/sdist/wheel PASS (`0.2.2`). 이후 G7 작업 source는 이 승인 hash와 다르며 G6 결과를 현재 G7 전체 source의 승인으로 해석하지 않는다.

G6 기능과 문맥 gate는 PASSED_OFFLINE이다. G7은 focused context의 incomplete 상태를 finish 근거로 쓰지 않고 bounded re-query 또는 WAITING으로 처리해야 한다. G7/G8/G9와 전체 performance evolution acceptance는 아직 미완료이며 commit/push는 하지 않았다.
