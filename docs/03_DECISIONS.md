# 결정 기록

## 확정된 결정

| ID | 결정 | 근거 |
|---|---|---|
| D-001 | hackingBuddyGPT 커밋 `b2653771...` 고정 | 프레임워크 API 변동 격리 |
| D-002 | Python 3.13, uv.lock 사용 | 프레임워크 요구와 재현성 |
| D-003 | Single Agent | 7일 MVP 범위 통제 |
| D-004 | domain에서 framework/LLM import 금지 | adapter 교체 가능성 보존 |
| D-005 | 대상 저장소는 read-only, shell/network capability 금지 | 안전한 정적 분석 |
| D-006 | 외부 설정과 IAM trust는 Evidence가 없으면 UNKNOWN | 과대 확정 방지 |
| D-007 | 정적 Attack Path의 CONFIRMED 금지 | exploit을 수행하지 않는 범위 반영 |

## 구현 전 확정할 항목

| ID | 질문 | 선택/결정 | 담당 | 기한 |
|---|---|---|---|---|
| O-001 | Evidence ID canonical input과 길이는? | 미정 | A/B | Day 1 |
| O-002 | Pydantic 공개 필드와 alias는? | 미정 | A/B | Day 1 |
| O-003 | job permission 미지정 표현은? | 미정 | B 제안/A 승인 | Day 1 |
| O-004 | no-progress 및 중복 호출 임계값은? | 미정 | A 제안/B 검토 | Day 3 |
| O-005 | `pull_request_target` checkout 판정표는? | 미정 | B 제안/A 검토 | Day 4 |
| O-006 | redaction 탐지 항목과 hash 원문 기준은? | 미정 | B 제안/A 검토 | Day 5 |
| O-007 | 실제 smoke provider/model/예산은? | 미정 | A/B | Day 6 이전 |

새 결정을 추가할 때는 ID, 결정, 근거, 영향받는 schema/test를 함께 기록합니다.
