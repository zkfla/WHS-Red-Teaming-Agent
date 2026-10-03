"""``map_web_compare_sessions`` in isolation — the mapper that feeds the
object-bound authorization (IDOR) signal. It has strong anti-overclaim guards
but no direct unit coverage; these lock the ones that matter so a later refactor
cannot silently weaken them. Pure function: no graph, no orchestration state.

Guards under test (not every permutation — only the overclaim-prone ones):
- a comparison is recorded as ``observation_only``, never upgraded to a verdict;
- the tool's own ``same_status``/``same_body`` summary is cross-checked against
  the raw values, so a lying/mismatched summary is rejected, not trusted;
- a principal cannot be compared against itself;
- malformed/incomplete output yields no observation at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.core.contracts.target import EndpointTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.ids import ActionId, InvocationId, ToolId
from redteam.orchestration.observations.web_http import map_web_compare_sessions

NOW = datetime.now(timezone.utc)
TARGET = EndpointTarget(host="bank.local")
ARGS = {
    "target_ref": "bank",
    "path": "/orders/1234",
    "first_session_ref": "alice",
    "second_session_ref": "bob",
}


def _output(**overrides: Any) -> dict[str, Any]:
    """A well-formed two-principal comparison (alice 200 vs bob 403)."""
    output = {
        "target_ref": "bank",
        "first": {"principal_ref": "alice-principal", "status_code": 200, "body_sha256": "a" * 64},
        "second": {"principal_ref": "bob-principal", "status_code": 403, "body_sha256": "b" * 64},
        "same_status": False,
        "same_body": False,
        "verdict": "observation_only",
        "request_count": 2,
    }
    output.update(overrides)
    return output


def _result(output: Any) -> ToolResult:
    return ToolResult(
        invocation_id=InvocationId("inv_1"),
        action_id=ActionId("action_1"),
        tool_id=ToolId("web.http_compare_sessions"),
        success=True,
        output=output,
        started_at=NOW,
        finished_at=NOW,
    )


def _observation(mutation: Any) -> dict[str, Any] | None:
    for node in mutation.nodes:
        for item in node.observations:
            if item.get("kind") == "http_session_comparison":
                return item
    return None


def test_valid_comparison_records_an_observation_only_signal() -> None:
    mutation = map_web_compare_sessions(target=TARGET, arguments=ARGS, result=_result(_output()))

    observation = _observation(mutation)
    assert observation is not None
    assert observation["verdict"] == "observation_only"
    assert (observation["first_status"], observation["second_status"]) == (200, 403)
    # A status/body difference is a measurement, never a vulnerability verdict.
    assert "vulnerable" not in observation and "violation" not in observation


@pytest.mark.parametrize(
    "overrides",
    [
        {"same_status": True},  # claims equal status; raw is 200 vs 403
        {"same_body": True},  # claims equal body; raw digests differ
    ],
)
def test_rejects_a_summary_that_contradicts_the_raw_values(overrides: dict[str, Any]) -> None:
    # The tool's own same_status/same_body must match the raw status/digest, or
    # the comparison is untrustworthy and must not become a graph observation.
    mutation = map_web_compare_sessions(
        target=TARGET, arguments=ARGS, result=_result(_output(**overrides))
    )
    assert mutation.nodes == []


def test_rejects_a_principal_compared_against_itself() -> None:
    # Same principal on both sides proves nothing about cross-account access.
    same = _output(
        first={"principal_ref": "alice-principal", "status_code": 200, "body_sha256": "a" * 64},
        second={"principal_ref": "alice-principal", "status_code": 200, "body_sha256": "a" * 64},
        same_status=True,
        same_body=True,
    )
    mutation = map_web_compare_sessions(target=TARGET, arguments=ARGS, result=_result(same))
    assert mutation.nodes == []


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-dict",
        {},  # missing everything
        _output(second="nope"),  # one side not a dict
        _output(first={"principal_ref": "alice-principal", "status_code": 999,
                       "body_sha256": "a" * 64}),  # invalid HTTP status
        _output(verdict="vulnerable"),  # tool tried to assert a verdict
        _output(request_count=1),  # not actually two requests
    ],
)
def test_malformed_output_yields_no_observation(bad: Any) -> None:
    mutation = map_web_compare_sessions(target=TARGET, arguments=ARGS, result=_result(bad))
    assert mutation.nodes == []
