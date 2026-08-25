# FinRedTeam — 범용 + 금융 특화 Red Teaming Agent

`instruction.md`의 계약을 구현한다. 대상은 로컬 Docker에서만 실행되는
의도적 취약 교육용 뱅킹 앱(vuln-bank). Black-box 정찰 → 그래프 모델링 →
후보 도출 → 오라클 검증 → Verified Finding. 지표(VVR 등)는 `evaluator/`에서만
계산하고 에이전트는 계산하지 않는다.

이 문서는 **일별 계획**과 **모듈 의존 순서**만 다룬다. 설계 이유·규칙은
`instruction.md`가 원본이며 충돌 시 그쪽이 우선한다.

---

## 0. 시작 전 사람이 할 일 (Day 0)

코드 작성 전에 vuln-bank를 로컬 Docker로 띄우고 손으로 한 번 공격해서
실제 경로·필드명·통화 단위를 확인한다. 확인된 값만
`targets/configs/vulnbank.yaml`에 적는다 (에이전트 코드가 소스를 읽는 것은 금지,
사람이 조사해 설정 파일에 적는 것은 허용).

- [ ] vuln-bank Docker 기동, 접속 확인 (`base_url`)
- [ ] 회원가입/로그인 요청·응답 필드명 확인 → `auth.*`
- [ ] 잔액/거래/카드 조회 엔드포인트, 필드명, 통화 코드 확인 → `observation.*`
- [ ] 송금 등 가치 이동 엔드포인트와 정상 파라미터 확인 → `baseline_ops`
- [ ] 소유자 필드명(`owner_id` 등) 확인 → `entitlement.owner_field`

---

## 1. 모듈 의존 순서 (전체 관통)

방향은 "먼저 만들어야 하는 것 → 그것에 의존하는 것". 같은 줄은 서로 독립이라
순서 무관하게 만들 수 있다.

```
config.py
  │
  ├─▶ money.py  (Decimal + Currency, 그 외 의존 없음)
  │
  ▼
models/core.py  (Verdict, Principal, Endpoint)
  │
  ├─▶ models/state.py    (ObservedState, StateDelta — money.py 사용)
  ├─▶ models/knowledge.py (Fact, Lesson, Entitlement)
  └─▶ models/finding.py  (CandidateFinding, ExecutionResult, Finding
                            — state.py, knowledge.py, money.py 사용)
  │
  ▼
llm/base.py  (LLMClient 프로토콜, models 의 pydantic 스키마만 참조)
  ├─▶ llm/anthropic_.py
  ├─▶ llm/openai_.py
  └─▶ llm/factory.py  (config.py + 위 셋)
  │
  ▼
storage/db.py       (JSONL, models 의 컬렉션 타입만 참조)
knowledge/sensitive.py  (등급 사전 — 정적 데이터, 의존 없음. storage 보다 먼저 필요)
  │
  ▼
storage/evidence.py (db.py + knowledge/sensitive.py)
storage/run_store.py (db.py + config.py)
  │
  ▼
targets/loader.py + targets/configs/*.yaml  (models, config)
probing/session.py  (targets/loader — auth 흐름 실행)
  │
  ▼
probing/finance/state.py  (session.py + targets 의 observation 설정 + models/state + money)
oracles/base.py           (models/core.Verdict 만)
  │
  ├─▶ oracles/generic.py     (base 만, LLM·네트워크 호출 금지)
  ├─▶ oracles/value.py       (base + money + models/state)   INV-1A, INV-1B
  ├─▶ oracles/idempotency.py (base + models/finding)          INV-4
  └─▶ oracles/amount.py      (base + money)                   INV-6
  │
  ▼
knowledge/schema.py  (models 참조)
knowledge/graph.py   (schema + models/knowledge.Entitlement, networkx,
                       entitled_*/observed_* 분리)
  │
  ▼
recon/discovery.py    (targets/loader + session)
recon/fingerprint.py  (discovery + session, passive 만)
knowledge/inference.py (models/knowledge.Fact)
recon/entitlement.py  (graph + targets 의 entitlement 설정 + ENTITLEMENT_RULES)
recon/mapper.py       (discovery + fingerprint + entitlement → graph 오케스트레이션)
  │
  ▼
memory/working.py    (의존 없음, 단기 컨텍스트)
memory/semantic.py   (models/knowledge.Fact, confidence 필드)
memory/episodic.py   (models/finding + models/knowledge.Lesson)
memory/summarizer.py (semantic + episodic, 결정적 압축 — LLM 미사용, 3k 토큰 상한)
  │
  ▼
knowledge/baseline.py  (probing/finance/state.py + models/finding.ExpectedDelta
                         + money + targets 의 baseline_ops + session)
  │
  ▼
oracles/authz.py       (graph 의 entitled_* + base)          INV-2
oracles/exposure.py    (graph + authz 결과 AND 조합)          INV-9
templates/attacks.yaml (개념만, 의존 없음 — procedure/payload 금지)
  │
  ▼
planning/prompts.py     (templates + models)
planning/candidates.py  (graph + oracles/registry + templates + strategy)
oracles/registry.py     (value/authz/idempotency/amount/generic/exposure
                          + graph + baseline, layers 로 필터링)
  │
  ▼
planning/planner.py     (llm/base + models/finding.CandidateFinding + prompts)
planning/prioritizer.py (memory/episodic)
  │
  ▼
probing/generic/web.py  (session)                SQLi/XSS/PathTraversal
probing/generic/api.py  (session + graph)         BOLA/BFLA/MassAssignment/JWT
probing/generic/external.py (인터페이스만, 실연동 금지)
probing/executor.py     (session + finance/* + generic/* + config 의 safety 상한)
oracles/verifier.py     (registry — candidate+result → Finding 판정)
  │
  ▼
agent/budget.py       (config + llm.Usage)
agent/phase.py         (타입/상수)
agent/strategy.py      (memory + graph 상태 기반 STRATEGIES 선택)
agent/termination.py   (budget + memory + graph)
  │
  ▼
agent/loop.py  (위 전체 — bootstrap → recon → entitlement → baseline → 순환)
  │
  ▼
oracles/concurrency.py (money + models/state, config 의 RACE_TRIALS/MAX_CONCURRENT) INV-1C
  │
  ▼
reporting/repro.py      (storage — VIOLATION 재현 스크립트)
reporting/bugbounty.py  (storage)
reporting/report.py     (run_store manifest — VVR/Precision 계산 금지)
  │
  ▼
evaluator/ground_truth.yaml (사람이 사전 조사해 작성)
evaluator/reset_env.py      (에이전트 패키지와 완전 분리)
evaluator/scorer.py         (findings + ground_truth → VVR/Precision/FP/missed)

__main__.py  (loader + llm/factory + agent/loop 를 연결하는 CLI 진입점,
              최상단이지만 실제로는 Day 1부터 스텁으로 계속 갱신)
```

**핵심 관례**: 오라클(`oracles/`)은 이 그래프에서 `llm/`이나 `probing/`,
`storage/`를 향하는 화살표가 없다 — 순수 함수로 유지한다는 §8 규칙을
의존 순서로도 강제하기 위함이다. `agent/`는 항상 최상위에서 나머지를
조립만 한다.

---

## 2. 일별 계획

### Day 1 — 스캐폴딩
**만드는 것**: `config.py`, `money.py`, `models/*`, `llm/*`,
`storage/db.py`, `storage/evidence.py`(+ `knowledge/sensitive.py` 최소 버전),
`storage/run_store.py`, `targets/loader.py` + `configs/vulnbank.yaml`,
`probing/session.py`, `__main__.py`(스텁: bootstrap principals만).

**순서**: config → money → models(core→state/knowledge→finding) →
llm(base→anthropic_/openai_→factory) → knowledge/sensitive(최소) →
storage(db→evidence→run_store) → targets/loader → probing/session →
`__main__.py --smoke`.

**완료 판정**: `python -m finredteam --smoke` 가 주체 3명(attacker_A,
victim_V, merchant_M) 토큰 발급 + `runs/<id>/manifest.json` 생성.
`pytest tests/test_money.py` 통과 (Decimal 연산, 통화 불일치 예외,
`quantize` 반올림).

---

### Day 2 — 금융 오라클 뼈대
**만드는 것**: `probing/finance/state.py`, `oracles/base.py`,
`oracles/generic.py`, `oracles/value.py`(INV-1A, INV-1B),
`oracles/idempotency.py`(INV-4), `oracles/amount.py`(INV-6),
`tests/test_oracles.py`.

**순서**: oracles/base → oracles/{generic, value, idempotency, amount}
(서로 독립, 병렬 작성 가능) → probing/finance/state (session 이용해 실제
잔액 스냅샷 획득) → 테스트는 가짜 `ObservedState`/`StateDelta` 스냅샷으로
작성 (실제 서버 불필요, 순수 함수 검증).

**완료 판정**: `pytest tests/test_oracles.py` 통과. 특히
"INV-1A는 보존되지만 INV-1B는 위반"(음수 송금 시나리오)을 가짜 델타로
재현하는 테스트가 있어야 한다 — §8 "자주 틀리는 지점 1" 검증.

---

### Day 3 — 정찰 · 권한 그래프
**만드는 것**: `knowledge/schema.py`, `knowledge/graph.py`
(`entitled_*`/`observed_*` 분리), `knowledge/inference.py`,
`recon/discovery.py`, `recon/fingerprint.py`, `recon/entitlement.py`,
`recon/mapper.py`, `tests/test_graph.py`.

**순서**: knowledge/schema → knowledge/graph → recon/discovery →
recon/fingerprint → recon/entitlement (ENTITLEMENT_RULES 우선순위:
self_created → explicit_owner_field → normal_flow) → recon/mapper
(오케스트레이션) → knowledge/inference.

**완료 판정**: vuln-bank 대상 실행 시 그래프에 `entitled_*` 간선이
정상 경로(회원가입/대시보드)로만 채워짐을 로그로 확인.
`tests/test_graph.py`에 "`observed_*` 관측이 `entitled_*`를 추가/변경하지
않는다"를 강제하는 테스트 포함 (§18 완료 조건 7 선행 구현).

---

### Day 4 — 베이스라인 · 메모리
**만드는 것**: `knowledge/baseline.py`, `memory/working.py`,
`memory/semantic.py`, `memory/episodic.py`, `memory/summarizer.py`.

**순서**: memory/working → memory/semantic → memory/episodic →
memory/summarizer → knowledge/baseline (`probing/finance/state.py`로
정상 파라미터 실행 전후 스냅샷을 찍어 `BaselineRecord` 기록).

**완료 판정**: `targets/configs/vulnbank.yaml`의 `baseline_ops`(transfer 등)
최소 1건이 실측되어 `storage`의 `baselines` 컬렉션에 기록됨.
`memory/summarizer` 출력이 3k 토큰 이내 (근사 토큰 카운터로 테스트).

---

### Day 5 — 엔드투엔드 1바퀴 (★분기점)
**만드는 것**: `templates/attacks.yaml`(F2, F4, G-SQLI 최소 3종),
`planning/prompts.py`, `planning/candidates.py`, `oracles/registry.py`,
`planning/planner.py`, `planning/prioritizer.py`, `probing/executor.py`,
`oracles/verifier.py`.

**순서**: templates/attacks.yaml → planning/prompts → oracles/registry →
planning/candidates → planning/planner (LLM 호출, `complete_json` 만 사용) →
planning/prioritizer → probing/executor → oracles/verifier.

**완료 판정**: 음수 금액 송금 후보 하나를 수동으로 만들어 실행했을 때
`Finding(verdict=VIOLATION, invariant=INV-1B, expected_delta.provenance=
"baseline_observed")`가 나온다. `provenance != baseline_observed`인
후보는 이 시점부터 절대 VIOLATION으로 판정되지 않아야 한다
(§6, §18 완료 조건 8 — 여기서 반드시 테스트로 고정).

---

### Day 6 — 에이전트 순환 (★정체성)
**만드는 것**: `agent/budget.py`, `agent/phase.py`, `agent/strategy.py`,
`agent/termination.py`, `agent/loop.py`.

**순서**: agent/budget → agent/phase → agent/strategy → agent/termination
→ agent/loop (Day 1~5 산출물을 §13 순서대로 조립: BOOTSTRAP → RECON →
ENTITLEMENT → BASELINE → 순환 루프).

**완료 판정**: vuln-bank 대상 실행이 최소 2라운드 이상 순환하며
(a) 라운드마다 그래프 간선 수가 증가하고, (b) 전략이 최소 1회
`BROAD_SWEEP` → 다른 전략으로 전환되는 로그가 남는다.
종료 사유가 매니페스트에 기록된다.

---

### Day 7 — Generic Layer · F4 · 리포트 · 채점 분리
**만드는 것**: `probing/generic/web.py`, `probing/generic/api.py`,
`probing/generic/external.py`(인터페이스만), `oracles/exposure.py`(INV-9),
`oracles/concurrency.py`(INV-1C, F4), `oracles/authz.py`(INV-2, 이 시점에
graph 의 entitled_* 가 안정화되어 있으므로 여기서 완성),
`reporting/report.py`, `reporting/bugbounty.py`, `reporting/repro.py`,
`evaluator/reset_env.py`, `evaluator/scorer.py`,
`evaluator/ground_truth.yaml`(사람이 사전 조사해 작성).

**순서**: oracles/authz → oracles/exposure → oracles/concurrency →
probing/generic/{web, api} → probing/generic/external(스텁) →
reporting/repro → reporting/bugbounty → reporting/report →
evaluator/ground_truth.yaml(수기 작성) → evaluator/reset_env →
evaluator/scorer.

**완료 판정**: 리포트가 `by_layer`(finance/generic) 분리 집계로 출력되고
`verification_yield`만 자체 계산(VVR 아님). F4가 "5회 중 N회 재현" 형식으로
보고된다. `python -m finredteam.evaluator.scorer runs/<id>`가 **별도
프로세스로** GT 대비 F-VVR/Generic-VVR/Precision/FP/missed 를 출력한다.

---

## 3. 우선순위 (시간이 부족할 때)

`instruction.md` §17 그대로: **Day 5 > Day 6 > Day 4(baseline) > Day 7.**
범위를 줄일 땐 Generic Layer를 SQLi 하나로, F4를 통째로 빼는 것은 허용.
`money.py`, `entitled_*/observed_*` 분리, `expected_delta.provenance` 강제는
어떤 경우에도 빼지 않는다 — 판정의 정당성이 걸려 있다.

각 Day 는 실행/테스트로 완료를 확인한 뒤에만 다음 Day로 넘어간다.
커밋은 Day 단위, 메시지에 해당 Day의 완료 판정 결과를 적는다.
