# CI/CD Red Team Agent 확장 계획

> **문서 상태: 설계 참고 자료**
>
> 현재 구현 구조와 계약은 [Architecture](ARCHITECTURE.md),
> [Public contracts](CONTRACTS.md)를 기준으로 합니다. 전체 문서 안내는
> [문서 허브](README.md)를 참고하세요.

> **중요 — 구현 입력으로 사용 금지**
>
> **구현 에이전트는 구현 작업 시 이 문서를 읽거나 입력으로 사용하지 않는다.**
> 제품의 구현 로직, 탐지 규칙, LLM 프롬프트, 런타임 분석 입력으로 직접 참조하기 위한 문서가 아니다.
> 구현 시 이 문서를 파싱하거나 하드코딩 규칙의 근거 데이터로 사용하지 않는다.
> 이 문서는 개발 방향과 평가 목표를 공유하기 위한 설계 참고 문서이며, 실제 구현은 저장소에서 추출되는 범용 `Fact / Relation / Guard / Evidence / Unknown` 구조를 기준으로 작성한다.

## 목표

기존처럼 시나리오마다 취약점 규칙을 추가하지 않는다.

```text
기존
조건/정규식
→ Finding 생성
→ AttackPath 생성
→ LLM이 결과 확인
```

다음 구조로 바꾼다.

```text
저장소
→ Fact / Relation / Guard 추출
→ LLM이 공격 경로 추론
→ Validator 검증
```

즉 **코드는 사실을 정리하고, LLM은 사실을 연결한다.**

---

## 1. 시나리오 활용

`GHA-01 ~ GHA-10`은 제품에 하드코딩할 탐지 규칙이 아니라 **개발·평가용 시나리오**로 사용한다.

각 시나리오를 보고 다음을 정리한다.

```text
이 공격을 판단하려면
어떤 정보가 필요한가?
```

예:

```text
PR 입력 → env → eval
```

필요한 정보:

- PR 입력이 공격자 제어인지
- 어떤 env로 전달되는지
- env가 어디서 사용되는지
- `eval/bash -c` 같은 실행 sink가 있는지
- 해당 job의 실제 권한이 무엇인지

---

## 2. 공통 정보 추출 계층

시나리오별로 반복되는 정보를 공통 Fact로 만든다.

주요 항목:

- 공격자 제어 입력·파일
- 값 전달 흐름
- local action / script / reusable workflow 호출
- Artifact / Cache producer-consumer
- 실행 sink
- GitHub Token / Secret / OIDC 권한
- Cloud Identity
- Guard 및 validation
- UNKNOWN 정보

정규식도 **취약점을 바로 판단하는 용도**가 아니라 이런 Fact를 추출하는 용도로 사용한다.

---

## 3. Relation 생성

추출된 사실 사이 관계를 만든다.

예:

```text
PR title
→ ENV_X

ENV_X
→ step B

step B
→ script.py

script.py
→ artifact

artifact
→ privileged workflow
```

LLM은 이 Relation들을 조합해 전체 Attack Path를 만든다.

---

## 4. LLM 역할

LLM에는 기존 Finding이나 AttackPath 정답을 보여주지 않는다.

다음 정보만 제공한다.

```text
Fact
Relation
Guard
Evidence
Unknown
```

LLM은 이를 이용해:

- 공격 경로 조합
- 부족한 정보 판단
- Guard가 경로를 막는지 판단
- `SUPPORTED / CANDIDATE / REJECTED` 제안

을 수행한다.

---

## 5. Validator 강화

LLM이 존재하지 않는 관계를 만들어내지 못하게 한다.

각 Relation에 `relation_id`를 부여하고, LLM은 실제 Relation만 조합하도록 한다.

Validator는 다음을 검사한다.

- Relation이 실제 존재하는가
- Evidence가 존재하는가
- source/target 순서가 맞는가
- UNKNOWN이 있는데 SUPPORTED로 단정하지 않았는가

---

## 6. 구현 우선순위

### 1차

- GHA-01B/C
- GHA-02C/D
- GHA-03B/C
- GHA-03E 또는 GHA-10E
- GHA-05B/F/G

목표:

- env / output 값 전달
- local action / reusable workflow 추적
- Artifact 추적
- 권한 계산
- UNKNOWN 처리
- Guard 처리

### 2차

- Cloud Identity
- Secret 유출
- 공급망 → Credential 연결
