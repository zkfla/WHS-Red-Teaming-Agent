# 실행 프로필 선택과 공급자 429 처리 — 로컬 수정

## 범위

작업 checkout은 `WHS-Red-Teaming-Agent-dev-orchestration-p0-20261006`,
브랜치는 `dev-jm`, 기준 커밋은 `ec1b8d9`다. 기존 미커밋 P0/P1/P2/P3 작업을
보존한 로컬 수정이며 fetch/pull/commit/push/PR을 하지 않았다.
실제 LLM 요청·라이브 검색·외부 대상 스캔도 수행하지 않았다.

## 확인한 문제와 수정

1. 로컬 `data/p0-live-20261006/start.zsh`가 선택된 프로필과 관계없이
   `specialists.yaml`을 지정했다. 이 덮어쓰기를 제거하고 프로필 파일 존재 여부를
   확인한다. 기본 선택은 운영자용 `framework.env`에만 둔다. 현재 그 선택은
   `config/agents/specialists-p2.yaml`이며, 이미 설정한 환경변수가 우선한다.
   일반 App의 기본 프로필을 P2로 바꾸거나 파일명으로 최신 버전을 추측하지 않는다.
   시작 시 stderr로 파일 경로를, 대화형 CLI에서는 검증된 profile ID와 version을
   표시한다. 스크립트의 안내문이 `--json` 표준 출력에 섞이지 않게 한다.

2. 기존 공급자 실패 재시도는 대기하지 않았고, 429도 답변 수정 예산을 소비했다.
   `LLMProviderFailure`로 전달된 공급자 실패는 `transport_used`로 분류한다.
   실제 호출 횟수와 미확인 토큰 사용량은 환급하지 않는다. 정상 판단·수정 횟수는
   재분류하지만 공유 요청 총량, 위임 상한, 예약된 Run 풀은 늘리지 않는다.
   공급자 오류를 JSON 수정 지시로 넣지 않고, 기존 실제 검증 피드백은 유지한다.

3. LLM adapter가 같은 client를 사용하는 역할들의 cooldown을 공유한다.
   유효한 `Retry-After`는 최소 대기시간으로 보존하며 초 단위·HTTP 날짜를 지원한다.
   지연이 없으면 지수 backoff와 jitter를 적용한다. 긴 지연을 잘라 조기 재요청하지
   않고 설정된 총 대기에 맞지 않으면 요청을 보류한다. 대기 중 취소와 기존
   중단·시간·토큰 guard가 작동하며, 대기만으로 실제 요청 예산을 소비하지 않는다.
   재시도는 기존 StructuredDecisionClient 하나가 담당하고 SDK 재시도는 비활성이다.

4. 구조화된 공급자 코드/타입에 쿼터·지출 한도·크레딧 부족이 명시되면
   `quota`, `retryable=false`로 분류하고 같은 client의 후속 요청을 차단한다.
   본문 문구로 원인을 추측하거나 원문/인증 헤더를 보고서에 저장하지 않는다.
   429 숫자만 있는 기존 실제 실패 기록으로 쿼터 부족을 단정할 수는 없다.

5. 대기 상한/요청 총량에 도달한 공급자 실패는 `provider_unavailable`로 전달한다.
   worker 역시 비용·미해결 증거를 저장한 뒤 이 진단을 PLAN에 전달한다.
   다음 worker 턴이 재시도 한도를 새로 만들게 하지 않는다. 병렬 처리의 기존
   finally 정산은 이미 발생한 형제 요청 비용도 보존한다. Run은 성공이 아니라
   `finished` / 미해결 상태로 종료하며 기존 그래프와 조사 기록을 지우지 않는다.
   `run`의 대화 도우미는 새 LLM을 별도로 만들지 않고 해당 Run의 client를 사용한다.

## 책임 경계와 설정

- `core/ports/llm.py`: 안전한 실패, retry policy와 선택적 readiness port.
- `core/config.py`, `core/environment.py`, App bootstrap: 기존 설정 snapshot 검증·주입.
- `llm/litellm.py`, `llm/provider_backoff.py`: SDK 오류 정규화와 연결별 대기.
- Orchestration: 실제 요청 정산, 수정 예산 구분, 위임 기록·종료 전달.
- CLI: 선택된 프로필과 저장된 진단을 표시한다. 새로운 정책 판정을 하지 않는다.

기본값은 fallback 지연 1초, fallback 최대 30초, 한 structured decision의
총 cooldown 대기 최대 30초다. 기존 LLM 설정 경계에서 선택적으로 바꾼다.

```sh
export REDTEAM_LLM_RETRY_POLICY_JSON='{"base_delay_seconds":1,"max_delay_seconds":30,"max_wait_seconds":30}'
```

`max_wait_seconds=0`은 지연이 필요할 때 즉시 보류한다. `max_delay_seconds`는
fallback backoff 상한이며 서버가 준 최소 시간을 줄이는 값이 아니다.
음수·비유한 수·알 수 없는 설정 필드는 비밀값을 출력하지 않고 거절한다.
새 공통 operation, 별도 브로커/큐/DB, 모델·역할·Finance Lab별 예외를 추가하지 않았다.
Control 승인·허용 경로·MCP 등록·대상 인증을 변경하지 않았다.

## 실행과 제한

기존 실행 파일을 그대로 사용한다. 폴더 이름의 `p0`는 과거 실행 자료 위치이며
실제 실행 모드는 선택된 프로필 내용으로 결정된다.

```sh
zsh data/p0-live-20261006/start.zsh
```

이미 실행 중인 CLI에는 파일 변경이 자동 반영되지 않는다. 해당 CLI를 종료하고
위 실행 파일로 재시작해야 한다. 이 작업에서 사용자의 실행 중인 CLI를 종료하지 않았다.

현재 기본 선택은 P2 version 6이다. P3 Mobile은 연결 준비/합성 검증 단계이며,
실제 기기 정보나 실제 대상 코드 실행 규격을 대신 만들어 활성화하지 않는다.
프로필을 바꾼 경우 새 Run으로 확인한다. 종료된 Run을 자동 재시작하거나 기존
요청/예산을 초기화해 이어 실행하는 기능은 이 수정에 포함되지 않는다.

Cooldown은 **같은 프로세스의 같은 LLM client** 안에서 공유된다. 다른 client,
새 프로세스, 다른 사람의 프로그램과 계정 전체의 제한을 조정하는 분산 limiter가
아니다. 재시작 시 쿼터 차단/남은 대기시간이 별도 저장되지도 않는다.
이미 시작된 형제 요청은 취소된 것으로 간주하지 않는다.
429 처리 수정은 공급자 계정 접근 권한이나 크레딧을 복구하지 않는다.
실제 모델에서 429가 해소됐거나 P2 조사 목표를 달성했다고 판정하지 않는다.

## 검증

모델 응답·오류 fixture로 검증하며 실제 LLM 비용은 발생시키지 않았다.
관련 회귀 146개가 통과했다. 인증 실패의 기존 회귀도 실제 PLAN → REPLAN →
보고서 경로에서 공급자 진단·worker 상태·요청 비용 보존을 확인하도록 보강했고,
해당 위임/새 공급자 시험 35개가 통과했다. 새 시험은 다음을 포함한다.

- 429 뒤 정상 답변, 429 뒤 JSON 수정, 수정 도중 429와 원래 피드백 유지.
- 초/HTTP 날짜 대기, 비정상 헤더, 9,000초 지연의 조기 재요청 방지.
- 연결별 대기 공유, 오래된 형제 성공의 cooldown 삭제 방지.
- 총 대기 상한, 취소/guard, 실제 호출 총량과 알 수 없는 사용량 정산.
- 명시된 쿼터 실패의 단일 요청 후 차단, 원문/헤더 비노출.
- 직렬/병렬 worker 실패 전달·비용 정산·대상 실행 없음.
- 그래프와 실행 횟수 보존, terminal report 저장/로드의 미해결 판정.
- 기존 LLMConfig 로드와 새 설정 snapshot 왕복, 잘못된 설정 거절.

로컬 시작 스크립트는 `zsh -n`과 `--help`로 P2 기본값·명시적 P1 선택을
확인했다. 없는 프로필은 exit 2로 거절된다. 이 확인은 실제 LLM/MCP 실행 확인과 다르다.
최종 전체 회귀는 **3,126 passed / 61 skipped** (283.51초)다.
라이브 모델·외부 MCP·실기기·라이브 검색과 별도 Docker 실행 시험을 비활성화하고,
localhost 합성 MCP 통합 시험을 포함해 실행했다. skip을 실제 환경 통과로 보지 않는다.
Ruff, Mypy **613개 파일**, `git diff --check`도 통과했다.

대기 하한·재시도 상한·쿼터 오류 처리에는 OpenAI Docs skill로 확인한
[공식 rate limits 문서](https://developers.openai.com/api/docs/guides/rate-limits)와
[공식 error codes 문서](https://developers.openai.com/api/docs/guides/error-codes)를 사용했다.
