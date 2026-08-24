# CI/CD Red Team Agent

GitHub Actions 파일을 실행하지 않고 정적으로 분석해 Evidence, 공격 경로 후보,
Cloud Identity handoff를 생성하는 7일 프로토타입입니다. 분석 저장소의 텍스트는 모두
신뢰할 수 없는 입력으로 취급합니다.

## 설치

Python 3.13과 `uv`가 필요합니다.

```powershell
uv sync
```

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
  --model openai/gpt-5-mini `
  --attacker-profile fork-pr `
  --max-rounds 12 --max-tokens 100000 --max-cost 2.00 --max-duration 900
```

Provider API key는 `.env.example`을 참고해 환경 변수로만 설정합니다. 모델을 지정하지
않아도 파서, 공격자 제어 규칙, OIDC/AWS handoff까지 완료되며 LLM 검토만 생략됩니다.

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
- branch protection, environment approval, repository 기본 권한, IAM trust policy는
  로컬 Evidence가 없으면 `UNKNOWN`입니다.

## 개발 검증

```powershell
uv run pytest
uv run ruff check .
```

fixture 기반 E2E는 모델과 네트워크 없이 반복 실행됩니다. 실제 모델 smoke는 사용자가
명시한 LiteLLM provider와 자격 증명이 있을 때만 수행합니다.
