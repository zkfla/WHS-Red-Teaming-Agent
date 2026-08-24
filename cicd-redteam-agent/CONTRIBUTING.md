# 공동 작업 규칙

## 개발 전 게이트

`docs/01_PLAN_REVIEW.md`의 A/B 검토와 `docs/03_DECISIONS.md`의 필수 결정이 끝나기 전에는
기능 코드를 작성하지 않습니다.

## 소유권

- 개발자 A는 application/framework/contract/e2e를 우선 소유합니다.
- 개발자 B는 domain/security/unit/fixtures를 우선 소유합니다.
- 공개 모델, Evidence 규칙, Capability I/O, CLI 계약은 공동 승인 없이는 변경하지 않습니다.

## 통합 순서

1. 작은 fixture와 failing test를 먼저 추가합니다.
2. 담당 계층 안에서 최소 구현을 작성합니다.
3. 담당 테스트를 통과시킵니다.
4. 계층 간 계약이 바뀌면 결정 문서와 상대 담당자 검토를 먼저 받습니다.
5. 매일 마지막에 fake LLM 통합 흐름을 확인합니다.

## 금지 사항

- 대상 저장소의 shell, workflow, script, action 실행
- 네트워크 또는 cloud API를 Capability로 노출
- secret 값 저장 및 trace 기록
- domain에서 hackingBuddyGPT import
- 외부 검증 없이 `Confirmed`, IAM trust 성공, 공격 성공으로 표현
