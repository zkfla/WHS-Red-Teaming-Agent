# CI/CD Red Team Agent

> GitHub Actions 저장소를 실행하지 않고 분석해, 근거가 있는 공격 경로를 만드는 읽기 전용 보안 분석 프로토타입

CI/CD Red Team Agent는 workflow, job, step, artifact, credential 사이의 관계를 정적으로
분석합니다. 저장소에서 확인한 사실은 deterministic 규칙으로 처리하고, 선택적으로 LLM을
사용해 불확실한 연결을 조사합니다. reasoning mode에서는 명시적인 `--observe-github`
opt-in이 있을 때만 GitHub.com의 read-only repository policy를 관찰해 외부 `UNKNOWN`을
semantic state에 반영할 수 있습니다. 모든 결과는 원본 위치를 가리키는 Evidence와 함께
저장됩니다.

> [!IMPORTANT]
> 허가받은 저장소의 방어 목적 분석에만 사용하세요. 이 도구는 workflow, action, script를
> 실행하지 않으며 실제 공격이나 취약점 악용을 수행하지 않습니다.

## 핵심 기능

| 기능 | 설명 |
|---|---|
| 정적 분석 | GitHub Actions YAML과 로컬 action/script 참조를 실행 없이 추적합니다. |
| Build/package 실행 의미론 | 정적으로 해석 가능한 package install → repository-local dependency → lifecycle hook → script의 network/workspace/process side effect를 실행 없이 추적합니다. |
| 공격 경로 구성 | 공격자 입력에서 `GITHUB_TOKEN` 또는 Cloud Identity까지 관계를 연결합니다. |
| Evidence 기반 판정 | 로컬 근거를 우선하고 확인하지 못한 외부 상태는 `UNKNOWN`으로 보존합니다. |
| Candidate 중심 조사 | deterministic와 reasoning이 같은 `SecurityCandidate`에서 시작하고, bounded portfolio와 claim contract로 제출 범위를 고정합니다. |
| 선택적 GitHub 관찰 | `--observe-github`에서 selected Frontier의 policy/runtime evidence만 읽고 semantic state를 재계산합니다. |
| 선택적 Runtime telemetry | trusted operator JSONL을 `--runtime-telemetry-file`로 받아 exact workflow step에 network/file/process Observation을 결합하고 semantic state를 재계산합니다. |
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
Build / package lifecycle semantics
      │
      ▼
Event-scoped execution semantics
      │
      ▼
Fact / Relation / Guard / Evidence
      │
      ├── deterministic mode ──▶ SecurityCandidate ──▶ Finding / AttackPath
      │
      └── reasoning mode ──────▶ same Candidate portfolio
                                      │
                                      ▼
                                optional GitHub / runtime observation
                                      │
                                      ▼
                                semantic recompute
                                      │
                                      ▼
                                  LLM proposal ──▶ validator
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

### 0. 자연어 모드 (추천)

명령 옵션을 외울 필요 없이 다음처럼 실행할 수 있습니다.

```powershell
uv run cicd-agent
```

그 다음 한 문장으로 저장소와 원하는 분석을 입력합니다.

```text
분석 요청> https://github.com/step-security/github-actions-goat 이 저장소의 CI/CD 취약점과 공격 경로를 폭넓게 분석해줘
```

GitHub URL은 실행하지 않고 별도 로컬 분석용 복제본으로 가져온 뒤 분석합니다. 로컬 경로도
그대로 사용할 수 있습니다. 자연어 모드는 기본적으로 저장된 Codex ChatGPT 로그인을 사용하는
reasoning 분석을 선택하며, `AI 없이 정적 분석만 해줘`라고 명시하면 deterministic mode를
사용합니다. `gpt-5.6-luna로 분석해줘`처럼 모델명을 적으면 해당 모델을 사용하고,
`기본 모델로 분석해줘`라고 하면 Codex 기본 모델을 사용합니다. `실제 GitHub 설정과 실제
권한까지 GitHub API로 확인해줘`처럼 외부 관찰을 명시한 경우에만 read-only GitHub
observation을 opt-in 합니다.

한 줄 실행도 가능합니다.

```powershell
uv run cicd-agent https://github.com/step-security/github-actions-goat 이 저장소 CI/CD 취약점 분석해줘
```

분석이 끝나면 `analysis.json`을 기반으로 최종 Finding 또는 아직 해결되지 않은 상위 attack
frontier, 관련 workflow, missing Evidence와 PARTIAL 종료 원인을 터미널에 자연어로 출력합니다.
PARTIAL은 `0 findings`와 동일하게 표시하지 않습니다. 기존 실행 결과도 다시 읽을 수 있습니다.

```powershell
uv run cicd-agent .\output\<run-id>\analysis.json 결과 설명해줘
uv run cicd-agent 마지막 분석 결과 보여줘
```

기존 `analyze`, `resume`, `evaluate` 명령과 모든 고급 플래그는 그대로 유지됩니다.

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
├── checkpoint.json # reasoning mode의 마지막 안전 라운드·조사 세션 상태
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

### GitHub repository policy 관찰

reasoning mode에서 repository/fork/Actions/branch/Environment policy 같은 외부 상태를 read-only로
관찰하려면 명시적으로 `--observe-github`를 추가합니다. 기본 실행에서는 네트워크 관찰을
하지 않습니다.

```powershell
# trusted operator 환경에 GH_TOKEN 또는 GITHUB_TOKEN 설정
uv run cicd-agent analyze C:\path\to\repository `
  --output .\output `
  --mode reasoning `
  --provider codex-chatgpt `
  --observe-github
```

특정 workflow run의 pending deployment와 runner assignment까지 관찰할 때만 양수 run ID를
추가합니다. `--github-run-id`는 `--observe-github` 없이 사용할 수 없습니다.

```powershell
uv run cicd-agent analyze C:\analysis-targets\project `
  --output .\output `
  --mode reasoning `
  --provider codex-chatgpt `
  --observe-github `
  --github-run-id 4242
```

자동 repository 해석은 `github.com` origin만 허용하며 API credential은 `api.github.com`에만
전송합니다. 관찰이 실패하면 해당 외부 상태는 `UNKNOWN`으로 유지되고 reasoning 분석은
계속됩니다. Environment 응답은 reviewer identity를 저장하지 않고 reviewer 수, self-review 차단,
wait timer, deployment branch policy와 custom protection rule 수만 정규화합니다.
Runtime job은 정적 job key/name과 유일하게 정확히 일치할 때만 연결합니다. 같은 `runner_id`는
identity만 해소하고 cleanup/ephemerality는 계속 `UNKNOWN`이며, 서로 다른 concrete ID만 해당
run의 cross-job runner-state 경로를 `FALSE`로 닫습니다.

### Trusted runtime telemetry 관찰

Reasoning 분석은 분석 대상 저장소 밖의 trusted operator JSONL을 선택적으로 받아, 이미 수집된
process/network/workspace-file runtime event를 정확히 하나의 workflow/job/step identity에 결합할 수
있습니다. 이 기능은 workflow나 script를 실행하거나 telemetry를 직접 수집하지 않습니다.

```powershell
uv run cicd-agent analyze C:\analysis-targets\project `
  --output .\output `
  --mode reasoning `
  --provider codex-chatgpt `
  --runtime-telemetry-file C:\trusted\run-telemetry.jsonl
```

정적 package lifecycle side effect와 runtime Observation은 별도 bounded claim으로 유지됩니다.
정적 network/write 코드는 실제 전송·변조를 의미하지 않고, runtime network/file event 역시
credential exfiltration이나 악성 의도를 자동으로 의미하지 않습니다.

AWS GitHub OIDC trust까지 판정하려면 GitHub 관찰에 `--observe-aws`를 함께 추가합니다.
선택적으로 `--aws-profile <name>`을 지정할 수 있으며 IAM `GetRole`과
`GetOpenIDConnectProvider`만 read-only로 호출합니다. 정적 trust가 `TRUE`여도 실제 STS
AssumeRole 성공이나 role permission을 검증한 것은 아닙니다.

```powershell
uv run cicd-agent analyze C:\analysis-targets\project `
  --output .\output `
  --provider codex-chatgpt `
  --observe-github `
  --observe-aws `
  --aws-profile security-audit
```

### ChatGPT 로그인 기반 Codex

```powershell
codex login
codex login status

uv run cicd-agent analyze C:\path\to\repository `
  --output .\output
```

실제 터미널에서 모드 옵션을 생략하면 `기본 공격 경로 분석` 또는
`AI 심층 공격 경로 분석 (추천)`을 선택하는 안내가 표시됩니다. 1 또는 2를 입력해 분석 방식을
선택합니다. AI 심층 분석을 고르면 저장된 Codex ChatGPT 로그인을 사용하고 이어서
`분석 목표를 입력하세요`라고 묻습니다. 한국어나 영어 문장으로 목표를 입력하거나 Enter를 눌러
전체 범위를 분석할 수 있습니다.

자동화된 실행에서는 질문을 표시하지 않습니다. 대화형 터미널에서도 `--no-mode-prompt`와
`--no-objective-prompt`로 각 질문을 끌 수 있습니다.

미리 목표를 전달하려면 기존처럼 다음 옵션을 사용합니다.

```powershell
uv run cicd-agent analyze C:\path\to\repository `
  --output .\output `
  --mode reasoning --provider codex-chatgpt --model gpt-5.6-luna `
  --objective "fork PR에서 OIDC와 Cloud Identity 연결 경로를 우선 조사해"
```

긴 분석 목표는 `--objective-file C:\trusted\objective.md`로 전달할 수 있고, stdin은
`--objective-file -`를 명시한 경우에만 읽습니다. Objective 파일은 분석 대상 저장소 밖의
trusted operator 입력이어야 하며, deterministic 모드에서는 Objective를 받지 않습니다.
System policy와 Validator 규칙이 Objective보다 우선하고 repository/tool 출력은 계속
untrusted data로 취급됩니다. Reasoning 모드에서는 Objective에서 `fork_pr`, `artifact`, `cache`,
`oidc`, `cloud_identity` 같은 제한된 탐색 힌트를 deterministic하게 컴파일합니다. 이 구조화 Goal은
`ListAttackFrontiers`의 source→sink 후보 탐색·정렬과 `ListSecurityFacts`의 관련 Relation 정렬에만
쓰이며 Evidence, Fact, verdict, Guard 또는 취약점 결론을 생성하지 않습니다.

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
`--observe-github` 실행은 `KnowledgeState`, Task ledger와 attempts, 정규화된 Observation을
versioned investigation snapshot으로 함께 저장합니다. snapshot의 `AnalysisState`가 canonical
state와 다르거나 active Task lease가 남아 있으면 안전한 재개 지점이 아니므로 거부합니다.

Reasoning Agent는 조사 중인 가설, 근거 Relation/Evidence, 부족한 정보와 다음 확인 대상을
`working_memory`에 최대 8개까지 보존합니다. 가설 변경은 선택한 Capability 호출에 함께
포함되므로 별도 모델 호출이나 Agent 라운드를 소비하지 않습니다. 이 메모는 Finding이나
AttackPath가 아니며 최종 결과는 기존 `SubmitAnalysis` validator만 확정합니다.
작업 메모도 `R0001` 형태의 짧은 Relation 참조를 사용할 수 있으며, 관련 Relation ID,
Evidence와 missing evidence는 런타임이 자동으로 복원합니다.
최종 제출에서는 `R0001` 형태의 짧은 Relation 참조와 순서만 선택할 수 있으며, 정확한
Relation ID·node·edge·Evidence·missing evidence와 상태는 런타임이 원본 Fact에서 조립합니다.
따라서 모델이 긴 식별자를 다시 작성하면서 발생하는 오타가 최종 결과에 들어가지 않습니다.
`ListAttackFrontiers`는 먼저 실행 context, guard, missing evidence와 연속된 Relation 참조를 묶은
bounded 후보를 반환합니다. `COMPLETE`이면서 `proposal_ready=true`인 후보만 제출 검토 대상이고,
`PARTIAL`은 다음 조사 지점을 보여주는 진단용입니다. 단순 trigger→권한 연결은 공격자 입력이나
실행 통제가 이어지지 않으면 proposal-ready가 아닙니다. `ListSecurityFacts`는 Frontier가 충분하지
않을 때 기본 compact Fact를 반환하며, legacy 전체 제출
호환성이 필요할 때만 exact grounding ID를 명시적으로 요청합니다.

각 Frontier에는 readiness, sink impact, Evidence grounding, 예상 information gain, Objective 적합도,
조사 비용을 합친 deterministic candidate score가 적용됩니다. 런타임은 Relation·context 중복에
패널티를 주는 bounded portfolio를 선택하고 `OBSERVE`, `INVESTIGATE`, `SUBMIT`, `STOP_PARTIAL` 중
다음 전환을 결정합니다. `STOP_PARTIAL`은 round/token/cost/duration 한도, 다음 round token reserve
부족, 반복 호출·진행 정체를 하나의 policy 입력으로 정규화하며 현재 부분 상태를 보존합니다.
semantic recompute로 근거가 사라지거나 hard-block된 가설과 동일 Relation 묶음을 반복하는 가설은
자동 `DISCARDED`됩니다. `SubmitAnalysis`는 `SUBMIT` 상태의 허용 경로 또는
`decision_submission_allowed=true`인 완전한 terminal decision set만 허용합니다. broad 분석에서
이 호출은 최종 종료가 아니라 현재 root-cause representative batch를 commit하고 다음 uncovered
root cause로 portfolio를 회전시킵니다. 모든 eligible root-cause instance가 terminal일 때만 최종
`SUBMIT`으로 전환하며, 검색이 완결됐고 제출 가능한 경로가 없을 때만 빈 제출을 허용합니다.

`SecurityCandidate`는 Frontier와 다른 계층의 canonical semantic hypothesis입니다. 후보는
root cause, kind, scope, claim ceiling, supporting/contradicting Relation, Evidence, missing
Evidence, EvidenceRequirement와 grounded path variant를 함께 보유합니다. broad 분석은 선택된
Candidate/Frontier 모두에 terminal disposition을 요구하고, 같은 `root_cause_key` occurrence는
한 representative decision으로 함께 accounting합니다. terminal root cause는 다음 batch에서
재선택하지 않으며 uncovered root cause가 남으면 `INVESTIGATE`를 유지합니다. 정책이
`decision_submission_allowed=true`를 표시한 경우에만 현재 batch의 완전한 decision set을 commit할
수 있습니다.
`ACCEPTED` 후보는 runtime이 canonical claim contract에서 Finding/AttackPath를 materialize하므로
모델이 임의의 rule/title/summary나 `CONFIRMED`를 만들어낼 수 없습니다.

Frontier의 ordinary attacker-controlled execution은 `execution` sink로 표현합니다. 이는 그 자체로
privilege가 아니며 write-capable `GITHUB_TOKEN`, Secret access, OIDC/Cloud Identity 같은 별도
privileged capability Relation이 이어질 때만 해당 sensitive-impact 경로를 구성합니다.

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
- 외부 action을 다운로드하지 않습니다. GitHub/AWS API는 reasoning mode의 명시적
  `--observe-github`/`--observe-aws` opt-in에서만 read-only로 접근합니다.
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
