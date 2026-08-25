# Rules of Engagement

- Engagement ID: `vulnbank`
- 생성: 2026-08-25T16:36:52.528587+00:00
- 승인: 2026-08-25T16:36:52.537873+00:00

## Target
- 어댑터 설정: `targets/configs/vulnbank.yaml`
- http://localhost:5000

## In Scope (allowed_hosts)
- localhost
- 127.0.0.1

## Out of Scope (denied_hosts)
- (없음)

## Allowed Activities
- 정상 사용자 기능 실행/인증
- 상태 변경 요청(POST/PUT/PATCH)
- 테스트 계정 생성

## Prohibited Activities
- DELETE / 데이터 삭제
- DoS·가용성 영향 시험
- brute-force / password spraying
- race-condition/burst 시험
- 제3자·외부 서비스 접근
- 실제 사용자 데이터 접근

## Rate Limits
- Maximum requests per second: 20.0
- Maximum concurrency: 10
- Maximum amount per request: (전역 상한만 적용)

## Cleanup
종료 후 별도 정리를 요구하지 않는다.

## Stop Conditions
- 반복적인 HTTP 5xx (5회 연속)
- 요청/토큰 예산 초과

## Strategic Guidance (soft — 코드가 강제하지 않음)
- 최소한의 요청으로 검증하라.
- 개인정보/카드 원문(PAN) 노출 가능성을 우선적으로 확인하라.
- 로컬 1회성 Docker 컨테이너이므로 정리(cleanup)는 요구되지 않는다.
