"""정책 강제 지점 테스트.

1. `CompiledPolicy.check_request` — 네트워크 경계(`probing/session.py`)가
   실제로 부르는 함수. 허용 호스트 통과/미등록 호스트 차단/금지된 메서드 차단.
2. `agent/react.py::_handle_execute_experiment` — LLM이 금지된 것(state_change
   금지 상태에서 POST)을 골랐다고 가정해도, 실제 네트워크 실행 함수
   (`probing/executor.py::run`)까지 도달하지 않는지 — 이게 "LLM이 우회할 수
   없다"는 주장의 핵심 증거다(engagement §16 "LLM bypass" 테스트).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from finredteam.agent.budget import Budget
from finredteam.agent.react import AgentContext, _handle_execute_experiment
from finredteam.engagement.compiler import compile_policy
from finredteam.engagement.errors import PolicyViolation
from finredteam.engagement.schema import Engagement, EngagementActions, EngagementScope, EngagementTarget
from finredteam.planning.actions import ExecuteExperimentInput
from finredteam.planning.candidates import CandidateSkeleton


def _policy(**action_overrides):
    engagement = Engagement(
        engagement_id="t",
        target=EngagementTarget(name="test-target", base_url="http://localhost:5000"),
        scope=EngagementScope(allowed_hosts=["localhost", "127.0.0.1"]),
        actions=EngagementActions(**action_overrides),
    )
    return compile_policy(engagement)


# ── CompiledPolicy.check_request (네트워크 경계에서 실제로 도는 검사) ──


def test_check_request_passes_for_allowed_host_and_method():
    policy = _policy(allow_state_change=True)
    policy.check_request("POST", "http://localhost:5000/transfer")  # 예외 없어야 함


def test_check_request_blocks_unregistered_host():
    policy = _policy(allow_state_change=True)
    with pytest.raises(PolicyViolation) as exc_info:
        policy.check_request("GET", "http://evil.example.com/steal")
    assert exc_info.value.rule == "OUT_OF_SCOPE_HOST"


def test_check_request_blocks_denied_host_even_if_scheme_matches_allowed():
    engagement = Engagement(
        engagement_id="t",
        target=EngagementTarget(name="test-target", base_url="http://localhost:5000"),
        scope=EngagementScope(allowed_hosts=["localhost"], denied_hosts=["localhost"]),
    )
    policy = compile_policy(engagement)
    with pytest.raises(PolicyViolation) as exc_info:
        policy.check_request("GET", "http://localhost:5000/x")
    assert exc_info.value.rule == "OUT_OF_SCOPE_HOST"


def test_check_request_blocks_forbidden_state_change_method():
    policy = _policy(allow_state_change=False)
    with pytest.raises(PolicyViolation) as exc_info:
        policy.check_request("POST", "http://localhost:5000/transfer")
    assert exc_info.value.rule == "STATE_CHANGE_FORBIDDEN"


def test_check_amount_blocks_over_ceiling():
    policy = _policy(allow_state_change=True)
    with pytest.raises(PolicyViolation) as exc_info:
        policy.check_amount(Decimal("999999"))
    assert exc_info.value.rule == "AMOUNT_EXCEEDS_CEILING"


def test_check_amount_allows_within_ceiling():
    policy = _policy(allow_state_change=True)
    policy.check_amount(Decimal("10"))  # 예외 없어야 함


# ── react.py 핸들러 레벨: LLM이 금지된 걸 골라도 실제 실행 함수까지 도달 못함 ──


def _make_ctx(policy) -> AgentContext:
    return AgentContext(
        target=None,  # 이 테스트 경로에선 절대 참조되지 않아야 한다 — 참조되면 즉시 크래시로 드러남
        session=None,
        llm=None,
        store=None,
        budget=Budget(),
        policy=policy,
    )


def test_execute_experiment_blocked_by_state_change_policy_never_calls_executor(monkeypatch):
    policy = _policy(allow_state_change=False)
    ctx = _make_ctx(policy)

    skeleton = CandidateSkeleton(
        layer="finance", operation="transfer", principal_id="attacker_A", method="POST", path="/transfer"
    )
    ctx.pending_hypotheses["hyp_1"] = (skeleton, {"amount": "10", "to_account": "victim"})

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("executor.run() 이 호출됨 — 정책 위반 요청이 네트워크까지 도달함")

    monkeypatch.setattr("finredteam.agent.react.executor_module.run", _fail_if_called)

    observation = _handle_execute_experiment(ExecuteExperimentInput(hypothesis_id="hyp_1"), ctx)

    assert "정책 위반으로 차단됨" in observation
    assert "STATE_CHANGE_FORBIDDEN" in observation


def test_execute_experiment_blocked_by_amount_ceiling_never_calls_executor(monkeypatch):
    policy = _policy(allow_state_change=True)
    ctx = _make_ctx(policy)

    skeleton = CandidateSkeleton(
        layer="finance", operation="transfer", principal_id="attacker_A", method="POST", path="/transfer"
    )
    ctx.pending_hypotheses["hyp_1"] = (skeleton, {"amount": "999999", "to_account": "victim"})

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("executor.run() 이 호출됨 — 금액 상한을 넘는 요청이 네트워크까지 도달함")

    monkeypatch.setattr("finredteam.agent.react.executor_module.run", _fail_if_called)

    observation = _handle_execute_experiment(ExecuteExperimentInput(hypothesis_id="hyp_1"), ctx)

    assert "정책 위반으로 차단됨" in observation
    assert "AMOUNT_EXCEEDS_CEILING" in observation
