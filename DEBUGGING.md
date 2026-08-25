# 실제 LLM API 연동 디버깅 계획

`--fake-llm` 로는 배관(plumbing)만 검증된다 — 결정적이고, 항상 같은 순서로
움직이고, 실패할 수가 없게 짜여 있다. 실제 Anthropic/OpenAI API를 넣고
돌리면 완전히 다른 종류의 문제가 생긴다: LLM이 이상한 액션을 고르거나,
JSON이 깨지거나, 같은 실수를 반복하거나, 네트워크가 끊기거나, 예산을
이상하게 빨리 소진하거나. **원래는 이런 상황에서 "뭐가 문제였는지" 알
방법이 거의 없었다** — 이 문서는 그걸 채운 계획과, 실제로 구현된 결과를
같이 담는다(§0).

## 0. 구현 현황 — 전부 구현됨

| 항목 | 상태 |
|---|---|
| 3.1 실시간 콘솔 스트리밍 (`on_step`) | ✅ 구현됨 |
| 3.1/3.3 LLM 호출 시작/종료 이벤트 + timeout | ✅ 구현됨 (아래 §3.1 개정판) |
| 3.2 구조화 로깅 (`logging` 모듈, 파일 핸들러 포함) | ✅ 구현됨 — `finredteam/logging_setup.py`, `runs/<id>/agent.log` |
| 3.3 LLM 호출 원문 영속화 (`llm_calls`) | ✅ 구현됨 — provider 내부 trace hook 방식으로 (아래 §3.3 개정판) |
| 3.4 `LLMError` vs 코드 버그 구분 | ✅ 구현됨 (아래 §3.4 개정판) |
| 3.4 최상위 예외 처리 (finalize 보장) | ✅ 구현됨 |
| 3.5 자동 트랜스크립트(`transcript.md`) | ✅ 구현됨 — `reporting/transcript.py`, 매 `store.finalize()` 직후 자동 생성 |
| §3.6(신규) `replay` — 저장된 트레이스 색깔·표 입혀 재생 | ✅ 구현됨 — `finredteam/replay.py` |
| §3.7(신규) 세 번째 실패 종류 — `PolicyViolation`(Engagement 정책 위반) | ✅ 구현됨 — `engagement/errors.py`, 아래 §3.7 |

### 3.6 `replay` — 저장된 트레이스를 색깔·표 입혀 다시 보기

`transcript.md`/`agent.log` 는 파일이라 에디터로 열어야 한다. 터미널에서
바로, 실시간 스트리밍(§3.1)과 같은 느낌으로 다시 보고 싶을 때 쓴다 —
새로 실행하지 않고 저장된 `steps`/`llm_calls`/`findings`/`manifest` 만 읽는다.

```bash
python -m finredteam.replay latest              # 가장 최근 실행
python -m finredteam.replay <run_id>             # run_id 로 (경로도 가능)
python -m finredteam.replay <run_id> --full       # Observation 을 자르지 않고 전체
python -m finredteam.replay <run_id> --step 5     # 스텝 5만 깊게 — 매칭되는 LLM 호출 원문(프롬프트·raw 응답)까지

# pip install -e . 했으면 한 단어로: finredteam-replay latest
```

헤더에 어떤 Engagement/정책으로 실행됐는지도 같이 보여준다(`engagement_id`
+ `policy_hash`, manifest.json 에 실행 시점에 고정 저장됨 — §3.7 참고).
`__main__.py`(실시간 실행)와 `replay.py`(사후 재생)의 렌더링은
`finredteam/cli_render.py` 하나를 공유한다 — 그래서 두 화면이 같은 모양이다.

가독성을 위해 한 것들:
- Observation 이 JSON 이면 `rich` 의 JSON 신택스 하이라이팅으로 예쁘게 찍는다.
- VIOLATION/ANOMALY/PASS 여부에 따라 Observation 줄 색이 바뀐다(빨강/노랑/초록).
- 액션 종류별로 색을 다르게 해서(`propose_hypothesis`=노랑, `execute_experiment`=초록 등)
  스캔하기 쉽게 한다.
- 기본은 Observation 을 600자에서 자른다 — `--full` 로 전체를 본다.
- 끝에 VIOLATION 요약 표와 `transcript.md`/`agent.log` 경로를 같이 보여준다.

**`llm_calls` 를 스텝과 연결하는 법**: `LLMEvent`(`llm/base.py`) 자체는
"스텝"이라는 ReAct 개념을 모른다(그래야 `llm/` 이 벤더/프로토콜 계층으로만
남는다) — 대신 `__main__.py::_make_trace_hook` 이 `CALL_STARTED` 가 매 스텝
정확히 한 번 발생하는 것을 이용해 카운터로 `step` 번호를 그 자리에서 찍어
저장한다. `tests/test_replay.py::test_llm_trace_hook_stamps_step_per_call_started`
가 이 매칭을 고정한다.

### 3.7 세 번째 실패 종류 — `PolicyViolation` (Engagement 정책 위반)

§3.4 는 실패를 "API 자체의 실패(`LLMError`)" vs "우리 코드의 버그(그 외
모든 예외)" 둘로만 나눴는데, Engagement 레이어가 생기면서 **의도적으로
차단된 것**이라는 세 번째 종류가 생겼다 — 이건 버그도 API 실패도 아니라
"LLM이 Engagement 가 허용하지 않는 걸 고름"이라는, 정상적으로 예상 가능한
상황이다.

`engagement/errors.py::PolicyViolation(rule, detail)` 이 이 실패를 나타낸다.
두 군데서 다르게 처리한다:

- **`agent/react.py::_handle_execute_experiment`(사전 검사)**: 메서드 허용
  여부는 `policy.is_method_allowed()` 로 예외 없이 직접 물어보고, 금액
  상한은 `policy.check_amount()` 가 던지는 `PolicyViolation` 을 그 자리에서
  잡는다 — 두 경우 다 예외가 위로 새지 않고 `"정책 위반으로 차단됨(<rule>):
  <detail>"` 을 Observation 으로 돌려준다. LLM이 다음 스텝에서 왜 막혔는지
  보고 다른 걸 시도할 수 있다. `steps.jsonl`/`transcript.md` 에 다른 실패와
  똑같이 남는다 — 별도 컬렉션 없음.
- **`probing/session.py::_request()`(최종 강제, 방어 이중화)**: 여기서
  `PolicyViolation` 이 실제로 발생하면(=위 사전 검사를 우회/누락한 버그가
  있다는 뜻) 잡지 않고 그대로 위로 전파한다 — `agent/react.py::run()` 의
  최상위 `except Exception` 이 잡아 `logger.exception` + `manifest.
  termination_reason = "error: PolicyViolation"` 으로 남기고 크래시한다.
  **의도적으로 조용히 넘기지 않는다** — 사전 검사가 뚫렸다는 건 코드 버그이지
  "LLM이 이상한 걸 골랐다"는 정상 상황이 아니기 때문이다.
- **`__main__.py::main()`**: 부트스트랩 단계의 `ACCOUNT_CREATION_FORBIDDEN`
  처럼 루프 진입 전에 발생하는 `PolicyViolation` 은 여기서 잡아 붉은 글씨로
  사유를 보여주고 `exit code 1` 로 깔끔히 끝낸다(트레이스백 없음).

`runs/<id>/agent.log` 에서 `PolicyViolation` 을 찾으면(§4 표에도 추가) 코드
버그(사전 검사 누락)인지 아니면 그냥 `manifest.termination_reason` 이
`policy_violation:*` 로 끝난 정상적인 거부인지 구분해서 본다.

`§3.1`, `§3.3`, `§3.4` 는 최초 계획에서 한 번 더 개정됐다 — 아래 내용이
실제 구현과 일치하는 최신판이다. 원래 계획에서 뭐가, 왜 바뀌었는지는 각
절 끝의 "개정 이유"에 남겨둔다.

---

## 1. 지금 뭐가 안 보이는가 (문제 정의)

`agent/react.py::run()` 을 실제 LLM으로 돌렸을 때 발생 가능한 문제와,
지금 그걸 진단할 방법이 있는지 정리하면:

| 문제 상황 | 지금 진단 가능한가 |
|---|---|
| LLM이 이상한 JSON을 냄 (파싱 실패) | ❌ — `llm/anthropic_.py::complete_json` 이 원문을 버리고 "파싱 실패" 문자열만 Observation 에 남긴다 |
| LLM이 존재하지 않는 hypothesis_id 를 씀 | 🟡 — Observation 에 에러 문자열은 남지만, 그 직전에 LLM이 실제로 뭘 봤는지(프롬프트 원문)는 없다 |
| 특정 스텝에서 멈춤/오래 걸림 | ❌ — `_run_full()` 이 `agent_loop.run()` 이 **끝난 뒤에만** 전체 트레이스를 출력한다. 진행 중엔 아무것도 안 보인다 |
| 네트워크/레이트리밋 예외 | ❌ — `llm.complete_json()` 호출부에 try/except 가 없다. 예외가 나면 `run()` 전체가 크래시하고 `store.finalize()` 가 호출되지 않아 manifest 에 종료 사유도 안 남는다 |
| 토큰 예산이 예상보다 빨리 참 | 🟡 — 최종 manifest 에 총합만 남는다. 어느 스텝에서 얼마나 썼는지, 시스템 프롬프트가 얼마나 큰지는 안 보인다 |
| 같은 액션을 계속 반복함(루프에 빠짐) | 🟡 — `steps.jsonl` 을 사후에 직접 열어봐야 알 수 있다. 실행 중엔 안 보인다 |
| `discover_surface`/`propose_hypothesis` 가 계속 거절당함 | 🟡 — Observation 에 이유는 남지만, LLM이 그 이유를 실제로 "읽고" 반응했는지는 다음 스텝의 Thought 를 봐야 알 수 있다 |

핵심 결핍 두 가지로 요약된다:

1. **LLM 호출 원문(prompt/response)이 어디에도 남지 않는다** — `steps` 컬렉션엔
   `thought`/`action`/`action_input`/`observation`만 있고, 그걸 만들어낸
   프롬프트 원문과 벤더 응답 원문은 없다.
2. **실행 중 아무것도 안 보인다** — 다 끝나야 콘솔에 트레이스가 찍힌다.
   실제 API는 스텝당 수 초가 걸릴 수 있어서, 20스텝짜리 실행이면 몇 분을
   깜깜이로 기다려야 한다.

---

## 2. 지켜야 할 것 (기존 설계와 충돌 금지)

디버깅 인프라를 추가하다가 아래를 깨면 안 된다:

- **오라클은 여전히 순수 함수다.** 로깅을 `oracles/` 안에 넣지 않는다 —
  `test_oracles_are_pure_no_llm_or_network_imports` 가 깨진다.
- **LLM 생성 문장이 `Finding.reason` 에 들어가지 않는다** (§18 조건 12).
  새로 남기는 `thought`/raw prompt 로그는 어디까지나 **사람이 읽는 디버그
  기록**이지, 판정 파이프라인이 다시 읽어들이는 입력이 아니다. 에이전트가
  자기 과거 `thought` 를 "사실"처럼 취급해 다음 판단에 쓰게 하면 안 된다
  (LLM이 자기 자신을 정당화하는 순환 논리, §6 이 경계하는 것과 같은 함정).
- **`llm/` 밖에서 벤더 SDK 예외 타입을 직접 다루지 않는다.** `agent/react.py`
  가 `anthropic.RateLimitError` 를 알 필요는 없다 — `llm/base.py` 에 우리가
  정의한 `LLMError` 하나로 감싸서 노출한다(아래 §3.4).
- **민감정보 저장 정책은 그대로.** 프롬프트 원문에 계좌번호·금액 같은 테스트
  데이터가 들어갈 수 있지만, 이건 이미 `Fact`/`Finding.reason` 을 통해
  안전하게 가공된 값들(§10 evidence.py 를 거친 뒤의 요약)이지 타깃의 원본
  응답 본문이 아니다 — 새 로그도 이 경계를 넘지 않는다(원본 HTTP 응답 바디를
  직접 로그에 쓰지 않는다).

---

## 3. 관측 가능성 계층 (7개)

### 3.1 실시간 콘솔 스트리밍 + LLM 호출 시작/종료 이벤트 + 타임아웃

**스텝 단위 스트리밍**: `agent/react.py::run()` 에 선택적 콜백
`on_step: Callable[[Step], None]` 을 추가했다 — 스텝이 끝나는 즉시(루프 안에서)
호출된다. `__main__.py` 가 rich Console 로 즉시 찍는 콜백을 넘긴다.
`agent/react.py` 는 여전히 `rich`/콘솔을 모른다 — 콜백 시그니처만 안다
(계층 분리 유지).

**LLM 호출 자체의 시작/종료도 별도로 보인다.** 한 스텝 안에서 LLM 호출이
몇 초 걸릴 수 있는데, `on_step` 만으로는 "그 스텝이 끝나야" 보인다 — 호출이
오래 걸리는 중인지 멈춘 건지 구분이 안 된다. 그래서 LLM 클라이언트 자체가
`LLMEvent`(`CALL_STARTED`/`ATTEMPT_RESPONSE`/`CALL_FINISHED`)를 별도
콜백(`trace_hook`)으로 즉시 낸다 — 자세한 배선은 §3.3 참고.

**타임아웃**: 지금까지 LLM API 호출엔 타임아웃이 전혀 없었다(`config.REQUEST_TIMEOUT`
은 대상 앱 호출용이지 LLM 호출과 무관하다) — 벤더 쪽이 멈추면 루프 전체가
무한정 멈춘다. `config.LLM_REQUEST_TIMEOUT`(60초)을 신설해 `Anthropic()`/
`OpenAI()` 클라이언트 생성 시 넘긴다. 타임아웃이 실제로 발생하면 벤더
SDK가 `APITimeoutError`(→ `APIError` 계열)를 던지고, 이는 §3.4 의 `LLMError`
로 변환되어 "이번 스텝만 실패, 다음 스텝에서 재시도"로 처리된다 — 무한 hang
대신 최대 60초 대기 후 정상적으로 다음 스텝으로 넘어간다.

### 3.2 구조화 로깅 (`logging` 모듈) — 구현됨

`finredteam/logging_setup.py::configure_logging(run_dir, console_level)` 를
`__main__.py::main()` 이 `store` 생성 직후(= `run_dir` 확정 직후) 한 번
호출한다. 핸들러 2개를 `"finredteam"` 로거(모든 하위 모듈의 부모)에 건다:

- 콘솔 핸들러 — 레벨은 `--log-level` CLI 인자로 조절 (기본 WARNING)
- 파일 핸들러 — **항상 DEBUG**, `runs/<run_id>/agent.log` 로 — 콘솔을
  조용히 해뒀어도 사후 분석용 전체 기록은 항상 남는다 (실측: `--log-level`
  기본값으로 돌려도 콘솔엔 DEBUG/INFO 가 0줄, `agent.log` 에는 요청 하나하나
  다 남는 것을 확인했다).

로거 이름은 모듈 경로 그대로 쓴다(`logging.getLogger(__name__)`) — 표준
관행. 실제로 남기는 곳:

| 위치 | 레벨 | 내용 |
|---|---|---|
| `llm/anthropic_.py`/`openai_.py::complete_json` | DEBUG | 매 시도(최대 2회)의 raw 응답 텍스트(2000자까지), 파싱 성공/실패 |
| 〃 | WARNING | 파싱/검증 실패 사유 |
| `agent/react.py::run()` | INFO | 매 스텝 `action` 한 줄 요약 |
| `agent/react.py::_dispatch` | WARNING | 액션 입력 검증 실패, `resolve_skeleton` 거절, 알 수 없는 `hypothesis_id`, 중복 실험 재실행 거절 |
| `agent/react.py::run()` 최상위 | ERROR | 처리되지 않은 예외 (트레이스백 포함, `logger.exception`) |
| `probing/session.py::_request` | DEBUG | 실제 HTTP 메서드/경로/상태코드 |

### 3.3 LLM 호출 원문 영속화 (`llm_calls` 컬렉션) — provider 내부 trace hook

**가장 중요한 조각.** "LLM이 왜 그런 선택을 했는지"는 그 순간 실제로 본
프롬프트 전문을 봐야만 답할 수 있다.

**개정**: 최초 계획은 `llm/factory.py` 바깥에서 `complete_json()` 전체를
감싸는 외부 래퍼(`_LoggingLLMClient`)를 제안했는데, 그러면 **시도별
(attempt) raw 응답을 볼 수 없다** — `complete_json` 은 최대 2번 시도하고
최종 결과(파싱된 리스트 또는 빈 리스트)만 반환하므로, 외부 래퍼는 1차
시도가 왜 실패해서 재시도했는지 원문을 아예 못 본다. 그래서 **trace hook을
`llm/anthropic_.py`/`openai_.py` 내부로 옮겼다** — 실제로 raw 텍스트를
받는 바로 그 지점에서 이벤트를 낸다.

```python
class LLMEventKind(str, Enum):
    CALL_STARTED = "call_started"       # 프롬프트 조립 직후, API 호출 직전
    ATTEMPT_RESPONSE = "attempt_response"  # 매 시도(최대 2회)의 raw 응답 + 파싱 결과
    CALL_FINISHED = "call_finished"     # 최종 결과(성공/실패) + 총 레이턴시

LLMTraceHook = Callable[[LLMEvent], None]
```

`AnthropicClient`/`OpenAIClient` 생성자가 `trace_hook: LLMTraceHook | None`
을 받아 내부에 저장하고, `complete_json` 안에서 세 시점에 호출한다.
`llm/factory.py::build_llm_client(cfg, trace_hook=...)` 가 그대로 전달한다
— `_PricedLLMClient`(비용 계산용 외부 래퍼)는 그대로 두고, 원문 캡처만
안쪽으로 옮긴 것이다.

`storage/db.py::COLLECTIONS` 에 `"llm_calls"` 추가. `__main__.py` 가
`trace_hook=lambda event: store.append("llm_calls", event.model_dump(mode="json"))`
를 넘긴다(+ 콘솔에도 시작/종료를 짧게 찍는다, §3.1).

### 3.4 예외/실패 처리 정책 — "API 실패와 코드 버그를 구분해서, 둘 다 조용히 안 묻히게"

**개정**: 최초 계획은 `llm.complete_json()` 호출을 `except Exception` 으로
넓게 잡는 것이었는데, 이러면 **진짜 코드 버그도 "그냥 API가 실패했나 보다"
하고 같이 삼켜진다** — 예를 들어 SDK 응답 파싱 코드에 우리 쪽 버그가 있어
`AttributeError` 가 나도, 매 스텝 "LLM 호출 실패"로만 보이고 몇십 스텝을
그렇게 허비할 때까지 아무도 진짜 원인을 못 알아챈다. 그래서 **API 실패
전용 타입 `LLMError` 를 두고, 그것만 잡는다.**

```python
# llm/base.py
class LLMError(Exception):
    """API 자체의 실패(네트워크·레이트리밋·타임아웃·인증) — 우리 코드의
    버그와 구분하기 위한 전용 타입."""
```

`llm/anthropic_.py`/`openai_.py` 는 실제 벤더 호출(`.messages.create`/
`.chat.completions.create`) **그 지점만** `anthropic.APIError`/`openai.APIError`
로 잡아 `LLMError` 로 다시 던진다(`raise LLMError(...) from exc`). 그 외의
모든 코드(JSON 파싱, 스키마 검증, `response.usage` 접근 등)는 감싸지 않는다
— 거기서 예외가 나면 그건 우리 코드의 버그이므로 그대로 위로 전파되어야 한다.

```python
# agent/react.py::run()
try:
    results = llm.complete_json(system_prompt, user_prompt, ReActStep, temperature=0.3)
except LLMError as exc:
    logger.warning("LLM 호출 실패 (step=%d): %s", ctx.round, exc)
    results = []   # 이번 스텝만 실패 처리 — LLMError 가 아닌 예외는 여기서 안 잡는다
```

한 번의 API 오류가 전체 실행을 날리지 않는다 — 그 스텝만
"API 실패 또는 파싱 실패" Observation 으로 기록되고 다음 스텝에서 계속
시도한다. (단, `target_unhealthy()`/`consecutive_failures` 류 조건이
있으니 계속 실패하면 결국 `termination.py` 가 정지시킨다 — 무한 루프는
안 된다.) 반면 `LLMError` 가 아닌 예외는 이 `except` 를 그냥 통과해서
아래 (b) 의 최상위 핸들러까지 올라가 크래시 + 로그로 이어진다 — 사람이
반드시 알아채야 하는 문제이기 때문이다.

`tests/test_llm_error_handling.py` 가 이 구분을 고정한다: 벤더 `APIError`
는 `LLMError` 로 변환되고, 임의의 다른 예외(코드 버그를 흉내낸 `RuntimeError`)
는 감싸지지 않고 그대로 전파되는지 각각 검증한다.

**(b) `run()` 전체를 최상위에서 감싸 항상 `finalize()` 가 불리게 한다**:

```python
def run(...) -> AgentContext:
    ...
    try:
        while True:
            ...
    except Exception as exc:
        logger.exception("ReAct 루프 처리되지 않은 예외로 중단")
        store.finalize(f"error: {exc.__class__.__name__}", usage=llm.usage)
        raise
```

크래시하더라도 `manifest.json` 에 종료 사유(`error: <ExceptionType>`)와
그때까지의 토큰 사용량이 남는다 — 개정 전엔 이 경로에서 `finalize()`
자체가 호출 안 돼서 manifest 가 `finished_at: null` 인 채로 남았다.

### 3.5 사후 분석 — 자동 트랜스크립트 (구현됨)

매 실행 종료 시(정상 종료·LLM 의 finish 요청·예외로 인한 중단 전부)
`runs/<run_id>/transcript.md` 를 자동 생성한다 — 이미 저장된
`steps`/`findings`/`manifest` 로부터 만드는 순수 포매팅이라 새 정보가
필요 없다. `agent/react.py::_finalize()` 가 `store.finalize()` 바로
뒤에서 `reporting/transcript.py::write_transcript()` 를 호출한다 — 3군데
(정상 종료 두 갈래 + 최상위 예외 핸들러) 모두 이 헬퍼 하나를 거친다.

```
# Run 20260825-051000 — vulnbank (claude-sonnet-4-6)

## Step 1
**Thought**: 먼저 그래프에 뭐가 있는지 확인한다.
**Action**: observe_graph({})
**Observation**: {...}

## Step 2
...

## Findings
- [VIOLATION] INV-1B transfer POST /transfer — baseline 기대 델타...

## 종료
reason=max_rounds, tokens=12,400, cost=$0.08
```

`reporting/transcript.py` (신규, `reporting/report.py`와 나란히) 가
담당한다 — VVR 계산이 없는 `report.py` 와 같은 이유로, 이것도 순수 서술
포매팅일 뿐 채점이 아니다.

---

## 4. 흔한 실패 모드 → 어디를 봐야 하는가

실제 API로 돌리다 이상하면, 이 표대로 좁혀 들어간다.

| 증상 | 가장 유력한 원인 | 확인할 파일 |
|---|---|---|
| 같은 액션(예: `observe_graph`)만 반복 | Observation 이 너무 길어서 스크래치패드 창(최근 8개) 밖으로 필요한 정보가 밀려남 | `runs/<id>/steps.jsonl` 에서 observation 길이, `llm_calls.jsonl` 의 `user_prompt` 실제 내용 |
| 매 스텝 "파싱 실패" | 벤더가 JSON 앞뒤에 설명 텍스트를 붙임 / `max_tokens` 가 작아 응답이 중간에 잘림 | `llm_calls.jsonl` 의 `raw_response`, `config.py` 의 `LLMConfig.max_tokens` |
| `hypothesis_id` 못 찾음 에러가 반복 | LLM이 실행하지 않은 이전 id를 재사용하거나 오타 | `steps.jsonl` 에서 `propose_hypothesis` 응답의 실제 id vs 다음 `execute_experiment` 입력 비교 |
| 특정 스텝에서 오래 멈춤(hang처럼 보임) | API 레이턴시/레이트리밋 대기 | 실시간 콘솔(3.1)에 진행 표시 + `llm_calls.jsonl` 의 `latency_ms` |
| 프로세스가 죽고 manifest 가 비어 있음 | 처리 안 된 예외 (3.4 적용 전 상태) | `runs/<id>/agent.log` 의 마지막 트레이스백 |
| VIOLATION 이 하나도 안 나옴 | LLM이 `read_balance` 만 반복하고 `transfer` 를 안 씀(탐색이 얕음) | `transcript.md` 의 action 분포, `report.json` 의 `by_operation` |
| 토큰이 예상보다 빨리 참 | 시스템 프롬프트(그래프 스키마+invariant 목록+액션 카탈로그+템플릿 요약)가 매 스텝 통째로 다시 들어감 | `llm_calls.jsonl` 의 `tokens_in` 추이 — 스텝이 늘어도 거의 고정값이어야 정상, 계속 커지면 스크래치패드가 새고 있는 것 |
| `discover_surface` 를 안 씀 | 시스템 프롬프트에서 이 액션의 존재/효용이 잘 안 읽힘 | `planning/actions.py::ACTION_CATALOG` 문구 검토, `llm_calls.jsonl` 의 thought 에서 언급 여부 |
| `execute_experiment` 가 매번 "정책 위반으로 차단됨" 만 냄 | Engagement 가 그 operation/method 를 허용 안 함(의도된 동작일 수 있음) | `engagements/<id>/engagement.md` 의 Allowed/Prohibited Activities, `manifest.json` 의 `policy_hash` 가 기대한 Engagement 것과 일치하는지 |
| 실행 시작하자마자 `TypeError: ... unexpected keyword argument` 로 죽음 | 설치된 벤더 SDK 버전이 우리 코드가 쓰는 파라미터를 안 받음(§8.1) | `pip show anthropic`/`openai` 버전, `llm/anthropic_.py`·`llm/openai_.py` 의 `.create()` 호출부 |
| `termination_reason: llm_unavailable`, `tokens_in`/`requests` 가 0 | LLM 호출이 처음부터 계속 실패(레이트리밋/인증/네트워크) — 오라클/그래프 문제가 아니다(§8.2) | `runs/<id>/agent.log` 의 `LLM 호출 실패` 줄들, 429면 API 사용량/한도 확인 |
| 실행이 시작하자마자 `PolicyViolation` 트레이스백으로 죽음 | §3.7 의 사전 검사가 빠진 새 코드 경로(버그) — 정상 흐름이면 트레이스백 없이 붉은 메시지로 끝나야 한다 | `runs/<id>/agent.log` 에서 `PolicyViolation` 발생 지점, §3.7 |

---

## 5. CLI 플래그 (구현됨)

`pip install -e .` 했으면 `finredteam`(=`python -m finredteam`)이 한 단어
명령이다 — 아래는 디버깅에 관련된 플래그만 추린 것, 전체 사용법은
`RUNNING.md` 참고.

```bash
finredteam --max-rounds 20 --log-level DEBUG
#    콘솔 로그 레벨 (기본 WARNING). runs/<id>/agent.log 는 항상 DEBUG

# --target/--engagement 같은 선택 플래그는 없다 — 실행하면 바로 대화형
# Engagement Setup 이 시작된다(이미 승인된 Engagement 가 있으면 재사용 여부만
# 물어본다 — vuln-bank 예시가 이미 있으면 그대로 재사용된다).
# --no-key-prompt          API 키 환경변수 없으면 대화형으로 안 묻고 바로 실패 (CI용)
# --fake-llm               결정적 가짜 LLM — API 호출 자체가 없어서 §3의 진단 항목이 대부분 해당
#                          없음. Engagement Setup 대화도 즉시 끝난다(질문 없이 최소 정보로 진행)
```

---

## 6. 구현 순서 (저비용·고효용 순) — 전부 완료

1. ✅ **최상위 예외 처리 + `LLMError` 구분 (3.4)**
2. ✅ **실시간 콘솔 스트리밍 + LLM 호출 시작/종료 이벤트 + 타임아웃 (3.1)**
3. ✅ **자동 트랜스크립트 (3.5)**
4. ✅ **구조화 로깅 + 파일 핸들러 (3.2)**
5. ✅ **`llm_calls` 영속화 (3.3)** — provider 내부 trace hook 방식으로.
6. ✅ **CLI 플래그 (§5)**
7. ✅ **`PolicyViolation` — 세 번째 실패 종류 (3.7)** — Engagement 레이어 도입 시 추가.

`tests/test_llm_error_handling.py` 로 §3.4 의 핵심 계약(벤더 `APIError`→
`LLMError`, 그 외 예외는 안 감싸짐)을 고정했고, `tests/test_policy_
enforcement.py` 로 §3.7 의 계약(정책 위반이 사전 검사에서 잡히면 실제
요청까지 도달하지 않고 Observation 으로만 남음)을 고정했다. 나머지(로깅
호출 위치, 트랜스크립트 포맷)는 `--fake-llm` 라이브 실행으로
`runs/<id>/agent.log` 와 `runs/<id>/transcript.md` 가 실제로 만들어지는
것까지 확인했다 — 실제 벤더 API 로 검증한 건 아니다(연결 실패/레이트리밋
자체를 재현하려면 API 키가 필요하다).

---

## 7. 이 계획에서 일부러 안 하는 것

- ~~LLM 호출 자동 재시도(exponential backoff) 도입~~ — **철회됨**(§8 참고).
  실측 결과 벤더 SDK 기본 재시도(2회)만으로 부족한 레이트리밋을 만났다 —
  6스텝이 13초 만에 전부 실패하고 "saturated" 로 조용히 끝났다. 이제
  `agent/react.py::run()` 이 LLM 호출 실패마다 backoff 로 쉬고, `config.
  LLM_MAX_RETRIES` 로 벤더 SDK 자체 재시도 횟수도 늘렸다.
- **대화형 스텝 단위 확인(step-through) 모드** — "한 스텝마다 사람이
  Enter 눌러야 진행" 같은 기능은 디버깅엔 유용할 수 있지만 지금 계획엔
  넣지 않는다. 위 계층들로 사후 분석이 충분히 가능하다고 판단되면
  스킵, 안 되면 추가 검토.
- **원격 로그 수집(Sentry 등 외부 서비스 연동)** — 로컬 전용 프로젝트라
  범위 밖.

---

## 8. 실제 API 로 돌리다 발견된 버그 2건 (실측 기록)

### 8.1 `Messages.create()` 가 `temperature` 를 안 받음 (설치된 SDK 버전 차이)

**증상**: 실제 API 키를 넣고 첫 스텝에서 바로
`TypeError: Messages.create() got an unexpected keyword argument 'temperature'`
로 죽었다(`ReAct 루프가 처리되지 않은 예외로 중단됨`, 트레이스백까지).

**원인**: 설치된 `anthropic` 패키지가 `temperature` 를 최상위 키워드로 안 받는
버전이었다(신규 메이저 버전, `thinking`/`container`/`output_config` 등으로
파라미터가 개편되며 빠짐) — `requirements.txt` 가 `anthropic>=0.34` 로만
하한을 걸어놔서 버전에 따라 시그니처가 다를 수 있다.

**고침**: `llm/anthropic_.py::complete_json()` 이 `temperature` 를 최상위
키워드 대신 `extra_body={"temperature": temperature}` 로 보낸다 — SDK 가 그
값을 타입으로 노출하든 안 하든 요청 본문에 그대로 실린다. 서버가 그 값 자체를
거부해도(예: 특정 모델이 reasoning 모드라 온도 고정) `anthropic.APIError` →
`LLMError` 로 이미 잡히므로 크래시가 아니라 그 스텝만 실패 처리된다.

### 8.2 레이트리밋(429) 연속 실패가 "saturated" 로만 보임, 백오프 없음

**증상**: 8.1 을 고친 뒤 다시 돌렸더니 크래시는 안 나는데 6스텝이 13초 만에
전부 `LLM 호출 실패`(429 rate_limit_error)로 끝나고 `termination_reason:
saturated` 로 조용히 종료됐다 — `tokens_in`/`tokens_out`/`requests` 전부
0(성공한 호출이 단 한 번도 없었다는 뜻). "saturated"(그래프 정체)와 "LLM이
아예 응답을 못 받음"이 같은 사유로 뭉뚱그려져서, `manifest.json` 만 봐서는
원인을 알 수 없었다.

**원인**: 두 가지 복합.
1. `agent/react.py::run()` 이 `LLMError` 를 잡으면 그 스텝만 실패 처리하고
   **곧바로 다음 스텝을 또 쏘았다** — 레이트리밋이 걸린 상태에서 텀도 없이
   계속 두드리니 다음 스텝도 똑같이 걸린다(§7 의 원래 "그냥 넘어가면 충분"
   가정이 실측으로 깨졌다).
2. 벤더 SDK 자체 재시도(`anthropic.Anthropic(max_retries=2)` 기본값)만으로는
   이 레이트리밋 윈도우를 못 버텼다.

**고침**:
- `config.py::LLM_MAX_RETRIES = 5` 를 신설해 `AnthropicClient`/`OpenAIClient`
  생성 시 SDK 에 넘긴다 — 벤더 자체 재시도 기회를 늘린다.
- `agent/react.py::run()`: `LLMError` 를 잡을 때마다 `ctx._consecutive_llm_
  errors` 를 세고, `min(2 ** n, 30)`초만큼 잠깐 쉬었다가 다음 스텝으로
  넘어간다(성공하면 즉시 0으로 리셋). 레이트리밋 윈도우가 풀릴 시간을 준다.
- `AgentContext.llm_unavailable()`(신규, `target_unhealthy()`와 같은 자리) —
  연속 실패가 `_LLM_UNAVAILABLE_THRESHOLD`(5)를 넘으면 `termination.py::
  should_stop()` 이 `saturated` 보다 먼저 `"llm_unavailable"` 사유로 멈춘다 —
  이제 `manifest.json` 만 봐도 "LLM이 아예 응답을 못 받아서 끝났다"를 바로
  구분할 수 있다.

`tests/test_termination_policy_stop.py::test_llm_unavailable_reported_
distinctly_from_saturated` 가 우선순위를, 라이브 검증(실제 vuln-bank 대상,
항상 `LLMError` 를 던지는 가짜 클라이언트로 5스텝 만에 `llm_unavailable` 로
정확히 종료되는 것)이 실동작을 고정한다.

**후기(실사용자 확인)**: 이 백오프를 실제로 적용한 뒤에도 사용자가 여전히
5스텝 전부 429로 막히는 걸 관측(`runs/20260825-173529`, 2·4·8·16·30초까지
다 기다렸는데도 매번 실패). 원인은 코드가 아니라 **`ANTHROPIC_AUTH_TOKEN` 이
무료 Claude 플랜으로 발급한 `claude setup-token` 토큰**이었다 — 이 토큰은
유료 구독(Pro/Max) 사용량에 묶여 있어서, 무료 플랜이면 할당량 자체가 없어
백오프를 아무리 늘려도 안 풀린다. `RUNNING.md` §4/§6에 이 함정을 명시했다.
코드 관점에서 결론: `llm_unavailable` + backoff 는 정확히 설계대로 동작했다
— 이건 소프트웨어 버그가 아니라 인증 수단 자체의 한도 문제였다.
