# WHS Red Teaming Agent — 공격 그래프 & 툴 매니페스트

금융권 레드티밍(취약점 분석) AI 의 **뼈대**. 실제로 공격하는 코드는 아직 없고,
각 팀 코드끼리 주고받을 **데이터 형식과 규칙**이 들어 있다.


```
공격 그래프    = 지금까지 뭘 알아냈고 뭘 손에 넣었나   (상태)
툴 매니페스트  = 그래서 뭘 더 할 수 있나              (행동)
```

이 둘을 맞춰보면 **지금 쓸 수 있는 tool 목록**이 자동으로 나온다.
로그인 경로를 아직 못 찾았으면 가입 tool 은 애초에 목록에 안 뜨는 식이다.

---

# 1. 팀 경계

```
① Agent Team    ② Orchestration, Graph    ③ Capability    ④ Control Plane, Evidence
```

이 네 갈래에 맞춰 짰다. **각 팀은 클래스 하나 만들고 정해진 메서드만 채우면 된다.**

| 팀 | 만들 것 | 받는 것 | 돌려줄 것 | 그래프 |
|---|---|---|---|---|
| ① Agent | `AgentPort` | 그래프 요약 + 쓸 수 있는 tool 목록 | `ToolCall` | 읽기만 |
| ② Orchestration | run 전체 — 루프·예산·중단조건·정리·**에이전트 스케줄링** | — | — | **쓰기 가능** |
| ③ Capability | `Executor` | `ExecutionRequest` | `ProbeResult` | 읽기만 |
| ④ Control Plane | `PolicyPort`, `OraclePort` | 정책 질의 / 판정 요청 | 허용여부 / 판정결과 | **쓰기 가능** |

①③ 이 그래프를 직접 못 쓰게 막아뒀다. 직접 쓰면 결과를 넣을 때 하는 검사를
전부 건너뛰게 되기 때문이다. 대신 읽기 전용 창구를 넘기고, 쓰려고 하면 에러가 난다.

## 한 스텝이 도는 순서

```
① 쓸 수 있는 tool 목록을 만든다      ② 가 계산. 정책이 막은 건 여기서 빠진다
② 모델이 고른다                     ① 의 몫. 여기가 유일한 LLM 개입 지점
③ 실행 전 검사                      ④ 의 몫. 값 형식·대상 실존·정책을 본다
④ tool 실행                        ③ 의 몫. 결과만 돌려준다
⑤ 결과를 그래프에 반영               ④ 의 몫. 여기서 판정도 같이 붙는다
⑥ 목표 달성 여부 평가                6개 전부 매번 본다
```

이 순서는 `spec/schema/loop.py` 의 `run_step()` 에 이미 들어 있다.
② 가 자기 루프를 직접 짜도 되지만, ③ 검사가 정한 "대상 노드" 를 ④ 실행과
⑤ 반영에 **똑같이** 넘겨야 한다. 여기가 갈라지면 모델이 고른 곳과 나중에
검사하는 곳이 달라져서, 엉뚱한 곳을 건드려도 못 잡는다.

## 각 팀이 만들 것

### ① Agent Team

에이전트를 여러 개 둘 거면(Recon / web+api / Mobile / Reporting) **각자 이걸 구현하고
②가 스케줄링**하면 된다. `ToolCall.agent_id` 에 자기 이름을 넣으면 기록에 남는다.

tool 을 에이전트별로 나누는 축은 매니페스트에 이미 있다 — `mcp.server` 다
(`finredteam-recon` / `finredteam-web` / `finredteam-mobile` / `finredteam-crypto`).
Mobile Agent 에게 `http_request` 를 보여주면 엉뚱한 걸 고르니까, 목록을 줄 때
자기 서버 것만 거르는 게 좋다.

```python
from spec.schema.ports import AgentPort, ToolCall

class MyAgent:
    def choose(self, observation: dict, tools: list[dict], *, step: int) -> ToolCall | None:
        # observation = 지금 그래프 요약
        # tools       = 지금 쓸 수 있는 tool 목록 (MCP 형식)
        # None 을 돌려주면 "더 할 게 없다" — 루프가 끝난다
        return ToolCall(tool_id="http_request", params={...}, step=step)

assert isinstance(MyAgent(), AgentPort)     # 메서드가 어긋나면 여기서 터진다
```

`tools` 에는 **지금 쓸 수 있는 것만** 들어온다. 정책이 막았거나 조건이 안 맞는 건
애초에 안 보인다. 목록에 없는 걸 고르면 거부되고 그 사실이 기록에 남는다.

`ToolCall.rationale` 에 "왜 골랐는지" 를 적을 수 있다. 리포트에 쓰는 설명용이고
취약점 판정의 근거로는 쓰지 않는다 — 모델이 쓴 문장이라 확인할 방법이 없어서다.

### ② Orchestration, Graph

**이쪽이 제일 많이 남아 있다.** 다른 팀은 인터페이스 하나 채우면 되지만,
② 는 run 전체를 만든다.

`run_step()` 은 **한 스텝 안쪽의 이음매**일 뿐이다. 그 바깥이 전부 ② 몫이다.

```python
from spec.schema.loop import run_step

while ...:                          # ← 이 while 의 조건과 내용이 ② 가 만들 것
    out = run_step(step=n, graph=graph, registry=registry,
                   policy=MyPolicy(), agent=MyAgent(),
                   executor=MyExecutor(), oracle=MyOracle())
```

구체적으로, 스키마에 **자리는 만들어뒀는데 아직 아무도 안 채운 것**이 이만큼이다.
전부 ② 가 할 일이다.

| 할 일 | 쓸 자리 | 지금 상태 |
|---|---|---|
| 언제 멈출지 정하기 | `RunContext.max_steps` · `termination_reason` | 필드만 있고 쓰는 곳 없음 |
| 대상이 죽어가면 중단 | `FailureMode.expected=False` | 이상 신호를 세는 코드가 없음 |
| 요청·시간 예산 누적 | `request_budget` · `max_duration_s` | tool 1건씩만 검사, 합계는 아무도 안 셈 |
| 만든 계정·카드 치우기 | `graph.pending_cleanup()` | 목록만 쌓이고 치우는 코드가 없음 |
| 못 쓰게 된 상태 내리기 | `Node.retract()` | 함수만 있고 부르는 곳이 없음 |
| 단계 관리 (정찰 → 공격) | `ToolManifest.requires_phase` | 선언만 있고 "단계" 개념 자체가 없음 |
| 기준선 먼저 재기 | `requires_baseline_for` | 선언만 있음 |
| run 당 1번만 도는 tool | `effects.repeatable=False` | 선언만 있음 |
| 그래프 첫 세팅 | — | 처음 노드를 누가 심을지 안 정해짐 |
| 모델에게 줄 요약 크기 | `observe(max_per_kind, max_total)` | 값을 누가 정할지 안 정해짐 |

몇 개는 그냥 기능이 아니라 의무다.

- **정리(cleanup)** — 시험하다 만든 계정이 남으면 대상 시스템에 쓰레기가 쌓인 채로
  끝난다. `pending_cleanup()` 이 안 비면 리포트를 못 내게 해뒀는데, 정작 비우는
  코드가 없다. 정리용 tool 3개(`delete_principal` 등)는 모델에게 안 보이게
  숨겨놨으니 ② 가 직접 불러야 한다.
- **중단 조건** — 대상이 5xx 를 반복하면 멈춰야 한다. 매니페스트마다 "이건 예상된
  실패, 이건 이상 신호" 를 구분해 적어뒀는데, 이상 신호를 세는 쪽이 없다.
- **상태 내리기** — 카드 잔액을 다 쓰면 "충전됨" 을 내려야 그 카드를 쓰는 tool 이
  목록에서 빠진다. 안 내리면 못 하는 공격을 계속 시도한다.

그리고 ② 는 ④ 와 함께 **그래프에 쓸 수 있는 두 팀 중 하나**다.
①③ 은 읽기만 되니까, 상태를 바꾸는 일은 전부 이쪽으로 온다.

### ③ Capability

```python
from spec.schema.ports import Executor

class MyExecutor:
    def supports(self, tool_id: str) -> bool:
        return tool_id in ("http_request", "probe_path", ...)

    def execute(self, manifest, request) -> ProbeResult:
        # request.params   실제로 쓸 값
        # request.bindings 어느 대상을 칠지 — 이미 정해져서 온다
        return ProbeResult(...)

assert isinstance(MyExecutor(), Executor)
```

**에러를 던지지 말 것.** 실패해도 `ProbeResult(error=..., failure_code=...)` 로
돌려주면 된다. 실패한 것도 남아야 하는 기록이다.

`request.bindings` 가 곧 "여기만 건드려도 된다" 는 허가증이다. 그 밖을 치면
결과를 반영할 때 자동으로 잡혀서 기록에 남는다.

**웹과 모바일은 같은 구조다.** 채널만 다르고 규칙은 똑같다.

| 웹 | 모바일 | 하는 일 |
|---|---|---|
| `probe_path` | `inspect_apk` | 모르는 표면 찾기 (읽기 전용 울타리 안에서) |
| `http_request` | `app_interact` | **발견한** 표면 건드리기 |
| `endpoint` 노드 | `screen` 노드 | 그 표면이 그래프에 있어야 칠 수 있다 |
| `RequestRecord` | `AppActionRecord` | 결과를 담는 곳 |

모바일 쪽은 웹 걸 그대로 베껴 쓰면 된다. 표면 표기만 이렇게 달라진다.

```
"POST /transfer"                      # 웹 (기본 채널이라 접두어 없음)
"app tap com.bank/LoginActivity"      # 모바일
```

**권장 순서**: 웹 5개(`probe_path` → `create_principal` → `authenticate` →
`observe_state` → `http_request`) → 모바일 2개 → 나머지 →
`concurrent_burst`(진짜 동시 발사) / `forge_token`(JWT) 는 제일 나중.

### ④ Control Plane, Evidence

```python
from spec.schema.policy import PolicyPort, PolicyDecision
from spec.schema.ports import OraclePort

class MyPolicy:      # 승인서에 뭐가 허용됐나
    def allows_capability(self, capability) -> PolicyDecision: ...
    def check_concurrency(self, concurrency) -> PolicyDecision: ...
    def check_amount(self, amount) -> PolicyDecision: ...
    def check_surface(self, method, path) -> PolicyDecision: ...
    def check_budget(self, requests, duration_s) -> PolicyDecision: ...
    def policy_hash(self) -> str: ...

class MyOracle:      # 뭐가 취약점인가 판정
    def evaluate(self, result, view, transition) -> list[FindingRecord]:
        # view 는 읽기 전용이다 — 판정하면서 그래프를 고치면 안 된다
        return [...]
```

`MyPolicy` 는 **이미 돌아가는 예시**(`StaticPolicy`)가 같이 들어 있다. 급하면 그걸
쓰다가 나중에 갈아끼워도 된다. 그래서 ④ 가 준비되기 전에도 ②③ 는 개발할 수 있다.

기록 저장은 `graph.to_jsonl_records()` 로 내보내고
`AttackGraph.from_jsonl_records()` 로 다시 읽는다. 나중에 리플레이나 채점은
다른 프로그램에서 돌기 때문에, 저장한 파일만 받아서 그래프를 다시 세울 수 있어야 한다.

---

# 2. 파일 구조

전부 `spec/schema/` 안에 있다. 역할별로 세 묶음이다.

### 데이터를 정의하는 파일 — 읽기만 하면 된다

| 파일 | 역할 |
|---|---|
| `finance.py` | 돈을 어떻게 적을지. 금액, 출처, 잔액 기록, 돈의 이동 |
| `attack_graph.py` | 그래프 본체. 노드·연결·실행기록·판정·목표. **가장 큼 (995줄)** |
| `tool_manifest.py` | tool 하나를 적는 양식. yaml 한 장의 구조가 곧 이 파일 |

### 계약을 정의하는 파일 — 팀별로 여기 하나씩 만든다

| 파일 | 누가 만드나 |
|---|---|
| `policy.py` | **④** — `PolicyPort` (바로 쓸 수 있는 예시 `StaticPolicy` 포함) |
| `ports.py` | **① / ③ / ④** — `AgentPort`, `Executor`, `OraclePort` |

### 일을 하는 파일

| 파일 | 역할 |
|---|---|
| `registry.py` | yaml 파일들을 읽어 tool 목록으로 만든다 |
| `frontier.py` | "지금 쓸 수 있는 tool 이 뭔지" 계산한다 |
| `ingest.py` | tool 실행 결과를 그래프에 넣는 **유일한 통로** |
| `loop.py` | 위 전부를 올바른 순서로 이어놓은 것 — `run_step()` |
| `validate.py` | yaml 검사기 24개. CI 가 알아서 돌린다 |

### 그 밖에

```
spec/primitives/   기본 동작 13개  ← 새 tool 만들 땐 이것들 조합 (웹 11 + 모바일 2)
spec/tools/        예전에 쓰던 17개 ← 참고용. 대상 주소 목록 겸함
spec/tests/        시험 89개
spec/demo_*.py     돌려보는 예제 7개
```

서로 어떤 순서로 의존하는지 (거꾸로 참조하는 건 없다):

```
finance → attack_graph → tool_manifest → policy · ports · registry
                                                    ↓
                                           frontier · ingest
                                                    ↓
                                                  loop
```

`spec/` 은 프로젝트의 다른 코드를 하나도 가져다 쓰지 않는다.
pydantic, pyyaml, 파이썬 기본 라이브러리가 전부다.

---

# 3. 이 구조가 막는 것

전부 **모델에게 물어보지 않는다.** 미리 적어둔 것과 실제 기록을 코드가 맞춰볼 뿐이다.

| 문제 | 어떻게 막는가 |
|---|---|
| 시킨 곳이 아닌 데를 공격한다 | tool 이 건드릴 곳을 미리 적고, 실행 후 실제와 맞춰본다 |
| 오탐인데 확정이라고 주장한다 | 근거 없는 판정은 저장 자체가 거부된다 |
| 못 하는 걸 고르고 실패만 반복한다 | 조건이 안 맞는 tool 은 애초에 목록에 없다 |
| 비밀번호·개인정보가 로그로 샌다 | 민감하다고 적어둔 값은 기록 직전에 가려진다 |
| 승인 안 난 권한을 쓴다 | 권한 8종이 전부 정책에 물어보게 되어 있다 |

---


# 4. tool 을 새로 만들 때

`spec/primitives/` 에 yaml 한 장 추가하면 된다. 양식은
[`spec/tool_manifest.md`](spec/tool_manifest.md) 에 있고, 기존 파일을 복사해서
고치는 게 제일 빠르다.

검사와 시험은 **CI 가 push 할 때마다 알아서 돌린다.** 따로 실행할 필요 없다.
빠뜨리기 쉬운 두 가지만 기억하면 된다.

- **`from_param`** — "이 조건은 모델이 고른 그 대상이다" 를 적는 것.
  안 적으면 아무 대상이나 잡혀서, 실제로 친 곳과 검사하는 곳이 달라진다
- **`sensitive`** — 비밀번호나 요청 내용처럼 기록에 남으면 안 되는 값에 붙인다.
  안 붙이면 평문으로 저장된다

더 읽을 것: [`spec/attack_graph.md`](spec/attack_graph.md) — 그래프 스키마 상세

---

# 5. 아직 없는 것

- **판정 로직** — 인터페이스(`OraclePort`)는 있지만 내용은 ④ 가 채운다
- **실제 실행** — `primitives/` 는 선언이고, 실제로 요청 보내는 코드는 ③ 가 만든다
- **진짜 정책 객체** — `StaticPolicy` 는 예시다. 승인서 읽기·한도 조절·감사 로그는
  ④ 가 `PolicyPort` 를 만들어 채운다
- **run 운영** — 예산·중단조건·정리·단계 관리. 스키마에 자리는 있고 ② 가 채운다
  (위 ② 절의 표 참고). 지금 `run_step()` 은 한 스텝 안쪽만 본다
- **동시 실행** — 지금은 한 번에 한 스텝씩 도는 걸 전제로 짰다

---
