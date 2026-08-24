# 2인 분업과 통합 순서

## 기본 소유권

| 영역 | 개발자 A | 개발자 B | 공동 검토 |
|---|---|---|---|
| 프로젝트·프레임워크 contract | 주 담당 | 검토 | commit/fallback |
| application·CLI·report | 주 담당 | schema 검토 | 종료/출력 계약 |
| framework adapter·Agent·Capability | 주 담당 | domain I/O 검토 | tool schema |
| domain models·parser | contract 검토 | 주 담당 | schema freeze |
| relation·attacker control·identity | Agent 소비 검토 | 주 담당 | 상태 경계 |
| security·redaction | trace 검토 | 주 담당 | 민감정보 기준 |
| fake LLM E2E | 주 담당 | fixture 제공 | 통합 승인 |
| real-model smoke | 주 담당 | 결과 교차검토 | 비용 승인 |

## 7일 실행안

### Day 1 — 계약과 빈 수직 슬라이스

- A: install/import contract, CLI에서 빈 report까지
- B: Pydantic 모델과 enum, Evidence fixture
- 공동: 모델·ID·tool I/O·종료 상태 freeze
- merge 순서: B의 모델 계약 → A의 application/framework 수직 슬라이스

### Day 2 — Parser

- A: parser 결과 cache와 Capability 응답 계약
- B: YAML 1.2 parser와 line Evidence
- merge 순서: fixture → parser → cache adapter

### Day 3 — Relation과 Agent loop

- A: Single Agent와 5개 Capability, fake LLM
- B: workflow/job/step/file/credential 관계
- 공동: 첫 Attack Path node/edge 계약

### Day 4 — Attacker control과 참조 추적

- A: no-progress, 반복 호출, tool 오류 복구
- B: fork-pr/repo-writer와 깊이 3 추적
- 공동: TRUE/FALSE/UNKNOWN fixture 승인

### Day 5 — Secret/OIDC와 Cloud handoff

- A: AnalysisProposal과 원자적 submit validation
- B: token/secret/OIDC/AWS role과 identity candidate
- 공동: Candidate/Supported 경계 승인

### Day 6 — 종료·trace·강건성

- A: limits, 상태, exit code, partial report
- B: validator, dedup, redaction, injection fixture
- 공동: failure matrix 회귀 검토

### Day 7 — 통합과 freeze

- A: 승인된 provider로 real-model smoke
- B: 최종 fixture 회귀와 README
- 공동: framework import boundary, 과대 판정, v0.1.0 freeze

## 충돌을 줄이는 규칙

- A는 `domain/`을 직접 수정하지 않고 변경 제안을 B에게 전달합니다.
- B는 `framework/`을 직접 수정하지 않고 tool I/O 요구를 A에게 전달합니다.
- `models.py`, Capability schema, CLI 옵션은 두 명의 승인 후 merge합니다.
- 같은 날 양쪽 작업이 필요한 접점은 작은 contract PR을 먼저 merge합니다.
