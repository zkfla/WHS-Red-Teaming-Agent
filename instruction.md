# 프로젝트: FinRedTeam — 범용 + 금융 특화 Red Teaming Agent

## 0. 컨텍스트

인가된 보안 연구용 자율 에이전트를 만든다. 대상은 **의도적으로 취약하게 만들어진 오픈소스 교육용
뱅킹 앱(vuln-bank)** 이며 로컬 Docker에서만 실행된다. 실제 금융 서비스는 대상이 아니다.

### 핵심 계약

> **대상 시스템을 Black-box로 정찰하여 자산·권한 관계를 모델링하고,
> 그 모델을 근거로 취약점 후보(Candidate Finding)를 도출한다.
> 각 후보는 능동적 시험과 오라클로 검증되며,
> 검증을 통과한 것만 Verified Finding으로 확정한다.**

### 프로젝트 정체성 — 범용 Red Teaming + 금융 특화 (+α)

```
                        Agent
                          │
            ┌─────────────┴─────────────┐
            ▼                           ▼
   Generic Security Testing      Finance Testing
   SQLi / XSS / SSRF / JWT /     F1 / F2 / F3 / F4 / F8
   BOLA / API Misconfig          (권한·금액·멱등성·경쟁·노출)
            │                           │
            └─────────────┬─────────────┘
                          ▼
                       Findings
```

**두 레이어 모두 구현한다.** 지표는 절대 합산하지 않는다(§9 참조).

### 이건 스캐너가 아니라 에이전트다 — 반드시 구현할 4가지

| # | 능력 | 위치 | 없으면 |
|---|---|---|---|
| **1** | **관측 → 재계획 순환** | `agent/loop.py` | 그래프를 한 번 만들고 끝 → 정적 스크립트 |
| **2** | **메모리 (사실·실패·학습)** | `memory/` | 같은 실패 반복 |
| **3** | **적응 (실패→전략 전환)** | `agent/strategy.py` | 막히면 그냥 다음 후보로 |
| **4** | **자율적 종료 판단** | `agent/termination.py` | 고정 횟수만 돌고 끝 |

```
        ┌──────────────────────────────────────────────┐
        ▼                                              │
  ① 정찰(passive) ─▶ ② 지식 갱신 ─▶ ③ 계획 ─▶ ④ 능동시험 ─┤
                          ▲                    │        │
                          └──── ⑥ 학습 ◀── ⑤ 검증 ───────┘
                                              │
                                     Verified Finding
```

### 설계 원칙 8가지 (위반 금지)

1. **LLM은 Candidate까지. Verified 확정은 결정적 코드가 한다.**
2. **Black-box only — 정찰과 시험 모두.** DB·로그·소스코드 읽기 금지.
   관측 수단이 없으면 `UNKNOWN`. 환경 초기화·채점은 `evaluator/`(에이전트 밖).
3. **Candidate는 실행 *전에* 검증할 오라클을 선언한다.**
4. **판정 4종**: `PASS`/`VIOLATION`/`ANOMALY`/`UNKNOWN`. `VIOLATION`만 Verified.
5. **후보는 그래프에서 유도한다.**
6. **모든 증거는 `storage/evidence.py`를 경유한다.** 직접 파일을 쓰지 마라.
7. **★ 금액은 `Decimal`이다. `float` 금지.** (§4)
8. **★ 오라클의 기준값(`expected_delta`)은 LLM이 만들지 않는다.** (§6)

---

## 1. 기술 스택 (고정)

```
Python 3.11+   httpx   pydantic v2   rich   pyyaml   pytest   networkx
LLM: anthropic + openai  (둘 다. §2 참조)
```
저장은 `storage/` 경유, 물리 저장은 `runs/<run_id>/`. **외부 DB 금지.**
테스트는 `oracles/`, `memory/`, `storage/evidence.py`, `money`에 작성.

---

## 2. LLM 추상화 (`llm/`) ★

**특정 벤더에 묶지 마라.** Anthropic과 OpenAI를 모두 지원하고, 설정으로 교체 가능해야 한다.

```
llm/
├── base.py          # LLMClient 프로토콜
├── anthropic_.py    # Claude
├── openai_.py       # GPT
└── factory.py       # 설정 → 클라이언트
```

```python
# llm/base.py
class LLMClient(Protocol):
    def complete_json(self, system: str, user: str,
                      schema: type[BaseModel],
                      temperature: float = 0.3) -> list[BaseModel]:
        """구조화 출력 전용. 이 메서드 하나만 쓴다.
        내부에서 파싱 실패 시 1회 재시도, 그래도 실패하면 빈 리스트."""

    @property
    def usage(self) -> Usage: ...     # tokens_in, tokens_out, cost_estimate
```

```yaml
# config
llm:
  provider: anthropic        # anthropic | openai
  model: claude-sonnet-4-6   # 또는 gpt-5 등
  temperature: 0.3
  max_tokens: 4096
```

**구현 지침:**
- **호출부는 `LLMClient`만 안다.** `planner.py`, `recon/mapper.py`에서 벤더 SDK를 직접 import 하지 마라.
- 두 벤더의 구조화 출력 방식이 다르다. `complete_json` 내부에서 흡수한다.
  - Anthropic: 시스템 프롬프트로 JSON 강제 + 코드펜스 제거 후 파싱
  - OpenAI: `response_format={"type":"json_schema"}` 사용 가능하면 사용, 아니면 동일 방식
- **비용·토큰 집계는 공통 `Usage`로 정규화**한다. 벤더별 가격표는 `factory.py`에 둔다.
- 벤더 교체가 결과를 바꾸는지 확인할 수 있게 **매니페스트에 provider/model을 기록**한다.

---

## 3. 프로젝트 구조

```
finredteam/
├── __main__.py
├── config.py
│
├── money.py                     # ★ Decimal + Currency
├── llm/                         # ★ base.py anthropic_.py openai_.py factory.py
│
├── models/
│   ├── core.py                  # Verdict, Principal, Endpoint
│   ├── state.py                 # ObservedState, Snapshot, StateDelta
│   ├── finding.py               # CandidateFinding, ExecutionResult, Finding
│   └── knowledge.py             # Fact, Lesson, Entitlement
│
├── agent/
│   ├── loop.py                  # 메인 순환
│   ├── phase.py                 # RECON → MODEL → PROBE → DEEPEN
│   ├── strategy.py              # 적응
│   ├── termination.py
│   └── budget.py
│
├── memory/
│   ├── working.py
│   ├── episodic.py
│   ├── semantic.py
│   └── summarizer.py
│
├── knowledge/
│   ├── graph.py                 # entitled_* / observed_* 이중 관계
│   ├── schema.py
│   ├── baseline.py              # ★ 정상 동작 기준선 (expected_delta 출처)
│   ├── workflow.py              # 2차, 스텁
│   ├── sensitive.py
│   └── inference.py
│
├── recon/
│   ├── discovery.py
│   ├── fingerprint.py
│   ├── entitlement.py           # ★ 정상 경로로 권한 확정
│   └── mapper.py
│
├── planning/
│   ├── planner.py
│   ├── candidates.py
│   ├── prioritizer.py
│   └── prompts.py
│
├── probing/
│   ├── executor.py
│   ├── session.py
│   ├── generic/
│   │   ├── web.py               # SQLi, XSS, Path Traversal
│   │   ├── api.py               # BOLA, BFLA, Mass Assignment, JWT
│   │   └── external.py          # 2차, 인터페이스만
│   └── finance/
│       ├── http.py
│       ├── state.py
│       ├── burst.py
│       └── enumerate.py
│
├── oracles/
│   ├── base.py
│   ├── generic.py               # 시그니처 + 부작용
│   ├── value.py                 # INV-1A, INV-1B
│   ├── authz.py                 # INV-2
│   ├── idempotency.py           # INV-4
│   ├── concurrency.py           # ★ INV-1C — F4 전용
│   ├── amount.py                # INV-6
│   ├── exposure.py              # INV-9 + F8 AND 조합
│   ├── transition.py            # 2차, 스텁
│   ├── limit.py                 # 2차, 스텁
│   ├── registry.py
│   └── verifier.py
│
├── storage/
│   ├── db.py
│   ├── evidence.py              # 민감정보 중앙 통제
│   └── run_store.py
│
├── reporting/
│   ├── report.py                # ★ VVR 계산 안 함 (§9)
│   ├── bugbounty.py
│   └── repro.py
│
├── templates/attacks.yaml       # 개념만. 실행 절차 금지
│
├── targets/
│   ├── loader.py
│   └── configs/{vulnbank,finlab}.yaml
│
├── evaluator/                   # ★ Agent와 완전 분리
│   ├── reset_env.py
│   ├── scorer.py                # ★ VVR/Precision 계산은 여기서만
│   └── ground_truth.yaml
│
├── tests/
└── runs/
```

---

## 4. 금액 표현 (`money.py`) ★ float 금지

**금융 시스템에서 `float`는 오답을 만든다.** `0.1 + 0.2 != 0.3`이고,
그 오차가 그대로 `INV-1A`(가치 보존) 오탐이 된다.

```python
from decimal import Decimal, ROUND_HALF_UP

class Currency(BaseModel):
    code: str                  # "USD", "KRW"
    minor_unit: int            # USD=2, KRW=0
    model_config = {"frozen": True}

class Money(BaseModel):
    amount: Decimal
    currency: Currency
    model_config = {"frozen": True}

    @classmethod
    def parse(cls, raw, currency: Currency) -> "Money":
        """API 응답값을 Decimal 로 변환. float 를 절대 경유하지 마라."""
        return cls(amount=Decimal(str(raw)), currency=currency)   # str() 경유 필수

    def __add__(self, other): self._assert_same_currency(other); ...
    def __sub__(self, other): ...

    def _assert_same_currency(self, other):
        if self.currency != other.currency:
            raise CurrencyMismatch(...)      # ★ 다른 통화 산술은 예외

    def quantize(self) -> "Money":
        exp = Decimal(1).scaleb(-self.currency.minor_unit)
        return Money(self.amount.quantize(exp, ROUND_HALF_UP), self.currency)
```

**규칙 5가지:**
1. `ObservedState.balances`, `StateDelta.balance_delta`, `expected_delta` 전부 `Money`를 쓴다.
2. **JSON 파싱 시 `float`를 경유하지 마라.** `json.loads(..., parse_float=Decimal)`
   또는 응답을 문자열로 받아 `Decimal(str(x))`.
3. **통화가 다른 값끼리 더하지 마라.** 예외를 던진다.
   다중 통화 계좌가 섞이면 `INV-1A`는 **통화별로 따로 검사**한다.
4. **비교 허용오차는 통화의 최소 단위**로 정한다. `tol = 10^-minor_unit`.
   `0.01` 같은 상수를 하드코딩하지 마라 (KRW는 소수점이 없다).
5. 통화를 알 수 없으면 어댑터의 `default_currency`를 쓰고, 그것도 없으면
   해당 필드를 `missing`으로 표시한다 → 오라클이 `UNKNOWN`.

**★ 환율 주의**: F2는 "클라이언트 제어 환율"을 다룬다.
환산 결과를 검증할 때 **에이전트가 환율을 계산하지 마라.**
서버가 정상 경로에서 보여준 환율(`knowledge/baseline.py`)과 비교하는 것이지,
우리가 "맞는 환율"을 알고 있는 게 아니다.

---

## 5. 권한 관계 — `entitled_*` / `observed_*` 분리 ★

**v3의 문제**: 그래프에 `can_read` 하나만 두면 순환 논리가 생긴다.
"A가 조회에 성공했으니 `can_read` 기록" → 나중에 "A는 `can_read` 권한이 있으니 정상"
→ **BOLA를 스스로 정당화해버린다.**

**해결: 두 종류를 완전히 분리한다.**

| 종류 | 의미 | 출처 | 오라클에서 |
|---|---|---|---|
| **`entitled_*`** | **가져야 할** 권한 | 정상 경로·프로비저닝으로 확정 | **기준(ground truth) 역할** |
| **`observed_*`** | **실제로 통한** 접근 | 시험 중 관측 | **검사 대상** |

```python
class Entitlement(BaseModel):
    """정상 경로에서 확정된 '가져야 할 권한'."""
    principal: str
    relation: Literal["entitled_own","entitled_read","entitled_write",
                      "entitled_transfer_from","entitled_approve"]
    obj: str
    source: Literal["provisioned","self_created","explicit_owner_field",
                    "normal_flow"]           # ★ 어떻게 확정했나
    evidence_ref: str
```

### `recon/entitlement.py` — 권한 확정 규칙 (결정적, 우선순위 순)

```python
ENTITLEMENT_RULES = [
  # 1. 우리가 만든 것 — 가장 강한 근거
  ("self_created",        "회원가입/계좌개설로 우리가 생성한 객체 → entitled_own"),
  # 2. 서버가 명시한 소유자
  ("explicit_owner_field","응답에 owner_id == 요청 주체 → entitled_own"),
  # 3. 정상 UI 흐름에서 도달 가능
  ("normal_flow",         "로그인 직후 대시보드 등 기본 흐름에서 반환된 객체 → entitled_read"),
]
```

**핵심: `observed_*`는 절대 `entitled_*`로 승격되지 않는다.**
"조회가 됐다"는 사실은 권한의 근거가 아니라 **검사해야 할 관측**이다.

### `INV-2` 재정의

```python
def check_inv_2(cand, result, graph):
    exposed = objects_in_response(result)
    for obj in exposed:
        if graph.has_entitlement(cand.principal, obj, kind="read"):
            continue                                   # 정상
        if graph.entitlement_unknown(obj):
            return unknown(f"{obj} 의 정상 권한을 확정하지 못함")
        return violation(f"{cand.principal} 는 {obj} 에 대한 entitlement 없음")
    return passed()
```

**entitlement를 확정하지 못한 객체는 `UNKNOWN`이다.** 위반으로 처리하지 마라.
공동계좌·법인계좌처럼 정당한 접근일 수 있다.

부트스트랩에서 **각 주체의 entitlement를 최대한 확정**해두는 것이 F1 성능을 좌우한다.
`recon` 단계에서 정상 경로만 밟아 entitlement를 채우고, 그 다음에 시험을 시작한다.

---

## 6. `expected_delta` provenance ★ — LLM이 정답지를 쓰지 못하게

**v3의 문제**: `expected_delta`를 LLM이 채웠다.
그러면 **LLM이 정답지를 쓰고 코드가 그 정답지로 채점**하는 셈이라,
"판정은 코드가 한다"는 원칙이 형식적으로만 지켜진다.

**해결: 기준값의 출처를 명시하고, 신뢰할 수 있는 출처가 없으면 판정하지 않는다.**

```python
class ExpectedDelta(BaseModel):
    deltas: dict[str, Money]
    provenance: Literal[
        "baseline_observed",   # ★최강 — 동일 연산을 정상 파라미터로 실행해 실측
        "adapter_declared",    # 어댑터 설정에 명시된 시맨틱
        "spec_derived",        # OpenAPI/문서에서 유도
        "llm_inferred",        # LLM 추론 — 판정 근거로 쓰지 않음
    ]
    baseline_ref: str | None = None   # baseline_observed 인 경우 실측 기록 id
    note: str = ""
```

### `knowledge/baseline.py` — 정상 동작 기준선 ★

```python
class BaselineRegistry:
    """공격 전에 '정상 연산'을 한 번 실행해 상태 변화를 실측해둔다."""

    def learn(self, endpoint, principal, normal_params) -> BaselineRecord:
        """예: POST /transfer {amount: 10, to: victim} 을 정상 실행
           → 실측 델타 {attacker: -10, victim: +10} 를 기록
           → 이후 amount=-500 시험의 expected_delta 를 이 시맨틱에서 유도"""

    def derive_expected(self, endpoint, params) -> ExpectedDelta | None:
        """기준선이 있으면 파라미터에 비례해 기대 델타를 계산.
           없으면 None → 오라클은 UNKNOWN."""
```

**루프 순서가 바뀐다: 시험 전에 정상 경로를 먼저 밟는다.**
```
RECON → ENTITLEMENT 확정 → BASELINE 학습 → PROBE 시작
```

### 판정 규칙

```python
def check_inv_1b(cand, result, baselines):
    exp = cand.expected_delta
    if exp.provenance == "llm_inferred":
        return unknown("기준값이 LLM 추론이라 판정 불가 — baseline 학습 필요")
    if exp.provenance not in ("baseline_observed", "adapter_declared", "spec_derived"):
        return unknown("신뢰 가능한 기준값 없음")
    ...
```

**LLM은 `expected_delta`를 제안할 수 있지만, 그 값으로는 판정하지 않는다.**
LLM 제안은 "이 엔드포인트의 시맨틱이 이럴 것 같다"는 힌트일 뿐이고,
실제 기준은 **baseline 실측**에서 나온다. 이 구분을 코드로 강제하라.

> 이게 v4에서 가장 중요한 수정이다. 이걸 안 하면
> **"LLM이 스스로 답을 정하고 스스로 채점하는 시스템"** 이 되어 프로젝트 전제가 무너진다.

---

## 7. F4 Race Condition — `INV-1C` 별도 오라클 ★

**v3의 문제**: F4를 `INV-1A`(가치 보존)로만 검사했는데 부족하다.
경쟁 조건은 **성공 응답 수와 실제 상태 변화의 불일치**가 본질이고, **확률적**이다.

```python
# oracles/concurrency.py
def check_inv_1c(cand, result) -> Finding:
    """INV-1C: 동시 실행 정합성
       성공 응답 N건 × 단위 금액 == 실제 상태 변화 여야 한다."""

    ok      = [r for r in result.responses if is_success(r)]
    n_ok    = len(ok)
    unit    = cand.unit_amount                     # Money
    expected= unit * n_ok
    actual  = abs(result.delta.balance_delta[cand.subject_account])

    if result.before.has_missing() or result.after.has_missing():
        return unknown("잔액 관측 실패")
    if n_ok == 0:
        return passed("성공 응답 없음 — 동시성 제어 정상")

    if actual < expected - tol(unit.currency):
        # 성공은 N건인데 차감은 그보다 적음 → 돈이 생성됨
        return violation(
            f"성공 {n_ok}건 × {unit} = {expected} 기대, 실제 차감 {actual} "
            f"→ {expected - actual} 만큼 초과 인출")
    if actual > expected + tol(unit.currency):
        return anomaly(f"차감이 성공 건수보다 많음 ({actual} > {expected}) — 이중 차감 의심")
    return passed()
```

### F4 전용 추가 규칙 4가지

**(1) 확률적이므로 반복이 필수다.**
```python
RACE_TRIALS = 5          # 같은 후보를 5회 반복 실행
RACE_THRESHOLD = 1       # 1회라도 VIOLATION 이면 취약
```
1회 실패를 미탐으로 세지 마라. 결과는 **"5회 중 3회 재현"** 형태로 기록한다.

**(2) 매 시행 전 상태 복원이 필요하다.**
잔액이 이미 소진되면 다음 시행이 무의미하다.
**하지만 에이전트는 `env_snapshot_restore`를 호출할 수 없다**(Black-box 원칙).
→ 대신 **잔액을 정상 경로로 회복**시키거나(입금·송금), 회복 불가면 `UNKNOWN`으로 보고한다.
   시행 간 복원은 `evaluator/`가 시나리오 사이에 수행한다.

**(3) 사전 조건을 검사한다.**
```python
if current_balance < unit * n_concurrent:
    ...   # 잔액이 충분하면 동시성 결함이 있어도 전부 성공이 정상 → 의미 없는 시험
```
**잔액보다 많이 요청해야 경쟁 조건이 드러난다.** 후보 생성 시 이 조건을 강제하라.

**(4) DoS로 오인되지 않게 한다.**
`MAX_CONCURRENT = 10`, 시행 간 간격 최소 1초. 이 상한을 설정으로 빼지 마라.

**증거 형식:**
```json
{"trials": 5, "violations": 3, "reproduction_rate": 0.6,
 "per_trial": [{"n_success": 8, "expected": "800.00", "actual": "500.00", "verdict": "VIOLATION"}, ...]}
```

---

## 8. 오라클 목록

| ID | 검사 | 대응 | 파일 |
|---|---|---|---|
| `INV-1A` | 가치 보존 — 돈이 생성/소멸되지 않음 (**통화별**) | F2 보조 | `value.py` |
| `INV-1B` | 인가된 가치 흐름 — **baseline 기준** 델타와 일치 | F2 | `value.py` |
| `INV-1C` ★ | 동시 실행 정합성 — 성공 건수 × 단위 == 실제 변화 | **F4** | `concurrency.py` |
| `INV-2` | 권한 경계 — **`entitled_*`** 로 연결되지 않은 객체 접근 | F1, F8 | `authz.py` |
| `INV-4` | 멱등성 — 동일 논리 거래 식별자의 복수 side effect | F3 | `idempotency.py` |
| `INV-6` | 금액 정합성 — 부호·정밀도·서버 결정 환산 | F2 | `amount.py` |
| `INV-9` | 식별자 예측불가 (**단독 위반 아님**) | F8 | `exposure.py` |

**⚠️ 자주 틀리는 지점**

1. **`INV-1A` ≠ `INV-1B`.** 음수 송금(`A:+500, V:-500`)은 **총합이 보존된다.**
   보존만 검사하면 못 잡는다. F2의 주 오라클은 `INV-1B`다.
2. **`INV-1A`는 통화별로 따로 검사한다.** 통화가 섞인 합계는 의미가 없다.
3. **`INV-4`는 페이로드가 아니라 논리 거래 식별자 기준.** 같은 커피 3번 결제는 정상이다.
   식별자 필드를 못 찾으면 `UNKNOWN`.
4. **`INV-9` 단독은 취약점이 아니다.** `INV-9 ∧ INV-2 ∧ 남용통제부재` AND 조합.
5. **열거 결과를 외삽하지 마라.** "10회 성공 → 분당 600건" 금지.
6. **`oracles/`는 LLM·네트워크를 호출하지 않는다.** 순수 함수. 테스트로 강제.

---

## 9. 지표 계산은 `evaluator/`에서만 ★

**v3의 문제**: 에이전트가 자기 성능(VVR)을 계산하게 되어 있었다.
그러면 **"몇 건 찾았다"를 스스로 세는 것**이라 GT 대비 정확도가 아니다.
게다가 에이전트가 GT를 알면 Black-box 전제가 깨진다.

**역할 분리:**

| | 하는 일 | 접근 가능 |
|---|---|---|
| **Agent** | Finding 생성. **지표 계산 안 함** | 외부 HTTP + 테스트 계정만 |
| **Evaluator** | **GT 대비 채점** — VVR, Precision, FP Rate | + DB, 로그, GT 정답지 |

```python
# reporting/report.py — 에이전트 측
# ❌ VVR, Precision 계산 금지
# ✅ 아래만 보고한다:
{
  "verified_count": 4,           "candidate_count": 21,
  "anomaly_count": 3,            "unknown_count": 2,
  "verification_yield": 0.19,    # Verified/Candidate — GT 불필요, 자기 진단용
  "by_layer": {"finance": {...}, "generic": {...}},   # ★ 분리 집계
  "by_attack_class": {...},
  "termination_reason": "saturated",
}
```

```python
# evaluator/scorer.py — 에이전트 밖
def score(run_dir, ground_truth) -> ScoreReport:
    """GT 와 대조해 실제 지표를 계산한다."""
    matched = match_findings_to_gt(findings, gt)     # attack_vector + endpoint 기준
    return ScoreReport(
        f_vvr        = len(matched.finance) / len(gt.finance),
        generic_vvr  = len(matched.generic) / len(gt.generic),   # ★ 절대 합산 금지
        precision    = len(matched.all) / len(findings.verified),
        fp_rate      = len(unmatched_on_safe_build),
        missed       = gt.all - matched.all,          # ★ 미탐 목록 — 가장 유용한 출력
    )
```

**실행도 분리한다:**
```bash
python -m finredteam --target vulnbank            # 에이전트. GT 모름
python -m finredteam.evaluator.scorer runs/<id>   # 채점. GT 사용
```

**`missed` 목록이 가장 유용한 출력이다.** 왜 못 찾았는지가 다음 개선 방향이 된다.

---

## 10. `storage/` — 모든 증거의 단일 통로

**어떤 모듈도 파일에 직접 쓰지 않는다.** 민감정보 통제를 한 곳에서 강제하기 위해서다.

```python
# storage/evidence.py
def store_response(resp, context) -> EvidenceRef:
    fields = extract_field_names(resp.body)
    level  = classify(fields)                 # knowledge/sensitive.py 등급 사전
    if level in ("CRITICAL", "HIGH"):
        return EvidenceRef(                   # ★ 원문 폐기
            mode="redacted",
            schema=field_schema(resp.body),   # 타입만, 값 없음
            field_presence={f: True for f in fields},
            body_hash=sha256(canonical(resp.body)),   # 동일성 비교용
            size=len(resp.content), status=resp.status_code)
    return EvidenceRef(mode="full", body=truncate(resp.body, 4096), ...)
```

F8 열거 프로브는 타인의 잔액·이름을 실제로 받아온다.
**증거로 저장하면 그 행위 자체가 유출이다.** 필드 존재 여부와 해시만 남긴다.
`body_hash`는 "두 응답이 같은가"를 판정하는 데 필요하고, 내용은 필요 없다.

`db.py`는 파일 기반 경량 저장소(JSONL append + 인덱스).
컬렉션: `requests` `responses` `candidates` `executions` `states` `deltas`
`verdicts` `findings` `facts` `lessons` `entitlements` `baselines`

`run_store.py`는 `manifest.json`에 **LLM provider/model, 종료 사유, 라운드 수,
전략 전환 이력, 토큰·요청 수**를 남긴다. 리포트는 이 매니페스트만 읽는다.

---

## 11. `templates/attacks.yaml` — 개념만. 실행 절차 금지 ★

**잘못 만들면 에이전트가 즉시 스캐너가 된다.**
엔드포인트·페이로드·순서를 박아두면 YAML을 순회 실행하는 룰셋 엔진이 된다.

```yaml
- id: F2
  layer: finance
  name: 금액 무결성 위반
  concept: |
    요청이 의도한 가치 이동 방향·금액과 실제 상태 변화가 일치하지 않는다.
    음수 금액, 정밀도 남용, 클라이언트 제어 환율 등이 원인이 될 수 있다.
  oracle: [INV-1B, INV-6]
  requires_baseline: true          # ★ baseline 없으면 판정 불가
  preconditions:
    - 가치를 이동시키는 엔드포인트가 존재
    - 최소 2개 주체의 잔액을 관측 가능
  agent_must_determine:
    - 어떤 엔드포인트가 가치를 이동시키는가
    - 금액 필드가 무엇인가
    - 어떤 값을 시도할 것인가
    - 어느 주체로 실행할 것인가
  severity_if_violated: HIGH

- id: F4
  layer: finance
  name: 잔액 경쟁 조건
  concept: |
    동시 요청 시 잔액 검사와 차감 사이의 시간차로 초과 인출이 발생한다.
    확률적으로만 재현되므로 반복 시행이 필요하다.
  oracle: [INV-1C]
  requires_baseline: true
  preconditions:
    - 잔액을 차감하는 엔드포인트가 존재
    - 현재 잔액 < 단위금액 × 동시요청수   # ★ 아니면 시험 자체가 무의미
  agent_must_determine:
    - 어떤 엔드포인트가 잔액을 차감하는가
    - 단위 금액을 얼마로 할 것인가
    - 동시 요청 수 (상한 10)
  severity_if_violated: HIGH

- id: G-SQLI
  layer: generic
  name: SQL Injection
  concept: 사용자 입력이 SQL 쿼리에 삽입되어 쿼리 구조가 변경된다.
  detection: [error_signature, boolean_differential, time_delay]
  agent_must_determine:
    - 어떤 파라미터가 DB 질의에 도달하는가
    - 어떤 탐지 방식이 이 대상에 적합한가
  severity_if_violated: CRITICAL
```

**규칙:**
1. `procedure`, `payload`, `endpoint`, `steps` 같은 **실행 절차 필드를 만들지 마라.**
2. `agent_must_determine`은 **프롬프트에 실제로 들어간다.**
3. `concept`에 판정 로직을 쓰지 마라. 판정은 `oracles/`에 있다.

---

## 12. `targets/` — 어댑터와 설정 분리

```yaml
# targets/configs/vulnbank.yaml
adapter:
  name: vulnbank
  base_url: http://localhost:5000
  default_currency: {code: USD, minor_unit: 2}     # ★

  auth:
    register: {method: POST, path: /register, fields: {username: username, password: password}}
    login:    {method: POST, path: /login, token_path: "$.token"}
    token_header: "Authorization: Bearer {token}"

  observation:                                     # state_probe 용 읽기 전용
    balances:     {method: GET, path: /api/accounts, id_path: "$[*].id",
                   value_path: "$[*].balance", currency_path: "$[*].currency"}
    transactions: {method: GET, path: /api/transactions, id_path: "$[*].id"}
    cards:        {method: GET, path: /api/cards, id_path: "$[*].id"}

  entitlement:                                     # ★ 정상 권한 확정용
    self_created:  [account_open, card_issue]      # 이 흐름으로 만든 건 entitled_own
    owner_field:   owner_id                        # 응답의 소유자 필드명
    normal_flow:   [{method: GET, path: /api/dashboard}]

  baseline_ops:                                    # ★ 정상 동작 학습용
    - {op: transfer, method: POST, path: /transfer,
       normal_params: {amount: 10, to_account: "{victim_account}"},
       semantics: "src -= amount; dst += amount"}

  field_aliases:
    account_id: [acct_no, accountNumber, account_id, id]
    amount:     [amount, value, txn_value]
    recipient:  [to_account, payee, beneficiary_id]

  discovery:
    openapi: /apispec.json
    graphql: /graphql
    seed_paths: [/api, /api/v1]

settings:
  principals: [attacker_A, victim_V, merchant_M]
  initial_balance: 1000
  layers: [generic, finance]
  profile: sandbox
  llm: {provider: anthropic, model: claude-sonnet-4-6}
```

`loader.py`는 로딩 시 `observation` 엔드포인트가 실제로 응답하는지 검증하고,
실패하면 경고 후 `missing` 표시 → 오라클이 `UNKNOWN`을 반환할 근거가 된다.

---

## 13. 에이전트 루프 (`agent/loop.py`)

```python
def run(target, budget, llm: LLMClient) -> RunResult:
    store = RunStore.start(target, cfg)
    ctx = AgentContext(target, Memory(), Graph(), budget, store, llm)

    # Phase 1: BOOTSTRAP
    ctx.principals = bootstrap_principals(target)
    ctx.graph.add_entitlements(entitlement.from_provisioning(ctx.principals))  # ★

    # Phase 2: RECON (passive)
    endpoints = discovery.run(target, ctx.principals)
    for ep in endpoints:
        obs = fingerprint.probe(ep, ctx.principals)
        ctx.memory.semantic.add_facts(inference.derive(obs))
    ctx.graph.type_classify(llm=ctx.llm)              # LLM은 타입 분류만

    # Phase 3: ENTITLEMENT 확정 ★ (정상 경로만)
    ctx.graph.add_entitlements(entitlement.discover(ctx.principals, endpoints))

    # Phase 4: BASELINE 학습 ★ (정상 파라미터로 실측)
    for op in cfg.adapter.baseline_ops:
        ctx.baselines.learn(op, ctx.principals)

    # Phase 5~N: 순환
    while not termination.should_stop(ctx)[0]:
        ctx.round += 1
        strategy = strategy_selector.choose(ctx)
        oracles  = registry.instantiate(ctx.graph, ctx.baselines, layers=cfg.layers)

        skeletons = candidates.enumerate(ctx.graph, oracles, strategy, templates)
        cands     = planner.refine(skeletons, ctx.memory.summarize(), strategy, ctx.llm)
        cands     = prioritizer.rank_and_dedup(cands, ctx.memory.episodic)

        for c in cands[:strategy.batch_size]:
            if not safety.check(c):          continue
            if not ctx.budget.can_afford(c): break
            # ★ baseline 필요한 후보인데 baseline 없으면 실행 자체를 스킵
            if c.needs_baseline and not ctx.baselines.has(c.path):
                ctx.review_queue.append(unknown_finding(c, "baseline 미확보")); continue

            result  = executor.run(c, ctx.principals)       # 레이어별 분기
            finding = verifier.verify(c, result, oracles)
            ctx.store.append("findings", finding)

            # ⑥ 학습 → ② 지식 갱신 (핵심 되먹임)
            ctx.memory.semantic.add_facts(inference.derive_from_result(result))
            ctx.graph.apply_observed(result)               # ★ observed_* 만 갱신
            #                                                 entitled_* 는 건드리지 않는다

            if finding.verdict == Verdict.VIOLATION:
                ctx.findings.append(finding)
                ctx.pending.extend(candidates.derive_followups(finding, ctx.graph))
            else:
                ctx.memory.episodic.record(c, finding, lessons.extract(c, result, finding))
                if finding.verdict in (Verdict.ANOMALY, Verdict.UNKNOWN):
                    ctx.review_queue.append(finding)

        ctx.store.snapshot_round(ctx.round, ctx)

    return report.build(ctx.store.finalize())       # ★ VVR 계산 안 함
```

---

## 14. 메모리 · 적응 · 종료

**`memory/` 3계층**: `semantic`(확립된 사실, `observed` confidence만 판정에 사용) /
`episodic`(시도 이력·교훈) / `summarizer`(3k 토큰 압축)

**`agent/strategy.py`**
```python
STRATEGIES = {
  "BROAD_SWEEP":    (["F1","F2","F3","F8","G-SQLI","G-BOLA"], batch=8, depth="broad"),
  "DEEPEN_SUCCESS": ([],           batch=5, depth="deep"),
  "PIVOT":          ([],           batch=6, depth="broad"),
  "EXPLOIT_GRAPH":  (["F1","F8"],  batch=6, depth="deep"),
  "GENERIC_SWEEP":  (["G-*"],      batch=8, depth="broad"),
  "RACE_RETRY":     (["F4"],       batch=1, depth="deep"),   # ★ 확률적 재현용
}

def choose(ctx):
    if ctx.round == 1:                          return S["BROAD_SWEEP"]
    if ctx.has_pending_race_trials():           return S["RACE_RETRY"]      # ★
    if ctx.graph.grew_significantly(last_n=1):  return S["EXPLOIT_GRAPH"]
    if ctx.recent_violations():                 return S["DEEPEN_SUCCESS"](ctx.successful_classes())
    if ctx.consecutive_failures() >= 2:         return S["PIVOT"](ctx.least_tried_classes())
    if ctx.layer_untouched("generic"):          return S["GENERIC_SWEEP"]
    return S["BROAD_SWEEP"]
```

**`agent/termination.py`**: `budget` / `max_rounds` / `saturated` / `converged` / `target_unhealthy`
종료 사유를 매니페스트에 반드시 남긴다.

---

## 15. Generic Layer 검증 (`oracles/generic.py`)

**금융 불변식 같은 결정적 오라클이 없다. 정직하게 인정하고 다르게 다룬다.**

| 공격 | VIOLATION | ANOMALY |
|---|---|---|
| SQLi | 에러 시그니처 + boolean/time 차분 재현 | 에러 메시지만 |
| XSS | 이스케이프 없이 반영 + 컨텍스트 확인 | 문자열만 반영 |
| SSRF | 아웃바운드 콜백 수신 | 응답 시간 차이만 |
| BOLA | 타 주체 객체 반환 + **entitlement 없음 확인** | 200 응답만 |
| JWT | 서명 없는 토큰 수락 | alg 헤더 수정 가능 |

> **Generic Layer는 `ANOMALY` 비율이 높은 게 정상이다.**
> 그게 이 레이어의 한계이고, **Finance Layer가 왜 다른지 보여주는 대조군**이 된다.
> 무리하게 VIOLATION으로 승격하지 마라.

---

## 16. 안전장치 (`config.py`)

```python
ALLOWED_HOSTS    = {"localhost","127.0.0.1"}   # 요청 직전 검사, 위반 시 예외
MAX_AMOUNT       = Decimal("1000")
MAX_PROBES       = 10          # 열거 상한. 하드코딩
MAX_CONCURRENT   = 10          # DoS 오인 방지
RACE_TRIALS      = 5
RACE_MIN_INTERVAL= 1.0         # 초
REQUEST_TIMEOUT  = 10
MAX_ROUNDS       = 12
MAX_TOKENS_TOTAL = 500_000
```
불변식은 금액 규모에 무관하다. **1원으로 증명되므로 큰 금액을 쓸 이유가 없다.**

---

## 17. 일별 마일스톤

| Day | 목표 | 완료 판정 |
|---|---|---|
| **1** | 스캐폴딩, `config`, `money.py`, `models/`, `llm/`, `storage/`, `targets/loader`, `session` | `--smoke` 가 주체 3명 토큰 + 매니페스트 생성. **`money` 단위테스트 통과** |
| **2** | `probing/finance/state.py`, `oracles/`(1A·1B·4·6) + **단위 테스트** | `pytest tests/test_oracles.py` 통과 (가짜 스냅샷, Decimal) |
| **3** | `recon/`, `entitlement.py`, `knowledge/graph.py`(entitled/observed 분리) | 그래프에 `entitled_*` 간선이 정상 경로로 채워짐 |
| **4** | `knowledge/baseline.py` + `memory/` 3계층 + `summarizer` | **baseline 실측 1건 이상 기록**, 요약 3k 토큰 이내 |
| **5** | `planning/` + `executor` + `verifier` — **엔드투엔드 1바퀴** | 음수 송금이 `VIOLATION(INV-1B, provenance=baseline_observed)` ★분기점 |
| **6** | `agent/loop.py` 순환 + `strategy` + `termination` | **2라운드 이상 돌며 그래프가 자람** ★ |
| **7** | `probing/generic/` + `oracles/concurrency.py`(F4) + `reporting/` + `evaluator/scorer.py` | 레이어별 분리 집계 리포트 + **scorer가 GT 대비 채점** |

**우선순위**: Day 5 > Day 6 > Day 4(baseline) > Day 7.
시간이 없으면 Generic Layer를 SQLi 하나로, F4를 빼도 된다.
**하지만 `money.py`, `entitled/observed` 분리, `provenance`는 빼지 마라.** 판정의 정당성이 걸려 있다.

---

## 18. 완료 기준

```
$ python -m finredteam --target vulnbank

[llm]        anthropic / claude-sonnet-4-6
[bootstrap]  attacker_A(USD 1000.00) victim_V(USD 1000.00) merchant_M(USD 0.00)
[adapter]    vulnbank — observation 3/3 ok, currency USD(2)
[recon]      endpoints 21 · facts 34
[entitle]    entitled_* 12 edges (self_created 6, owner_field 4, normal_flow 2)
[baseline]   learned 2 ops — transfer{src-=amt, dst+=amt}, charge{merchant+=amt}

── Round 1  strategy=BROAD_SWEEP ─────────────────────
  candidates 8  [finance 6, generic 2]
  ✓ VIOLATION  INV-1B   F2   POST /transfer {"amount":"-500.00"}
       provenance=baseline_observed (ref: bl_transfer_01)
       expected {attacker_A: -500.00 USD, victim_V: +500.00 USD}
       actual   {attacker_A: +500.00 USD, victim_V: -500.00 USD}
  ~ ANOMALY    signature G-SQLI  GET /search?q=' → DB 에러 (부작용 미확인)
  ✗ lesson     "GET /accounts/{id} 타 주체 요청은 403" → F1 우선순위 하향
  ⊕ graph      observed_* +3 (entitled_* 불변)

── Round 2  strategy=EXPLOIT_GRAPH ───────────────────
  ✓ VIOLATION  INV-4    F3   POST /payments/charge ×3 (order_id 고정)
  ? UNKNOWN    INV-2    F1   acct:777 의 entitlement 미확정 → 검토 대기열

── Round 3  strategy=RACE_RETRY ──────────────────────
  ✓ VIOLATION  INV-1C   F4   POST /transfer ×10 동시 (5회 시행)
       trials 5 · violations 3 · reproduction_rate 0.60
       trial#2: 성공 8건 × 100.00 = 800.00 기대, 실제 차감 500.00

[stop] reason=saturated
──────────────────────────────────────────────────────
Rounds 3 · Candidates 21 · Verified 4 · Anomaly 3 · Unknown 2
  Finance: Verified 3 / Candidates 14
  Generic: Verified 1 / Candidates 7
Yield 0.19 · Facts 51 · Lessons 6
Evidence 213 stored (48 redacted)
Tokens 84k · Requests 213
Report: runs/20260824-1530/report.md
※ VVR/Precision 은 evaluator/scorer 로 별도 산출

$ python -m finredteam.evaluator.scorer runs/20260824-1530
F-VVR 3/5 (0.60) · Generic-VVR 1/4 (0.25) · Precision 4/4 (1.00) · FP 0
Missed: [VB-005 F6 limit bypass, VB-009 F1 multi-hop]
```

**필수 충족 조건:**
1. **두 레이어 동작 + 지표 분리 집계** ★정체성
2. Finance Layer에서 최소 2개 클래스 `VIOLATION`
3. **라운드 간 그래프가 자란다** ★에이전트
4. **전략이 최소 1회 전환된다** ★적응
5. **실패에서 lesson이 추출되어 다음 계획에 반영된다** ★학습
6. **모든 금액이 `Decimal`이다** — `float` 사용 시 테스트 실패 ★
7. **`entitled_*`가 `observed_*`로 오염되지 않는다** — 테스트로 강제 ★
8. **`provenance != baseline_observed`인 `INV-1B` 판정이 `VIOLATION`이 되지 않는다** ★
9. F4는 **반복 시행 결과(재현율)** 로 보고된다 ★
10. **에이전트가 VVR/Precision을 계산하지 않는다** — `evaluator/scorer`만 ★
11. 모든 `VIOLATION`에 재현 스크립트 첨부
12. `reason`에 LLM 생성 문장이 없다 (코드 생성 수치만)
13. `oracles/`가 LLM·네트워크를 호출하지 않는다
14. **민감 응답 원문이 저장되지 않는다**
15. `attacks.yaml`에 엔드포인트·페이로드가 없다 ★스캐너화 방지
16. **LLM provider를 openai로 바꿔도 동작한다** ★
17. allowlist 밖 호스트는 즉시 실패

---

## 19. 하지 말 것

- ❌ Ablation, baseline 비교 실험, 성능 벤치마크
- ❌ `INV-3`(FSM), `INV-5`(한도), `INV-7`(경로 동등성) 구현 — 스텁만
- ❌ 외부 도구(Nuclei·sqlmap) 실제 연동 — `generic/external.py`는 인터페이스만
- ❌ 웹 UI, 대시보드, 시각화 / 멀티에이전트 / MCP / 외부 DB / Docker 이미지 / CI
- ❌ **금액에 `float` 사용**
- ❌ **`observed_*`를 `entitled_*`로 승격**
- ❌ **에이전트가 GT를 읽거나 VVR을 계산**
- ❌ **에이전트가 DB·로그·소스코드를 읽음**
- ❌ **`attacks.yaml`에 실행 절차(procedure/payload/steps)**
- ❌ 벤더 SDK를 `llm/` 밖에서 직접 import

**기능을 넣고 싶을 때 물어라: "이게 없으면 Verified Finding이 줄어드는가?"**

---

## 20. 작업 방식

1. **먼저 vuln-bank를 띄우고 손으로 한 번 공격해봐라.** 확인한 경로·필드명·통화는
   `targets/configs/vulnbank.yaml`에 적어라. 코드에 하드코딩하지 마라.
   (사람이 조사해 설정에 적는 건 허용. **에이전트 코드가 소스를 읽는 건 금지.**)
2. 각 Day 끝에 실행해서 확인하고, 안 되면 다음 날로 넘어가지 마라.
3. 막히면 **범위를 줄여라.** 공격 클래스를 빼는 건 괜찮다.
   하지만 "LLM이 판정하게" 하거나 "DB를 직접 읽어" 우회하지 마라.
4. 커밋은 Day 단위. 메시지에 완료 판정을 적어라.

**시작 전에 `README.md`에 하루 단위 계획과 모듈 의존 순서를 먼저 쓰고, 확인받은 뒤 Day 1을 시작해라.**

