# 팀원 기능 이식 1차 — 2026-10-16

원본 변경 `92ec7f6f5b96902aed8f9b0b847fd4033f090af5`의 기능을 선택적으로 이식했다. 사용자 지시에 따라 팀원 프롬프트와 낮은 프로필 버전은 가져오지 않았다. 현재 한국어 프롬프트 보강은 별도 커밋 `e7dd2e01`이다. 이 기록은 후속 `a020b09c` 이식·실제 라이브 검증·원격 ancestry 병합·push의 완료 선언이 아니다.

## 반영과 보존

- 기존 instance checkpoint에 WorkerSession을 저장하고 첫 표시·증분 도구 결과·현재 상태 변경을 이어간다. prepare 단계에서 표시를 먼저 내구화하며, 원래 task purpose/prompt/tool/provider binding을 재개 때 검사한다.
- 고정 planner system/schema와 요청별 최신 사용량·판단 예산 DATA를 분리한다. 기존 v1/v2 배치 조회와 유한한 rejected-proposal replay를 함께 유지했다. replay 본문은 durable trace에 쓰지 않는다.
- cache read/write는 input의 부분집합으로 표시하며 총 토큰에 중복 합산하지 않는다. 안전한 숫자 토큰 계수는 credential-key 마스킹에 의해 누락되지 않게 했다. 요청 DATA 구성 실패는 provider 호출 전이므로 잠정 local attempt를 반환하고 실행 요청 trace를 남기지 않는다.
- descriptor의 logical_host/argument binding, 승인된 원래 actor 이어가기, 원본 요청 단위 session/account 증명, 물리 실행 상한 이후 유한한 로컬 해석·정산을 보존했다. specialist planning input은 기존 schema3/4와 대화/request 계약 버전을 결속한다.

## 리뷰 중 수정

Sol6.1High 독립 리뷰가 실행 전 거절의 진단 전달 누락과 crash recovery에서 `execution_started=false`를 무시하는 P2 두 건을 확인했다. 원본 진단 전달은 기존 `native_result_delivery` 소유자에서 범위·마스킹·1000자 구간으로 처리했다. 복구는 검증된 원본에서 실행 여부와 안전한 code/message/retryable을 얻는다. 원본 기록은 바꾸지 않는다.

새 crash 회귀는 원본 결과 commit 뒤 표시 checkpoint 전 중단을 재현하고, 재개 시 executed=false와 같은 거절 진단이 전달됨을 확인한다. 거절된 원래 호출은 다시 제출되지 않으며, 이후 수정된 새 호출만 한 번 실제 supervisor double에 전달된다. 이는 명시적 오프라인 double 검사다.

## 검사

- Planner prefix/회계/semantic replay/배치 조회 집중 검사61개 통과.
- Worker conversation/session/승인 이어가기/schema 관련47개 통과.
- 거절·재개/진단 전달/히스토리 안정성 집중20개 통과. 위47개와 일부 겹친다.
- Root 원본 단위 증명·완료·native 전달 안정성·요청 진단·중앙 복구60개 통과.
- 추가 실행/CLI 흐름은 처음29개 통과·3개 실패였다. 원인별로 local submission과 result 구분, 한국어 파일 UTF8 명시, canonical1000자 preview 기대값을 고친 뒤 관련7개 전부 통과했다. 이미 통과한 사례와 중복되므로 총합으로 더하지 않는다.
- Root 전체 CI 경로 Ruff 통과. Linux target mypy는 현재 목표 입력 변경을 포함한899개 파일에서 통과했다. untyped 함수 전체를 검사한 의미는 아니다. Native Windows target의 POSIX API attribute 오류63개는 별도로 기록했으며 전체 Windows typecheck 통과로 표현하지 않는다.
- 수정 파일과 staged diff 검사 통과. 실제 provider/대상 호출은 이 이식 과정에서 하지 않았다.

기존 원본·범위·승인·예산·후보 개정/목적 결속·최종 완료 게이트를 완화해서 테스트를 통과시키지 않았다. 프롬프트 효과 및 처리량 개선은 후속 새 라이브에서 별도로 확인한다.
