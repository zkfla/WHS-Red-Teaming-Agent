# CI/CD Red Team Agent — 공동개발 Scaffold

이 폴더는 hackingBuddyGPT 기반 CI/CD Red Team Agent를 두 명이 바로 개발하기 위한
기반 구조입니다. 기능 구현본이 아니며 기존 구현 폴더
`../cicd-redteam-agent/`와 완전히 분리되어 있습니다.

## 현재 포함된 것

- Python 3.13 및 `uv` 프로젝트 설정
- hackingBuddyGPT 고정 커밋과 개발 의존성
- 계획에서 합의한 application/framework/domain/security 경계
- contract/unit/integration/e2e 및 fixture 디렉터리
- 계획 검토, 결정 기록, A/B 분업 문서

파서, Agent loop, Capability, validator, CLI, 보고서 생성 로직은 아직 없습니다.

## 공동개발 시작 순서

1. 두 개발자가 `docs/01_PLAN_REVIEW.md`를 검토합니다.
2. 열린 항목을 `docs/03_DECISIONS.md`에서 확정합니다.
3. `docs/02_DIVISION_OF_WORK.md`의 소유권과 merge 순서를 승인합니다.
4. 두 사람의 검토 표시가 끝나면 Day 1 contract test부터 개발합니다.

## 준비 명령

```powershell
uv sync
uv run python --version
```

아직 테스트 코드가 없으므로 scaffold 상태에서 `pytest` 성공 여부를 완료 기준으로
사용하지 않습니다. 첫 개발 작업은 `tests/contract/`에 hackingBuddyGPT API 계약 시험을
추가하는 것입니다.

## 경계

- 분석 대상 workflow, action, script를 실행하지 않습니다.
- 외부 action을 다운로드하거나 GitHub/AWS API에 접근하지 않습니다.
- domain 계층은 hackingBuddyGPT를 import하지 않습니다.
- framework 의존성은 `src/cicd_redteam_agent/framework/`에만 둡니다.
- 정적 분석 결과는 Attack Path를 `CONFIRMED`로 승격하지 않습니다.
