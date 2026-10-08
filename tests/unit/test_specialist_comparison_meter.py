"""Live-evaluation request limits use fake transport here, not billed inference."""

import asyncio
import json

import pytest

from examples.specialist_live_comparison import MeteredModel, prior_usage
from redteam.core.ports.llm import StructuredCompletion


class Client:
    def __init__(self):
        self.calls = 0

    async def complete_structured_with_usage(self, **kwargs):
        self.calls += 1
        await asyncio.sleep(0)
        return StructuredCompletion({"answer": "synthetic"}, total_tokens=None)


async def test_parallel_callers_reserve_before_sending_and_unknown_usage_is_not_zero():
    client = Client()
    meter = MeteredModel(client, 2)
    result = await asyncio.gather(*[meter.complete_structured_with_usage() for _ in range(5)],
                                  return_exceptions=True)
    assert client.calls == meter.requests == 2
    assert sum(isinstance(r, RuntimeError) for r in result) == 3
    assert all(r["tokens"] is None for r in meter.calls)


async def test_trial_boundary_cannot_reset_global_allowance():
    client = Client()
    meter = MeteredModel(client, 1)
    await meter.complete_structured_with_usage()
    meter.trial_start = meter.requests
    with pytest.raises(RuntimeError, match="limit_reached"):
        await meter.complete_structured_with_usage()
    assert client.calls == 1


async def test_failure_consumes_reserved_call_without_logging_error_body():
    class BrokenClient:
        async def complete_structured_with_usage(self, **kwargs):
            raise OSError("private-provider-body")

    meter = MeteredModel(BrokenClient(), 1)
    with pytest.raises(OSError):
        await meter.complete_structured_with_usage()
    assert meter.requests == 1 and meter.calls[0]["outcome"] == "OSError"
    assert "private" not in str(meter.calls)


def test_previous_evaluation_usage_carries_forward_and_cannot_exceed_approval(tmp_path):
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({"requests_used": 43}))
    assert prior_usage(previous, 57) == 43
    with pytest.raises(ValueError, match="cumulative_request_limit"):
        prior_usage(previous, 58)
    previous.write_text(json.dumps({"requests_used": 10, "cumulative_requests_used": 90}))
    assert prior_usage(previous, 10) == 90
    with pytest.raises(ValueError, match="cumulative_request_limit"):
        prior_usage(previous, 11)


@pytest.mark.parametrize("value", [None, True, -1, 101, "43"])
def test_unknown_or_invalid_previous_usage_never_becomes_a_fresh_allowance(tmp_path, value):
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({"requests_used": value}))
    with pytest.raises(ValueError, match="prior_usage_unproven"):
        prior_usage(previous, 1)
