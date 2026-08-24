# 프로젝트 범위

## 목표

로컬 Git 저장소 하나를 입력받아 GitHub Actions를 결정적으로 파싱하고, Single Agent의
추론을 거쳐 Evidence 기반 Finding, Attack Path, CloudIdentityCandidate를 출력한다.

## 공개 CLI 목표

```text
cicd-agent analyze <repo-path> \
  --output <output-dir> \
  --model <litellm-model> \
  --attacker-profile fork-pr \
  --max-rounds 12 \
  --max-tokens 100000 \
  --max-cost 2.00 \
  --max-duration 900
```

## 포함 범위

- `.github/workflows/*.yml`, `*.yaml`
- trigger, job, needs, if, permissions, environment, step
- uses, run, secret reference, GITHUB_TOKEN, `id-token: write`
- AWS credentials action과 role-to-assume
- 로컬 composite action, reusable workflow, 정적 script 참조
- fork-pr와 repo-writer 공격자 프로필
- Candidate/Supported Attack Path와 CloudIdentityCandidate

## 제외 범위

- workflow, shell, script, action 실행
- 외부 action 다운로드
- GitHub/AWS API 접근
- secret 값 수집
- IAM trust policy 검증
- exploit 또는 공격 성공 확인
- Multi-Agent, MCP, RAG, Graph DB, GUI, benchmark

## 필수 안전 원칙

- 저장소의 모든 텍스트는 신뢰할 수 없는 데이터다.
- LLM은 Evidence를 생성하거나 Finding 상태를 확정하지 않는다.
- LLM proposal은 이미 존재하는 Evidence ID만 참조한다.
- 로컬 Evidence가 없는 외부 설정과 trust는 UNKNOWN이다.
- 정적 프로토타입의 Attack Path는 CONFIRMED가 될 수 없다.
