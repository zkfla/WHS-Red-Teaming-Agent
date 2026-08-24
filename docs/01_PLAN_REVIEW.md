# 구현 전 계획 검토

상태: `REVIEW_REQUIRED`

두 개발자가 아래 항목을 검토한 뒤 상태를 `READY_FOR_DAY_1`로 변경해야 합니다.

## 프레임워크 계약

- [ ] A: hackingBuddyGPT 커밋과 Python 3.13 조건 확인
- [ ] B: framework 타입이 domain으로 새지 않는 구조 확인
- [ ] A/B: Agent, Capability, Limits, JSONL logger contract 실패 기준 승인
- [ ] A/B: contract 실패 시 adapter만 교체한다는 fallback 범위 승인

## 공개 모델과 Evidence

- [ ] A/B: `schema_version="1.0"`과 ID 형식 승인
- [ ] A/B: TRUE/FALSE/UNKNOWN 의미 승인
- [ ] A/B: Candidate/Supported/Rejected/Confirmed 상태 경계 승인
- [ ] A/B: Evidence 위치, excerpt, hash 생성 규칙 승인
- [ ] A/B: proposal이 참조할 수 있는 node와 edge 계약 승인

## GitHub Actions 의미론

- [ ] B: YAML 1.2 및 permission effective 규칙 승인
- [ ] A/B: pull_request와 pull_request_target attacker-control 규칙 승인
- [ ] A/B: local action/reusable workflow/script 추적 기준과 깊이 3 승인
- [ ] A/B: expression, matrix, runtime path를 UNKNOWN으로 남기는 기준 승인
- [ ] A/B: secret reference만으로 Finding을 만들지 않는 규칙 승인

## 안전과 출력

- [ ] B: path traversal, symlink, binary, size 제한 승인
- [ ] A: capability가 shell/network를 노출하지 않는지 확인
- [ ] A/B: redaction 전 데이터가 결과와 trace에 들어가지 않는지 확인
- [ ] A/B: COMPLETE/PARTIAL/FAILED와 exit code 승인

## 운영 결정

- [ ] 실제 LiteLLM provider/model 선택
- [ ] 비용을 발생시키는 smoke run 담당자와 최대 예산 지정
- [ ] 브랜치/PR/리뷰 방식 결정
- [ ] 일일 통합 시간과 merge 순서 결정

## 승인

- 개발자 A: `[ ] 승인` / 이름: / 날짜:
- 개발자 B: `[ ] 승인` / 이름: / 날짜:
- 공동 상태: `REVIEW_REQUIRED`
