# 실행 통제: Control Plane

실행 모델에서 워커가 `ActionProposal`을 만든다고 했다. 그 제안이 실제 도구 호출로 바뀌기 전에 반드시 통과하는 관문이 이 장의 주제다.

에이전트의 모든 공격 행동은 실행 전에 Control Plane을 반드시 통과한다. 포트는 두 메서드로 요약된다.

```python
class ControlPlane(Protocol):
    async def evaluate(self, proposal: ActionProposal, context: ControlContext) -> PolicyDecision: ...
    async def approval_outcome(self, approval_id, current_proposal=None) -> PolicyDecision | None: ...
```

## 6단계 admission 파이프라인

순서대로 평가하다 거절되면 그 자리에서 멈춘다(early-exit).

```text
1 Scope → 2 ROE → 3 Policy → 4 RateLimit → 5 Budget → 6 Approval → 결과
  │        │        │          │             │
 거절     거절     거절        거절          거절   → 즉시 DENY (뒤 단계 실행 안 함)
```

| 단계 | 무엇을 보나, 통과 조건 |
| --- | --- |
| 1 Scope | 대상 host, IP, 도메인이 `allowed_*`에 있고 거절목록에 없어야 함. 허용목록이 없으면 기본 거절. IP는 CIDR, 도메인은 서픽스 매칭 |
| 2 ROE | 능력(recon, web, mobile, analysis)과 작업, 시각. `denied_capabilities`, `denied_operations`(canonical `<capability>.<operation>`), 허용 시간대 검사 |
| 3 Policy | `require_approval_for` 매칭으로 ALLOW 또는 REQUIRE_APPROVAL. 여기선 거절하지 않고 승인 게이트만 담당 |
| 4 RateLimit | 최근 60초 실행 횟수가 `max_actions_per_minute` 미만 (최근 60초 구간을 계속 밀며 세는 방식) |
| 5 Budget | `max_actions`, `max_tool_calls`, `max_duration_seconds`를 모두 넘지 않아야 함 |
| 6 Approval | 3이 승인 필요라 했을 때만. 승인 요청 생성, 승인ID 발급, 사람이 CLI로 승인하거나 거절 |

## 구조적 보장

ALLOW가 아닌 결정은 실행 명령으로 변환 자체가 불가능하다. `core.convert.build_tool_invocation`이 `verdict != ALLOW`면 `NonAllowDecisionError`를 던진다. 정책이 아니라 기계적 가드다. 거절된 요청은 물리적으로 도구에 도달할 수 없고, 이는 프롬프트가 아니라 타입과 코드, 정적 테스트(`test_control_plane_bypass.py`)로 강제된다.

## 두 번 심사하는 이유 (TOCTOU)

행동 하나는 통제를 두 번 통과한다. 제안 시점의 `evaluate`와 실행 직전의 `admit_invocation`이다. 왜 두 번인가. `evaluate`의 ALLOW는 proposal을 만든 그 시점의 판단이고, 그 결정이 곧바로 실행으로 이어지지 않을 수 있다. 특히 REQUIRE_APPROVAL이면 사람이 승인을 누를 때까지 기다리는데, 그 사이에 허용 시간대가 끝나거나 예산이 소진되거나 대상 상태가 바뀔 수 있다. 검사 시점과 사용 시점 사이의 이 간극(TOCTOU, time-of-check to time-of-use)을 메우려고, 실행 직전 `admit_invocation`이 현재 상태로 범위(scope), 시간(time), 대상(target), 외부 전송(outbound), 지속시간(duration)을 다시 본다. 따라서 ALLOW는 "지금 실행해도 된다"가 아니라 "지금 admission 심사를 받을 자격이 있다"는 뜻이고, 마지막 관문은 admission이다.

## 위험 등급 승인

승인은 단순 목록 매칭을 넘어 위험 등급으로도 게이트한다. 행동의 위험 등급은 **Capability Tool Registry의 `ToolDescriptor.risk_level`이 단일 진실 공급원**이다. Control은 드리프트를 막으려고 자체 위험 표를 두지 않으며, 적격 도구가 여럿이면 가장 높은 위험을 택한다(보수적). `ApprovalPolicy`는 `risk_threshold`(이 등급 이상이면 사람 승인, 기본값 HIGH), `auto_approve_up_to`(이 등급 이하만 자동 승인), 그리고 선택한 도구의 provider 선언 명령에 대해 동작하는 `command_approval_rules`를 둔다. 알 수 없는(해석 불가) 위험은 자동 승인되지 않으며, 명령 규칙이 발동하면 자동 승인 상한을 무시하고 사람을 요구한다. 레거시 `auto_approve`는 알려진 LOW 위험만 자동 승인한다.

## 심각도와 승인 재확인

DENY에는 에이전트의 후속 반응을 위한 꼬리표가 붙는다. `BLOCK_ACTION`은 다른 방법 시도, `REPLAN_GOAL`은 목표 전환, `ABORT_RUN`은 세션 종료를 뜻한다. 규격 변경 없이 `matched_rules`의 `"severity:<level>"` 태그로 전달된다. 구체적으로 `BLOCK_ACTION`이면 같은 가설 안에서 다른 Probe를 시도하고, `REPLAN_GOAL`이면 그 가설이나 경로를 접고 플래너가 재계획하며, `ABORT_RUN`이면 종료 사유를 남기고 Run을 끝낸다. 승인 대기 중에는 다른 워커 인스턴스가 자원을 쓸 수 있으나, 대기 중인 원래 실행의 식별자와 점유 자원은 유지된다.

`approval_outcome`은 멱등적이며 Scope, ROE, Policy를 재실행하지 않고 결과가 바뀔 수 있는 단계(RateLimit, Budget)만 재검한다. `current_proposal`이 주어지면 저장된 proposal과 도구, 인자, 대상이 일치해야 하고, 불일치면 사람이 보지 못한 ALLOW로 실행되지 않도록 stale DENY로 귀결된다. ALLOW는 `admit_invocation`과 함께 소비되며 scope, time, target, outbound, duration 게이트가 현재 proposal에 다시 적용된다. DENY와 REQUIRE_APPROVAL은 실행되지 않아 실행 Evidence가 없지만 시도와 사유는 구조화된 기록으로 남는다.
