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
Safe parser
      │
      ▼
Event-scoped execution semantics
      │
      ▼
Fact / Relation / Guard / Evidence
      │
      ├── deterministic mode ──▶ scenario rules + attack graph
      │
      └── reasoning mode ──────▶ LLM proposal ──▶ validator
                                                   │
                                                   ▼
                              analysis.json + summary.md + trace.jsonl
```

Parser의 permission 요약은 문법 호환용이고 보안 판정의 권한 원본이 아닙니다. 별도 execution
semantic layer가 event별 trigger/job context, reachability, effective `GITHUB_TOKEN` permission,
local reusable-workflow caller ceiling과 `workflow_run` 경계를 계산한 뒤 Fact를 발행합니다.
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
├── trace.jsonl     # redaction된 분석 이벤트
├── checkpoint.json # reasoning mode의 마지막 안전 라운드 상태
└── events.jsonl    # append-only Agent 실행 이력
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
  --model gpt-5.6-luna `
  --objective "fork PR에서 OIDC와 Cloud Identity 연결 경로를 우선 조사해"
```

긴 분석 목표는 `--objective-file C:\trusted\objective.md`로 전달할 수 있고, stdin은
`--objective-file -`를 명시한 경우에만 읽습니다. Objective 파일은 분석 대상 저장소 밖의
trusted operator 입력이어야 하며, deterministic 모드에서는 Objective를 받지 않습니다.
System policy와 Validator 규칙이 Objective보다 우선하고 repository/tool 출력은 계속
untrusted data로 취급됩니다. Reasoning 모드에서는 Objective에서 `fork_pr`, `artifact`, `cache`,
`oidc`, `cloud_identity` 같은 제한된 탐색 힌트를 deterministic하게 컴파일합니다. 이 구조화 Goal은
`ListSecurityFacts`의 관련 Relation 정렬과 모델의 조사 우선순위에만 쓰이며 Evidence, Fact, verdict,
Guard 또는 취약점 결론을 생성하지 않습니다.

`codex-chatgpt`는 API key나 Codex 인증 토큰을 프로그램으로 복사하지 않습니다. 공식
`openai-codex` Python SDK가 저장된 ChatGPT 인증을 재사용하는 LLM 연결을 제공합니다.
각 라운드에서 Codex가 다음 Capability 하나를 선택하면 기존 hackingBuddyGPT loop가
그 도구를 실행하고 결과를 다음 라운드로 돌려줍니다. 따라서 LiteLLM 방식과 동일하게
`list → inspect/search → submit_analysis` 흐름을 수행합니다.
Codex는 라운드 사이의 별도 대화를 소유하지 않습니다. hackingBuddyGPT의 구조화 상태,
진행 정보와 최근 Agent 이벤트만 임시 Codex thread에 전달하므로 상태 원본과 checkpoint는
provider와 무관하게 기존 Agent에 남습니다.
`--model`을 생략하면 Codex의 현재 기본 모델을 사용합니다. 모델을 지정하지 않고
provider도 선택하지 않으면 deterministic 분석만 수행합니다.

Reasoning 실행이 모델 오류나 예산 제한으로 PARTIAL 종료됐다면 마지막으로 확정된
라운드 경계에서 같은 실행 디렉터리를 이어갈 수 있습니다. 필요하면 누적 한도를 더 큰
값으로 올립니다.

```powershell
uv run cicd-agent resume .\output\<run-id> --max-rounds 20 --max-duration 1800
```

재개는 원래 저장소 커밋이 그대로이고 작업 트리가 깨끗할 때만 허용됩니다. reasoning
Objective가 있던 실행은 exact Objective, SHA-256 hash와 deterministic compiled Goal을
checkpoint에서 복원하며 재개 중 변경할 수 없습니다. 완료된 실행, 다른 프롬프트 버전,
Objective hash/compiled Goal 불일치, 손상된
checkpoint/event journal, 이미 사용한 양 이하의 한도는 거부됩니다. 중단된 라운드의 부분 상태와 대화 조각은 버리고 마지막 checkpoint부터 다시
시도하며, 기존 `analysis.json`, `summary.md`, `trace.jsonl`은 같은 run 디렉터리에서 갱신됩니다.

Reasoning Agent는 조사 중인 가설, 근거 Relation/Evidence, 부족한 정보와 다음 확인 대상을
`working_memory`에 최대 8개까지 보존합니다. 가설 변경은 선택한 Capability 호출에 함께
포함되므로 별도 모델 호출이나 Agent 라운드를 소비하지 않습니다. 이 메모는 Finding이나
AttackPath가 아니며 최종 결과는 기존 `SubmitAnalysis` validator만 확정합니다.
작업 메모도 `R0001` 형태의 짧은 Relation 참조를 사용할 수 있으며, 관련 Relation ID,
Evidence와 missing evidence는 런타임이 자동으로 복원합니다.
최종 제출에서는 `R0001` 형태의 짧은 Relation 참조와 순서만 선택할 수 있으며, 정확한
Relation ID·node·edge·Evidence·missing evidence와 상태는 런타임이 원본 Fact에서 조립합니다.
따라서 모델이 긴 식별자를 다시 작성하면서 발생하는 오타가 최종 결과에 들어가지 않습니다.
`ListSecurityFacts`는 기본적으로 이 선택에 필요한 compact Fact만 반환하며, legacy 전체 제출
호환성이 필요할 때만 exact grounding ID를 명시적으로 요청합니다.

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
label은 parser, Agent capability, prompt에 전달되지 않습니다. 평가 결과에는 탐지 정확도와
라운드·토큰·Capability 호출·working-memory 상태 같은 관측 지표만 기록하며 private
chain-of-thought는 저장하지 않습니다.

Reasoning mode는 일시적인 모델 연결·timeout·rate-limit·server 오류만 최대 두 번
재시도하고 전체 duration 제한을 공유합니다. 오래된 limit 메시지와 context 한도를 넘는
과거 라운드는 모델 입력에서만 축약하며 원본 분석 상태는 유지합니다. 자세한 정책은
`docs/AGENT_RUNTIME.md`를 참고하세요.

## 안전 경계

- workflow, script, local action을 실행하지 않습니다.
- 외부 action을 다운로드하거나 GitHub/AWS API에 접근하지 않습니다.
- `.git`, binary, 1 MiB 초과 파일과 저장소 밖 symlink를 읽지 않습니다.
- 파일 slice는 최대 200줄, literal 검색 결과는 최대 50건입니다.
- secret 값은 저장하지 않고 GitHub secret expression은 참조로만 남깁니다.
- branch protection, environment approval, 기본 token 권한, IAM trust처럼 로컬에서 확인할
  수 없는 상태는 `UNKNOWN`으로 처리합니다.
- 정적 Attack Path는 `CONFIRMED`로 승격하지 않습니다.
- Codex LLM 연결은 빈 임시 작업 폴더, read-only sandbox, approval deny-all에서 실행됩니다.
  사용자 Codex 설정에 등록된 plugin과 MCP server도 이 실행에서는 비활성화됩니다.
  Codex 자체 도구에는 대상 저장소를 주지 않고, Capability schema와 구조화 상태 요약,
  최근 일곱 Agent 이벤트 및 redaction된 도구 결과만 전달합니다.
- branch protection, environment approval, repository 기본 권한, IAM trust policy는
  로컬 Evidence가 없으면 `UNKNOWN`입니다.

## 문서 안내

문서를 처음 읽는다면 **[문서 허브](docs/README.md)**에서 목적에 맞는 문서를 선택하세요.

| 문서 | 이런 때 읽으세요 |
|---|---|
| [Implementation status](docs/IMPLEMENTATION_STATUS.md) | 현재 완료·부분·미구현 범위와 데모 절차를 확인할 때 |
| [Architecture](docs/ARCHITECTURE.md) | 처리 파이프라인과 모듈 경계를 이해할 때 |
| [Public contracts](docs/CONTRACTS.md) | CLI, 출력 schema, 종료 조건을 구현하거나 연동할 때 |
| [Testing](docs/TESTING.md) | 테스트 범위와 release check를 확인할 때 |
| [Plan coverage](docs/PLAN_COVERAGE.md) | 현재 구현·검증 완료 범위를 확인할 때 |

새로운 ChatGPT-login Codex 실모델 검증이 필요하면 사용량 절감을 위해
`--model gpt-5.6-luna`를 명시합니다. 기본 테스트는 외부 모델을 호출하지 않습니다.

fixture 기반 E2E는 모델과 네트워크 없이 반복 실행됩니다. ChatGPT 로그인 기반 real-model
smoke는 `tests/fixtures/global-chain`에서 `proposal_validated` COMPLETE까지 확인했습니다.
실제 LiteLLM provider smoke는 해당 provider 자격 증명과 비용·사용량 한도가 있을 때만
수행합니다. Codex 경로의 보고서 비용 `0.0`은 SDK가 통화 비용을 제공하지 않는다는 뜻이며,
무료 사용을 보장하지 않습니다.

## 프로젝트 상태

| 항목 | 값 |
|---|---|
| Version | `0.1.0` |
| Runtime | Python `3.13+` |
| Package manager | `uv` |
| Analysis modes | `deterministic`, `reasoning` |
| License | MIT |
