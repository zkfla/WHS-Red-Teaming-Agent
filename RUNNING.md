# 실행 방법

이 문서 하나만 보면 FinRedTeam 을 처음부터 끝까지 돌릴 수 있게 정리한다.
설계/아키텍처는 `README.md`, 로그·디버깅 인프라는 `DEBUGGING.md` 참고.

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
- vuln-bank 가 떠 있는지: `curl http://localhost:5000/` 가 200 이면 OK

## 2. 실행 모드 3가지

```bash
# ① 배관 확인 — 부트스트랩(계정 생성/로그인) + manifest 생성만, LLM 호출 없음
finredteam --target vulnbank --smoke

# ② API 키 없이 흐름만 확인 — 결정적 가짜 LLM 이 정해진 순서로 행동
#    (실제 취약점 탐색 품질은 검증 안 됨, 배관/오라클/그래프 로직 확인용)
finredteam --target vulnbank --fake-llm --max-rounds 10

# ③ 진짜 탐색 — 실제 LLM 이 ReAct 루프로 가설을 세우고 실험한다
finredteam --target vulnbank --max-rounds 20
```

③은 API 키가 필요하다 — 아래 3절 참고.

## 3. API 키 넣는 방법

`--fake-llm`/`--smoke` 가 아닌 모드로 실행하면, 우선순위대로 키를 찾는다:

1. **환경변수**가 이미 있으면 그걸 그대로 쓴다 (아무것도 안 물어봄):
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...      # Git Bash
   $env:ANTHROPIC_API_KEY="sk-ant-..."      # PowerShell
   finredteam --target vulnbank --max-rounds 20
   ```

2. 환경변수가 **없으면 터미널에서 직접 물어본다** — 아래처럼 대화형으로:
   - 전에 저장해 둔 키가 있으면 번호로 고를지, 새로 입력할지(`n`) 선택
   - 새로 입력할 땐 입력 값이 화면에 안 보이게 마스킹된다 (`Prompt.ask(password=True)`)
   - 입력 후 "다음에 또 쓰게 로컬에 저장할까요?" 라고 물어본다 — `y` 하면
     `.finredteam/credentials.json` 에 저장되고, 다음 실행부터는 번호로 선택만
     하면 된다. 이 파일은 `.gitignore` 에 있어 커밋되지 않는다.

   ```
   ┌────────────────────────────────────────┐
   │ anthropic API 키가 필요합니다 (ANTHROPIC_API_KEY 환경변수 없음) │
   └────────────────────────────────────────┘
   #   이름        키
   1   personal    sk-ant6…ab12
   n   (새 키 입력)
   선택 [1/n] (1):
   ```

3. CI 등 **비대화형 환경**이거나 물어보는 것 자체를 막고 싶으면 `--no-key-prompt`
   를 붙인다 — 환경변수가 없으면 바로 에러로 종료한다 (조용히 멈춰있지 않는다).

   ```bash
   finredteam --target vulnbank --max-rounds 20 --no-key-prompt
   ```

다른 프로바이더를 쓰려면 `--llm-provider openai --llm-model gpt-5` 처럼
지정한다 — 그러면 `OPENAI_API_KEY` 기준으로 같은 흐름이 적용된다.

## 4. 실행 결과 보기

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

| 파일                | 내용                                      |
|---------------------|-------------------------------------------|
| `manifest.json`     | run 메타데이터 (target/llm/termination 등) |
| `steps.jsonl`        | ReAct 스텝(Thought/Action/Observation)     |
| `findings.jsonl`     | Verified Finding                           |
| `llm_calls.jsonl`    | LLM 호출 원문 (요청/응답, `--fake-llm` 이면 비어있음) |
| `agent.log`          | 전체 로그 (항상 DEBUG 레벨)                |
| `transcript.md`      | 실행이 끝나면 자동 생성되는 사람이 읽는 요약 |

## 5. 문제가 생기면

- **`ANTHROPIC_API_KEY ... 없음` 에러** → 3절대로 키를 넣거나 `--fake-llm`
- **vuln-bank 연결 안 됨** (`ConnectionError` 등) → Docker 컨테이너 떠 있는지,
  `curl http://localhost:5000/` 로 확인
- **왜 이런 판정이 나왔는지 궁금함** → `--log-level DEBUG` 로 다시 실행하거나,
  실행 끝난 뒤 `finredteam-replay <run_id> --step N --full` 로 그 스텝의 LLM
  원문까지 확인 (자세한 건 `DEBUGGING.md`)
- **저장해 둔 API 키를 지우고 싶음** → `.finredteam/credentials.json` 파일을
  직접 열어서 해당 항목을 지우거나 파일 자체를 삭제
