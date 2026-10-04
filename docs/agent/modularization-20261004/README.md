# 모듈화 계획·작업 기록 — 2026-10-04

사용자 요청으로 로컬에 보관하던 계획과 작업 기록을 저장소 공유 문서로 게시한다. 제품 구현은 [PR #30](https://github.com/zkfla/WHS-Red-Teaming-Agent/pull/30)에서 `dev`의 `b49281b5`로 병합했다. 이 문서 추가는 제품 소스 변경을 포함하지 않는다.

먼저 [최종 구현계획과 실행 검증 기록](2026-10-04-WHS-modularization-implementation-plan.md)을 읽는다. §6은 기존 문제와 구현의 대응, §11~13은 리뷰 지적의 처리, §14는 마지막 검사와 병합 결과, §15는 지원 경계와 남은 한계다.

## 최종 결과

- 기존 규격의 도구·MCP·역할·해석·참고 자료·추론 제공자를 명시적으로 등록·선택하고 앱에서 조립하도록 연결했다. 새 정책·대상·완료 의미 자체는 공통 계약 구현이 필요하다.
- 직접 추가한 라이브러리는 Pluggy와 pydantic-settings다. 기존 실행·통신·저장 SDK를 재사용했다.
- [최종 PR CI](https://github.com/zkfla/WHS-Red-Teaming-Agent/actions/runs/37161315456)와 [병합 후 dev CI](https://github.com/zkfla/WHS-Red-Teaming-Agent/actions/runs/37161638825)가 모두 통과했다. Python 3.10/3.14 각각 2,730 passed, 51 skipped이며 전체 Ruff, mypy 544파일, 두 Compose 설정, MCP 컨테이너와 금융 모의환경도 통과했다.
- 실행 검사의 모델 응답은 결정된 대역이다. 실제 모델의 판단 품질, 실기기·APK 및 준비되지 않은 외부 제품 동작을 검증한 결과는 아니다.

## 계획과 배경 감사

| 문서 | 용도 |
|---|---|
| [최종 모듈화 구현계획](2026-10-04-WHS-modularization-implementation-plan.md) | 구현 결정·지적 처리·검증·병합·지원 경계 |
| [모듈 간 결합성 감사](2026-10-04-WHS-modularity-coupling-audit.md) | 구현 전 기준 커밋의 문제 30개와 실제 변경 영향 |
| [검증 계약 감사](2026-10-03-WHS-validation-contract-audit.md) | 이전 세션의 검증 계약 분석 자료 |
| [기존 폐쇄 루프 계획](2026-10-03-WHS-closed-loop-implementation-plan.md) | 이전 계획과 이번 모듈화 결정의 배경 자료 |

## 조사·구현 기록

아래 문서는 각 작성 시점의 원래 판단과 중간 상태를 보존한다. 중간 리뷰의 미해결·보류 표현을 현재 결함 판정으로 읽지 않는다. 최신 상태는 최종 계획과 최종 독립 리뷰에서 확인한다. 검토 당시 행 번호는 현재 소스의 행 번호와 다를 수 있으며, 이동된 링크에 현재 행을 추정해 붙이지 않았다. 저장소 밖 사용자 제공 자료는 파일명으로 출처를 남겼다.

| 영역 | 기록 |
|---|---|
| 확장 조사 | [오케스트레이터 통합](modularization-runtime/extension-orchestrator-findings.md), [도구·MCP 워커](modularization-runtime/worker-a-capability-mcp-findings.md), [역할·참고 자료 워커](modularization-runtime/worker-b-roles-methodology-findings.md) |
| 상태·통제 조사 | [오케스트레이터 통합](modularization-runtime/state-control-orchestrator-findings.md) |
| 도구·MCP 구현 | [역량 구현 보고](modularization-runtime/impl-a-capability-report.md), [모바일 벤더 전환](modularization-runtime/impl-c-mobile-vendor-report.md), [프로필 리뷰 보완](modularization-runtime/impl-d-capability-review2-report.md) |
| 전문 역할 구현 | [역할·해석·참고 구현](modularization-runtime/impl-b-agents-report.md), [공개 연결 계약](modularization-runtime/impl-b-core-api-notes.md) |
| 구현 통합 | [오케스트레이터 최종 요약](modularization-runtime/impl-orchestrator-final-summary.md), [루트 컴파일 경계](modularization-runtime/impl-root-compiler.md) |
| 상태·통제 구현 | [구현 노트](modularization-runtime/state-control-impl-A-note.md), [조정 구성 수명](modularization-runtime/state-control-coordinator-lifetime-note.md), [최종 경계 노트](modularization-runtime/state-control-boundary-final-note.md) |
| 재개·저장 보완 | [I07](modularization-runtime/impl-review-2-I07-note.md), [I08](modularization-runtime/impl-review-2-I08-note.md), [I17](modularization-runtime/impl-review-2-I17-note.md) |

## 독립 검토 기록

| 단계 | 기록 |
|---|---|
| 준비 조사 | [평가자 준비 메모](modularization-runtime/evaluator-initial-notes.md) |
| 계획 검토 | [1차](modularization-runtime/evaluator-plan-review-1.md), [2차](modularization-runtime/evaluator-plan-review-2.md) |
| 구현 검토 | [1차](modularization-runtime/evaluator-implementation-review-1.md), [2차](modularization-runtime/evaluator-implementation-review-2.md), [최종 수락 및 후속 보완 확인](modularization-runtime/evaluator-implementation-review-final.md) |

이 검토 기록의 소스 판정과 실제 실행 검사 결과는 구분한다. 실행 검증 결과는 최종 계획 §14와 연결된 GitHub CI에서 확인한다.

로컬 `no-git` 원본, 터미널·실행 로그, 캐시와 임시 비교 자료는 그대로 Git 제외 상태로 유지한다. 여기 게시한 27개 계획·감사·작업 문서와 이 목차는 Git으로 공유한다.
