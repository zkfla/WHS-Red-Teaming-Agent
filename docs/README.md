# 문서 허브

이 디렉터리는 CI/CD Red Team Agent의 설계, 계약, 테스트, 계획 문서를 모아둔 곳입니다.
프로젝트 사용법은 루트 [README](../README.md)에서 시작하세요.

## 추천 읽기 순서

1. [README](../README.md) — 프로젝트 개요와 빠른 실행
2. [Implementation status](IMPLEMENTATION_STATUS.md) — 완료·부분·미구현 범위와 데모 절차
3. [Architecture](ARCHITECTURE.md) — 분석 파이프라인과 책임 경계
4. [Public contracts](CONTRACTS.md) — CLI, 출력, validator, 종료 계약
5. [Testing](TESTING.md) — 테스트 범위와 release check

## 현재 기준 문서

| 문서 | 역할 | 주요 독자 |
|---|---|---|
| [Implementation status](IMPLEMENTATION_STATUS.md) | 현재 범위와 완료 기준의 단일 기준 | 전체 팀 |
| [Architecture](ARCHITECTURE.md) | 시스템 구조와 데이터 흐름의 기준 | 개발자, 리뷰어 |
| [Public contracts](CONTRACTS.md) | 외부 동작과 검증 규칙의 기준 | 개발자, 연동 담당자 |
| [Testing](TESTING.md) | 필수 테스트와 release check | 개발자, QA |
| [Plan coverage](PLAN_COVERAGE.md) | 구현 및 검증 완료 범위 | 프로젝트 관리자, 리뷰어 |

## 계획 및 설계 이력

| 문서 | 상태 | 용도 |
|---|---|---|
| [Implementation plan](../PLANS.md) | 완료된 v0.1.0 계획 | 단계별 목표와 완료 조건 추적 |
| [Expansion plan](CI_CD_RED_TEAM_AGENT_EXPANSION_PLAN.md) | 설계 참고 | 범용 Fact/Relation 기반 확장 방향 검토 |

계획 문서는 현재 동작을 정의하는 계약이 아닙니다. 내용이 충돌하면
`CONTRACTS.md`와 실제 테스트를 우선합니다.

## 평가 fixture 문서

| 문서 | 역할 |
|---|---|
| [Scenario coverage](../tests/fixtures/SCENARIOS.md) | GitHub Actions 시나리오와 탐지 경계 |
| [Reasoning matrix](../tests/fixtures/reasoning-matrix/README.md) | 평가 variant와 oracle 격리 원칙 |

fixture 문서는 테스트 데이터 가까이에 두어 제품 문서와 평가용 정답의 경계를 유지합니다.

## 문서 유지 원칙

- README에는 프로젝트 소개와 가장 짧은 실행 경로만 둡니다.
- 현재 범위는 `IMPLEMENTATION_STATUS.md`, 세부 동작은 `ARCHITECTURE.md`, `CONTRACTS.md`,
  `TESTING.md`에 기록합니다.
- 구현 현황은 `PLAN_COVERAGE.md`, 미래 방향은 별도 계획 문서에 기록합니다.
- 같은 내용을 여러 문서에 복사하지 않고 기준 문서로 링크합니다.
- CLI나 출력 계약을 변경하면 코드, 계약 문서, 테스트를 함께 갱신합니다.
