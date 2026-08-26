# FinRedTeam — 범용 + 금융 특화 Red Teaming Agent (ReAct)

`instruction.md`의 계약을 구현한다. 하나의 로컬 대상(vuln-bank)에 종속되지
않도록, 실제 Red Teaming/Bug Bounty처럼 **사람이 먼저 Scope/Rules of
Engagement를 정의하고 승인해야만 실행되는 Engagement 레이어**(아래 §2)를
거친다. Black-box 정찰 → 그래프 모델링 → 가설/실험 → 오라클 검증 → Verified
Finding. 지표(VVR 등)는 `evaluator/`에서만 계산하고 에이전트는 계산하지 않는다.

**루프 구조는 ReAct 다.** 원래 `instruction.md` §13 pseudocode 는 코드가
전략을 배치로 정해 후보를 일괄 나열하는 결정적 파이프라인이었는데, 사용자
요청으로 "다음에 뭘 할지"를 LLM 이 매 스텝 스스로 판단하는 구조로 다시
설계했다. 이후 한 번 더, "LLM이 공격 템플릿을 고르는 Agent"에서
**"LLM이 보안 가설을 세우고, 코드는 그 가설을 안전하게 실험·판정하는
Agent"**로 다시 다듬었다 (아래 §2 참고). 판정은 시종일관 100% 코드다 —
이건 협상 대상이 아니었다.

이 문서는 **아키텍처**와 **현재 구현 상태**를 다룬다. 설계 이유·불변식의
원본은 `instruction.md`이며 충돌 시 그쪽이 우선한다 (단, 루프 구조는
이 문서가 최신이다 — instruction.md 는 애초의 배치 파이프라인 설명 그대로
남아 있다).

---

## 1. 아키텍처 — ReAct 루프

```
        ┌───────────────────────────────────────────────────────┐
        │  BOOTSTRAP → RECON(passive, 1홉 링크 추출) → ENTITLEMENT 확정 │
        │            → BASELINE 사전학습                             │
        └───────────────────────────────────────────────────────┘
                                  │
                                  ▼
        ┌──────────────────── 매 스텝 ─────────────────────────┐
        │                                                      │
        │   ① Observation 조립 (코드)                           │
        │      - 최근 스텝 스크래치패드(누적, 최근 8개)             │
        │      - 그래프 요약 · 대기 중인 가설 · 예산 · 힌트          │
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

LLM 이 고를 수 있는 건 이 7개뿐이다 — 자유 텍스트 경로·페이로드를 직접 쓰게
하지 않는다. **가설(부작용 없음)과 실험(부작용 있음)을 분리했다** —
`propose_hypothesis`는 그래프 검증만 하고, `execute_experiment`만 실제로
요청을 보낸다.

| 액션 | 입력 | 부작용 | Observation |
|---|---|---|---|
| `observe_graph` | (없음) | 없음 | 그래프 요약 — 알려진 계좌, entitled_own, 크롤로 발견된 엔드포인트 |
| `recall_memory` | `query` | 없음 | 매칭된 사실·교훈 |
| `learn_baseline` | `op` | 정상 파라미터 실행 1회 | 실측 완료 여부 (금액 원문 아님) |
| `discover_surface` | `path, method(GET\|HEAD\|OPTIONS)` | 읽기 전용 요청 1회 | 상태코드 + 응답 본문에서 발견된 경로 후보 (MAX_PROBES 상한) |
| `propose_hypothesis` | `operation, principal_id, target, params` | **없음** — 그래프 검증만 | `hypothesis_id` 또는 검증 실패 사유 |
| `execute_experiment` | `hypothesis_id` | **실제 요청 실행** | 관련된 오라클 전체의 판정 (VIOLATION/ANOMALY 는 전부, 아니면 PASS 요약) |
| `finish` | `reason` | 없음 | 루프 종료 요청 (예산 한도가 항상 우선) |

`propose_hypothesis`의 `target`은 그래프가 실제로 아는 노드 id
(`"account:1234567890"`)여야 한다 — `planning/candidates.py::resolve_skeleton`
이 존재 여부를 검증하고, 없으면 에러 문자열을 그대로 Observation 으로
돌려줘서 LLM 이 스스로 고치게 한다. `operation`(`read_balance`/`transfer`)은
**어떤 메커니즘으로 요청을 만들지**만 고른다 — 오라클을 정하지 않는다.

### 오라클 선택 — candidate 가 아니라 결과가 정한다 ★

`execute_experiment`가 실행되면 `oracles/registry.py`에 등록된 오라클
**전체 집합**이 예외 없이 돈다. `candidate.operation`이 "read_balance"인지
"transfer"인지는 오라클 선택에 전혀 관여하지 않는다 — 각 오라클이 자기
판정 재료(예: `logical_txn_id`, `unit_amount`, 실제 balance delta)가
없으면 스스로 UNKNOWN 을 낸다.

```python
# oracles/registry.py
def run_all(candidate, result, graph=None) -> list[OracleOutcome]:
    outcomes = [fn(candidate, result) for fn in REGISTRY.values()]       # 항상 전체
    if graph is not None:
        outcomes.extend(fn(candidate, result, graph) for fn in GRAPH_REGISTRY.values())
    return outcomes
```

이게 왜 중요한지는 실제로 확인됐다 — 음수 송금 실험 하나에서 **INV-1B 와
INV-6 이 동시에 VIOLATION** 으로 잡혔다(§3 라이브 로그 참고). 예전처럼
"이건 F2 니까 INV-1B 만 본다"고 candidate 가 오라클 범위를 스스로 정했다면
INV-6 위반은 놓쳤을 것이다. §설계원칙 3("오라클은 실행 전에 선언한다")은
candidate 단위가 아니라 **시스템 단위**로 지킨다 — 매번 다르게 정할 수
있는 게 아니라, 애초에 고정된 전체 집합이 돈다.

### 능동 Recon — 초기 정찰 이후에도 표면을 넓힌다

- `recon/mapper.py`: seed_paths 를 fingerprint 한 뒤, 응답 본문(HTML
  href/action, JSON 문자열)에서 발견된 경로를 **1홉만 추가로** fingerprint
  한다(무한 크롤 아님, `MAX_PROBES` 상한).
- `discover_surface` 액션: 루프 실행 중에도 LLM 이 스스로 추측한 경로를
  안전하게(GET/HEAD/OPTIONS 만) 찔러볼 수 있다. 마찬가지로 응답에서 새
  경로가 보이면 같이 알려준다.

### vulnbank.yaml 사전지식 최소화

`targets/configs/vulnbank.yaml`의 `discovery.seed_paths`는 원래 24개
(취약점 대상 엔드포인트를 사실상 다 나열)였는데, 지금은 부트스트랩에
필요한 인증 흐름 + 흔한 API 관례 경로 7개로 줄였다 — `/`, `/register`,
`/login`, `/dashboard`, `/api`, `/api/v1`, `/api/docs`. `bill-payments`,
`merchants/payments/charge`, `forgot-password`(v1~v3), `internal/*` 같은
표면은 이제 설정에 없다 — 위의 1홉 크롤과 `discover_surface` 가 찾아내야
한다(실측: 크롤만으로 `/blog`, `/careers`, `/compliance`, `/privacy`,
`/merchant/login`, `/merchant/register` 등이 저절로 드러났다).

`observation.balance`/`baseline_ops.transfer`(정상 고객이 앱을 쓰면 당연히
아는 "잔액조회·송금 기능이 있다"는 정보)는 그대로 남겨뒀다 — §12 가 원래
허용하는 "사람이 정상 경로를 조사해 설정에 적는" 부분이라, 없애면 black-box
경계가 아니라 baseline 학습 인프라 자체가 무너진다.

### 안 바뀐 것

- **LLM 은 액션만 고른다. 판정은 `oracles/verifier.py`(순수 함수)만 한다.**
- `expected_delta` 는 여전히 `knowledge/baseline.py` 실측값에서만 나온다.
- Black-box, `entitled_*`/`observed_*` 분리, `Money`/`Decimal` 전부 그대로.
- `agent/termination.py` 의 예산·포화·수렴 조건은 LLM 의 `finish` 요청과
  무관하게 항상 최종 결정권을 갖는다(Engagement 의 policy_stop 이 이보다도
  먼저 체크된다 — §2).

### 바뀐 것 — 안전장치가 config.py 하드코딩에서 Engagement 로

`config.py` 의 안전장치는 이제 숫자/불리언 **상한**(전역 최대치)만 갖고
있고, 실제 실행마다 어디까지 허용할지는 승인된 Engagement 가 정한다 — 특히
허용 호스트는 더 이상 `config.py` 에 하드코딩돼 있지 않다(§2). `execute_
experiment`도 실제 요청을 보내기 전에 `CompiledPolicy`(허용 메서드/금액
상한)를 통과해야 한다 — 위 액션 표의 "실제 요청 실행"은 이제 이 검사를
거친 뒤에만 일어난다.

---

## 2. Engagement — Scope / Rules of Engagement

vuln-bank 하나에 종속되지 않게, **CLI 인터페이스에 `--target`/`--engagement`
같은 선택 플래그가 아예 없다.** `finredteam`(서브커맨드·인자 없이) 을 실행
하면 바로 대화형 Engagement Setup 이 시작된다 — vuln-bank 도 특별 취급되는
CLI 대상이 아니라, 대화에서 입력된 여러 Target 중 하나일 뿐이다(그저 저장소에
미리 승인돼 커밋된 예시가 하나 있을 뿐이다).

```
finredteam
      ↓ 이미 승인된 Engagement 가 있으면 재사용할지 먼저 묻는다(번호로 선택)
      ↓ 없거나 새로 만들기로 하면 —
"테스트하려는 대상과 목적을 자유롭게 설명해주세요" (자연어, 한 번에)
      ↓
LLM 이 그 설명을 구조화 필드(Target/Scope/허용·금지 행위/속도·금액 제한/
민감정보/Cleanup/Stop Condition)로 해석하면서, 부족하거나 애매한 항목을
**한 번에 하나씩** 질문으로 구체화한다 — 언급 안 된 항목은 항상 가장
보수적인(거부) 값으로 남는다, LLM 이 스스로 허용 범위를 넓히는 건 스키마
구조상 불가능하다(`engagement/setup.py::_draft_to_engagement`)
      ↓
engagement.md (사람이 읽는 RoE) + engagement.yaml (구조화 정책) + manifest.json
(engagement_id 는 사용자가 짓지 않는다 — 시스템이 감사/저장용으로 자동 생성)
      ↓ 사람이 최종 승인해야 approved_at 이 찍힘 — 그 전엔 저장도 안 됨
      ↓
Policy Compiler(`engagement/compiler.py::compile_policy`)
      = Engagement ∩ Global Safety Ceiling(`config.py`)
      ↓
CompiledPolicy — `probing/session.py::Session._request()` 가 실제 네트워크
전송 직전 항상 재검사(호스트/메서드/금액), `agent/react.py` 는 그 전에 한 번
더 사전 검사해서 LLM 에게 왜 막혔는지 알려준다(이중 방어, 우회 불가)
      ↓
승인된 그 Engagement 를 **그 자리에서 바로 실행한다** (BOOTSTRAP → RECON →
… → ReAct 루프, §1)
```

핵심 규칙: **Engagement는 전역 안전 상한보다 느슨해질 수 없다** — 숫자(속도/
동시성/금액)는 `min(engagement, 전역상수)`, DoS/브루트포스/레이스 테스트는
지금 코드베이스에 그걸 실행할 액션 자체가 없어서 전역 상한 자체가 항상 거짓
으로 고정돼 있다.

`finredteam setup` 서브커맨드는 같은 대화형 흐름을 실행 없이 준비만 해둔다
(미리 만들어두고 싶을 때). vuln-bank는 `engagements/examples/vulnbank/`에
미리 승인된 예시로 커밋돼 있어서, 그것 하나만 있는 상태로 그냥 `finredteam`
을 실행하면(대화형이면 재사용 여부를 묻고, 자동화 환경이면 후보가 정확히
하나뿐이라 자동 재사용) 바로 실행된다. 실행 방법은 `RUNNING.md` 참고.

---

## 3. 모듈 구조 (현재)

```
finredteam/
├── __main__.py          CLI — 인자 없이 실행하면 바로 Engagement Setup+실행 | setup 서브커맨드 | --smoke | --fake-llm
├── setup_cli.py           `finredteam setup`/bare 실행 공용 — LLM 클라이언트 지연 생성(build_llm_factory) (로직은 engagement/setup.py)
├── config.py              안전장치 상수(MAX_AMOUNT, MAX_PROBES, MAX_REQUESTS_PER_SECOND, ...) + LLMConfig/RunConfig
│                          — 허용 호스트는 여기 없다, Engagement 가 실행마다 명시적으로 선언한다(§2)
├── credentials.py          API 키 또는 `claude setup-token` OAuth 토큰 대화형 선택/입력 +
│                          로컬 저장(.finredteam/credentials.json)
├── logging_setup.py        콘솔(레벨 조절)+파일(runs/<id>/agent.log, 항상 DEBUG) 로깅 설정
├── cli_render.py           rich 렌더링 공통 함수 — __main__.py(실시간)·replay.py(사후 재생)가 공유
├── replay.py               저장된 트레이스를 색깔·표 입혀 재실행 없이 다시 보기
├── money.py               Decimal 전용 Money/Currency — float 유입을 타입 레벨에서 차단
├── jsonpath.py            targets/*.yaml 용 최소 JSONPath 서브셋
│
├── engagement/             ★ Scope/Rules of Engagement 레이어 (§2)
│   ├── schema.py            구조화 Engagement 모델(bool/enum/숫자/list — 자유 텍스트는 guidance 하나뿐)
│   ├── setup.py             자연어 설명 → LLM 이 한 번에 하나씩 질문해 구체화(EngagementDraft/
│   │                          ClarificationTurn) + 재사용 제안 + engagement_id 자동 생성 +
│   │                          `_draft_to_engagement`(언급 안 된 필드는 전부 False/보수적 기본값 강제)
│   ├── store.py             engagement.md/engagement.yaml/manifest.json 저장 + 승인(approve)
│   ├── loader.py             engagement.yaml 로드 + engagement_id/경로로 examples/ 또는 자체 디렉터리 자동 탐색
│   ├── compiler.py           compile_policy() — Engagement ∩ Global Safety Ceiling(config.py)
│   ├── policy.py             CompiledPolicy — is_host_allowed/is_method_allowed/check_request/check_amount
│   ├── cleanup.py            생성 리소스 추적 + CLEANUP phase(정리 방법 없으면 정직하게 "not_configured")
│   └── errors.py             PolicyViolation — 네트워크 경계에서 실제로 던져지는 예외
│
├── models/                core(Verdict/Principal/Endpoint), state(ObservedState/StateDelta),
│                          finding(CandidateFinding — attack_class 없음, operation 로 대체),
│                          knowledge(Fact/Lesson(operation)/Entitlement)
│
├── llm/                   base(LLMClient 프로토콜, LLMError, LLMEvent) · anthropic_ · openai_ ·
│                          chatgpt_subscription(★ `codex login` 구독 인증, `complete_json()`만 구현 —
│                          `chatgpt.com/backend-api/codex/responses`, API 요금 없음) · codex_auth
│                          (`~/.codex/auth.json` 로더) · factory · fake(가짜 LLM — API 키 없이 배관 시험용)
│
├── storage/                db(JSONL, steps/llm_calls/created_resources/cleanup 포함 16개 컬렉션)
│                          · evidence(민감정보 redaction) · run_store(manifest.json — engagement_id/policy_hash 포함)
│
├── targets/                loader.py + configs/vulnbank.yaml(seed_paths 최소화, §1 참고 — 어댑터 설정,
│                          Engagement 와는 별개 축)
│
├── probing/                session(★ Engagement 정책 강제 + Decimal 안전 HTTP — 실제 네트워크로 나가는
│                          유일한 지점) · executor(실행)
│   ├── finance/            state(잔액 관측) · burst(F4 동시요청 + 재시행)
│   └── generic/             web(SQLi boolean-diff) · api(JWT alg=none 위조) · external(인터페이스만)
│
├── recon/                  discovery · fingerprint(passive + 응답 본문 경로 추출) ·
│                          entitlement(self_created 등) · mapper(1홉 크롤 포함)
│                          · ★ form_discovery(HTML form + 연결된 JS fetch() 핸들러
│                          스크래핑 primitive) · auth_discovery(로그인/회원가입
│                          endpoint·필드·토큰·헤더스킴 발견, BOOTSTRAP 전 사전 크롤
│                          기반) · operation_discovery(송금형 폼 발견 + 자기 잔액
│                          렌더링 텍스트 관측) — 전부 실패하면 예외 없이 None,
│                          호출부가 LEGACY_BOOTSTRAP(사람이 작성한 어댑터 YAML)로
│                          폴백한다
│
├── knowledge/               graph(entitled_*/observed_* 분리, networkx) · baseline(실측 기준선)
│                          · schema · sensitive(민감등급) · inference(관측→Fact)
│
├── memory/                  working(단기) · semantic(Fact) · episodic(operation 별 시도이력·Lesson)
│                          · summarizer(3k 토큰)
│
├── planning/
│   ├── actions.py          ★ ReAct 액션 스키마 (observe_graph/recall_memory/learn_baseline/
│   │                          discover_surface/propose_hypothesis/execute_experiment/finish)
│   ├── candidates.py        AttackTemplate 로더(개념 설명용) + resolve_skeleton(operation+target
│   │                          검증, 오라클 안 정함) + graph_menu(발견된 엔드포인트 포함)
│   ├── planner.py           baseline 선형 대입(_expected_delta_for) + build_candidate
│   ├── prompts.py           그래프 스키마 + invariant 목록 + 액션 카탈로그 + Engagement guidance(소프트) 조립
│   └── prioritizer.py       already_tried_and_failed (중복 실험 감지)
│
├── oracles/                 base · value(INV-1A/1B) · amount(INV-6) · idempotency(INV-4)
│                          · concurrency(INV-1C) · authz(INV-2, graph 필요) · generic(SQLi/XSS/BOLA/JWT)
│                          · registry(★ candidate 가 못 줄이는 오라클 전체 집합, INV-2 만
│                          │   GRAPH_REGISTRY 로 별도 디스패치)
│                          · verifier(run_all_outcomes 전체 + verify 단일 요약)
│                          — ★ 전부 순수 함수. LLM·네트워크 import 를 소스 검사로 강제(tests/test_oracles.py)
│
├── agent/
│   ├── budget.py            토큰/스텝 예산
│   ├── phase.py             Phase 상수
│   ├── strategy.py           suggest_nudge(ReAct 힌트 전용 — 강제 배치는 완전히 제거됨)
│   ├── termination.py        Engagement policy_stop(최우선) + 예산/포화/수렴/대상이상, LLM finish 요청보다 항상 우선
│   ├── react.py              ★ 메인 루프 — AgentContext(policy/created_resources 포함), Step, 액션 디스패치,
│   │                          CLEANUP phase(_finalize)
│   └── loop.py               react.py 로 위임하는 얇은 진입점 (하위 호환용 이름 유지)
│
├── templates/attacks.yaml   개념만 — procedure/payload/endpoint 금지. oracle 필드는 참고용일 뿐
│                            (더 이상 오라클 실행 범위를 정하지 않는다)
│
├── reporting/                report(by_operation + by_invariant 분리 집계, VVR 계산 안 함)
│                          · transcript(자동 생성 사람이 읽는 요약) · repro(curl 재현) · bugbounty(서술)
│
└── evaluator/                scorer(★ invariant+endpoint 기준으로 GT 매칭 — attack_class 없음)
                             · ground_truth.yaml(attack_class 라벨 없음, invariant 만)
                             · reset_env(에이전트와 완전 분리)

engagements/examples/vulnbank/   vuln-bank 용 미리 승인된 Engagement (커밋됨) — §2
```

**핵심 관례**: `oracles/`는 `llm/`·`probing/`·`storage/`를 import 하지 않는다
— 순수 함수라는 §8 규칙을 소스 검사 테스트(`test_oracles_are_pure_no_llm_or_network_imports`)로
강제한다. `agent/react.py`는 항상 최상위에서 나머지를 조립만 한다.

---

## 4. 구현 현황

Day 1~7 전부 구현 완료 + 실제 vuln-bank(Commando-X/vuln-bank, 로컬 Docker) 대상
라이브로 검증.

★ **Human pre-analysis 제거(2026-08-26)**: 사람은 Base URL, 허용 Scope,
테스트 Credentials, Rules of Engagement만 제공한다. 로그인/회원가입 endpoint,
인증 흐름, 파라미터 구조, 송금 같은 비즈니스 Operation, 정상 상태 변화는
FinRedTeam이 Black-box 관측과 능동 Recon으로 직접 발견·학습한다
(`recon/form_discovery.py` + `recon/auth_discovery.py` + `recon/
operation_discovery.py`) — Target 소스코드나 사람이 사전 분석한 내부 구조·
취약점 정보는 Agent 입력으로 쓰지 않는다. `targets/configs/vulnbank.yaml`의
`adapter.auth`/`baseline_ops`는 이제 discovery가 실패했을 때만 쓰이는
LEGACY_BOOTSTRAP 폴백이며, 실제로 쓰이면 `manifest.legacy_bootstrap_used`에
정직하게 기록된다(숨기지 않음, §5절 "이 실행이 어느 범위까지 완전한
autonomous black-box인지" 참고). 라이브 실측(2026-08-26): vuln-bank 대상
로그인·회원가입·송금 Operation·baseline까지 전부 `legacy_bootstrap_used: []`
(완전 자동 발견)로 부트스트랩 완료 확인.

**라이브로 확인된 VIOLATION** (가장 최근 `--fake-llm` 실행, 오라클 전체 실행 기준):
- **transfer/INV-1B + INV-6 동시 확정** — 음수 송금 시 부호 반전(INV-1B) 이자
  동시에 부호 불일치(INV-6) — candidate 가 오라클 범위를 스스로 정하던 예전
  설계였다면 하나만 잡혔을 것.
- **read_balance/INV-2** — `/check_balance/<계좌>` 인증 없이 타인 잔액 조회 (BOLA).
- **G-SQLI** (별도 스크립트로 검증) — `/transactions/<계좌>` boolean-based
  injection + DB 에러 메시지 노출.

**F4(경쟁조건)**: 오라클(`oracles/concurrency.py`)과 재시행 메커니즘
(`probing/finance/burst.py`, RACE_TRIALS 반복 + 잔액 회복 + 재현율 집계)은
구현·라이브 실행까지 했으나, 대상이 Flask 비-threaded 개발서버라 실행에서는
재현되지 않음(PASS 로 정확히 판정되는 것 자체가 메커니즘이 올바로 동작한다는
증거). `propose_hypothesis`로는 아직 낼 수 없다(§17 우선순위 참고).

**능동 Recon 실측**: seed_paths 를 7개로 줄인 상태에서도 `/blog`, `/careers`,
`/compliance`, `/privacy`, `/merchant/login`, `/merchant/register` 등이
1홉 크롤만으로 발견됐다. `discover_surface` 로 `/api/v1/payments/charge`,
`/api/v1/merchants/register` 처럼 설정에 없는 경로도 직접 찔러 존재
여부(405=존재, 404=없음)를 스스로 확인하는 것까지 확인.

**정직하게 밝히는 한계**:
- **Black-box Auto-Discovery(§ recon/form_discovery.py 등)의 한계**: 정규식/
  `HTMLParser` 기반 best-effort라 완전한 JS 파서가 아니다 — `<form>` + 별도
  정적 JS의 `fetch()` 호출(FormData 또는 이름 있는/인라인 익명 핸들러)
  패턴에서는 실측 확인됐지만, 번들링·난독화된 JS나 SPA 프레임워크(React/Vue
  등)가 가상 DOM으로 렌더링하는 폼에는 안 통할 수 있다 — 그 경우 예외 없이
  `None`을 돌려주고 LEGACY_BOOTSTRAP(사람이 작성한 어댑터 YAML)으로 자동
  폴백하며 `manifest.legacy_bootstrap_used`에 정직하게 기록된다. Operation
  Discovery(`recon/operation_discovery.py`)로 학습한 baseline은 payer 쪽
  델타만 실측한다(payee 쪽까지 관측하려면 로그인 페이지 재요청이 더 필요해
  이번 패스에서는 범위 밖) — payer 델타만으로도 `provenance="baseline_
  observed"`는 유효하다, 완전하지 않을 뿐이다. 인증된 "홈" 페이지 후보
  경로(`/dashboard` 등)는 사람이 이 대상을 조사해 적어둔 값이 아니라 흔한
  웹앱 관례 목록(기존 discovery.seed_paths 의 "정상적으로 앱을 쓰면 자연히
  보이는 최소한" 취지와 동일)이다.
- `resolve_skeleton` 에 `read_balance`/`transfer` 두 operation 만 구현되어
  있다. 멱등성(INV-4)/경쟁조건(INV-1C) 후보를 `propose_hypothesis` 로
  자동 생성하는 것은 아직 없다 — 오라클 자체는 있지만 실험 생성기가 없다.
- Generic Layer(`probing/generic/*`)는 아직 액션에 안 붙어 있다 — 별도
  스크립트로 직접 호출해서 라이브 검증만 했다.
- `discover_surface` 가 새 경로를 발견해도, 그걸 바로 `propose_hypothesis`
  의 `operation`으로 실험하는 자동 연결은 아직 없다(발견 → 그래프에 사실로
  남음 → 사람이 보거나 다음 설계 확장에서 자동화).
- OpenAI 프로바이더(`llm/openai_.py`)는 코드는 있지만 실제 API 키로
  라이브 테스트하지 않았다. ChatGPT Subscription 프로바이더
  (`llm/chatgpt_subscription.py`, `codex login` 구독 인증)는 실제 크리덴셜로
  vuln-bank 대상 8스텝 라이브 실행까지 검증됐다(BOLA 2건 확정 탐지) — 단,
  OpenAI 쪽 strict JSON schema 제약 때문에 `{"items":[...]}` 래퍼를 가끔
  생략하고 응답하는 경향이 있어 방어적으로 언래핑한다(`_unwrap_items`),
  토큰 자동 갱신은 없어 만료되면 사람이 `codex login` 을 다시 해야 한다.
- **Engagement 레이어(§2)의 한계**: `max_concurrency`/`allow_file_upload`/
  `allow_race_testing`은 컴파일은 되지만 대응하는 실행 가능한 액션이 아직
  없어서(동시 요청을 실제로 보내는 ReAct 액션 자체가 없음) 소비되지 않는다.
  Stop Condition은 `repeated_5xx`/`request_budget_exceeded`/
  `unexpected_sensitive_data`(CRITICAL 등급 필드 노출, 기본 활성화)/
  `latency_degradation`(baseline 대비 연속 초과, 오탐 위험 때문에 기본
  비활성화 — opt-in)이 실제로 `agent/react.py::AgentContext.policy_stop_reason()`
  에 연결되어 있다. `external_side_effect`만 Hard Stop으로 승격하지 않았다
  — 블랙박스 환경에서는 실제 외부(제3자) 부작용 발생을 결정적으로 관측할
  방법이 없어서(out-of-scope 요청 자체는 이미 `CompiledPolicy.check_request`
  가 원천 차단함), 응답 본문 키워드 기반 best-effort 신호만 Observation에
  참고 노트로 남긴다(실행을 멈추지 않음, `ctx._external_side_effect_hits`
  카운터는 로깅용으로만 존재). `allow_account_creation=false`라도
  `engagement.target.credentials`(사용자가 이미 갖고 있는 테스트 계정)로
  제공된 만큼은 등록 없이 로그인만 하고 실행된다 — 여전히 등록 없이는 못
  채우는 principal이 남으면 `PolicyViolation("ACCOUNT_CREATION_FORBIDDEN")`
  으로 fail-closed. `data_access.allow_raw_response_storage=false` 강제도
  없다 — evidence 저장은 Finding 검증 체인의 핵심이라 이번엔 건드리지 않았다.

---

## 5. 사용법

빠르게 명령만 찾으려면 `RUNNING.md` 를 봐도 된다 (API 키 대화형 입력 방법 포함).

최초 1회, 가상환경에 이 패키지를 editable 로 설치하면 `python -m finredteam...`
대신 한 단어 명령(`finredteam`, `finredteam-replay`, `finredteam-scorer`)을 쓸 수 있다:

```bash
pip install -e .
```

```bash
# 단위 테스트 (네트워크 불필요)
python -m pytest tests/ -v

# vuln-bank 는 engagements/examples/vulnbank/ 에 미리 승인된 Engagement 가
# 커밋돼 있다 — 그것 하나만 있으면 아래처럼 인자 없이 실행할 때 자동으로
# (또는 대화형이면 물어보고) 재사용된다. --target/--engagement 같은 선택
# 플래그는 없다.

# 부트스트랩 + manifest 만 확인 (Day1 범위)
finredteam --smoke

# API 키 없이 ReAct 루프 배관 확인 (결정적 가짜 LLM — Engagement Setup 대화도 즉시 끝남)
finredteam --fake-llm --max-rounds 10

# 실제 LLM 으로 — 진짜 탐색. 환경변수를 미리 안 넣으면 터미널에서 대화형으로
# 물어본다(저장된 것 중 선택 또는 새로 입력 — 자세한 건 RUNNING.md 4절).
# API 키 대신 `claude setup-token` 으로 만든 OAuth 토큰도 그대로 쓸 수 있다
# (ANTHROPIC_AUTH_TOKEN). provider 는 세 가지(anthropic/openai/
# chatgpt_subscription) 중 환경변수/`~/.codex/auth.json` 으로 자동 감지되고,
# 대화형 터미널이면 감지된 걸 기본값으로 매번 직접 고를 수도 있다(RUNNING.md 4절).
export ANTHROPIC_API_KEY=sk-ant-...
finredteam --max-rounds 20

# 새 대상을 처음 테스트할 때는 그냥 finredteam 을 실행하면 대화가 시작된다
# ("테스트하려는 대상과 목적을 자유롭게 설명해주세요" — RUNNING.md 3절)

# GT 대비 채점 (에이전트와 완전 분리된 별도 프로세스)
finredteam-scorer runs/<run_id>

# 실행이 끝난 뒤(또는 다른 사람이 공유한 run_id 로도) 트레이스를 색깔·표 입혀 다시 보기
finredteam-replay latest              # 가장 최근 실행
finredteam-replay <run_id> --step 5   # 스텝 하나만 깊게(LLM 원문 포함)
```

`pip install -e .` 를 안 했다면 위 세 명령은 각각
`python -m finredteam`, `python -m finredteam.replay`,
`python -m finredteam.evaluator.scorer` 로도 그대로 동작한다 (완전히 동일한 코드,
`pyproject.toml` 의 `[project.scripts]` 는 진입점 이름만 짧게 해줄 뿐이다).

실행 중 콘솔에 찍히는 색깔/표(Panel·Table)는 `finredteam/cli_render.py` 에
`__main__.py`(실시간 실행)와 `replay.py`(사후 재생)가 공유하는 렌더링 함수로
모여 있다 — 그래서 실행 중 화면과 나중에 `replay` 로 다시 보는 화면이 같은
모양이다.

디버깅 인프라(로그 파일·LLM 원문 저장·자동 트랜스크립트·`replay`)는
`DEBUGGING.md` 에 별도로 정리되어 있다.

---

## 6. 우선순위 (시간이 부족할 때)

`instruction.md` §17 그대로: **Day 5 > Day 6 > Day 4(baseline) > Day 7.**
`money.py`, `entitled_*/observed_*` 분리, `expected_delta.provenance` 강제,
그리고 **"판정은 항상 코드가 한다 + candidate 가 오라클 범위를 스스로
좁히지 않는다"**는 어떤 경우에도 빼지 않는다 — 판정의 정당성이 걸려 있다.
