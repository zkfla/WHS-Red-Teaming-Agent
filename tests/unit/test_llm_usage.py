"""Provider usage survives format repair and remains local to each request."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from redteam.llm.litellm import LiteLLMClient
from redteam.orchestration.cognition import DecisionBudget, StructuredDecisionClient

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def client(monkeypatch, responses):
    async def completion(**kwargs):
        assert kwargs["num_retries"] == 0
        text, tokens = responses.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(total_tokens=tokens) if tokens is not None else None,
        )

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            get_llm_provider=lambda model: (model, "openai", None, None),
            acompletion=completion,
        ),
    )
    return LiteLLMClient("unit-model")


@pytest.mark.asyncio
async def test_bad_json_repair_retains_reported_tokens(monkeypatch):
    provider = client(monkeypatch, [("not json", 11), ('{"answer":"done"}', 13)])
    budget = DecisionBudget(3)

    async def guard():
        assert not budget.token_halt_required(30)

    result = await StructuredDecisionClient(provider).ask(
        stage="usage-repair",
        payload={},
        response_schema=SCHEMA,
        budget=budget,
        before_call=guard,
    )
    assert result == {"answer": "done"}
    assert budget.used == 2
    assert budget.tokens_used == 24
    assert budget.tokens_fully_known


@pytest.mark.asyncio
async def test_missing_usage_does_not_reuse_previous_request_cost(monkeypatch):
    provider = client(monkeypatch, [('{"answer":"one"}', 7), ('{"answer":"two"}', None)])
    decider = StructuredDecisionClient(provider)
    first, second = DecisionBudget(1), DecisionBudget(1)
    for budget in (first, second):
        await decider.ask(stage="usage-local", payload={}, response_schema=SCHEMA, budget=budget)
    assert first.tokens_used == 7 and first.tokens_fully_known
    assert second.unknown_usage_count == 1
    assert second.token_halt_required(20)


@pytest.mark.parametrize("expire_before_retry", [False, True])
async def test_analysis_requests_share_actual_delegation_cost_and_fresh_time_guard(
    expire_before_retry,
):
    import dataclasses
    from datetime import datetime, timezone

    from redteam.core.contracts.graph import Hypothesis
    from redteam.core.enums import EvidenceSourceType, Verdict
    from redteam.core.ports.llm import StructuredCompletion
    from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier
    from redteam.orchestration.delegation import DelegationRequest
    from redteam.orchestration.delegation_decision import open_delegation
    from redteam.orchestration.nodes.verify import verify_node
    from redteam.orchestration.state import RuntimeState, initial_graph_state
    from tests.integration import _engine_llm_harness as h
    from tests.unit import test_semantic_completion_receipts as receipts

    store, _, service, original_deps = receipts._harness(receipts._success_llm())
    delegated = open_delegation(
        DelegationRequest(
            path_step_id=receipts.STEP_ID,
            agent_id=str(receipts.AGENT),
            objective="review the actual response",
        ),
        path_id=receipts.PATH_ID,
        path_version=1,
        decision_limit=5,
    )
    runtime = RuntimeState(
        run_id=receipts.RUN,
        engagement_id=receipts.ENGAGEMENT,
        delegations={delegated.delegation_id: delegated},
        started_at=datetime.now(timezone.utc),
    )

    class Reviewer:
        calls = 0

        async def complete_structured_with_usage(self, **kwargs):
            self.calls += 1
            assert kwargs["num_retries"] == 0
            if self.calls == 1:
                if expire_before_retry:
                    runtime.started_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
                return StructuredCompletion(value="invalid json", total_tokens=5)
            return StructuredCompletion(value=receipts._success_llm(), total_tokens=7)

    reviewer = Reviewer()
    engagement = h.make_engagement(receipts.HOST, engagement_id=str(receipts.ENGAGEMENT))
    engagement = engagement.model_copy(
        update={
            "execution_budget": engagement.execution_budget.model_copy(
                update={"max_duration_seconds": 300}
            )
        }
    )
    deps = dataclasses.replace(
        original_deps,
        engagement=engagement,
        verifier=StructuredLLMAnalysisVerifier(reviewer, store, service),
    )
    proposal, result = receipts._proposal(), receipts._result()
    await service.record_hypothesis(
        Hypothesis(
            hypothesis_id=str(proposal.hypothesis_id),
            description="target returns 200",
        )
    )
    state = initial_graph_state(runtime)
    state.update(
        action_proposal=proposal,
        tool_result=result,
        plan_step=receipts._plan_step(violation=False).model_copy(
            update={"delegation_id": delegated.delegation_id}
        ),
    )
    output = await verify_node(state, deps=deps)
    spent = output["runtime"].delegations[delegated.delegation_id]
    assert reviewer.calls == (1 if expire_before_retry else 2)
    assert spent.decision_attempts_used == reviewer.calls
    assert spent.tokens_used == (5 if expire_before_retry else 12)
    assert output["runtime"].last_verdict is (
        Verdict.UNKNOWN if expire_before_retry else Verdict.PASS
    )
    originals = [
        record for record in store.records if record.source_type is EvidenceSourceType.TOOL
    ]
    assert len(originals) == 1
    assert originals[0].metadata["tool_result"]["output"] == result.output
