# Blocked Action Evidence

## 1. Purpose

정책에 의해 실행되지 않은 Action도 최종 보고서에서 확인할 수 있도록
`DENY` 및 `REQUIRE_APPROVAL` 결정을 Evidence Store에 기록한다.

실행이 허용된 Action의 결과는 기존과 같이 Orchestration의 `verify_node`에서
Evidence로 기록한다. 이 기능은 기존 Core Contract를 변경하지 않고 Control 팀
내부 구현과 Bootstrap 배선으로 제공한다.

## 2. Scope

| `PolicyVerdict` | Tool 실행 | 이 메서드의 Evidence 기록 |
| --- | --- | --- |
| `ALLOW` | 가능 | 기록하지 않음 |
| `DENY` | 불가 | 기록 |
| `REQUIRE_APPROVAL` | 승인 전 실행 불가 | 기록 |

`DENY`와 `REQUIRE_APPROVAL` 시점에는 실행할 Tool이 선택되지 않았으므로
`ToolId`를 만들거나 기록하지 않는다. `invocation_id`도 `None`으로 기록한다.

## 3. Interface

구현 위치:

```text
src/redteam/evidence/service.py
```

팀에서 합의한 호출 인터페이스는 다음과 같다.

```python
await evidence_service.record_blocked_action(
    proposal,
    decision,
)
```

호출자가 전달하는 인자는 `proposal`, `decision` 두 개뿐이다.

`EvidenceService`의 실제 Python 클래스 선언에는 인스턴스 자신을 나타내는
`self`가 포함된다.

```python
async def record_blocked_action(
    self,
    proposal: ActionProposal,
    decision: PolicyDecision,
) -> EvidenceRecord:
    ...
```

`self`는 Python이 인스턴스 메서드를 호출할 때 자동으로 전달하므로 공개 호출
인터페이스의 인자에 포함되지 않는다. 이 메서드는 Evidence Store 등
`EvidenceService` 인스턴스의 상태를 사용하므로 `@staticmethod`로 정의하지 않는다.

`run_id`, `engagement_id`, `action_id`, `hypothesis_id`는
`ActionProposal`에서 가져온다. 별도의 ID 인자는 받지 않는다.

## 4. Invocation Rules

완성된 `PolicyDecision.verdict`가 다음 중 하나일 때 호출한다.

```text
DENY
REQUIRE_APPROVAL
```

Control Plane의 처리 순서는 다음과 같다.

```text
PolicyDecision 생성
    ↓
Audit 기록
    ↓
DENY 또는 REQUIRE_APPROVAL이면 Evidence 기록 시도
    ↓
PolicyDecision 반환
```

연결 위치는 `DefaultControlPlane.evaluate()` 내부이다.

- 일반 `DENY` 경로는 `_finalize()`에서 연결한다.
- `_finalize()`를 거치지 않는 `REQUIRE_APPROVAL` 경로에는 별도로 연결한다.
- `ALLOW` 경로에서는 호출하지 않는다.
- Control은 이 과정에서 Event를 발행하지 않는다.

## 5. Input Validation

`EvidenceService.record_blocked_action()`은 저장 전에 다음 조건을 검증한다.

### 5.1 Action ID 일치

```python
decision.action_id == proposal.action_id
```

두 값이 다르면 서로 다른 Action의 결정으로 간주하고 `ValueError`를 발생시킨다.

### 5.2 Verdict 확인

허용되는 verdict는 다음 두 가지뿐이다.

```python
PolicyVerdict.DENY
PolicyVerdict.REQUIRE_APPROVAL
```

`ALLOW` 또는 지원하지 않는 verdict가 전달되면 `ValueError`를 발생시킨다.
잘못된 입력을 무시하거나 Evidence를 생성해서는 안 된다.

## 6. Action Key

Action Key는 Action Naming Convention에 따라 다음 형식으로 생성한다.

```python
action_key = f"{proposal.capability}.{proposal.operation}"
```

예시:

```text
capability = "recon"
operation  = "port_scan"
action key = "recon.port_scan"
```

Action Key는 Agent의 의도를 나타내며 실행 수단인 `ToolId`와 다르다.

```text
Action Key: recon.port_scan
ToolId:     recon.nmap_scan
```

`EvidenceService`는 capability 또는 operation의 별칭을 변환하거나 정규화하지
않는다. `ActionProposal`에 담긴 canonical 값을 그대로 사용한다.

## 7. Evidence Mapping

차단된 Action은 다음 형식의 `EvidenceRecord`로 저장한다.

| `EvidenceRecord` 필드 | 값 |
| --- | --- |
| `run_id` | `proposal.run_id` |
| `engagement_id` | `proposal.engagement_id` |
| `source_type` | `EvidenceSourceType.SYSTEM` |
| `source_id` | `"control_plane"` |
| `action_id` | `proposal.action_id` |
| `invocation_id` | `None` |
| `hypothesis_id` | `proposal.hypothesis_id` |
| `content_type` | `"application/json"` |
| `artifact_ref` | `None` |
| `digest` | 민감정보 처리된 Evidence payload의 digest |
| `summary` | Action Key와 거절 또는 승인 요구 사유 |
| `created_at` | Evidence 생성 시각 |
| `metadata` | 아래 정의를 따름 |

`metadata`는 다음 형식을 사용한다.

```python
{
    "record_kind": "blocked_action",
    "action_key": action_key,
    "verdict": decision.verdict.value,
    "reasons": redacted_reasons,
    "matched_rules": decision.matched_rules,
    "approval_id": (
        str(decision.approval_id)
        if decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
        and decision.approval_id is not None
        else None
    ),
}
```

`approval_id`는 `REQUIRE_APPROVAL` 결정에서만 값이 존재할 수 있다.
`DENY`에서는 `None`으로 기록한다.

## 8. Summary Format

### DENY

```text
Blocked action <action_key>: <reason>
```

### REQUIRE_APPROVAL

```text
Approval required for action <action_key>: <reason>
```

`decision.reasons`가 비어 있으면 고정된 기본 문구를 사용한다. summary와
metadata에는 원본 `proposal.arguments`를 포함하지 않는다.

## 9. Sensitive Data Handling

`ActionProposal.arguments`와 정책 사유에는 credential, token, cookie 또는
개인정보가 포함될 수 있다. 다음 원칙을 적용한다.

- 원본 `proposal.arguments`를 summary 또는 metadata에 저장하지 않는다.
- digest 계산용 payload에 기존 EvidenceService의 민감정보 마스킹을 적용한다.
- `decision.reasons`가 summary 또는 metadata에 들어가기 전에 동일한 마스킹
  정책을 적용한다.
- `matched_rules`에는 정책 식별자만 저장하고 요청 인자 원문을 포함하지 않는다.
- 오류 로그에는 `action_id`, `action_key`, `verdict` 등 비민감 식별자만 남긴다.

정책 사유의 `Cookie:`·`Set-Cookie:`·`Authorization:` 헤더와 흔한
`key=value` 비밀값은 저장 전에 가린다. 이 처리는 임의의 자유 텍스트에 포함된
모든 개인정보를 판별할 수 없으므로, 정책을 만드는 쪽에서도 원본 credential이나
개인정보를 `reasons`와 `matched_rules`에 넣지 않아야 한다.

## 10. Error Handling Responsibility

### 10.1 EvidenceService

`EvidenceService.record_blocked_action()`은 다음 오류를 호출자에게 전달한다.

- 지원하지 않는 verdict
- Action ID 불일치
- `EvidenceRecord` 생성 실패
- Evidence Store 저장 실패

EvidenceService 내부에서 이러한 예외를 숨기지 않는다. 하위 계층은 저장 성공
여부를 호출자에게 정확히 알려야 하며, 실패를 무시할지는 호출자가 결정한다.

### 10.2 DefaultControlPlane

Evidence 기록은 이미 완료된 정책 판단 이후의 부가 기록 작업이다.
`DefaultControlPlane`은 Evidence 호출에서 발생한 `Exception`을 잡아 로그로
남기고, 이미 생성한 `PolicyDecision`을 그대로 반환한다.

```python
if (
    self._evidence_service is not None
    and decision.verdict
    in {PolicyVerdict.DENY, PolicyVerdict.REQUIRE_APPROVAL}
):
    try:
        await self._evidence_service.record_blocked_action(
            proposal,
            decision,
        )
    except Exception:
        logger.exception(
            "Failed to record blocked action evidence",
            extra={
                "action_id": str(proposal.action_id),
                "verdict": decision.verdict.value,
            },
        )

return decision
```

예외 처리는 Evidence 호출을 감싼 좁은 범위에서만 수행하며 `BaseException`은
잡지 않는다. Evidence 저장 실패가 다음 결과를 만들어서는 안 된다.

- `DENY` 또는 `REQUIRE_APPROVAL` verdict 변경
- 차단된 Action의 Tool 실행
- Control Plane의 `PolicyDecision` 반환 중단

## 11. Dependency Injection and Bootstrap

기존 생성 코드와 테스트의 호환성을 위해 `DefaultControlPlane`의
EvidenceService 의존성은 optional로 추가한다.

```python
evidence_service: EvidenceService | None = None
```

실제 애플리케이션 Bootstrap에서는 다음 원칙을 지킨다.

1. 하나의 `EvidenceStore` 객체를 생성한다.
2. 같은 Store로 `EvidenceService`를 생성한다.
3. `EvidenceService`를 `DefaultControlPlane`에 주입한다.
4. 동일한 Store를 Orchestration의 `RuntimeDeps.evidence_store`에도 전달한다.

Control에서 기록한 차단 Evidence와 Orchestration에서 기록한 실행 결과
Evidence는 동일한 저장소에 보관된다. `app/bootstrap.py`는 구현체를 조립하는
Composition Root로서 이 배선을 담당한다.

## 12. Ownership and Progress

Track A와 Track B의 담당 범위, 구현 순서 및 진행 상태는
[`PLAN.md`](./PLAN.md)를 기준으로 한다.

이 문서는 `record_blocked_action()`의 동작과 저장 규격만 정의한다. 담당자 또는
진행 상태가 변경되더라도 이 문서에 중복해서 기록하지 않는다.

## 13. Events and Core Contracts

이 기능을 위해 새로운 Event를 추가하지 않는다. Control은 기존 Orchestration과
중복되는 정책 또는 Evidence Event를 발행하지 않는다.

다음 공통 Contract도 변경하지 않는다.

```text
core/contracts/**
core/ports/**
core/ids.py
core/enums.py
```

현재 기능은 기존 `ActionProposal`, `PolicyDecision`, `EvidenceRecord` 및
`EvidenceStore`를 사용한다.

## 14. Required Tests

### 14.1 Track B 단위 테스트

- `DENY` 결정이 올바른 `EvidenceRecord`로 저장되는가
- `REQUIRE_APPROVAL` 결정과 `approval_id`가 저장되는가
- Action Key가 `<capability>.<operation>` 점 형식인가
- `invocation_id`가 `None`인가
- `ToolId`가 저장되지 않는가
- `ALLOW` 전달 시 `ValueError`가 발생하는가
- proposal과 decision의 Action ID가 다르면 `ValueError`가 발생하는가
- 민감한 arguments와 reasons 원문이 저장 파일에 남지 않는가
- Evidence Store 실패가 호출자에게 전달되는가
- 기존 `record_tool_result()` 동작에 영향이 없는가

### 14.2 Track A 연결 테스트

- EvidenceService가 없을 때 기존 Control Plane 동작이 유지되는가
- `DENY`와 `REQUIRE_APPROVAL`에서만 메서드가 호출되는가
- Evidence 기록 실패에도 원래 `PolicyDecision`이 반환되는가
- `ALLOW`에서는 메서드가 호출되지 않는가
- Bootstrap에서 Control과 Orchestration이 동일한 Evidence Store를 사용하는가

### 14.3 통합 확인

최종 리포트 누락 문제의 해결 여부를 확인하려면 다음 흐름을 검증한다.

```text
DENY 또는 REQUIRE_APPROVAL 발생
    ↓
blocked_action Evidence 생성
    ↓
Evidence Store에서 조회 가능
    ↓
Reporter 입력 또는 최종 리포트에 포함
```

Reporter가 Tool 실행 결과만 필터링한다면 별도 수정이 필요할 수 있다. 해당
수정은 Reporter의 현재 조회 방식 확인 후 Agent 팀과 협의한다.

## 15. Remaining TODO

동일한 Action이 checkpoint 복구 또는 정책 재평가로 반복 처리되면 차단
Evidence가 중복 생성될 수 있다. 중복 방지 또는 idempotency 정책은 이번 변경
범위에 포함하지 않는다.

```text
TODO: Define and implement blocked-action Evidence deduplication for replayed
or re-evaluated actions.
```

중복 방지 기준이 추가되기 전까지 호출될 때마다 새로운 `EvidenceRecord`를
생성한다.
