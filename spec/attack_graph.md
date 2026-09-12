# 공격 그래프 스키마

정본은 `spec/schema/attack_graph.py` + `spec/schema/finance.py` (문서와 어긋나면 코드가 이긴다)

## 한 줄

**노드는 "공격자가 지금까지 손에 넣은 것", 간선은 "그걸로 할 수 있었던 일"이다.**

## 노드

```python
Node(
  node_id="card:17",          # 항상 "{kind}:{key}" — 로그에서 grep 가능해야 한다
  kind="card",
  attrs={"created": True, "funded": True},   # 술어  → 전제조건 매칭
  facts=CardFacts(...),                      # 사실  → 판정
  first_seen_step=3, last_seen_step=7,
  retractions=[],                            # 내려간 술어의 이력
)
```

### 종류 10개

| kind | 무엇 | 관례 attrs |
|---|---|---|
| `principal` | 로그인 가능한 주체 | `authenticated` `has_account` `has_token` `is_admin` `ours` |
| `account` | 계좌 (피해자 것 포함) | `balance_known` `readable` `writable` |
| `card` | 가상카드 | `created` `funded` `limit_known` |
| `merchant` | 가맹점 계정 | `registered` |
| `biller` | 청구 기관 | `discovered` `min_amount_known` |
| `token` | 인증 토큰 | `valid` `forged` |
| `role` | 권한 역할 | `name` |
| `loan` | 대출 건 | `requested` `approved` `credited` |
| `endpoint` | 발견된 요청 표면 | `method` `reachable` `requires_auth` |
| `objective` | **terminal** — 공격자 목표 | `achieved` |

`principal` 과 `account` 가 따로인 이유: principal 은 행위 주체, account 는 공격 **대상**이다.
피해자 계좌는 principal 없이도 노드가 되어야 한다.

`ours` 는 "우리가 만들었거나 조종하는 주체" 다. 이게 그래프에 있어야
`attacker_accounts()` 가 **기록에서** 공격자 자산을 유도할 수 있다 —
OBJ-FUNDS-EXFIL 은 "공격자 쪽 잔액이 늘었나" 로 CRITICAL 을 내는데,
그 집합이 호출자가 손으로 넘기는 인자면 증거 사슬 밖의 값 하나에 최고 심각도가
매달리고, 그게 틀려도 기록만 봐서는 알 수 없다.

### attrs 에 값을 담을 수 없다

```python
Node(..., attrs={"password": "x"})           # ValueError
Node(..., attrs={"note": "4242424242424242"}) # ValueError — PAN 모양
Node(node_id="card-9", kind="card", key="9")  # ValueError — id 형식
```

`facts` 쪽은 PAN 을 막는 validator 가 있는데(`CardFacts.pan_last4`) `attrs` 는
자유 dict 라 그 검사를 우회하는 경로였다. ③ Capability 가 `produced_nodes` 로
dict 를 그대로 올려 보내므로 실제로 뚫린다.

### 술어는 내려갈 수 있다

```python
node.retract("funded", step=5, reason="잔액 소진")
```

상태가 누적만 되면 안 된다. 소진된 카드를 계속 `funded` 로 두면 **못 하는 공격이
목록에 남는다.** 지운 기록을 남기는 이유는 "왜 갑자기 못 쓰게 됐나" 가 추적돼야 해서다.

## 금융 사실 (`finance.py`)

`attrs` 로는 금액을 담을 수 없다 — `bool|str|int` 평평한 dict 라서 Decimal 도, 통화도,
"언제 어디서 본 값인지" 도 못 적는다. 금융 레드팀에서 그건 치명적이다.

```python
class MoneyValue:      # ★ float 을 넣으면 생성이 실패한다
    amount: Decimal
    currency: str
    minor_unit: int

class Provenance:      # ★ 모든 관측값에 붙는다
    step: int
    tool_id: str
    evidence_ref: str          # 빈 문자열이면 생성 실패
    confidence: measured | server_declared | asserted | inferred
```

### 잔액은 시계열이다

```python
card.balance_at(3)      # 실험 전
card.delta(3, 4)        # 변화량 — 통화가 다르면 계산하지 않고 None
```

금융 판정은 거의 전부 "요청 전후 차이"다. 최신값만 들고 있으면 델타를 못 만든다.

### 증거 강도를 구분한다

```python
PolicyBound(field="amount", minimum=..., declared_by="server")           # strong
PolicyBound(field="loan_amount", maximum=..., declared_by="probe_assertion")  # weak
```

**서버가 스스로 알려준 경계를 서버가 안 지킨 것**과 **우리가 상식으로 가정한 걸 넘은 것**은
증거 무게가 다르다. 구분이 없으면 리포트에서 둘이 똑같아 보인다.

### 민감정보는 값을 담지 않는다

```python
CardFacts(pan_fully_exposed=True, cvv_exposed=True, pan_last4="4242")
```

전체 PAN 을 `pan_last4` 에 넣으면 **생성이 거부된다.** `PrincipalFacts` 에는 비밀번호
필드 자체가 없다. 그래프는 evidence store 에 오래 남아서, 여기 한 번 새면 회수가 안 된다.

### 관측은 지워지지 않는다

같은 노드를 다시 관측하면 `upsert()` 가 `merge_facts()` 로 **누적**한다.

```python
g.upsert(node_with_balance_at_step_3)
g.upsert(node_with_balance_at_step_4)
g.facts_of("account:1").delta(3, 4)      # -100.00 USD
```

통째로 교체하면 관측할 때마다 이력이 1건으로 리셋되고 `delta()` 가 영원히 None 이
된다 — 잔액을 시계열로 설계한 이유가 사라지고 금융 판정 전체가 "판정 불가" 로 죽는다.

병합 규칙은 전부 "관측은 지워지지 않는다" 에서 나온다:
list 는 합집합, bool 은 **sticky True**(`pan_fully_exposed` 는 한 번 True 면 True),
새 값이 None 이면 무시, 그 외는 갱신.

### 가치 이동

```python
flow.net()            # 통화별 순합 — 폐쇄계면 전부 0 이어야 한다
flow.is_conserved()   # True / False / None(판정 불가)
flow.explain()        # "2개 객체, 통화별 순합 [USD: 9900.00] → 보존 위반"
```

★ **`closed` 의 기본값은 `False`(개방계)다.** 폐쇄계가 기본이면 "관측을 덜 한 상태"
가 곧 VIOLATION 으로 둔갑한다. "이 전이의 가치 이동을 빠짐없이 봤다" 는 주장이고,
주장은 명시해야 한다. 같은 이유로 `Provenance.confidence` 의 기본값도 가장 약한
`inferred` 다 — 강한 등급이 기본이면 깜빡한 값이 판정의 단독 근거로 승격된다.

허용오차는 최소 단위 **미만**이다(`< tolerance`). Decimal 이라 부동소수 오차가
애초에 없으므로 1센트 창조를 봐줄 이유가 없다.

## 간선

### relation — 누가 무엇에 권한이 있나

```python
Relation(src="principal:a", dst="account:1",
         kind="entitled", relation="entitled_own", provenance=...)
```

- `kind="observed"` 인 간선에 `entitled_*` 를 넣으면 **예외가 난다**
- `entitled_own` 은 상위 권한 — `satisfies("read")` 도 `satisfies("transfer_from")` 도 True
- 같은 (주체, 대상, 관계)는 중복 저장되지 않는다
- 관계에도 `provenance` 가 필수다 — 소유 관계에 근거가 없으면 그걸 기준으로 낸
  권한 위반 판정 전체가 검증 불가능해진다

**이 구분을 소비하는 곳은 전제조건이다** (`NodePattern.relation`, 아래 참고).
소비처가 없으면 `entitled`/`observed` 를 나눈 것 자체가 장식이 된다 —
실제로 한동안 `Relation.satisfies()` 를 호출하는 코드가 하나도 없었다.

### transition — 무엇을 해서 무엇을 얻었나

하이퍼엣지(여러 노드 → tool → 여러 노드)라 nx 간선이 아니라 레코드로 저장한다.

| 상태 | 의미 |
|---|---|
| `planned` | frontier 에 올라와 선택됨 |
| `blocked` | **정책이 거부** — `blocked_reason` 필수 |
| `succeeded` / `failed` | 실행 성공 / 목적 미달성 |
| `setup_failed` | 전제는 맞았는데 런타임 셋업 실패 |

`blocked` 를 그래프에 남기는 게 중요하다. 리포트가 "이 영역은 정책상 안 봤다" 를
말할 수 있어야 한다. 조용히 건너뛰면 커버리지를 속이는 게 된다.

## 전제조건 (`NodePattern`)

```yaml
- kind: principal
  attrs: {has_account: true}   # 부분 일치
  count: 2                     # 서로 다른 노드 2개
  bind: racers
  from_param: target           # ★ 모델이 params 로 고른 **바로 그** 노드
  distinct_from: [attacker]    # 다른 bind 와 겹치면 안 된다 (대칭·순서 무관)
  related_to: attacker         # ★ 관계 조건 — 방향은 related_to → 이 패턴
  relation: entitled_own       #   "동사만"("own") 쓰면 entitled 채널만 본다
  relation_absent: false       # ★ true 면 그 관계가 **없어야** 한다
  max_age_steps: 5             # 관측이 오래됐으면 다시 관측하게
  optional: false
```

**`from_param` 이 이 스키마에서 가장 중요한 필드다.** `binds_to`(매니페스트)는
*종류*만 말하고 *동일성*은 말하지 않는다. 동일성 선언이 없으면 `resolve()` 가
패턴에 맞는 아무 노드나 집고, `ingest()` 가 그 엉뚱한 바인딩을 허용 표면으로
삼는다 — 모델은 `POST /transfer` 를 골랐는데 대조 기준은 `GET /balance` 가 되는 식.
스코프 이탈 탐지 전체가 틀린 값으로 돈다.

`distinct_from` 이 없으면 **자기 계좌로 송금해놓고 "타인 계좌 탈취"라고 판정하는
사고**가 난다. 그리고 이 제약은 **대칭이고 선언 순서에 의존하지 않는다** —
한쪽에만 적어도 양쪽에 걸린다.

관계 조건은 권한 경계를 전제로 적는 유일한 수단이다:

```yaml
# "내가 소유하지 않은 계좌" — OBJ-AUTHZ-BYPASS(INV-2) 의 전제
- {kind: principal, attrs: {authenticated: true}, bind: me}
- {kind: account, bind: victim, related_to: me,
   relation: entitled_own, relation_absent: true}
```

`resolve()` 는 성공하면 바인딩 dict, 실패하면 **왜 안 되는지 설명하는 문장**을 낸다.
그리디가 아니라 **백트래킹**이다 — 앞 전제가 유일한 후보를 가져가버려서
"후보는 있는데 실패" 하는 일이 없다.

### 두 가지 해소 모드

```python
g.resolve(patterns, for_frontier=True)          # 가용성: "후보가 있는가"
g.resolve(patterns, params=chosen_params)       # 확정: "그 노드로 묶는다"
```

frontier 계산 시점에는 모델이 아직 params 를 안 골랐으므로 `from_param` 을 특정할
수 없다. 두 모드를 섞으면 frontier 가 집은 임의의 노드가 그대로 실행 대상이 된다.

## 판정과 목표

```python
FindingRecord(finding_id="f-01", invariant="INV-1B", verdict="VIOLATION",
              reason="...",                  # ★ 코드가 만든 수치 문장만
              evidence_refs=["ev-101"])      # ★ VIOLATION 인데 비면 생성 실패
```

목표 카탈로그 6개 (`OBJECTIVE_CATALOG`)는 매 run 마다 **전부** 평가된다. 재료가 없으면
정직하게 UNKNOWN 을 낸다 — 목표를 골라서 볼 수 있으면 불리한 판정을 피하는 경로가 생긴다.

```
OBJ-FUNDS-EXFIL / OBJ-VALUE-FORGE / OBJ-AUTHZ-BYPASS
OBJ-PRIV-ESC    / OBJ-PII-EXFIL   / OBJ-POLICY-BREACH
```

```python
g.evaluate_objectives()          # ★ 공격자 계좌를 손으로 넘기지 않는다
```

- 공격자 자산은 `attacker_accounts()` 가 **그래프에서 유도**한다
  (`attrs["ours"]` 인 주체의 `entitled_own` 관계 — `observed_*` 는 근거가 아니다)
- `requires_all_of`(선행 목표)가 미달성이면 VIOLATION 으로 부르지 않고 ANOMALY 로 낮춘다
- `severity` 와 `reason` 이 `ObjectiveNode` 에 복사된다 — ④ Evidence 는 별도
  프로세스라, 카탈로그를 다시 조회하지 않고 기록만으로 정렬·필터할 수 있어야 한다
- 목표는 `graph.nodes` 에도 `objective:OBJ-*` 노드로 들어간다.
  `NodeKind` 에 terminal 로 선언해놓고 정작 `nodes` 밖에만 두면 선언과 실물이 어긋난다

## 저장과 재생

```python
records = g.to_jsonl_records()                  # 컬렉션별 append-only
g2 = AttackGraph.from_jsonl_records(records)    # 왕복 가능
```

- 모르는 컬렉션은 **조용히 버리지 않고 예외**를 낸다 — 증거가 사라진 줄 모르고
  리포트가 나가는 걸 막는다
- 스키마 버전이 지원 밖이면 예외 — 4팀이 각자 릴리스하는 구조라 실제로 생길 수 있다

## 무결성 점검

```python
g.integrity_report()
# {"근거 없는 주장": [...], "사유 없는 차단": [...], "정리 안 된 자원": [...],
#  "출처 없는 관계": [...], "선언 밖 표면": [...], "약한 근거의 관계": [...]}
```

비어 있지 않으면 리포트를 내면 안 된다.

"선언 밖 표면" 이 **기록**에서 나오는 게 중요하다. 이탈 판정을 `ingest()` 반환값으로만
주면 호출부가 무시하는 순간 사라지고, 저장된 기록만 보는 ④ Evidence 는 영영 알 수 없다.
그래서 `record_transition()` 이 `Transition.surface_drift` 에 남긴다.

### 표면 대조의 기준 (`AllowedSurfaces`)

```python
allowed = manifest.allowed_surfaces(bound_endpoints=bindings["target"])
t.drift_against(allowed)          # 선언 밖이면 그 목록
```

세 가지가 합쳐진다 — 정적 `surface`, 실행 시점 바인딩, 접두어 울타리
(`surface_prefixes`, 정찰 tool 전용). **비어 있으면 전부 이탈이다.**
"선언 안 했으니 통과" 가 되면 필드가 장식이 된다.

### 기록 규칙

- `transition_id` 중복은 **거부**한다. `path_to()`/`transitions_touching()` 이
  id 집합 조회라 중복이 들어오면 경로 복원이 조용히 이중 계상된다
- `to_jsonl_records()` 는 전이를 step 순으로 내보낸다. 읽을 때만 정렬하면
  저장→로드→저장 결과가 내용은 같은데 순서가 달라 `round_trip_ok()` 가 거짓이 된다
- `Transition.params` 는 `ingest()` 가 매니페스트의 `sensitive` 선언대로 치환한 뒤
  기록한다 — 이 줄이 그대로 evidence store 로 나가기 때문이다

## 쓰기 권한

```python
GraphView(g)   # ①③ 에게는 이걸 넘긴다 — upsert/record_* 가 없다
```

③ Capability 가 직접 쓰면 `ingest()` 의 증거·표면 검사를 우회한다. 파이썬이 강제하진
못하지만, **넘기는 타입 자체가 계약**이 된다.
