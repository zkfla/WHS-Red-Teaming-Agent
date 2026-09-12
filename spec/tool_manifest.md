# 툴 매니페스트 규격

정본은 `spec/schema/tool_manifest.py` · 검사기는 `spec/schema/validate.py`
적재는 `spec/schema/registry.py` · 정책 계약은 `spec/schema/policy.py`

## 한 줄

**tool 하나 = YAML 한 장.** 그 파일이 "무슨 권한을 쓰고, 어디를 건드리고, 뭐가 있어야
돌고, 돌면 뭐가 생기는지" 를 전부 선언한다. control plane 은 이 선언만 읽고 허용/거부한다.

## 전체 모습

```yaml
id: http_request                 # 파일명과 같아야 한다
version: 1
layer: meta | generic | finance   # 분류일 뿐 — 검사 면제 근거가 아니다(offline 참고)
summary: 이미 발견한 엔드포인트에 임의 메서드·바디로 요청을 보낸다.

# ━━━ control plane 전용 — 모델에 노출 금지 ━━━
capabilities:
  required: [state_change]       # policy.PolicyPort 와 1:1 (import 시점에 단언)
  concurrency: 1
  max_amount_touched: "1000.00"
  request_budget: 3
  max_duration_s: 30.0

effects:
  creates_resource_types: []
  mutates_balance: true
  reversible: false
  repeatable: true

# ━━━ 스코프 — 셋 중 하나는 반드시 있어야 한다 (offline 이 아니면) ━━━
surface:                         # ① 정적 선언 (칠 곳을 미리 아는 tool)
  - {method: POST, path: "/transfer"}
surface_binding: target          # ② 동적 선언 (실행 시점에 정해지는 tool)
surface_prefixes:                # ③ 울타리 (모르는 곳을 찾는 정찰 tool)
  - {methods: [GET, HEAD, OPTIONS], path_prefix: "/api/"}
offline: false                   # true = 네트워크를 안 탄다 → 선언 의무 면제

# ━━━ 그래프 연결 ━━━
preconditions:
  # ★ from_param — "이 전제는 모델이 target 으로 고른 **바로 그** 노드다"
  - {kind: endpoint, attrs: {reachable: true}, bind: target, from_param: target}
postconditions: []
may_produce: [card, merchant, loan]   # 실행 시점에야 정해지는 산출물

# ━━━ 모델에 보이는 부분 ━━━
params:
  target: {type: string, required: true, binds_to: endpoint}
  amount: {type: money, policy_checked: true}
  body:   {type: json, sensitive: true}      # ← 기록에는 «redacted» 로 남는다

# ━━━ 인계 계약 ━━━
mcp: {expose: true, server: finredteam-web, hide_when_unavailable: true}
failure_modes:
  - {code: target_5xx, expected: false, description: 대상이 서버 에러 반복}
cleanup_with: [delete_principal]
cleanup_impossible_reason: ""
```

## 두 부류의 필드

`prompt_view()` / `to_mcp_tool()` 이 모델에 보낼 부분만 잘라낸다 — `id`, `summary`, `params`.

**`capabilities` 를 감추는 이유**: 보이면 "이건 race_testing 필요 없는데요" 라고 우기는
경로가 생긴다. 권한 선언은 협상 대상이 아니다.

**`preconditions` 를 감추는 이유**: frontier 계산에 이미 반영돼 있다. 못 쓰는 tool 은
목록에 없으므로 전제를 읽을 이유가 없다.

## capability 8종 — `PolicyPort` 와 1:1

```python
from spec.schema.policy import CAPABILITY_FLAGS, StaticPolicy, check_manifest

check_manifest(policy, manifest, params)   # 거부 사유 목록. 비면 허용
```

| capability | StaticPolicy 필드 |
|---|---|
| `state_change` | `allow_state_change` |
| `account_creation` | `allow_account_creation` |
| `file_upload` | `allow_file_upload` |
| `delete` | `allow_delete` |
| `race_testing` | `allow_race_testing` |
| `dos` | `allow_dos` |
| `bruteforce` | `allow_bruteforce` |
| `external_service` | `allow_external_service` |

**이 대응은 문서 약속이 아니라 코드가 강제한다.** `policy.py` 가 import 시점에
`CAPABILITY_FLAGS` 가 `Capability` 어휘 전체를 덮는지 단언하므로, 새 capability 를
어휘에만 추가하면 **import 자체가 실패**한다. 검사 없는 권한이 태어날 수 없다.

> 한동안 `race_testing` 과 `bruteforce` 는 어휘에만 있고 검사하는 코드가 어디에도
> 없었다(이 파일 주석이 스스로 "미강제(dead code)" 라고 적어놨다). 즉 동시 발사
> 권한은 선언만 되고 아무도 묻지 않았다. 어휘와 검사가 어긋나면 정책이 조용히
> 안 걸리기 시작한다 — 가장 늦게 발견되는 종류의 사고다.

`check_manifest()` 는 capability 외에 동시성·요청/시간 예산·정적 금액 상한·
**런타임 금액**(`policy_checked` 파라미터)·정적 표면까지 본다.
기본값은 전부 **금지**다 — 승인서에 적힌 것만 열린다.

## 스코프 선언 — 정적 / 동적

칠 곳을 미리 아는 tool 은 `surface` 에 적는다. 실행 후 실제로 만진 곳과 대조한다.

`http_request` 처럼 **어디를 칠지가 파라미터인** tool 은 정적 선언이 불가능하다.
대신 `surface_binding` 으로 "이 파라미터가 대상을 가리킨다" 고 적고, control plane 이
그 값이 **그래프에 실존하는 endpoint 노드**인지 확인한다.

→ 정찰로 발견한 표면만 칠 수 있다. **임의 경로를 지어낼 수 없다.**

실행 후 대조도 된다 — 바인딩된 엔드포인트가 곧 그 실행의 허용 표면이므로,
그 밖을 건드렸으면 `ingest()` 가 잡는다.

### 정찰 tool — 면제가 아니라 울타리

`probe_path` / `discover_surface` 는 **모르는 경로를 찾는 게 목적**이라 정확한 경로를
미리 못 적는다. 그렇다고 면제하면, 하필 **LLM 이 준 문자열이 그대로 요청이 되는
유일한 tool** 이 스코프 대조에서 통째로 빠진다.

```yaml
surface_prefixes:
  - {methods: [GET, HEAD, OPTIONS], path_prefix: "/", note: "읽기 전용으로만"}
```

정확한 경로는 못 적어도 **범위**는 적을 수 있고, 그건 engagement 승인 범위와 대조
가능하다. 승인 범위(`StaticPolicy.allowed_path_prefixes`)보다 넓은 울타리를 선언하면
`check_manifest()` 가 그 tool 전체를 차단한다. 접두어 선언은 **읽기 전용 tool 만**
쓸 수 있다(검사기 ㉓).

> 예전에는 이 두 tool 이 `layer: meta` 로 검사 ⑨ 를 면제받고 `surface: []` 로 있었고,
> `ingest()` 가 빈 선언을 `None` 으로 바꿔 넘겨 drift 검사를 아예 껐다.
> `discover_surface.yaml` 의 주석이 "layer: meta 라 면제받지만 그건 안전해서가 아니다"
> 라고 스스로 적어둔 지점이다. 이제 "네트워크를 타는가" 는 분류(`layer`)가 아니라
> 사실(`offline`)로 판단한다.

## 금지 필드 ★

```
oracles / invariant / expected_verdict / attack_class
```

`charge_idempotency` 에 `oracles: [INV-4]` 라고 적고 싶어진다. 적으면 안 된다 —
**tool 이 판정 범위를 정하는 순간, 실험 하나가 불변식 둘을 동시에 터뜨리는 경우를 놓친다.**

실제 사례: 음수 송금 실험 1건이 INV-1B(부호 반전)와 INV-6(금액 불일치)를 같이 터뜨렸다.
tool 이 오라클을 선언하는 구조였으면 하나만 잡혔다.

tool 은 "어떻게 찌를지" 만 정하고, "무엇이 위반인지" 는 오라클 전체가 매번 돌며 정한다.

## 파라미터

```yaml
params:
  target: {type: string, required: true, binds_to: endpoint}
  amount: {type: money, policy_checked: true}
```

- `binds_to` — 값이 자유 문자열이 아니라 **그래프에 실존하는 노드**여야 한다 (*종류*)
- `from_param`(전제 쪽) — 그 전제가 **바로 그 노드**여야 한다 (*동일성*)
- `policy_checked` — 실행 직전에 `check_amount()` 로 **다시** 검사한다.
  매니페스트의 정적 `max_amount_touched` 로는 런타임에 정해지는 금액을 못 잡는다
- `sensitive` — 기록(`Transition.params`)에서 «redacted» 로 치환된다
- `type: money` 에 float 을 넣으면 거부된다. 범위(`minimum`/`maximum`)도 걸린다
- `type: json` 은 실제로 파싱해본다

선언만 하고 검사를 안 하면 장식이므로, 실제 검사 함수가 붙어 있다:

```python
m.validate_params(given)                      # 형식·enum·범위·float 금지·JSON 파싱
m.check_node_bindings(given, node_kind_of)    # 노드 실존 + 종류 일치
m.redact(given)                               # 기록용 사본 — 비밀 치환
```

### `binds_to` 만으로는 부족하다 ★

```yaml
# ✗ 이것만 있으면 resolve() 가 reachable 한 아무 endpoint 나 집는다
preconditions: [{kind: endpoint, attrs: {reachable: true}, bind: target}]
params: {target: {type: string, binds_to: endpoint}}

# ✓ from_param 이 있어야 "모델이 고른 그것" 으로 확정된다
preconditions: [{kind: endpoint, attrs: {reachable: true}, bind: target, from_param: target}]
```

이 연결이 없으면 모델은 `POST /transfer` 를 골랐는데 `ingest()` 의 대조 기준은
`GET /balance` 가 된다 — 스코프 이탈 탐지 전체가 틀린 값으로 돈다.
검사기 ⑳ 가 CI 에서 막고, `preflight()` 가 런타임에서 한 번 더 잡는다.

### 비밀은 기록에 남기지 않는다

```python
m.redact({"target": "endpoint:POST /register", "password": "Rt!pass123"})
# {"target": "endpoint:POST /register", "password": "«redacted»"}
```

`Transition.params` 는 그대로 JSONL 로 나가 evidence store 에 오래 남는다.
`finance.py` 는 `PrincipalFacts` 에 비밀번호 필드를 **아예 두지 않을** 만큼 엄격한데,
`params` 가 그 원칙을 통째로 우회하는 경로였다 — `create_principal` 의 `password` 와
`extra_fields`(주입용 PII 포함)가 평문으로 샜다.

검사기 ㉑ 이 자격증명류 이름과 `json` 타입 파라미터에 `sensitive` 를 강제한다.

## 정리 의무

자원을 만들면 되돌릴 방법을 선언해야 한다.

```yaml
cleanup_with: [delete_principal, delete_card]
```

되돌릴 방법이 **정말 없으면** 그 사실을 적는다.

```yaml
cleanup_impossible_reason: "청구서 결제는 취소 API 가 없다"
```

가짜 정리 tool 을 만들어 규칙을 통과시키는 것보다 정직하다. Engagement 승인자가
그걸 알고 결정해야 한다. 검사기는 **침묵만** 막는다.

## 리턴 타입 — `ProbeResult`

모든 tool 이 같은 걸 돌려준다. 그래야 control plane 이 어느 tool 이든 같은 방식으로
처리한다.

```
ProbeResult
  └ executions: [ExecutionRecord]
      ├ requests: [RequestRecord]     method / path / status / evidence_ref
      │                               ★ 응답 본문은 안 담는다. 키 이름만.
      ├ value_flow                    잔액 델타
      └ *_signals                     exposure / authz / policy / idempotency / concurrency
```

**오라클은 `ExecutionRecord` 만 본다.** 여기 없는 신호는 판정에 쓸 수 없다 —
"LLM 이 보기에 수상하다" 가 들어올 자리가 구조적으로 없다.

## MCP 변환

```python
m.to_mcp_tool()
# {"name": ..., "description": ..., "inputSchema": {...}}
```

`capabilities` / `surface` / `preconditions` / `effects` 는 나가지 않는다.

`hide_when_unavailable: true` 면 frontier 밖일 때 MCP 목록에서 아예 뺀다.
안 그러면 모델이 못 쓰는 tool 을 보고 고르려 한다.

`annotations.readOnlyHint` 와 실제 동작이 어긋나면 검사기가 막는다 —
모델이 안전한 tool 로 오해하기 때문이다.

## 매니페스트 적재 — `ToolRegistry`

```python
from spec.schema.registry import ToolRegistry

reg, errs = ToolRegistry.load("spec/tools", "spec/primitives")
reg.require("http_request")            # 없으면 KeyError
reg.mcp_tools(available=ready_ids)     # frontier 밖 tool 은 목록에서 뺀다
```

**적재 경로는 이것 하나다.** 예전에는 `yaml.safe_load` + `glob("*.yaml")` 조합이
데모·테스트·검사기에 일곱 군데 복붙돼 있었고, 더 나쁜 건 `manifests[m.id] = m` 로
dict 에 담아서 **두 파일이 같은 id 를 선언하면 하나가 조용히 사라졌다** —
교차 검사는 살아남은 쪽만 봤다. `ToolRegistry.add()` 는 중복 id 를 거부한다.

## 검사기 23가지

```bash
PYTHONPATH=. python -m spec.schema.validate spec/tools spec/primitives
```

**파일 단위 ①~⑭, ⑲~㉓**

| # | 검사 |
|---|---|
| ① | 금지 필드 |
| ② | 스키마 위반 (`extra=forbid` — 오타 필드도) |
| ③ | 파일명 ≠ id |
| ④ | `attrs` / 노드 종류 오타 |
| ⑤ | 잔액 바꾸는데 `state_change` 미선언 |
| ⑥ | 잔액 바꾸는데 금액 상한도 `policy_checked` 도 없음 |
| ⑦ | `concurrency>1` 인데 `race_testing` 미선언 |
| ⑧ | 자원 만드는데 권한이 하나도 없음 |
| ⑨ | 표면 미선언 (정적도 바인딩도 접두어도 없는데 `offline` 이 아님) |
| ⑩ | 잔액 바꾼다면서 읽기 전용 메서드만 |
| ⑪ | `surface_binding` 파라미터가 `binds_to: endpoint` 가 아님 |
| ⑫ | `binds_to` / `may_produce` 가 없는 노드 종류 |
| ⑬ | 자원 만드는데 정리 방법도 "못 치움" 선언도 없음 |
| ⑭ | `readOnlyHint` 와 실제 동작 불일치 / `failure_modes` 미선언 |
| ⑲ | `from_param` 이 없는 파라미터를 가리키거나 `binds_to` 종류가 어긋남 |
| ⑳ | **`surface_binding` 파라미터를 `from_param` 으로 전제와 안 묶음** |
| ㉑ | 자격증명·`json` 파라미터에 `sensitive` 미선언 |
| ㉒ | `distinct_from` / `related_to` 가 없는 bind 를 가리킴 (오타면 제약이 사라진다) |
| ㉓ | 상태를 바꾸는 tool 이 `surface_prefixes` 로 광범위 선언 |

**교차 참조 ⑮~⑱**

| # | 검사 |
|---|---|
| ⑮ | `cleanup_with` 가 실존하는 tool 을 가리키는가 |
| ⑯ | 폐기된 tool 의 대체재가 실존하는가 |
| ⑰ | MCP tool 이름 충돌 |
| ⑱ | **전제로 요구하는 노드를 아무 tool 도 안 만든다** (= 영원히 실행 불가) |

(id 중복은 `ToolRegistry` 가 적재 단계에서 거부한다 — 검사기는 그 오류를 그대로 싣는다)

목적은 문법이 아니라 **선언 누락**이다. 작성자가 악의 없이 빠뜨려도 CI 에서 걸린다.
실제로 작성 중 누락 세 개를 이 검사가 잡아냈고, ⑨ 를 `layer` 대신 `offline` 기준으로
바꾸자마자 `discover_surface` 의 선언 누락이 바로 걸렸다.
