# 한 번의 점검 따라가기

앞뒤 장에서 계약을 하나씩 설명하지만, 그것들이 실제로 어떻게 맞물리는지는 흐름으로 봐야 이해된다. 이 장은 가상의 점검 한 번을 처음부터 끝까지 추적한다. 등장하는 객체 이름은 모두 실제 계약과 같다.

## 설정

- **대상**: 결제 API `https://pay.example.test` (Engagement의 `allowed_hosts`에 등록됨)
- **목표**: 접근 통제(권한) 결함 점검
- **예산**: `ExecutionBudget(max_actions=20)`, `RateLimitPolicy(max_actions_per_minute=10)`
- **승인 정책**: `ApprovalPolicy(require_approval_for=[], risk_threshold=HIGH)`

## 1. 가설 수립 (Planner)

Planner가 목표와 현재 관측으로 확인할 후보를 세운다.

```python
Hypothesis(
    description="이체 API가 호출자의 자원 소유권을 검사하지 않을 수 있다 (IDOR)",
    status="open",
)
```

공격 그래프에 `hypothesis` 노드로 기록되고 `HypothesisCreated` 이벤트가 발행된다. 아직 아무것도 실행하지 않았다.

## 2. 검증 작업 분해 (Atomizer)

가설은 바로 실행할 수 없다. 원자화 역할이 가설을 검증 가능한 Probe로 쪼갠다.

```python
MissionTask(
    objective="사용자 A 세션으로 사용자 B 소유 이체 건을 조회 시도",
    completion_criteria=[
        CompletionCriterion(kind="hypothesis_confirmed", hypothesis_id=...),
    ],
)
```

`TaskStatus`가 `PENDING`에서 `READY`로 바뀌고 워커 인스턴스에 할당된다.

## 3. 행동 제안 (Worker, MODEL 단계)

web 워커 인스턴스가 MODEL 단계에서 다음 행동을 판단하고 `ActionProposal`을 만든다. 워커는 도구를 직접 부르지 않는다.

```python
ActionProposal(
    capability="web",
    operation="send_http_request",
    binding_mode="legacy_action",
    target=UrlTarget(url="https://pay.example.test/api/transfers/8842"),
    arguments={"method": "GET", "headers": {"Authorization": "<세션 A>"}},
    reason="사용자 A 세션으로 사용자 B 소유 이체 8842 조회",
    hypothesis_id=...,
    availability_impact=False,   # 읽기 요청이므로 가용성 영향 없음
)
```

## 4. 통제 심사 (Control Plane, TOOLS 단계)

`evaluate(proposal, context)`가 6단계를 순서대로 돈다.

```text
Scope      pay.example.test ∈ allowed_hosts          ✓
ROE        web.send_http_request 금지 아님, 시간대 내  ✓
Policy     require_approval_for 비어 있음 → 승인 불필요  ✓ (ALLOW)
RateLimit  최근 60초 10회 미만                        ✓
Budget     max_actions 20회 미만                      ✓
→ PolicyDecision(verdict=ALLOW)
```

ALLOW이므로 `build_tool_invocation(proposal, decision)`이 `ToolInvocation`을 만든다. 만약 DENY였다면 이 함수가 `NonAllowDecisionError`를 던져 여기서 끝난다. 거절된 행동은 도구에 도달할 수 없다.

## 5. 실행 직전 재검 (admission)

승인 대기가 없었더라도, `admit_invocation`이 현재 상태로 범위, 시간, 대상, 외부 전송, 지속시간을 **다시** 본다. 평가와 실행 사이에 시간대가 끝났을 수도 있기 때문이다. 통과하면 실행으로 넘어간다.

## 6. 도구 실행과 증거 (SETTLE 단계)

```python
ToolResult(
    success=True,
    output={"status": 200, "body": {"transfer_id": 8842, "owner": "userB", ...}},
)
```

원본 결과는 증거로 저장된다.

```python
EvidenceRecord(
    source_type="tool",
    invocation_id=...,
    hypothesis_id=...,
    digest="sha256:...",
    artifact_ref="...",   # 큰 본문은 참조로
)
```

`ToolExecutionFinished`와 `EvidenceCreated` 이벤트가 흐르고, SETTLE에서 상태를 정리해 계속할지 결정한다.

## 7. 해석과 Finding (research-evidence)

여기서부터가 핵심이다. 도구가 성공했다고 취약점이 확정되는 것이 아니다.

- **도구 실행 성공**: 호출이 되어 200과 본문을 돌려받았다.
- **검증 작업 결과**: 사용자 A 세션으로 사용자 B의 자원이 반환되었다. 가설이 `confirmed`.
- **취약점 판단**: 관측이 가설을 뒷받침하므로 Finding을 만든다.

```python
Finding(
    title="이체 API IDOR (소유권 미검사)",
    severity="high",
    verified=True,
    evidence_ids=[위 EvidenceRecord.evidence_id],
)
```

`FindingCreated`, `FindingVerified` 이벤트가 발행되고, 공격 그래프에 `finding` 노드와 `supported_by` 엣지가 추가된다.

## 8. 만약 거절되었다면

대상이 범위 밖(`allowed_hosts`에 없음)이었다면 1단계 Scope에서 멈춘다.

```python
PolicyDecision(verdict=DENY, matched_rules=["severity:block_action"])
```

`severity`가 `BLOCK_ACTION`이므로 에이전트는 이 행동만 버리고 같은 가설 안에서 다른 Probe를 시도한다. 시도와 사유는 구조화된 기록으로 남지만, 실행되지 않았으므로 실행 Evidence는 없다. 만약 능력 자체가 금지(`REPLAN_GOAL`)였다면 가설을 접고 재계획했을 것이다.

## 이 예시가 보여주는 것

- 도구 실행 성공과 취약점 확정은 다른 단계다(6단계와 7단계). Evidence가 뒷받침해야 Finding이 `verified`가 된다.
- 통제는 두 번 일어난다. 제안 시점의 `evaluate`(4단계)와 실행 직전의 `admission`(5단계).
- 거절도 레드팀 결과의 일부다. 무엇이 왜 막혔는지가 증거로 남는다.
