# dev-jm-native-specialists 브랜치 안내

기준 dev: `153265e32e029481628e8c057fe0ea476d939d64`. 작성일: 2026-10-10.
구형 `dev-jm`은 보존한다. 이 브랜치는 최신 dev 실행 기반으로 전문 기능을
선택 이식한 preview이며, 두 브랜치의 전체 병합이나 PRD 완료를 뜻하지 않는다.

## 구성과 실제 동작

사용자 목표 → 전략 플래너 → 작업 분해 → 기존 MissionTask/outbox → 기존
InstanceDriver → native MODEL/TOOLS/SETTLE → Control/MCP/증거/Graph → 조건 검토
→ 후속 Task와 새 실행 instance. 새 작업 큐나 대체 실행 엔진은 만들지 않았다.

역할은 [설정](../../config/agents/native-specialists.yaml)과 그 파일 기준의
version/hash 관리 MD로 조립한다. 역할 이름을 중앙 루프에서 비교하지 않는다.

| 논리 역할 | 책임 |
| --- | --- |
| 대화 | 사용자 입력과 Run 귀속, 승인 추정 금지 |
| 플래너 | 현재 목표·관측에 기반한 조사 질문과 전략 개정 |
| 작업 분해 | 질문·완료 조건·의존성을 기존 Task에 보존 |
| Web / Mobile 실행 | 자기 Task와 역할별 실제 도구를 native 루프로 선택·실행 |
| 연구·증거 | 원본·가설·현재 증거 유효성을 구분하고 완료 근거 검토 |
| 코드 작성 | 증거 참조 코드 artifact 작성, 다른 역할의 독립 검토 |

일곱 논리 역할이 항상 일곱 프로세스로 실행되는 것은 아니다. Web/Mobile만
대상 실행 역할로 등록하고 나머지는 전문 판단 stage로 연결한다. Control,
ROE, 실제 도구 준비 상태는 역할 등록과 별개다.

구현한 주요 계약:

- Task v2 완료 조건·영수증 hash, 실패·중복 원본의 완료 거절.
- 현재 관련 입력 CAS와 제안 멱등성, Ready 기동과 후속 조사.
- Run 공통 LLM 호출 원장, 명시적 총 요청 횟수 무제한과 유한 repair/429.
- provider metadata 기반 공유 자원, 미정산 실행 marker와 원래 요청 재개.
- 로컬 Run 실행 소유권, 승인/cohort 재개, 미진입 증명이 있는 zero-checkpoint 복구.
- `status --activation-db`의 읽기 전용 기동/호출 진단과, 명시적 Work/Evidence
  경로를 추가한 저장 원본 교차 진단. provider 조회·재실행·해제 허가는 아님.
- 증거 현재 유효성 소비자, 미실행 코드 artifact 작성·독립 검토·불변 저장.

## 설정과 비용 없는 확인

저장소 루트에서 명시적으로 선택한다. 상대 MD 경로는 프로필 디렉터리 기준이다.

```sh
export REDTEAM_SPECIALIST_PROFILE="$PWD/config/agents/native-specialists.yaml"
export REDTEAM_LLM_REQUEST_POLICY_JSON='{"mode":"bounded","max_requests":100,"max_concurrent_requests":2}'
```

모델 공급자·키·승인된 engagement·MCP 실행/정책 설정은 기존 실행 설정이 별도로
필요하다. 키를 Git에 저장하지 않는다. 위 두 값만으로 실환경 실행 준비가 끝나지 않는다.
MD/profile/정책이 달라진 Run은 새 구성으로 강제 재개하지 않는다.

`unlimited_requests`를 명시하고 `max_requests:null`로 설정하면 **LLM 총 요청 수만**
무제한이다. 무료를 뜻하지 않으며 동시성·repair·429·전략/기동 횟수·시간·Control
제한은 유지한다. 누락/0/-1은 무제한이 아니다.

```sh
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
./.venv/bin/pytest -q tests/integration/test_native_specialist_closed_loop.py tests/integration/test_specialist_resume.py tests/integration/test_zero_checkpoint_resume.py
```

이 시험은 scripted 모델과 합성 도구를 명시적으로 사용한다. 실제 금융권 성능이나
라이브 MCP 설치 완료를 증명하지 않는다. 전체 검증·모듈별 근거는
[구현 기록](dev-first-specialists-implementation-20261010.md)을 확인한다.

## 아직 완료하지 않은 작업

- entered인데 checkpoint가 없는 실행의 provider 확인·명시적 해제와 writer/orphan 복구.
  Work/저장 원본의 읽기 전용 진단은 구현했지만, 이것만으로 claim을 해제하지 않는다.
- 생성 코드의 합성 case 실행 및 실제 격리 검증. **생성된 코드는 현재 실행하지 않는다.**
- 증거 자동 만료 기동, 승인된 실제 Mobile/LLM 평가.
- 수정 전 dev에서도 재현한 Docker 격리 실패 3건의 원인·안전성 확인.
- 최종 전체 인계 및 운영 검증. 실제 대상 생성 코드 실행 규격은 미확정·비활성이다.

직전 로컬 검증은 unit/integration 3,739 passed / 76 skipped, 별도 localhost
5개 파일 26 passed다. 기존 실패가 있는 Docker 두 파일을 제외한 결과이며 전체
원본 시험 집합이 green이라고 주장하지 않는다. Ruff·변경 접점 mypy·diff 검사도
실시했다. 생략·미실행을 성공으로 계산하지 않는다.

기동/원본 교차 진단의 사용법과 조회 범위는 [복구 계약](../contracts/activation-recovery.md)의
5.2~5.3절을 확인한다. 진단 모드의 exit 0은 조회 성공이며 Run 완료/재개 허가가 아니다.
