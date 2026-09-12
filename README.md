# WHS Red Teaming Agent — Attack Graph & Tool Manifest

금융권 레드티밍(취약점 분석) AI 의 **계층 사이 계약**. 이 브랜치(`graph`)에 들어 있는
건 실행 코드가 아니라 **자료구조와 규칙**이다.

```
attack graph   = 지금까지 뭘 얻었나 (상태)
tool manifest  = 뭘 더 할 수 있나  (행동)
```

이 둘을 대조하면 **지금 실행 가능한 tool 집합**이 나온다. 그게 이 규격의 존재 이유다.

---

## 먼저 돌려보기 (네트워크 안 탄다)

```bash
pip install -r requirements.txt

PYTHONPATH=. python spec/demo_teams.py      # ★ 한 스텝 전 과정 — 여기부터
PYTHONPATH=. python spec/demo_frontier.py   # 상태가 쌓이며 할 수 있는 게 늘어난다
PYTHONPATH=. python spec/demo_guards.py     # 세 가드가 무엇을 막는가

PYTHONPATH=. python -m spec.schema.validate spec/tools spec/primitives
PYTHONPATH=. pytest spec/tests -q
```

대상 앱을 띄우지 않아도 전부 돈다. 자세한 설명은 **[`spec/README.md`](spec/README.md)**.

---

## 팀 분담

```
① Agent          ② Orchestration · Graph     ③ Capability      ④ Control Plane · Evidence
```

| 팀 | 그래프 | 진입점 | 구현할 것 |
|---|---|---|---|
| ① Agent | 읽기 | `GraphView.observe()` · `mcp_tool_list()` | tool 선택 |
| ② Orchestration | **쓰기** | `compute_frontier()` / `upsert()` / `record_transition()` | 루프 |
| ③ Capability | 읽기 | `ProbeResult` 를 반환할 뿐 | 실제 실행 |
| ④ Control Plane | **쓰기** | `preflight()` / `ingest()` / `to_jsonl`·`from_jsonl` | **`PolicyPort` 구현** |

**경계를 나누는 것보다 경계에서 주고받는 자료구조를 먼저 못 박는 게 본질이다.**
그래서 각 팀이 받을 타입이 이미 다 있다 — ④ 가 준비되기 전에도 ②③ 는
참조 구현(`StaticPolicy`)으로 돈다.

### 한 스텝이 도는 순서

```
① compute_frontier()  정책 먼저(영구 차단) → 전제 나중(일시적 미충족)
② 모델이 고른다        ← 유일한 LLM 개입 지점
③ preflight()         파라미터 형식 · 노드 실존 · 정책 · params 로 바인딩 확정
④ tool 실행 → ProbeResult
⑤ ingest()            오라클 전체 실행 → 판정을 전이에 붙여 기록
⑥ evaluate_objectives()  목표 카탈로그 6개 전부 평가 (줄일 수 없다)
```

★ ③ 이 만든 `bindings` 를 ⑤ 에 **그대로** 넘겨야 한다. 이 인계가 끊어지면
스코프 대조가 조용히 엉뚱한 기준으로 돈다.

---

## 이 규격이 막는 것

전부 **LLM 에게 묻지 않는다.** 선언과 기록을 코드가 대조할 뿐이다.

| 문제 | 어떻게 막는가 |
|---|---|
| 시킨 곳이 아닌 데를 공격한다 | tool 이 건드릴 곳을 선언하고, 실행 후 실제와 대조 |
| 오탐인데 확정이라고 주장한다 | 증거(`evidence_ref`) 없는 판정은 기록 자체가 거부 |
| 못 하는 걸 고르고 실패를 반복한다 | 전제가 안 맞는 tool 은 애초에 목록에 없다 |
| 비밀·PII 가 증거저장소로 샌다 | `sensitive` 선언된 파라미터는 기록 직전 치환 |
| 승인 안 난 권한을 쓴다 | capability 8종이 전부 정책에 묻는다 (import 시점에 단언) |

---

## 저장소 구조

```
spec/
├── schema/          ★ 규격 본체 — 여기만 새 아키텍처로 그대로 간다
├── primitives/      원시 동작 11개  ← 새로 짤 때 쓰는 tool 어휘
├── tools/           레거시 17개     ← 기존 프로브를 옮긴 것. 참고용
├── tests/           회귀 시험 68개
└── demo_*.py        돌려보는 예제 7개
```

`spec/` 은 프로젝트의 다른 코드를 하나도 import 하지 않는다.
pydantic, pyyaml, 표준 라이브러리가 전부다 — 아키텍처가 바뀌어도 그대로 옮겨간다.

---

## 기여할 때

새 tool 을 추가하면 **반드시** 검사기를 통과해야 한다.

```bash
PYTHONPATH=. python -m spec.schema.validate spec/tools spec/primitives
```

CI(`.github/workflows/spec.yml`)가 모든 push/PR 에서 돌린다. 23개 검사의 목적은
문법이 아니라 **선언 누락**이다 — `from_param` 이나 `sensitive` 를 악의 없이
빼먹어도 정책·스코프·비밀 보호가 조용히 안 걸리기 시작하기 때문이다.
사람 리뷰로는 잡히지 않는 종류다.

읽는 법: [`spec/tool_manifest.md`](spec/tool_manifest.md) · [`spec/attack_graph.md`](spec/attack_graph.md)

---

## 범위 (중요)

이 도구는 **승인된 engagement 안에서만** 쓴다. `spec/tools/` 의 `surface` 선언은
훈련용 대상(vulnbank)의 정찰 결과이지 일반 공개용 취약점 정보가 아니다.
`StaticPolicy` 의 기본값은 전부 **금지**다 — 승인서에 적힌 것만 열린다.
