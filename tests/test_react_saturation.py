"""실측 버그 회귀 가드: recon(discover_surface/observe_graph)만 반복하다가
막 propose_hypothesis 를 하나 등록한 스텝조차 "무성장"으로 잡혀 saturated 로
조기 종료되는 문제(§ agent/react.py::run() 의 edge_count 기반 성장 판정,
§ agent/strategy.py::suggest_nudge).

실제 라이브 실행에서 재현됨: recon 5스텝 + propose_hypothesis 1스텝 = 6스텝
연속 "무성장"으로 판정되어 execute_experiment 를 한 번도 못 해보고 종료됨
(runs/20260826-084240, round=6, termination_reason=saturated).
"""
from __future__ import annotations

from finredteam.agent.budget import Budget
from finredteam.agent.react import AgentContext
from finredteam.agent.strategy import suggest_nudge
from finredteam.engagement.compiler import compile_policy
from finredteam.engagement.schema import Engagement, EngagementTarget
from finredteam.knowledge.baseline import BaselineRecord
from finredteam.models.core import Verdict
from finredteam.models.finding import CandidateFinding, ExecutionResult, Finding
from finredteam.models.knowledge import Entitlement


def _ctx() -> AgentContext:
    engagement = Engagement(
        engagement_id="t", target=EngagementTarget(name="test-target", base_url="http://localhost:5000")
    )
    return AgentContext(
        target=None, session=None, llm=None, store=None, budget=Budget(), policy=compile_policy(engagement)
    )


def test_ready_for_hypothesis_is_false_before_bootstrap_entitlements():
    ctx = _ctx()
    assert ctx.ready_for_hypothesis_but_none_proposed() is False


def test_ready_for_hypothesis_is_true_once_bootstrap_grants_entitlement():
    # BOOTSTRAP 이 끝나면 항상 이 상태다(entitled_own(자기 계좌) 최소 1개) —
    # discover_surface/observe_graph 만 반복할 이유가 없다는 신호.
    ctx = _ctx()
    ctx.graph.add_entitlements(
        [Entitlement(principal="attacker_A", relation="entitled_own", obj="account:123", source="self_created")]
    )
    assert ctx.ready_for_hypothesis_but_none_proposed() is True


def test_ready_for_hypothesis_turns_false_once_a_hypothesis_is_proposed():
    ctx = _ctx()
    ctx.graph.add_entitlements(
        [Entitlement(principal="attacker_A", relation="entitled_own", obj="account:123", source="self_created")]
    )
    ctx._hypothesis_counter = 1  # propose_hypothesis 가 최소 1번 성공함
    assert ctx.ready_for_hypothesis_but_none_proposed() is False


def test_nudge_fires_even_at_round_1_when_ready_for_hypothesis():
    # BOOTSTRAP 직후 계좌는 이미 있는데 round<=1 가드 때문에 힌트가 늦게
    # 나오면 그만큼 recon 만 도는 스텝이 늘어난다 — round 1부터도 나와야 한다.
    ctx = _ctx()
    ctx.round = 1
    ctx.graph.add_entitlements(
        [Entitlement(principal="attacker_A", relation="entitled_own", obj="account:123", source="self_created")]
    )
    nudge = suggest_nudge(ctx)
    assert nudge is not None
    assert "propose_hypothesis" in nudge


def test_nudge_is_silent_once_first_hypothesis_registered():
    ctx = _ctx()
    ctx.round = 1
    ctx.graph.add_entitlements(
        [Entitlement(principal="attacker_A", relation="entitled_own", obj="account:123", source="self_created")]
    )
    ctx._hypothesis_counter = 1
    assert suggest_nudge(ctx) is None


# ── 실측 버그 회귀 가드: baseline까지 준비된 operation을 계속 안 써보는 문제 ──
# 실제 라이브 실행에서 재현됨(runs/20260826-085120): BOOTSTRAP 이 transfer
# baseline 을 미리 학습해두는데도, read_balance 만 계속 VIOLATION 이 나오니
# 33스텝 내내 transfer 는 한 번도 propose_hypothesis 로 시도되지 않고 saturated
# 로 종료됨.


def _episode_and_finding(operation: str, verdict: Verdict) -> tuple[CandidateFinding, Finding]:
    candidate = CandidateFinding(
        id=f"c-{operation}", layer="finance", operation=operation, principal_id="attacker_A", method="GET", path="/x"
    )
    result = ExecutionResult(candidate=candidate)
    finding = Finding(candidate=candidate, result=result, verdict=verdict)
    return candidate, finding


def _stub_baseline_record(op: str) -> BaselineRecord:
    return BaselineRecord(ref_id=f"bl_{op}_01", op=op, path="/x", principal_id="attacker_A", deltas={})


def test_baseline_learned_but_never_tried_is_empty_when_no_baseline_learned():
    ctx = _ctx()
    assert ctx.baseline_learned_but_never_tried() == []


def test_baseline_learned_but_never_tried_flags_untried_operation_with_baseline():
    ctx = _ctx()
    ctx.baselines.register(_stub_baseline_record("transfer"))
    assert ctx.baseline_learned_but_never_tried() == ["transfer"]


def test_baseline_learned_but_never_tried_clears_once_operation_is_attempted():
    ctx = _ctx()
    ctx.baselines.register(_stub_baseline_record("transfer"))
    ctx.episodic.record(*_episode_and_finding("transfer", Verdict.PASS))
    assert ctx.baseline_learned_but_never_tried() == []


def test_nudge_prefers_untried_baseline_over_repeating_a_winning_operation():
    # read_balance 로 이미 VIOLATION 을 여러 번 찾았어도(=recent_violations() 도
    # True) — baseline까지 준비된 미시도 operation(transfer) 힌트가 우선해야 한다.
    ctx = _ctx()
    ctx.round = 5
    ctx._hypothesis_counter = 1  # 첫 가설은 이미 낸 상태
    ctx.graph.add_entitlements(
        [Entitlement(principal="attacker_A", relation="entitled_own", obj="account:123", source="self_created")]
    )
    ctx._prev_edge_count = ctx.graph.edge_count()  # graph_grew_significantly() 를 끔
    ctx.baselines.register(_stub_baseline_record("transfer"))
    candidate, finding = _episode_and_finding("read_balance", Verdict.VIOLATION)
    ctx.episodic.record(candidate, finding)
    ctx.findings.append(finding)

    nudge = suggest_nudge(ctx)

    assert nudge is not None
    assert "transfer" in nudge
    assert "baseline" in nudge
