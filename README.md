# FinRedTeam — 범용 + 금융 특화 Red Teaming Agent (ReAct)

`instruction.md`의 계약을 구현한다. 대상은 로컬 Docker에서만 실행되는
의도적 취약 교육용 뱅킹 앱(vuln-bank). Black-box 정찰 → 그래프 모델링 →
후보 도출 → 오라클 검증 → Verified Finding. 지표(VVR 등)는 `evaluator/`에서만
계산하고 에이전트는 계산하지 않는다.

**루프 구조는 ReAct 다.** 원래 `instruction.md` §13 pseudocode 는 코드가
전략을 배치로 정해 후보를 일괄 나열하는 결정적 파이프라인이었는데, 사용자
요청으로 "다음에 뭘 할지"를 LLM 이 매 스텝 스스로 판단하는 구조로 다시
설계했다. 단, **판정은 여전히 100% 코드**다 — 이건 협상 대상이 아니었다
(아래 "ReAct 로 바뀌어도 안 바뀐 것" 참고).

이 문서는 **아키텍처**와 **현재 구현 상태**를 다룬다. 설계 이유·불변식의
원본은 `instruction.md`이며 충돌 시 그쪽이 우선한다 (단, 루프 구조는
이 문서가 최신이다 — instruction.md 는 애초의 배치 파이프라인 설명 그대로
남아 있다).

---

## 1. 아키텍처 — ReAct 루프

```
        ┌───────────────────────────────────────────────────────┐
        │  BOOTSTRAP → RECON(passive) → ENTITLEMENT 확정 → BASELINE 사전학습 │
        └───────────────────────────────────────────────────────┘
                                  │
                                  ▼
        ┌──────────────────── 매 스텝 ─────────────────────────┐
        │                                                      │
        │   ① Observation 조립 (코드)                           │
        │      - 최근 스텝 스크래치패드(누적, 최근 6개)             │
        │      - 그래프 요약 · 예산 · 힌트(strategy.suggest_nudge) │
        │      - memory summarizer 출력 (3k 토큰)                 │
        │                                                      │
        │   ② LLM 호출 (딱 1번) → {thought, action, action_input} │
        │                                                      │
        │   ③ 액션 디스패치 (코드) — 판정은 항상 코드가 만든다        │
        │                                                      │
        │   ④ 스크래치패드에 append, ①로 복귀                     │
        │                                                      │
        └──────────────────────────────────────────────────────┘
                                  │
              MAX_ROUNDS/MAX_TOKENS_TOTAL 도달 또는 LLM 의 finish 요청
                                  ▼
                         종료 사유를 manifest 에 기록
```

### 액션 어휘 (`planning/actions.py`)

LLM 이 고를 수 있는 건 이 5개뿐이다 — 자유 텍스트 경로·페이로드를 직접 쓰게
하지 않는다.

| 액션 | 입력 | 부작용 | Observation |
|---|---|---|---|
| `observe_graph` | (없음) | 없음 | 그래프 요약(`planning/candidates.py::graph_menu`) — 알려진 계좌, entitled_own |
| `recall_memory` | `query` | 없음 | 매칭된 사실·교훈 |
| `learn_baseline` | `op` | 정상 파라미터 실행 1회 | 실측 완료 여부 (금액 원문 아님) |
| `propose_candidate` | `attack_class, principal_id, target, params` | **실제 요청 실행** | `verdict, invariant, reason` (오라클이 만든 문장) |
| `finish` | `reason` | 없음 | 루프 종료 요청 (예산 한도가 항상 우선) |

`propose_candidate`의 `target`은 그래프가 실제로 아는 노드 id
(`"account:1234567890"`)여야 한다 — `planning/candidates.py::resolve_skeleton`
이 존재 여부를 검증하고, 없으면 에러 문자열을 그대로 Observation 으로
돌려줘서 LLM 이 스스로 고치게 한다. `invariants`(오라클 목록)는 LLM 이
정하지 않는다 — `attack_class` 로부터 `templates/attacks.yaml` 에서
자동으로 끌어온다(§설계원칙 3).

### ReAct 로 바뀌어도 안 바뀐 것

- **LLM 은 액션만 고른다. 판정은 `oracles/verifier.py`(순수 함수)만 한다.**
  `propose_candidate` 의 Observation 에 들어가는 verdict/reason 은 전부
  코드가 계산한 값이고, LLM 은 그걸 읽기만 한다(§설계원칙 1).
- `expected_delta` 는 여전히 `knowledge/baseline.py` 실측값에서만 나온다.
  LLM 이 고른 파라미터는 baseline 에 선형으로 대입될 뿐, 정답을 LLM 이
  만들지 않는다(§6).
- Black-box, `entitled_*`/`observed_*` 분리, `Money`/`Decimal`, 안전장치
  (`config.py`) 전부 그대로.
- `agent/termination.py` 의 예산·포화·수렴 조건은 LLM 의 `finish` 요청과
  무관하게 항상 최종 결정권을 갖는다 — LLM 이 계속 하자고 해도 예산이
  끝나면 끝난다.

### ReAct 로 바뀌면서 달라진 것 (트레이드오프)

- `agent/strategy.py::choose()`(배치 전략 강제 선택)는 루프에서 더 이상
  호출되지 않는다 — 대신 `suggest_nudge()`가 같은 신호(그래프 성장, 최근
  성공/실패, 미탐색 레이어)로 힌트 한 줄만 Observation 에 끼워 넣는다.
  "전략 전환"이 강제 배치가 아니라 LLM 의 Thought 로 표현된다.
- `planning/candidates.py::enumerate_skeletons`(일괄 나열)가
  `resolve_skeleton`(LLM 이 고른 대상 하나를 검증)으로 바뀌었다.
- `planning/planner.py::refine()`(스켈레톤 일괄에 LLM 파라미터 채움)이
  `build_candidate()`(이미 LLM 이 고른 파라미터를 조립)로 바뀌었다 —
  `_expected_delta_for`(baseline 선형 대입 + payer/payee 역할 재매핑)는
  그대로다.
- `planning/prioritizer.py::rank_and_dedup`(배치 순위)가
  `already_tried_and_failed()`(정확히 같은 조합 재시도 감지)로 바뀌었다.
- 재현성이 낮아지고 스텝당 LLM 호출이 1회씩 필요해 토큰 비용이 늘어난다.
- `storage/db.py` 에 `steps` 컬렉션이 추가됐다(§10 의 원래 12개 컬렉션
  목록에는 없던 것 — ReAct 트레이스 감사용으로 사용자 요청에 따라 추가).

---

## 2. 모듈 구조 (현재)

```
finredteam/
├── __main__.py          CLI — --smoke | --fake-llm | 실제 LLM
├── config.py             안전장치 상수(ALLOWED_HOSTS, MAX_AMOUNT, ...) + LLMConfig/RunConfig
├── money.py               Decimal 전용 Money/Currency — float 유입을 타입 레벨에서 차단
├── jsonpath.py            targets/*.yaml 용 최소 JSONPath 서브셋
│
├── models/                core(Verdict/Principal/Endpoint), state(ObservedState/StateDelta),
│                          finding(CandidateFinding/ExpectedDelta/ExecutionResult/Finding),
│                          knowledge(Fact/Lesson/Entitlement)
│
├── llm/                   base(LLMClient 프로토콜) · anthropic_ · openai_ · factory
│                          · fake(가짜 LLM — API 키 없이 ReAct 배관 시험용)
│
├── storage/                db(JSONL, steps 포함 13개 컬렉션) · evidence(민감정보 redaction)
│                          · run_store(manifest.json)
│
├── targets/                loader.py + configs/vulnbank.yaml(사람이 실측해 채움)
│
├── probing/                session(allowlist+Decimal 안전 HTTP) · executor(실행)
│   ├── finance/            state(잔액 관측) · burst(F4 동시요청 + 재시행)
│   └── generic/             web(SQLi boolean-diff) · api(JWT alg=none 위조) · external(인터페이스만)
│
├── recon/                  discovery · fingerprint(passive) · entitlement(self_created 등) · mapper
│
├── knowledge/               graph(entitled_*/observed_* 분리, networkx) · baseline(실측 기준선)
│                          · schema · sensitive(민감등급) · inference(관측→Fact)
│
├── memory/                  working(단기) · semantic(Fact) · episodic(시도이력·Lesson) · summarizer(3k 토큰)
│
├── planning/
│   ├── actions.py          ★ ReAct 액션 스키마 (observe_graph/recall_memory/learn_baseline/
│   │                          propose_candidate/finish)
│   ├── candidates.py        AttackTemplate 로더 + resolve_skeleton(LLM 이 고른 target 검증) + graph_menu
│   ├── planner.py           baseline 선형 대입(_expected_delta_for) + build_candidate
│   ├── prompts.py           ReAct system/user 프롬프트 조립
│   └── prioritizer.py       already_tried_and_failed (중복 재시도 감지)
│
├── oracles/                 base · value(INV-1A/1B) · amount(INV-6) · idempotency(INV-4)
│                          · concurrency(INV-1C) · authz(INV-2, graph 필요) · generic(SQLi/XSS/BOLA/JWT)
│                          · registry(invariant→함수, INV-2 만 GRAPH_REGISTRY 로 별도 디스패치)
│                          · verifier(최종 Finding 조립)
│                          — ★ 전부 순수 함수. LLM·네트워크 import 를 소스 검사로 강제(tests/test_oracles.py)
│
├── agent/
│   ├── budget.py            토큰/라운드 예산
│   ├── phase.py             Phase 상수
│   ├── strategy.py           STRATEGIES(레거시, 미사용) + suggest_nudge(ReAct 힌트)
│   ├── termination.py        예산/포화/수렴/대상이상 4조건, LLM 의 finish 요청보다 항상 우선
│   ├── react.py              ★ 메인 루프 — AgentContext, Step, 액션 디스패치
│   └── loop.py               react.py 로 위임하는 얇은 진입점 (하위 호환용 이름 유지)
│
├── templates/attacks.yaml   개념만 — procedure/payload/endpoint 금지
│
├── reporting/                report(VVR 계산 안 함) · repro(curl 재현) · bugbounty(서술)
│
└── evaluator/                scorer(GT 대비 채점 — 여기서만 VVR/Precision) · ground_truth.yaml
                             · reset_env(에이전트와 완전 분리)
```

**핵심 관례**: `oracles/`는 `llm/`·`probing/`·`storage/`를 import 하지 않는다
— 순수 함수라는 §8 규칙을 소스 검사 테스트(`test_oracles_are_pure_no_llm_or_network_imports`)로
강제한다. `agent/react.py`는 항상 최상위에서 나머지를 조립만 한다.

---

## 3. 구현 현황

Day 1~7 전부 구현 완료 + 실제 vuln-bank(Commando-X/vuln-bank, 로컬 Docker) 대상
라이브로 검증. `vulnbank-target/`은 사람이 clone 해 손으로 공격해서 실측한
값만 `targets/configs/vulnbank.yaml`에 반영했다(에이전트 코드는 그 소스를
읽지 않는다).

**라이브로 확인된 VIOLATION**:
- **F2/INV-1B** — 음수 송금 시 부호 반전 (attacker +500, victim -500)
- **F1/INV-2** — `/check_balance/<계좌>` 인증 없이 타인 잔액 조회 (BOLA)
- **G-SQLI** — `/transactions/<계좌>` boolean-based injection + DB 에러 메시지 노출

**F4(경쟁조건)**: 오라클(`oracles/concurrency.py`)과 재시행 메커니즘
(`probing/finance/burst.py`, RACE_TRIALS 반복 + 잔액 회복 + 재현율 집계)은
구현·라이브 실행까지 했으나, 대상이 Flask 비-threaded 개발서버라 이번
실행에서는 재현되지 않음(PASS 로 정확히 판정되는 것 자체가 메커니즘이 올바로
동작한다는 증거).

**ReAct 루프**: `--fake-llm`(결정적 가짜 LLM, API 키 불필요)으로
`observe_graph → propose_candidate(F1) → propose_candidate(F2, 음수) →
propose_candidate(F2, 양수 대조군) → finish` 트레이스가 실제 vuln-bank
대상으로 정상 순환, 스크래치패드 누적, VIOLATION 2건 확정, LLM 스스로
예산 소진 전 종료 요청까지 전부 확인.

**정직하게 밝히는 한계**:
- `resolve_skeleton` 에 F1/F2 만 구현되어 있다. F3(멱등성)/F4(경쟁조건 후보 자동생성)/
  F8(열거) 는 오라클은 있지만 `propose_candidate` 로 아직 낼 수 없다
  (§17 우선순위: "F4는 빠져도 된다" 참고 — 시간 제약으로 다음 작업으로 남김).
- Generic Layer(`probing/generic/*`)는 아직 `propose_candidate` 액션에
  안 붙어 있다 — 별도 스크립트로 직접 호출해서 라이브 검증만 했다.
- OpenAI 프로바이더(`llm/openai_.py`)는 코드는 있지만 실제 API 키로
  라이브 테스트하지 않았다.

---

## 4. 사용법

```bash
# 단위 테스트 (네트워크 불필요)
python -m pytest tests/ -v

# 부트스트랩 + manifest 만 확인 (Day1 범위)
python -m finredteam --target vulnbank --smoke

# API 키 없이 ReAct 루프 배관 확인 (결정적 가짜 LLM)
python -m finredteam --target vulnbank --fake-llm --max-rounds 8

# 실제 LLM 으로 — 진짜 탐색
export ANTHROPIC_API_KEY=sk-ant-...
python -m finredteam --target vulnbank --max-rounds 12

# GT 대비 채점 (에이전트와 완전 분리된 별도 프로세스)
python -m finredteam.evaluator.scorer runs/<run_id>
```

---

## 5. 우선순위 (시간이 부족할 때)

`instruction.md` §17 그대로: **Day 5 > Day 6 > Day 4(baseline) > Day 7.**
`money.py`, `entitled_*/observed_*` 분리, `expected_delta.provenance` 강제,
그리고 지금은 추가로 **"판정은 항상 코드가 한다"(ReAct 로 바뀌어도 불변)**
는 어떤 경우에도 빼지 않는다 — 판정의 정당성이 걸려 있다.
