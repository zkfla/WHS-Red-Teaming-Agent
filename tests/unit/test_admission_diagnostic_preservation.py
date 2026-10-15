"""Generic admission rejection keeps safe exception type, spent budget, and attached owner cause.

Drives the real _finalize_hypothesis_pass commit path with a fake transaction that raises.
"""

from types import SimpleNamespace

import pytest

from redteam.orchestration.cognition import DecisionBudget, DecisionFailure
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.hypothesis_planner import _finalize_hypothesis_pass

RAW = "RAW_MODEL_MARKER"


class _Tx:
    def __init__(self, error):
        self.error = error
        self.finished = []

    def check_planning_inputs(self, *args):
        raise self.error

    def check_planner_inventory(self, judgment):
        pass

    def finish_hypothesis_judgment(self, identity, judgment):
        self.finished.append(identity)


class _State:
    def __init__(self, error):
        self.tx = _Tx(error)

    async def transaction(self, commit):
        return commit(self.tx)


def _budget():
    budget = DecisionBudget.reasoning_chain(normal_limit=3, repair_limit=2, transport_limit=1)
    budget.consume(stage="hypothesis_planning")
    budget.consume(stage="hypothesis_planning", repair=True)
    budget.consume(stage="hypothesis_planning", transport=True)
    return budget


async def _finalize(error, budget):
    state = _State(error)
    deps = SimpleNamespace(central_state=state)
    snapshot = SimpleNamespace(mission=SimpleNamespace(revision=1), binding="binding-1")
    with pytest.raises(DecisionFailure) as failed:
        await _finalize_hypothesis_pass(
            deps, snapshot, budget, identity="planner_pass_test", proposal=None,
            judgment={}, graph_read={}, task_read={}, targets=set(), principals=set(),
            max_tasks=3,
        )
    return failed.value, state.tx


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [StateConflict(RAW), ValueError(RAW)])
async def test_generic_rejection_keeps_type_budget_and_no_commit(error):
    budget = _budget()
    failure, tx = await _finalize(error, budget)

    diagnostic = failure.diagnostic
    assert diagnostic.code == "hypothesis_admission_rejected"
    assert diagnostic.stage == "hypothesis_planning"
    assert diagnostic.retryable is False
    assert diagnostic.cause_scope == "unknown"
    assert diagnostic.error_type == type(error).__name__
    assert diagnostic.exception_chain == [type(error).__name__]
    assert diagnostic.attempts == budget.used == 3
    assert (diagnostic.normal_used, diagnostic.repair_used, diagnostic.transport_used) == (1, 1, 1)
    assert diagnostic.normal_limit == 3 and diagnostic.repair_limit == 2
    assert diagnostic.transport_limit == 1
    assert diagnostic.stage_attempts == {"hypothesis_planning": 3}
    assert RAW not in diagnostic.model_dump_json()
    assert RAW not in failure.detail
    assert tx.finished == []
    assert budget.used == 3


@pytest.mark.asyncio
async def test_exception_chain_keeps_type_names_only():
    try:
        try:
            raise KeyError(RAW)
        except KeyError as inner:
            raise ValueError(RAW) from inner
    except ValueError as exc:
        error = exc
    failure, _ = await _finalize(error, _budget())
    assert failure.diagnostic.exception_chain == ["ValueError", "KeyError"]
    assert RAW not in failure.diagnostic.model_dump_json()


@pytest.mark.asyncio
async def test_attached_owner_diagnostic_is_kept_and_not_retryable():
    error = StateConflict(RAW)
    error.diagnostic = DecisionDiagnostic(
        code="owner_evidence_not_current", stage="candidate_admission", kind="validation",
        owner="orchestration.central_state", cause_scope="evidence_validity",
        evidence_ids=["ev-1"], criterion_ids=["criterion-1"],
        repair_condition="Refresh the original evidence record.", summary="Evidence is stale.",
        location="$.updates", retryable=True,
    )
    budget = _budget()
    failure, tx = await _finalize(error, budget)

    diagnostic = failure.diagnostic
    assert diagnostic.code == "owner_evidence_not_current"
    assert diagnostic.owner == "orchestration.central_state"
    assert diagnostic.cause_scope == "evidence_validity"
    assert diagnostic.evidence_ids == ["ev-1"]
    assert diagnostic.criterion_ids == ["criterion-1"]
    assert diagnostic.repair_condition == "Refresh the original evidence record."
    assert diagnostic.summary == "Evidence is stale."
    assert diagnostic.location == "$.updates"
    assert diagnostic.stage == "hypothesis_planning"
    assert diagnostic.retryable is False
    assert diagnostic.error_type == "StateConflict"
    assert diagnostic.attempts == 3
    assert diagnostic.normal_used == 1 and diagnostic.repair_used == 1
    assert diagnostic.transport_used == 1
    assert tx.finished == []
