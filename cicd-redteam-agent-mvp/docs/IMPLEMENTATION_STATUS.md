# 프로토타입 구현 현황

> **현재 상태를 판단하는 기준 문서**
>
> 최종 확인일: 2026-08-29 · 대상 버전: `0.1.0`

CI/CD Red Team Agent는 로컬 GitHub Actions 저장소를 실행하지 않고 분석해, 공격자 입력에서
쓰기 권한 `GITHUB_TOKEN` 또는 Cloud Identity 후보까지 이어지는 경로를 Evidence와 함께
제시하는 기술 검증용 프로토타입입니다. reasoning mode에서는 명시적인 opt-in으로 GitHub.com
  repository policy와 opt-in AWS IAM trust를 read-only 관찰해 일부 외부 `UNKNOWN`을 semantic
  state에 반영할 수 있습니다.

## 프로토타입이 하는 일

```text
Local repository path
        ↓
Workflow / Job / Step parser
        ↓
Event-scoped execution semantics
        ↓
Fact / Relation / Guard / Evidence extraction
        ↓
deterministic graph
        ↓
optional read-only GitHub observation → semantic recompute
        ↓
optional LLM reasoning
        ↓
deterministic validator
        ↓
analysis.json + summary.md + trace.jsonl
```

- GitHub Actions YAML, local action, reusable workflow, script 참조를 정적으로 분석합니다.
- PR 입력, 저장소 파일, Artifact, Cache, 외부 의존성 등의 공격자 통제 가능성을 구분합니다.
- Token, Secret reference, OIDC 및 Cloud Identity 후보와 연결된 경로를 만듭니다.
- 확인하지 못한 외부·실행 시점 상태는 `UNKNOWN`으로 유지합니다.
- `--observe-github`에서는 selected Frontier에 필요한 GitHub repository policy와 선택적 run-time
  상태만 typed Observation으로 관찰하고 execution semantics와 guard를 재계산합니다.
- LLM은 Fact를 연결해 제안하고, 최종 판정은 코드 Validator가 수행합니다.
- Reasoning 분석은 선택적으로 trusted 자연어 Objective를 받아 조사 목표를 좁힐 수 있습니다.
  Objective는 system policy나 Validator를 덮지 못하며 repository/tool 출력과 분리됩니다.
  deterministic Goal compiler가 제한된 source/mechanism/sink 힌트를 만들고 관련 Relation을
  우선 정렬하지만 Fact/verdict/최종 판정은 변경하지 않습니다.

## 구현 범위

| 기능 | 상태 | 현재 범위 |
|---|---|---|
| Workflow·Job·Step 파싱 | 완료 | Trigger, permission 선언, environment, runner, action 및 script |
| GitHub Actions 실행 의미론 | 완료 | event-scoped trigger/job context, reachability, permission lattice, workflow_run/workflow_call |
| Evidence와 저장소 경계 | 완료 | 줄 번호, redaction, 크기·경로·검색 제한 |
| PR 입력·파일 통제 분석 | 완료 | `fork-pr`, `repo-writer` attacker profile |
| GitHub Token 경로 | 완료 | event별 effective permission을 계산해 공격자 통제 실행에서 write-capable Token까지 연결 |
| Artifact 경로 | 완료 | PR 생산 → 특권 workflow 다운로드·실행 → Token |
| Cache 경로 | 완료 | PR merge-ref cache 오염 경로 + low-trust default-branch cache read-only 정책 구분 |
| Mutable dependency 경로 | 완료 | 외부 Action·workflow·image·install source를 Candidate로 연결 |
| OIDC·Cloud Identity | 완료 | action 인증 모드 + GitHub aud/sub + AWS IAM trust의 TRUE/FALSE/UNKNOWN 판정 |
| Secret 흐름 | 부분 구현 | env, shell, output, file, artifact, 외부 action Fact 추출 |
| Reusable workflow | 부분 구현 | local caller→callee context와 permission ceiling은 deterministic, 외부 callee 상태는 `UNKNOWN` |
| Self-hosted Runner | 부분 구현 | exact unique job identity와 실제 `runner_id` 비교; cleanup·ephemerality는 `UNKNOWN` |
| Environment protection | 부분 구현 | reviewer/wait/branch/custom rule 및 pending deployment 관찰; 빈 pending 결과는 승인으로 간주하지 않음 |
| 검토 우선순위·수정 방향 | 완료 | 전역 경로 정렬과 Relation 기반 권장 조치 표시 |
| 대화형 분석 실행 | 완료 | 터미널 모드 선택, Reasoning 자연어 목표 입력, 자동화 비대화형 유지 |
| 자연어 Reasoning Objective | 완료 | 대화형 입력, text/file/explicit stdin, trust boundary, checkpoint/resume 보존 |
| Objective Goal compilation | 완료 | source/mechanism/sink 힌트, goal-aware Fact 우선순위, verdict 비간섭 |
| Goal-aware Graph Frontier | 완료 | context-sensitive source→sink 후보, guard 우선 차단, diversity/limit metadata |
| Deterministic 조사 정책 | 완료 | candidate score, 6개 portfolio, 가설 reconciliation, OBSERVE/INVESTIGATE/SUBMIT/STOP_PARTIAL gate |
| Reasoning matrix 평가 | 완료 | 10개 그룹 × 3개 variant의 정확도·시간 표 자동 생성 |
| GitHub API 관찰 | 부분 구현 | Generic Evidence A-G resolver/transport와 semantic recompute 완료; P0 actor/run/current-permission, P1 event linkage/principal, P2 workflow-run status/jobs/steps를 controlled private fixtures에서 live 검증하고 해당 slices freeze 완료; runner cleanup·historical authorization·Cloud trust는 후속 범위 |
| `if`·actor/status/dependency 의미론 | 완료(offline 검증) | P4 truth/reachability와 attacker satisfier를 분리하고 `contains/startsWith/endsWith`, event-aware refs, repository/actor/label trust 및 canonical Frontier guard/source를 구현; wildcard `needs.*`, matrix aggregate, historical event-time permission과 exact label revision binding은 근거 없으면 `UNKNOWN` |
| 동적 조사 재개 | 완료 | Knowledge/Task ledger/attempt/Observation을 safe checkpoint에 영속화하고 canonical state 대조 |
| Cloud API 연동 | 부분 구현 | opt-in AWS IAM role/provider read-only 관찰; STS·role permission·Azure/GCP는 미구현 |
| Audit log·Runner 심층 조사 | 부분 구현 | workflow-run job/runner identity는 조회, cleanup·ephemeral 설정과 audit history는 미구현 |
| Production·Kubernetes 영향 검증 | 미구현 | Token과 Cloud Identity 후보를 최종 sink로 사용 |
| 실제 공격·배포 실행 | 미구현 | 프로토타입 안전 경계상 의도적으로 제외 |

## 데모 핵심 시나리오

| 시나리오 | 기대 결과 | 대표 fixture |
|---|---|---|
| PR Shell Injection | 직접 명령 해석을 `SUPPORTED`, 안전한 env 전달을 차단 경로로 구분 | `gha-01-shell-injection` |
| `pull_request_target` PR 코드 실행 | checkout version/provenance와 2026 built-in fork 보호에 따라 차단/UNKNOWN/통제 실행 구분 | `pr-target-blocked`, `pr-target-controlled`, `pr-target-unknown` |
| Artifact 특권 Workflow 실행 | PR Artifact에서 write Token까지 전역 경로 | `gha-03-artifact-trust` |
| Mutable external Action + Token | 외부 compromise가 필요한 `CANDIDATE` 경로 | `gha-04-mutable-action` |
| Cache poisoning | PR Cache에서 특권 Job write Token까지 전역 경로 | `reasoning-matrix/gha-08` |
| OIDC + Cloud Identity | 누락 evidence는 UNKNOWN, 완전한 AWS trust는 TRUE/FALSE | `oidc-risk` |

Secret, reusable workflow, self-hosted Runner는 프로토타입에서 Fact 추출과
`CANDIDATE/UNKNOWN` 설명 범위로 제한합니다.

## 현재 한계

- GitHub URL을 직접 clone하지 않습니다. 사용자가 준비한 로컬 저장소 경로가 필요합니다.
- branch rules, Environment approval과 runner assignment는 로컬 YAML만으로 확정할 수 없으며
  `--observe-github`와 필요한 run ID/권한이 있을 때만 해당 typed evidence를 조회합니다.
- repository 기본 `GITHUB_TOKEN` 권한은 `--observe-github` opt-in과 적절한 GitHub credential이
  있을 때만 외부 관찰로 해소할 수 있습니다.
- AWS trust는 `--observe-aws`에서만 read-only 조회합니다. 실제 Role assumption 성공, role
  permission, Azure/GCP trust는 조회하지 않습니다.
- Mutable dependency는 실제 침해가 아니라 외부 compromise를 전제로 한 Candidate입니다.
- 정적 분석 결과는 실제 악용을 증명하지 않으므로 `CONFIRMED`를 사용하지 않습니다.
- 지원하지 않는 동적 YAML 표현, 복잡한 JavaScript 및 런타임 생성 파일은 `UNKNOWN`이 될 수
  있습니다.
- Frontier는 등록된 Relation 의미만 fail-closed 방식으로 순회합니다. 새 Relation type은 semantic
  registry에 명시적으로 추가하기 전까지 경로 후보에 포함되지 않습니다.
- P4 condition security는 GitHub 외부 actor/event allowlist나 임의 동적 expression을 YAML만으로
  추정하지 않으며, label 존재를 승인된 exact commit 증거로 사용하지 않습니다.

## 실행 명령

### 분석 대상 준비

```powershell
git clone https://github.com/example/project.git C:\analysis-targets\project
```

### LLM 없는 deterministic 분석

```powershell
uv run cicd-agent analyze C:\analysis-targets\project --output .\output
```

### ChatGPT 로그인 기반 reasoning 분석

```powershell
codex login
uv run cicd-agent analyze C:\analysis-targets\project `
  --output .\output `
  --provider codex-chatgpt `
  --objective "fork PR에서 OIDC 경로를 우선 조사해"
```

### GitHub repository policy를 함께 관찰하는 reasoning 분석

```powershell
# trusted operator 환경에 GH_TOKEN 또는 GITHUB_TOKEN 설정
uv run cicd-agent analyze C:\analysis-targets\project `
  --output .\output `
  --provider codex-chatgpt `
  --observe-github
```

GitHub 관찰이 실패하면 외부 상태는 `UNKNOWN`으로 유지되고 모델 분석은 계속됩니다.

### 30개 reasoning fixture 평가

```powershell
uv run cicd-agent evaluate tests/fixtures/reasoning-matrix `
  --output output/evaluation `
  --provider codex-chatgpt
```

평가 후 `matrix-evaluation/evaluation.json`과 `evaluation.md`에 Fact 추출, 상태 정확도,
`missing_evidence` 보존, 안전 variant 오탐 및 deterministic/reasoning 시간을 기록합니다.
30개 reasoning case는 실제 모델 호출을 포함하므로 사용량과 실행 시간을 확인한 뒤 실행합니다.
일반 reasoning 분석의 긴 Objective는 `--objective-file`로 전달할 수 있으며 stdin은
`--objective-file -`를 지정한 경우에만 읽습니다. 분석 대상 저장소 내부 파일은 trusted
Objective source로 사용할 수 없습니다.

## 데모 절차

1. 의존성을 설치합니다: `uv sync`
2. 핵심 복합 fixture를 분석합니다.

   ```powershell
   uv run cicd-agent analyze tests/fixtures/global-chain --output output/demo
   ```

3. 출력된 run 디렉터리의 `summary.md`를 엽니다.
4. 분석 범위와 판정 분포를 확인합니다.
5. Global attack path의 검토 우선순위, Evidence 위치, missing Evidence를 설명합니다.
6. `GITHUB_TOKEN` 및 Cloud Identity 경로의 권장 조치를 확인합니다.
7. `analysis.json`에서 동일한 Evidence와 Relation이 구조화되어 있는지 확인합니다.

## 프로토타입 완료 기준

- 위 6개 핵심 시나리오가 회귀 테스트로 고정되어 있습니다.
- 공격 경로가 기존 Relation과 Evidence만 참조합니다.
- `UNKNOWN`과 `FALSE`가 각각 Candidate와 Rejected 규칙을 지킵니다.
- CLI 명령 하나로 동일한 fixture 분석을 재현할 수 있습니다.
- 보고서가 우선순위, 근거 위치, 추가 확인 항목과 수정 방향을 제공합니다.
- 평가 결과가 JSON과 Markdown 표로 자동 저장됩니다.

이 범위를 넘는 GitHub policy/API 관찰 확대, Cloud API 연동, GUI, 실제 공격 실행 및 다중 CI
제품 지원은 후속 작업입니다.
