# CI/CD Red Team Agent

GitHub Actions 파일을 실행하지 않고 정적으로 분석해 Evidence, 공격 경로 후보,
Cloud Identity handoff를 생성하는 7일 프로토타입입니다. 분석 저장소의 텍스트는 모두
신뢰할 수 없는 입력으로 취급합니다.

확실한 workflow·job·step·artifact·credential 관계는 deterministic 규칙으로 처리하고,
개별 시나리오의 부분 경로를 공격자 시작점에서 GitHub Token 또는 Cloud Identity까지
연결합니다. LLM은 선택 사항이며, 향후 불확실한 연결의 추가 조사와 우선순위 결정에만
사용합니다.

## 설치

Python 3.13과 `uv`가 필요합니다.

```powershell
uv sync
```
To evaluate real-model reasoning without leaking fixture answers into analysis:

```powershell
uv run cicd-agent evaluate tests/fixtures/reasoning-matrix --output output/evaluation `
  --provider codex-chatgpt --scenario gha-01
```

The evaluator loads each `oracle.json` only after the corresponding repository analysis and writes
an aggregate `matrix-evaluation/evaluation.json` with extraction and status accuracy.

프레임워크는 hackingBuddyGPT 커밋
`b2653771aed383ffe92933f8f2f9b7684403af8a`에 고정되며 전체 전이 의존성은
`uv.lock`에 기록됩니다.

## 사용

결정적 정적 분석만 수행:

```powershell
uv run cicd-agent analyze C:\path\to\repository --output .\output
```

LiteLLM 모델의 추가 검토 포함:

```powershell
uv run cicd-agent analyze C:\path\to\repository `
  --output .\output `
  --provider openai `
  --model openai/gpt-5-mini `
  --attacker-profile fork-pr `
  --max-rounds 12 --max-tokens 100000 --max-cost 2.00 --max-duration 900
```

`.env.example`을 `.env`로 복사하면 프로그램이 프로젝트의 `.env`를 자동으로 읽습니다.
이미 설정된 프로세스 환경 변수는 `.env`보다 우선합니다. 키가 둘 이상 있으면 `auto`가
임의 선택하지 않고 종료하므로 `--provider openai|anthropic|openrouter|gemini|litellm` 중
하나를 지정해야 합니다. 다른 위치의 신뢰된 파일은 `--env-file C:\safe\agent.env`로
지정할 수 있으며, 분석 대상 저장소 내부의 `.env`는 로딩하지 않습니다.

ChatGPT 요금제의 Codex 로그인을 hackingBuddyGPT Agent의 LLM 연결로 사용할 수도
있습니다. 먼저 Codex에서 브라우저 로그인을 한 뒤 실행합니다.

```powershell
codex login
codex login status
uv run cicd-agent analyze C:\path\to\repository `
  --output .\output `
  --provider codex-chatgpt `
  --model gpt-5.6-terra
```

`codex-chatgpt`는 API key나 Codex 인증 토큰을 프로그램으로 복사하지 않습니다. 공식
`openai-codex` Python SDK가 저장된 ChatGPT 인증을 재사용하는 LLM 연결을 제공합니다.
각 라운드에서 Codex가 다음 Capability 하나를 선택하면 기존 hackingBuddyGPT loop가
그 도구를 실행하고 결과를 다음 라운드로 돌려줍니다. 따라서 LiteLLM 방식과 동일하게
`list → inspect/search → submit_analysis` 흐름을 수행합니다.
`--model`을 생략하면 Codex의 현재 기본 모델을 사용합니다. 모델을 지정하지 않고
provider도 선택하지 않으면 deterministic 분석만 수행합니다.

## 출력

각 실행은 `<output>/<run-id>/` 아래에 다음 파일을 만듭니다.

- `analysis.json`: schema version 1.0의 전체 구조화 결과
- `summary.md`: Finding, Attack Path, Cloud handoff, 미확인 Evidence 요약
- `trace.jsonl`: redaction 이후 관측 이벤트

종료 코드는 COMPLETE 0, 입력·설정 오류 2, 프레임워크/모델 초기화 실패 3,
PARTIAL 4, 최종 validation 실패 5입니다.

## 안전 경계

- workflow, script, local action을 실행하지 않습니다.
- 외부 action을 다운로드하지 않고 GitHub/AWS API에 접근하지 않습니다.
- `.git`, binary, 1 MiB 초과 파일, 저장소 밖 symlink를 읽지 않습니다.
- 파일 slice는 최대 200줄, literal 검색은 최대 50건입니다.
- secret 값은 저장하지 않으며 GitHub secret expression은 참조로만 남깁니다.
- 정적 Attack Path는 `CONFIRMED`로 승격하지 않습니다.
- Codex LLM 연결은 빈 임시 작업 폴더, read-only sandbox, approval deny-all에서 실행됩니다.
  사용자 Codex 설정에 등록된 plugin과 MCP server도 이 실행에서는 비활성화됩니다.
  Codex 자체 도구에는 대상 저장소를 주지 않고, hackingBuddyGPT 대화 기록과 Capability
  schema 및 redaction된 도구 결과만 전달합니다.
- branch protection, environment approval, repository 기본 권한, IAM trust policy는
  로컬 Evidence가 없으면 `UNKNOWN`입니다.

## 개발 검증

```powershell
uv run pytest
uv run ruff check .
```

fixture 기반 E2E는 모델과 네트워크 없이 반복 실행됩니다. ChatGPT 로그인 기반 real-model
smoke는 `tests/fixtures/global-chain`에서 `proposal_validated` COMPLETE까지 확인했습니다.
실제 LiteLLM provider smoke는 해당 provider 자격 증명과 비용·사용량 한도가 있을 때만
수행합니다. Codex 경로의 보고서 비용 `0.0`은 SDK가 통화 비용을 제공하지 않는다는 뜻이며,
무료 사용을 보장하지 않습니다.

Codex 인증과 자동화 방식은 OpenAI 공식 문서의
[Authentication](https://developers.openai.com/codex/auth/),
[Non-interactive mode](https://developers.openai.com/codex/noninteractive/),
[Codex SDK](https://developers.openai.com/codex/sdk/) 계약을 따릅니다.

세부 개발 계약은 `PLANS.md`와 `docs/ARCHITECTURE.md`, `docs/CONTRACTS.md`,
`docs/TESTING.md`를 참고합니다. 현재 완료 범위는 `docs/PLAN_COVERAGE.md`에 기록합니다.
