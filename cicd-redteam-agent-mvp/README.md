# CI/CD Red Team Agent

> GitHub Actions 저장소를 실행하지 않고 분석해, 근거가 있는 공격 경로를 만드는 읽기 전용 보안 분석 프로토타입

CI/CD Red Team Agent는 workflow, job, step, artifact, credential 사이의 관계를 정적으로
분석합니다. 저장소에서 확인한 사실은 deterministic 규칙으로 처리하고, 선택적으로 LLM을
사용해 불확실한 연결을 조사합니다. 모든 결과는 원본 위치를 가리키는 Evidence와 함께
저장됩니다.

> [!IMPORTANT]
> 허가받은 저장소의 방어 목적 분석에만 사용하세요. 이 도구는 workflow, action, script를
> 실행하지 않으며 실제 공격이나 취약점 악용을 수행하지 않습니다.

## 핵심 기능

| 기능 | 설명 |
|---|---|
| 정적 분석 | GitHub Actions YAML과 로컬 action/script 참조를 실행 없이 추적합니다. |
| 공격 경로 구성 | 공격자 입력에서 `GITHUB_TOKEN` 또는 Cloud Identity까지 관계를 연결합니다. |
| Evidence 기반 판정 | 로컬 근거만 `SUPPORTED`로 판정하고 외부 상태는 `UNKNOWN`으로 남깁니다. |
| 선택적 LLM 추론 | LiteLLM 또는 ChatGPT 로그인 기반 Codex로 Fact/Relation을 조합합니다. |
| 안전한 결과 저장 | secret을 redaction하고 구조화 결과, 요약, trace를 분리해 기록합니다. |

## 동작 방식

```text
Local repository
      │
      ▼
Safe parser ──▶ Fact / Relation / Guard / Evidence
      │
      ├── deterministic mode ──▶ scenario rules + attack graph
      │
      └── reasoning mode ──────▶ LLM proposal ──▶ validator
                                                   │
                                                   ▼
                              analysis.json + summary.md + trace.jsonl
```

LLM은 저장소를 직접 읽거나 Evidence를 만들지 않습니다. 제한된 capability로 redaction된
정보만 확인하고 경로를 제안하며, 최종 결과는 deterministic validator가 검증합니다.

## 빠른 시작

### 1. 설치

Python 3.13과 `uv`가 필요합니다.

```powershell
git clone <repository-url>
cd cicd-redteam-agent-mvp
uv sync
```

### 2. 저장소 분석

모델과 네트워크 없이 정적 분석만 실행합니다.

```powershell
uv run cicd-agent analyze C:\path\to\repository --output .\output
```

### 3. 결과 확인

```text
output/<run-id>/
├── analysis.json   # schema version 1.0 구조화 결과
├── summary.md      # Finding, Attack Path, Cloud Identity 요약
└── trace.jsonl     # redaction된 분석 이벤트
```

정적 분석은 실제 악용 여부를 증명하지 않습니다. 로컬 근거가 충분하면 `SUPPORTED`, 외부
설정이나 실행 상태가 필요하면 `CANDIDATE` 또는 `UNKNOWN`으로 기록합니다.

## LLM 추론 사용하기

LLM은 선택 사항입니다. provider를 지정하지 않으면 deterministic mode로 동작합니다.

### API key 기반 provider

`.env.example`을 `.env`로 복사해 필요한 key만 설정합니다.

```powershell
uv run cicd-agent analyze C:\path\to\repository `
  --output .\output `
  --mode reasoning `
  --provider openai `
  --model openai/gpt-5-mini
```

프로세스 환경 변수가 `.env`보다 우선합니다. key가 여러 개라면 `--provider`를 명시해야
하며, 분석 대상 저장소 내부의 `.env`는 인증 정보로 읽지 않습니다.

### ChatGPT 로그인 기반 Codex

```powershell
codex login
codex login status

uv run cicd-agent analyze C:\path\to\repository `
  --output .\output `
  --provider codex-chatgpt `
  --model gpt-5.6-terra
```

`codex-chatgpt`는 API key나 Codex 인증 토큰을 프로그램에 복사하지 않습니다. 모델을
생략하면 Codex의 현재 기본 모델을 사용합니다.

## 평가와 개발 검증

```powershell
# 전체 테스트
uv run pytest

# 정적 검사
uv run ruff check .

# reasoning fixture 한 시나리오 평가
uv run cicd-agent evaluate tests/fixtures/reasoning-matrix `
  --output output/evaluation `
  --provider codex-chatgpt `
  --scenario gha-01
```

평가기는 각 저장소 분석이 끝난 뒤에만 `oracle.json`을 읽습니다. fixture의 정답이나 GHA
label은 parser, Agent capability, prompt에 전달되지 않습니다.

## 안전 경계

- workflow, script, local action을 실행하지 않습니다.
- 외부 action을 다운로드하거나 GitHub/AWS API에 접근하지 않습니다.
- `.git`, binary, 1 MiB 초과 파일과 저장소 밖 symlink를 읽지 않습니다.
- 파일 slice는 최대 200줄, literal 검색 결과는 최대 50건입니다.
- secret 값은 저장하지 않고 GitHub secret expression은 참조로만 남깁니다.
- branch protection, environment approval, 기본 token 권한, IAM trust처럼 로컬에서 확인할
  수 없는 상태는 `UNKNOWN`으로 처리합니다.
- 정적 Attack Path는 `CONFIRMED`로 승격하지 않습니다.
- Codex 연결은 빈 임시 디렉터리, read-only sandbox, approval deny-all로 실행되며 등록된
  plugin과 MCP server를 비활성화합니다.

## 문서 안내

문서를 처음 읽는다면 **[문서 허브](docs/README.md)**에서 목적에 맞는 문서를 선택하세요.

| 문서 | 이런 때 읽으세요 |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | 처리 파이프라인과 모듈 경계를 이해할 때 |
| [Public contracts](docs/CONTRACTS.md) | CLI, 출력 schema, 종료 조건을 구현하거나 연동할 때 |
| [Testing](docs/TESTING.md) | 테스트 범위와 release check를 확인할 때 |
| [Plan coverage](docs/PLAN_COVERAGE.md) | 현재 구현·검증 완료 범위를 확인할 때 |

## 프로젝트 상태

| 항목 | 값 |
|---|---|
| Version | `0.1.0` |
| Runtime | Python `3.13+` |
| Package manager | `uv` |
| Analysis modes | `deterministic`, `reasoning` |
| License | MIT |
