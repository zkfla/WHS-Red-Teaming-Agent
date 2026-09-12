# spec/ — 공격 그래프 · 툴 매니페스트 규격

레드팀 AI 의 **계층 사이 계약** 두 개. 실행 코드가 아니라 자료구조와 규칙이다.

```
attack graph   = 지금까지 뭘 얻었나 (상태)
tool manifest  = 뭘 더 할 수 있나  (행동)
```

이 둘을 대조하면 **지금 실행 가능한 tool 집합**이 나온다. 그게 이 규격의 존재 이유다.

---

## 30초 만에 돌려보기

```bash
pip install pydantic pyyaml pytest
PYTHONPATH=. python spec/demo_frontier.py     # 상태가 쌓이며 할 수 있는 게 늘어난다
PYTHONPATH=. python spec/demo_teams.py        # 한 스텝 전 과정 — 4개 팀 경계
PYTHONPATH=. python spec/demo_guards.py       # 세 가드가 무엇을 막는가
PYTHONPATH=. python -m spec.schema.validate spec/tools spec/primitives
PYTHONPATH=. pytest spec/tests -q
```

전부 네트워크를 타지 않는다. 대상 앱을 띄우지 않아도 된다.

---

## 이게 왜 필요한가

레드팀 AI 에게 MCP tool 을 열어주면 자유도가 올라간다. 그러면 두 가지가 문제가 된다.

| 문제 | 이 규격이 하는 일 |
|---|---|
| 시킨 곳이 아닌 데를 공격한다 | tool 이 **건드릴 곳을 미리 선언**하고, 실행 후 실제와 대조한다 |
| 오탐인데 확정이라고 주장한다 | 증거(`evidence_ref`) 없는 판정은 **기록 자체가 거부**된다 |
| 못 하는 걸 고르고 실패를 반복한다 | 전제가 안 맞는 tool 은 **애초에 목록에 없다** |
| 비밀·PII 가 증거저장소로 샌다 | `sensitive` 선언된 파라미터는 기록 직전 치환된다 |
| 승인 안 난 권한을 쓴다 | capability 8종이 **전부** 정책에 묻는다 (import 시점에 단언) |

전부 LLM 에게 묻지 않는다. 선언과 기록을 코드가 대조할 뿐이다.

---

## 파일 지도

```
spec/
├── schema/                 ★ 규격 본체 (이것만 새 아키텍처로 그대로 간다)
│   ├── finance.py            금융 사실을 어떻게 적을지 — 금액·잔액 시계열·정책 경계
│   ├── attack_graph.py       그래프 — 노드/관계/전이/목표/정리/저장
│   ├── tool_manifest.py      매니페스트 — tool 한 개를 적는 양식 + MCP 변환
│   ├── policy.py             ④ 와의 계약 — PolicyPort(Protocol) + StaticPolicy
│   ├── registry.py           매니페스트를 읽는 **하나뿐인** 진입점
│   ├── frontier.py           "지금 뭘 할 수 있나" — 한 스텝의 ①번
│   ├── ingest.py             ③ Capability → ② Graph 접합부 (결과를 넣는 유일한 경로)
│   └── validate.py           검사기 — 23가지 (실행 파일)
├── primitives/             원시 동작 11개  ← 새로 짤 때 쓰는 tool 어휘
├── tools/                  레거시 17개     ← 기존 프로브를 옮긴 것. 참고용
├── tests/                  회귀 시험 67개
└── demo_*.py               돌려보는 예제 7개
```

의존 방향은 한 줄이다 (순환 없음):

```
finance → attack_graph → tool_manifest → policy → registry → frontier
                                              ↘  ingest  ↙
```

`spec/` 은 프로젝트의 다른 코드를 하나도 import 하지 않는다. pydantic, pyyaml, 표준
라이브러리가 전부다 — 그래서 아키텍처가 바뀌어도 그대로 옮겨간다.

---

## 코드 구조 — 어디부터 읽나

클래스가 40개쯤 되지만 **대부분은 데이터를 담는 상자**다(pydantic 모델, 필드 선언뿐).
실제 로직이 있는 건 아래 9개고, 이것만 읽으면 전체가 이해된다.

| 함수 | 파일 | 하는 일 |
|---|---|---|
| `AttackGraph.resolve()` | attack_graph | **전제조건 해소.** 성공하면 바인딩, 실패하면 *왜 안 되는지 문장* |
| `AttackGraph.record_transition()` | attack_graph | **불변식 강제 지점.** 전이를 남기는 유일한 통로 |
| `Transition.grounding_ok()` | attack_graph | 주장은 있는데 증거가 없으면 False |
| `Transition.drift_against()` | attack_graph | 선언 밖 표면을 건드렸으면 그 목록 |
| `ValueFlow.is_conserved()` | finance | 통화별 순합이 0인가. 개방계면 `None`(판정 불가) |
| `ToolManifest.validate_params()` | tool_manifest | 모델이 넣은 값의 형식·범위·float 금지 검사 |
| `ToolManifest.to_mcp_tool()` | tool_manifest | MCP 정의로 변환 — 권한·전제는 빼고 |
| `ToolManifest.redact()` | tool_manifest | 기록에 남길 params 에서 비밀을 가린다 |
| `merge_facts()` | finance | 같은 노드를 다시 관측했을 때 **사실을 누적**한다 |
| `preflight()` | ingest | 실행 전 검사. **모델이 고른 params 로 바인딩을 확정한다** |
| `ingest()` | ingest | 실행 결과를 그래프에 반영하는 **유일한 경로** |
| `compute_frontier()` | frontier | 지금 실행 가능한 tool 집합. 한 스텝의 ①번 |
| `check_manifest()` | policy | capability·동시성·금액·예산·표면을 정책에 묻는다 |

나머지는 전부 조회 헬퍼이거나 필드 선언이다.

### 읽는 순서

```
1. demo_teams.py       ← 전체가 어떻게 도는지 먼저 눈으로 본다
2. frontier.py         ← 60줄. 한 스텝이 어디서 시작하는지
3. ingest.py           ← preflight() 와 ingest() 가 짝이다. 여기가 접합부
4. attack_graph.py     ← resolve() 와 record_transition() 만 보면 된다
5. tool_manifest.py    ← ToolManifest 필드 목록 = YAML 한 장의 구조
6. policy.py           ← ④ 팀이 구현할 인터페이스
7. finance.py          ← 금융 판정이 필요할 때만
```

### 각 파일이 대략 무엇인가

```
finance.py        금융 값 타입 + 노드별 사실 7종 + 가치 이동
                  · MoneyValue (float 거부) · Provenance (증거 필수)
                  · AccountFacts / CardFacts / ... · ValueFlow

attack_graph.py   그래프 자료구조 + 조회
                  · Node / Relation / Transition / FindingRecord
                  · AttackGraph (쓰기 가능) · GraphView (읽기 전용)

tool_manifest.py  매니페스트 + 리턴 타입
                  · ToolManifest (YAML 한 장의 구조)
                  · ProbeResult / ExecutionRecord / RequestRecord

policy.py         ④ Control Plane 계약. PolicyPort(Protocol) + StaticPolicy
                  · capability 8종 전부가 여기서 검사된다 (import 시점에 단언)
                  · policy_hash() — "어떤 승인 범위에서 나온 발견인가" 의 증명

registry.py       매니페스트 적재. id 중복을 **거부**한다(덮어쓰지 않는다)

frontier.py       compute_frontier() — 정책 먼저, 전제 나중.
                  · blocked = 영구 커버리지 공백  · unmet = 나중에 열릴 수 있음

ingest.py         ③ Capability ↔ ② Graph 접합부. preflight() + ingest()

validate.py       YAML 검사기. check_file() 19가지 + check_cross() 4가지
```

---

## 두 규격

### 1. 공격 그래프 — 지금까지 뭘 얻었나

노드는 **공격자가 손에 넣은 것**이다. 엔드포인트 지도가 아니다.

```python
Node(node_id="card:17", kind="card",
     attrs={"created": True, "funded": True},        # 술어 — 전제조건 매칭용
     facts=CardFacts(...))                            # 도메인 사실 — 판정용
```

`attrs` 와 `facts` 를 나눈 이유: 전제 매칭은 "카드가 충전됐나"(bool)만 알면 되고,
판정은 "얼마나, 무슨 통화로, 어디서 봤나"가 필요하다. 섞으면 매칭 규칙이 흐려진다.

노드 종류 10개 — `principal` `account` `card` `merchant` `biller` `token` `role`
`loan` `endpoint` `objective`

간선은 두 계열이다.

```
relation   (entitled / observed)   누가 무엇에 권한이 있나 / 실제로 뭐가 통했나
transition (tool 실행 1건)          무엇을 해서 무엇을 얻었나
```

**`observed` 는 `entitled` 로 승격되지 않는다.** "조회가 됐다"는 권한의 근거가 아니라
검사해야 할 관측이다. 이걸 간선 종류로 나눠 두면 구조적으로 섞일 수 없다.

### 2. 툴 매니페스트 — 뭘 더 할 수 있나

tool 하나 = YAML 한 장.

```yaml
id: http_request
summary: 이미 발견한 엔드포인트에 임의 메서드·바디로 요청을 보낸다.

capabilities:                 # ← control plane 전용. 모델에 안 보인다
  required: [state_change]
  concurrency: 1
  max_amount_touched: "1000.00"

surface_binding: target       # ← "이 파라미터가 대상을 가리킨다"

preconditions:                # ← 이게 있어야 실행 가능
  # ★ from_param 이 "모델이 고른 **바로 그** 노드" 를 묶는다
  - {kind: endpoint, attrs: {reachable: true}, bind: target, from_param: target}

params:                       # ← 모델에 보이는 부분
  target: {type: string, required: true, binds_to: endpoint}
  amount: {type: money, policy_checked: true}
  body:   {type: json, sensitive: true}      # ← 기록에는 «redacted» 로 남는다
```

`capabilities` 를 모델에 안 보여주는 이유: 보이면 "이건 그 권한 필요 없는데요" 라고
우기는 경로가 생긴다. 권한 선언은 협상 대상이 아니다.

**`binds_to` 와 `from_param` 은 다른 말을 한다.** `binds_to` 는 *종류*("endpoint 노드
여야 한다")만, `from_param` 은 *동일성*("이 전제는 그 파라미터가 가리키는 노드다")을
말한다. 동일성 선언이 없으면 `resolve()` 가 패턴에 맞는 아무 노드나 집고, `ingest()`
가 그 엉뚱한 바인딩을 허용 표면으로 삼는다 — 모델은 A 를 골랐는데 대조 기준은 B 가
되는 식이라, 스코프 이탈 탐지 전체가 무의미해진다.

---

## 원시 동작 11개 (`primitives/`)

레시피(`loan_limit`, `card_limit` …)가 아니라 조립 부품이다.

| tool | 권한 | 하는 일 |
|---|---|---|
| `probe_path` | — | 모르는 경로를 읽기 전용으로 찔러본다 (`surface_prefixes` 울타리 안에서) |
| `http_request` | `state_change` | **발견한** 엔드포인트에 요청을 보낸다 |
| `observe_state` | — | 잔액을 읽어 델타의 기준을 만든다 |
| `concurrent_burst` | `race_testing` | 같은 요청을 동시에 N번 |
| `repeat_request` | `state_change` | 같은 요청을 순차로 N번 |
| `create_principal` | `account_creation` | 가입 (추가 필드 주입 지점 겸함) |
| `authenticate` | `state_change` | 로그인해 토큰 획득 |
| `forge_token` | `bruteforce` | 토큰 위조 (`offline: true` — 네트워크를 타지 않는다) |
| `delete_principal` / `delete_card` / `cancel_loan` | `delete` | 정리 전용 — 모델에 노출되지 않는다 |

레거시 17개는 이 11개 조합으로 전부 재현된다 (`demo_primitives.py` 로 확인).
`card_limit`·`loan_limit`·`bill_limit` 은 셋 다 "어떤 주소에 이상한 값 넣기" 하나였다.

---

## 팀 경계

```
① Agent          ② Orchestration · Graph     ③ Capability      ④ Control Plane · Evidence
```

| 팀 | 그래프 | 진입점 | 구현할 것 |
|---|---|---|---|
| ① Agent | 읽기 | `GraphView.observe()` · `mcp_tool_list()` | tool 선택 |
| ② Orchestration | **쓰기** | `compute_frontier()` / `upsert()` / `record_transition()` | 루프 |
| ③ Capability | 읽기 | `ProbeResult` 를 반환할 뿐 | 실제 실행 |
| ④ Control Plane | **쓰기** | `preflight()` / `ingest()` / `to_jsonl`·`from_jsonl` | **`PolicyPort` 구현** |

③ 이 그래프를 직접 쓰면 `ingest()` 의 증거·표면 검사를 우회하는 경로가 생긴다.
그래서 ①③ 에는 `GraphView`(읽기 전용)를 넘긴다.

④ 가 구현할 것은 `spec/schema/policy.py` 의 `PolicyPort` 하나다. 참조 구현
(`StaticPolicy`)이 같은 파일에 있으니, ④ 가 준비되기 전에도 ②③ 는 그걸로 돈다.

### 한 스텝이 도는 순서

```
① compute_frontier()  정책 먼저(영구 차단) → 전제 나중(일시적 미충족)
② 모델이 고른다        ← 유일한 LLM 개입 지점
③ preflight()         파라미터 형식 · 노드 실존 · 정책 · **params 로 바인딩 확정**
④ tool 실행 → ProbeResult
⑤ ingest()            오라클 전체 실행 → 판정을 전이에 붙여 기록
                      증거 검사 · 표면 대조 · 비밀 치환 · 정리 등록이 여기서 강제된다
⑥ evaluate_objectives()  목표 카탈로그 6개 전부 평가 (줄일 수 없다)
```

★ ③ 이 만든 `bindings` 를 ⑤ 에 **그대로** 넘겨야 한다. 이 인계가 끊어지면
  스코프 대조가 조용히 엉뚱한 기준으로 돈다 — `demo_teams.py` 가 이 이음매를
  손으로 값을 넣지 않고 통과하는 걸 보여준다.

---

## 검사기가 막는 것

```bash
PYTHONPATH=. python -m spec.schema.validate spec/tools spec/primitives
```

23가지를 본다. 문법이 아니라 **선언 누락**이 목적이다 — tool 작성자가 악의 없이
"race_testing 필요하다" 를 빼먹어도 정책이 조용히 안 걸리기 시작하기 때문이다.

대표적인 것:

- 잔액을 바꾸는데 `state_change` 를 선언 안 했다
- 동시 요청이 2개 이상인데 `race_testing` 을 선언 안 했다
- 자원을 만드는데 정리 방법도, "못 치운다" 선언도 없다
- `readOnlyHint: true` 인데 실제로는 상태를 바꾼다 (모델이 안전한 tool 로 오해한다)
- 전제로 요구하는 노드를 **아무 tool 도 만들지 않는다** (= 영원히 실행 불가)
- **`surface_binding` 파라미터를 `from_param` 으로 전제와 안 묶었다** (⑳)
- 자격증명·자유 입력 파라미터에 `sensitive` 를 안 붙였다 (㉑)
- `distinct_from` / `related_to` 가 존재하지 않는 bind 를 가리킨다 (㉒ — 오타면
  배타 제약이 조용히 사라지고, 공격자와 피해자가 같은 노드로 잡힌다)
- 상태를 바꾸는 tool 이 `surface_prefixes` 로 광범위 선언을 했다 (㉓)

실제로 이 검사가 작성 중 누락 세 개를 잡아냈다: `loan` 노드 종류 미등록,
존재하지 않는 정리 tool 선언, 원시 동작의 동적 산출물 미선언.
⑨ 를 `layer` 대신 `offline` 기준으로 바꿨을 때 `discover_surface` 의 선언 누락도
바로 걸렸다 — 그 파일 주석이 스스로 "면제받고 있다" 고 적어둔 지점이었다.

---

## 아직 없는 것 (정직하게)

- **오라클 계층** — `ingest()` 가 `oracle_runner` 를 주입받는 형태로 자리만 비워뒀다.
  판정 로직 자체는 이 규격 밖이다.
- **실제 실행** — `primitives/` 는 선언이고, 이를 수행하는 코드는 ③ Capability 팀 몫이다.
- **실제 정책 객체** — `StaticPolicy` 는 참조 구현이다. Engagement 승인서 파싱,
  동적 한도, 감사 로깅은 ④ 팀이 `PolicyPort` 를 구현해 채운다.
- **호스트 범위** — `check_surface()` 는 경로 접두어만 본다. 다른 호스트를 막는 건
  ④ 의 네트워크 계층 몫이다(이 규격은 "허용된 호스트 안에서의 이탈" 을 다룬다).
- **`tools/` 17개** — 기존 프로브를 옮긴 것이라 새 아키텍처의 tool 목록이 아니다.
  규격이 실물을 담을 수 있는지 검증한 사례이자, 대상 엔드포인트 목록으로 남긴다.
- **동시 실행** — 지금 스키마는 순차 스텝(`step: int`)을 전제한다. 병렬 tool 실행이
  필요해지면 순서 모델을 다시 봐야 한다.
- **`resolve()` 의 탐색 비용** — 백트래킹은 전제 수가 적다는 가정 위에 있다.
  전제가 수십 개가 되면 후보 가지치기를 손봐야 한다.

---

## 더 읽을 것

- `attack_graph.md` — 그래프 스키마 상세
- `tool_manifest.md` — 매니페스트 상세
- `tests/test_spec.py` — 각 규칙이 **왜** 있는지가 docstring 에 적혀 있다
