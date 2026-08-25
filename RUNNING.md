# 실행 방법

이 문서 하나만 보면 FinRedTeam 을 처음부터 끝까지 돌릴 수 있게 정리한다.
설계/아키텍처는 `README.md`, 로그·디버깅 인프라는 `DEBUGGING.md` 참고.

**실행은 그냥 `finredteam` 하나다.** `--target`/`--engagement` 같은 선택
플래그는 없다 — 실행하면 바로 대화형 Engagement Setup 이 시작되고, 승인되면
그 자리에서 바로 돈다. vuln-bank 도 특별 취급되는 CLI 대상이 아니라, 대화로
입력되는 여러 Target 중 하나다 — 다만 저장소에 미리 승인돼 커밋된 예시
(`engagements/examples/vulnbank/`) 가 하나 있어서, 그것만 있는 상태로 실행
하면 바로(또는 재사용 여부만 확인하고) 돈다.

## 0. 사전 준비 (최초 1회)

```bash
# 1) vuln-bank 를 로컬 Docker 로 띄운다 (별도 클론, targets/configs/vulnbank.yaml 이 가리키는 대상)
#    이미 떠 있다면 생략

# 2) 파이썬 가상환경 + 의존성
python -m venv .venv
.venv\Scripts\Activate.ps1        # PowerShell
# 또는: source .venv/Scripts/activate   # Git Bash
pip install -r requirements.txt

# 3) 한 단어 명령(finredteam / finredteam-replay / finredteam-scorer) 쓰려면 editable 설치
pip install -e .
```

`pip install -e .` 를 안 했다면 아래 모든 `finredteam ...` 명령을
`python -m finredteam ...` 로 바꿔서 그대로 쓰면 된다 — 완전히 같은 코드다.

## 1. 매번 실행 전 체크

- 가상환경 활성화되어 있는지 (`(.venv)` 프롬프트)
- vuln-bank 를 테스트할 거면 떠 있는지: `curl http://localhost:5000/` 가 200 이면 OK

## 2. 실행 모드 3가지

```bash
# ① 배관 확인 — 부트스트랩(계정 생성/로그인) + manifest 생성만, LLM 호출 없음
finredteam --smoke

# ② API 키 없이 흐름만 확인 — 결정적 가짜 LLM 이 정해진 순서로 행동
#    (Engagement Setup 대화도 즉시 끝난다 — 질문 없이 최소 정보로 진행)
#    (실제 취약점 탐색 품질은 검증 안 됨, 배관/오라클/그래프 로직 확인용)
finredteam --fake-llm --max-rounds 10

# ③ 진짜 탐색 — 실제 LLM 이 ReAct 루프로 가설을 세우고 실험한다
finredteam --max-rounds 20
```

③은 API 키가 필요하다 — 아래 4절 참고.

**실행할 때마다 무슨 일이 일어나는지:**

1. 이미 승인된 Engagement 가 있으면(예: vuln-bank 예시) 재사용할지 먼저
   물어본다(번호로 선택, 또는 그냥 엔터로 새로 만들기).
2. 비대화형 환경(스크립트/CI, 또는 이 문서를 그대로 따라하는 파이프 실행)
   이고 승인된 Engagement 가 **정확히 하나**뿐이면, 묻지 않고 자동으로
   그걸 재사용한다.
3. 없거나 새로 만들기로 하면 3절의 대화가 시작된다.

## 3. 새 대상을 테스트하려면 — 그냥 실행하고 설명하면 된다

새 대상을 실행하려면 **두 가지**가 다 있어야 한다:

1. **어댑터 설정** `targets/configs/<name>.yaml` — 그 서비스에 어떻게
   요청을 보낼지(base_url, 회원가입/로그인 엔드포인트·필드명 등). 이건
   대화가 만들어주지 않는다 — 사람이 대상을 정상적으로 한 번 써보면서
   (black-box, 소스코드는 안 읽는다) 직접 조사해서 작성해야 한다
   (`targets/configs/vulnbank.yaml` 이 실제 예시다). 이게 없으면 실행이
   `targets/configs/<name>.yaml 없음` 에러로 막힌다(Engagement 는 승인돼도
   어댑터가 없으면 여전히 못 돈다).
2. **Engagement** (Scope/Rules of Engagement) — 대화로 만든다, 이 절에서
   다룬다. 승인 안 된 대상은 절대 실행되지 않는다.

```bash
finredteam
```

이미 승인된 Engagement 를 재사용할지 먼저 물어보고(있으면), 새로 만들기로
하면:

```
테스트하려는 대상과 목적을 자유롭게 설명해주세요
(예: "로컬 5000번 포트에 떠 있는 뱅킹 앱을 테스트하고 싶다,
      실제 사용자 데이터는 없고 테스트 계정만 쓴다")
```

이렇게 **자유 텍스트로 한 번에** 설명하면, LLM 이 그걸 구조화 필드(Target/
Scope/허용·금지 행위/속도·금액 제한/민감정보/Cleanup/Stop Condition)로
해석하면서, 부족하거나 애매한 항목을 **한 번에 하나씩** 질문한다. 몇 가지
성질:

- **언급 안 된 항목은 항상 가장 보수적인(거부) 값으로 남는다** — LLM 이
  스스로 허용 범위를 넓히는 건 구조적으로 불가능하다
  (`engagement/setup.py::_draft_to_engagement` 가 `None`을 전부 `False`/
  최소값으로 강제 변환한다).
- base_url/어댑터 설정 이름처럼 실행에 구조적으로 꼭 필요한 값은, LLM이
  안 물어봐도 코드가 끝에 직접 물어봐서 확보한다.
- 질문 12회 한도 — 그 안에 안 끝나면 지금까지 파악된 정보로 진행한다.

다 끝나면 최종 내용(engagement.md 형식)을 보여주고
`이 설정을 확정하시겠습니까?` 를 물어본다 — 여기서 승인해야만
`engagements/<engagement_id>/`에 `engagement.md`(사람이 읽는 문서)와
`engagement.yaml`(코드가 강제하는 정책)이 저장되고, **그 자리에서 바로
실행이 시작된다.** `engagement_id` 는 사용자가 짓지 않는다 — 감사/저장용
으로 시스템이 자동 생성한다(예: `vulnbank-a1b2c3`).

`finredteam setup` 은 같은 대화를 실행 없이 준비만 해둔다(미리 만들어두고
싶을 때):

```bash
finredteam setup                # 대화형으로 만들고 승인만, 바로 실행은 안 함
finredteam setup --fake-llm     # 테스트/CI용 — 질문 없이 즉시 진행
```

`finredteam setup` 이 만든 Engagement 는 `.gitignore` 로 커밋되지 않는다
(실제 대상 정보라 민감할 수 있어서) — 예시로 저장소에 미는 건
`engagements/examples/vulnbank/` 뿐이다.

허용 호스트 밖으로 나가는 요청, 금지된 메서드(예: 상태 변경 금지 상태에서
POST), 금액 상한을 넘는 요청은 LLM 이 뭘 골라도 코드(`probing/session.py`
네트워크 경계 + `agent/react.py` 사전 검사)가 실제 요청 전송 전에 막는다.

## 4. API 키 넣는 방법

`--fake-llm`/`--smoke` 가 아닌 모드로 실행하면, 우선순위대로 키를 찾는다.
(Engagement Setup 대화 자체도 LLM 을 쓰므로, 재사용이 아니라 새로 만드는
경우엔 이 키가 대화 시작 시점에 필요하다 — 재사용이면 전혀 필요 없다.)

Anthropic 은 **두 가지 인증 방식**을 받는다 — 어느 쪽이든 아래 순서로 자동
탐색된다:

- **API 키** (`sk-ant-...`, `ANTHROPIC_API_KEY`) — Anthropic Console 에서
  발급.
- **OAuth 토큰** (`ANTHROPIC_AUTH_TOKEN`) — `claude setup-token` 명령으로
  만든 것. Claude Code 로그인을 이미 해뒀다면 API 키를 따로 안 만들어도
  이걸로 그대로 인증된다.

  ⚠️ **무료 플랜 토큰은 안 된다(실측 확인).** 이 토큰은 Claude 구독
  (Pro/Max, 유료) 사용량에 묶여 있다 — 무료 플랜으로 `claude setup-token`
  받은 토큰을 넣으면 매 호출이 `429 rate_limit_error` 로 막힌다(재시도/
  백오프를 아무리 늘려도 안 풀림 — 할당량 자체가 없는 것). 실제로 LLM을
  쓰려면 둘 중 하나가 필요하다: ① Anthropic Console(console.anthropic.com)
  에서 결제 등록하고 받은 API 키, 또는 ② Claude Pro/Max 유료 구독 후 다시
  `claude setup-token`.

1. **환경변수**가 이미 있으면 그걸 그대로 쓴다 (아무것도 안 물어봄) —
   `ANTHROPIC_API_KEY` 가 `ANTHROPIC_AUTH_TOKEN` 보다 우선한다:
   ```bash
   # API 키
   export ANTHROPIC_API_KEY=sk-ant-...      # Git Bash
   $env:ANTHROPIC_API_KEY="sk-ant-..."      # PowerShell

   # 또는 claude setup-token 으로 만든 OAuth 토큰
   claude setup-token                        # 토큰 값을 출력해준다
   export ANTHROPIC_AUTH_TOKEN=<위에서 나온 토큰>   # Git Bash
   $env:ANTHROPIC_AUTH_TOKEN="<토큰>"              # PowerShell

   finredteam --max-rounds 20
   ```

2. 환경변수가 **없으면 터미널에서 직접 물어본다** — 아래처럼 대화형으로:
   - 전에 저장해 둔 게 있으면 번호로 고를지, 새로 입력할지(`n`) 선택
   - 새로 입력할 때는 먼저 API 키인지 OAuth 토큰인지 고르고, 값은 화면에
     안 보이게 마스킹돼 입력된다 (`Prompt.ask(password=True)`)
   - 입력 후 "다음에 다시 선택할 수 있게 로컬에 저장할까요?" 라고 물어본다 — `y` 하면
     `.finredteam/credentials.json` 에 종류(API 키/OAuth 토큰)와 함께 저장되고,
     다음 실행부터는 번호로 선택만 하면 된다. 이 파일은 `.gitignore` 에 있어
     커밋되지 않는다.

   ```
   ┌────────────────────────────────────────┐
   │ anthropic 인증정보가 필요합니다 (ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN 환경변수 없음) │
   └────────────────────────────────────────┘
   #   이름        종류        값
   1   personal    API 키      sk-ant6…ab12
   2   work        OAuth 토큰  eyJhbGci…3xYZ
   n   (새로 입력)
   선택 [1/2/n] (1):
   ```

3. CI 등 **비대화형 환경**이거나 물어보는 것 자체를 막고 싶으면 `--no-key-prompt`
   를 붙인다 — 환경변수가 없으면 바로 에러로 종료한다 (조용히 멈춰있지 않는다).

   ```bash
   finredteam --max-rounds 20 --no-key-prompt
   ```

**Anthropic 대신 OpenAI 를 쓰려면**(예: Anthropic 무료 플랜 한도에 막혔을 때)
`--llm-provider openai --llm-model gpt-5` 처럼 명시하면 된다 —
`OPENAI_API_KEY` 기준으로 같은 흐름이 적용된다(OpenAI 는 OAuth 토큰 방식이
없다, API 키만).

`--llm-provider` 를 안 주면 **환경변수로 자동 판단**한다 — `ANTHROPIC_API_KEY`/
`ANTHROPIC_AUTH_TOKEN` 만 있으면 anthropic, `OPENAI_API_KEY` 만 있으면
openai. 둘 다 있거나 둘 다 없으면(그리고 대화형 터미널이면) 직접 물어본다:

```
어떤 LLM 을 쓸까요? 1) Anthropic (Claude)  2) OpenAI (GPT) [1/2] (1):
```

비대화형이거나 `--no-key-prompt` 면 물어보지 않고 기존처럼 anthropic 을
기본값으로 쓴다(스크립트/CI 안 깨지게).

## 5. 실행 결과 보기

```bash
# 방금 실행한 것(또는 다른 run_id)을 색깔·표로 다시 보기 — 아무것도 재실행하지 않음
finredteam-replay latest
finredteam-replay <run_id>
finredteam-replay <run_id> --step 5      # 스텝 하나만 깊게 (LLM 원문 포함)
finredteam-replay <run_id> --full        # 잘림 없이 전체

# GT(ground truth) 대비 채점 — 에이전트와 완전히 분리된 별도 프로세스
finredteam-scorer runs/<run_id>
```

각 실행 결과는 `runs/<run_id>/` 아래 저장된다:

| 파일                      | 내용                                      |
|---------------------------|-------------------------------------------|
| `manifest.json`           | run 메타데이터 (target/llm/termination/`engagement_id`/`policy_hash` 등) |
| `steps.jsonl`              | ReAct 스텝(Thought/Action/Observation)     |
| `findings.jsonl`           | Verified Finding                           |
| `llm_calls.jsonl`          | LLM 호출 원문 (요청/응답, `--fake-llm` 이면 비어있음) |
| `created_resources.jsonl`  | 실행 중 생성한 리소스(테스트 계정 등) — CLEANUP phase 대상 |
| `cleanup.jsonl`            | CLEANUP phase 결과 (`success`/`failed`/`not_configured`, Engagement 가 `cleanup.required`일 때만) |
| `agent.log`                | 전체 로그 (항상 DEBUG 레벨)                |
| `transcript.md`            | 실행이 끝나면 자동 생성되는 사람이 읽는 요약 |

## 6. 문제가 생기면

- **`ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN ... 없음` 에러** → 4절대로 API
  키나 `claude setup-token` 토큰을 넣거나 `--fake-llm`
- **`termination_reason: llm_unavailable`, 매 스텝 `429 rate_limit_error`**
  (30초까지 백오프해도 안 풀림) → 십중팔구 **무료 플랜으로 받은 OAuth 토큰**
  이다(실측 확인, 4절 참고) — 결제 등록된 API 키나 Pro/Max 구독 토큰으로 바꾼다
- **vuln-bank 연결 안 됨** (`ConnectionError` 등) → Docker 컨테이너 떠 있는지,
  `curl http://localhost:5000/` 로 확인
- **왜 이런 판정이 나왔는지 궁금함** → `--log-level DEBUG` 로 다시 실행하거나,
  실행 끝난 뒤 `finredteam-replay <run_id> --step N --full` 로 그 스텝의 LLM
  원문까지 확인 (자세한 건 `DEBUGGING.md`)
- **저장해 둔 API 키를 지우고 싶음** → `.finredteam/credentials.json` 파일을
  직접 열어서 해당 항목을 지우거나 파일 자체를 삭제
- **`targets/configs/<name>.yaml 없음`** → Engagement 는 승인됐는데 어댑터
  설정이 아예 없다는 뜻 — 3절 1번대로 사람이 직접 작성해야 함
- **`대화형 터미널이 아니라서 진행할 수 없음`** → 비대화형 환경인데 승인된
  Engagement 가 0개거나 2개 이상이다 — 한 번은 대화형 터미널에서 실행해
  Engagement 를 만들어 두거나, `engagements/` 아래에 하나만 남긴다
- **`아직 승인되지 않음`** → 대화 마지막에 `확정하시겠습니까?` 에 `y` 로
  답해야 `approved_at` 이 찍힌다 — 중간에 취소하면 저장 자체가 안 됨
- **`정책 위반으로 실행이 거부됨`** → Engagement 가 그 행위를 허용하지 않는다는
  뜻(의도된 동작) — `engagements/<id>/engagement.md` 를 열어 어떤 항목을
  풀어야 할지 확인하고 `finredteam setup` 을 다시 돌린다(새 Engagement 로)
