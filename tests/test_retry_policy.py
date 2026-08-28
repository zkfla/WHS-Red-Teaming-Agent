import httpx
import pytest

from whs_redteam.exceptions import (
    CapabilityPolicyViolation,
    RequestLimitExceeded,
    ScopeViolation,
)
from whs_redteam.orchestration.retry import ErrorDisposition, RetryPolicy


@pytest.mark.parametrize(
    "error",
    [TimeoutError("timeout"), ConnectionError("reset"), httpx.ConnectError("down")],
)
def test_transient_errors_receive_exponential_backoff(error: Exception) -> None:
    policy = RetryPolicy(base_delay_seconds=2, max_delay_seconds=10)
    first = policy.classify(error, attempt_count=1, max_attempts=5)
    third = policy.classify(error, attempt_count=3, max_attempts=5)
    fifth = policy.classify(error, attempt_count=5, max_attempts=5)

    assert first.disposition is ErrorDisposition.RETRY
    assert first.delay_seconds == 2
    assert third.delay_seconds == 8
    assert fifth.disposition is ErrorDisposition.TERMINAL


@pytest.mark.parametrize(
    "error",
    [
        ScopeViolation("outside"),
        CapabilityPolicyViolation("disabled"),
    ],
)
def test_policy_errors_are_rejected_without_retry(error: Exception) -> None:
    decision = RetryPolicy().classify(error, attempt_count=1, max_attempts=3)
    assert decision.disposition is ErrorDisposition.REJECT
    assert decision.delay_seconds == 0


def test_budget_exhaustion_and_unknown_errors_are_terminal() -> None:
    policy = RetryPolicy()
    budget = policy.classify(
        RequestLimitExceeded("exhausted"), attempt_count=1, max_attempts=3
    )
    unknown = policy.classify(RuntimeError("bug"), attempt_count=1, max_attempts=3)
    assert budget.disposition is ErrorDisposition.TERMINAL
    assert unknown.disposition is ErrorDisposition.TERMINAL


def test_retry_after_hint_is_clamped_to_maximum() -> None:
    decision = RetryPolicy(base_delay_seconds=2, max_delay_seconds=30).classify(
        TimeoutError("timeout"),
        attempt_count=1,
        max_attempts=3,
        retry_after_seconds=600,
    )
    assert decision.disposition is ErrorDisposition.RETRY
    assert decision.delay_seconds == 30
