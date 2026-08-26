"""Stop Condition 감지 로직 — unexpected_sensitive_data/latency_degradation 은
실제로 `policy_stop_reason()`을 트리거해야 하고(engagement §12, LLM finish
요청보다 항상 우선), external_side_effect 는 best-effort 참고 신호일 뿐
`policy_stop_reason()`을 절대 건드리지 않아야 한다(사용자 확정 설계 결정).
"""
from __future__ import annotations

from finredteam.agent.budget import Budget
from finredteam.agent.react import AgentContext, _handle_execute_experiment
from finredteam.engagement.compiler import compile_policy
from finredteam.engagement.schema import (
    Engagement,
    EngagementActions,
    EngagementScope,
    EngagementStopConditions,
    EngagementTarget,
)
from finredteam.models.core import Principal
from finredteam.models.finding import ExecutionResult
from finredteam.planning.actions import ExecuteExperimentInput
from finredteam.planning.candidates import CandidateSkeleton


class _FakeStore:
    db = None

    def append(self, *args, **kwargs) -> None:
        pass


def _policy(**stop_overrides):
    engagement = Engagement(
        engagement_id="t",
        target=EngagementTarget(name="test-target", base_url="http://localhost:5000"),
        scope=EngagementScope(allowed_hosts=["localhost"]),
        actions=EngagementActions(allow_state_change=True),
        stop_conditions=EngagementStopConditions(**stop_overrides),
    )
    return compile_policy(engagement)


def _make_ctx(policy) -> AgentContext:
    ctx = AgentContext(
        target=None,
        session=None,
        llm=None,
        store=_FakeStore(),
        budget=Budget(),
        policy=policy,
    )
    ctx.principals = [Principal(id="attacker_A", username="u", password="p", account_number="123")]
    return ctx


def _propose(ctx: AgentContext) -> None:
    skeleton = CandidateSkeleton(
        layer="finance", operation="transfer", principal_id="attacker_A", method="POST", path="/transfer"
    )
    ctx.pending_hypotheses["hyp_1"] = (skeleton, {"amount": "10", "to_account": "victim"})


def _stub_executor(monkeypatch, **result_overrides):
    def _fake_run(session, target, candidate, principal, watch_accounts, *, db=None):
        defaults = dict(status_codes=[200], latencies_ms=[], sensitive_levels=[], response_texts=[])
        defaults.update(result_overrides)
        return ExecutionResult(candidate=candidate, **defaults)

    monkeypatch.setattr("finredteam.agent.react.executor_module.run", _fake_run)


# ── unexpected_sensitive_data (기본값 True) ──────────────────────────


def test_unexpected_sensitive_data_triggers_on_critical_field(monkeypatch):
    policy = _policy()  # 기본값 True
    ctx = _make_ctx(policy)
    _propose(ctx)
    _stub_executor(monkeypatch, sensitive_levels=["CRITICAL"])

    _handle_execute_experiment(ExecuteExperimentInput(hypothesis_id="hyp_1"), ctx)

    assert ctx._critical_sensitive_hits == 1
    assert ctx.policy_stop_reason() == "policy_stop:unexpected_sensitive_data(>=1)"


def test_unexpected_sensitive_data_ignores_high_level(monkeypatch):
    # HIGH(balance/token/account_number 등)는 정상 인증 응답에도 흔해 오탐 유발 —
    # CRITICAL(password/cvv/card_number 등)만 잡아야 한다.
    policy = _policy()
    ctx = _make_ctx(policy)
    _propose(ctx)
    _stub_executor(monkeypatch, sensitive_levels=["HIGH"])

    _handle_execute_experiment(ExecuteExperimentInput(hypothesis_id="hyp_1"), ctx)

    assert ctx._critical_sensitive_hits == 0
    assert ctx.policy_stop_reason() is None


def test_unexpected_sensitive_data_can_be_disabled():
    policy = _policy(unexpected_sensitive_data=False)
    ctx = _make_ctx(policy)
    ctx._critical_sensitive_hits = 999
    assert ctx.policy_stop_reason() is None


# ── latency_degradation (기본값 False, opt-in) ───────────────────────


def test_latency_degradation_triggers_after_consecutive_degraded_requests(monkeypatch):
    policy = _policy(
        latency_degradation=True, latency_degradation_multiplier=3.0, latency_degradation_consecutive_threshold=2
    )
    ctx = _make_ctx(policy)
    _propose(ctx)
    # 처음 3개는 baseline 표본(평균 50ms), 이후 2개가 baseline*3(=150ms)을 연속 초과.
    _stub_executor(monkeypatch, latencies_ms=[50, 50, 50, 500, 500])

    _handle_execute_experiment(ExecuteExperimentInput(hypothesis_id="hyp_1"), ctx)

    assert ctx._consecutive_latency_degraded == 2
    assert ctx.policy_stop_reason() == "policy_stop:latency_degradation(>=2x3.0baseline)"


def test_latency_degradation_ignores_single_spike(monkeypatch):
    policy = _policy(latency_degradation=True, latency_degradation_multiplier=3.0)
    ctx = _make_ctx(policy)
    _propose(ctx)
    # baseline 3개(50ms) 이후 스파이크 1개(500ms) → 정상(50ms)으로 바로 복귀.
    _stub_executor(monkeypatch, latencies_ms=[50, 50, 50, 500, 50])

    _handle_execute_experiment(ExecuteExperimentInput(hypothesis_id="hyp_1"), ctx)

    assert ctx._consecutive_latency_degraded == 0
    assert ctx.policy_stop_reason() is None


def test_latency_degradation_disabled_by_default():
    policy = _policy()  # 기본값 False
    ctx = _make_ctx(policy)
    ctx._consecutive_latency_degraded = 999
    assert ctx.policy_stop_reason() is None


# ── external_side_effect: best-effort 참고 신호일 뿐, Hard Stop 이 아니다 ──


def test_external_side_effect_keyword_appends_advisory_note_but_never_stops(monkeypatch):
    policy = _policy()
    ctx = _make_ctx(policy)
    _propose(ctx)
    _stub_executor(monkeypatch, response_texts=["알림이 발송되었습니다: user@example.com"])

    observation = _handle_execute_experiment(ExecuteExperimentInput(hypothesis_id="hyp_1"), ctx)

    assert ctx._external_side_effect_hits == 1
    assert "[참고]" in observation
    assert ctx.policy_stop_reason() is None  # 어떤 정책값을 줘도 이 신호만으로는 절대 멈추지 않음


def test_response_without_keyword_has_no_advisory_note(monkeypatch):
    policy = _policy()
    ctx = _make_ctx(policy)
    _propose(ctx)
    _stub_executor(monkeypatch, response_texts=["{\"status\": \"ok\"}"])

    observation = _handle_execute_experiment(ExecuteExperimentInput(hypothesis_id="hyp_1"), ctx)

    assert ctx._external_side_effect_hits == 0
    assert "[참고]" not in observation


# ── 우선순위 회귀 가드 ────────────────────────────────────────────────


def test_repeated_5xx_still_checked_before_new_conditions(monkeypatch):
    policy = _policy()
    ctx = _make_ctx(policy)
    ctx._consecutive_5xx = policy.stop_repeated_5xx_threshold
    ctx._critical_sensitive_hits = 999
    assert ctx.policy_stop_reason() == f"policy_stop:repeated_5xx(>={policy.stop_repeated_5xx_threshold})"
