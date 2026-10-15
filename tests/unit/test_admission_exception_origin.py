"""Admission rejection carries one bounded origin row from redteam traceback frames.

The real _finalize_hypothesis_pass commit path raises through test-owned (foreign) frames. The
cause frame is compiled with a redteam module name so that it counts as trusted. Only the
validated module, code name and line of trusted frames are checked; no message is.
"""

import sqlite3
from types import SimpleNamespace

import pytest

from redteam.orchestration.cognition import DecisionBudget, DecisionFailure
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.diagnostics import (
    DecisionDiagnostic,
    exception_chain_for,
    exception_origins_for,
)
from redteam.orchestration.hypothesis_planner import (
    _admission_rejection_diagnostic,
    _finalize_hypothesis_pass,
)
from redteam.sqlite_support import run_transaction

RAW = "RAW_MODEL_MARKER"
PLANNER = "redteam.orchestration.hypothesis_planner"
FIXTURE = "redteam.orchestration.origin_fixture"


def _trusted_boom():
    source = f"def boom():\n    raise StateConflict('{RAW}')\n"
    namespace = {"__name__": FIXTURE, "StateConflict": StateConflict}
    exec(compile(source, "<origin-fixture>", "exec"), namespace)
    return namespace["boom"]


def _caught_cause(boom):
    try:
        boom()
    except StateConflict as caught:
        return caught
    raise AssertionError("fixture did not raise")


class _Tx:
    def __init__(self, error, cause=None):
        self.error = error
        self.cause = cause
        self.finished = []

    def check_planning_inputs(self, *args):
        if self.cause is not None:
            raise self.error from self.cause
        raise self.error

    def check_planner_inventory(self, judgment):
        pass

    def finish_hypothesis_judgment(self, identity, judgment):
        self.finished.append(identity)


class _MemoryState:
    def __init__(self, tx):
        self.tx = tx

    async def transaction(self, commit):
        return commit(self.tx)


class _SqliteState:
    """Real run_transaction: the write before the commit must be rolled back."""

    def __init__(self, path, tx):
        self.path = path
        self.tx = tx

    async def transaction(self, commit):
        def operation(db):
            db.execute("INSERT INTO probe_writes (note) VALUES ('would-commit')")
            return commit(self.tx)

        return await run_transaction(self.path, operation, write=True)


def _budget():
    budget = DecisionBudget.reasoning_chain(normal_limit=3, repair_limit=2, transport_limit=1)
    budget.consume(stage="hypothesis_planning")
    budget.consume(stage="hypothesis_planning", repair=True)
    budget.consume(stage="hypothesis_planning", transport=True)
    return budget


async def _finalize(state, budget):
    deps = SimpleNamespace(central_state=state)
    snapshot = SimpleNamespace(mission=SimpleNamespace(revision=1), binding="binding-1")
    with pytest.raises(DecisionFailure) as failed:
        await _finalize_hypothesis_pass(
            deps, snapshot, budget, identity="planner_pass_origin", proposal=None,
            judgment={}, graph_read={}, task_read={}, targets=set(), principals=set(),
            max_tasks=3,
        )
    return failed.value


def _origin_row(diagnostic):
    row = diagnostic.trace[-1]
    assert row["outcome"] == "admission_exception_origin"
    return row["origins"]


@pytest.mark.asyncio
async def test_real_finalize_origin_names_current_and_cause_redteam_frames():
    boom = _trusted_boom()
    tx = _Tx(StateConflict(RAW), cause=_caught_cause(boom))
    budget = _budget()
    failure = await _finalize(_MemoryState(tx), budget)

    diagnostic = failure.diagnostic
    origins = _origin_row(diagnostic)
    assert [origin["scope"] for origin in origins] == ["current", "cause"]
    assert origins[0]["module"] == PLANNER and origins[0]["function"] == "commit"
    assert origins[1] == {"scope": "cause", "module": FIXTURE, "function": "boom", "line": 2}
    assert all(set(origin) == {"scope", "module", "function", "line"} for origin in origins)
    assert all(origin["module"].startswith("redteam.") and origin["line"] > 0 for origin in origins)
    assert RAW not in diagnostic.model_dump_json() and RAW not in failure.detail
    assert diagnostic.error_type == "StateConflict"
    assert diagnostic.exception_chain == ["StateConflict", "StateConflict"]
    assert (diagnostic.attempts, diagnostic.normal_used) == (3, 1)
    assert tx.finished == []


def test_foreign_frames_are_excluded_and_chain_is_bounded():
    boom = _trusted_boom()
    trusted = _caught_cause(boom)

    foreign = StateConflict(RAW)
    with pytest.raises(StateConflict) as raised:
        raise foreign
    assert exception_origins_for(raised.value) == []

    def wrap(error, depth):
        for _ in range(depth):
            outer = StateConflict(RAW)
            outer.__cause__ = error
            error = outer
        return error

    within_limit = exception_origins_for(wrap(trusted, 4))
    assert [origin["scope"] for origin in within_limit] == ["cause"]
    assert within_limit[0]["function"] == "boom"
    assert exception_origins_for(wrap(trusted, 6)) == []


@pytest.mark.asyncio
async def test_attached_owner_and_budget_trace_are_kept_and_trace_stays_bounded():
    error = StateConflict(RAW)
    error.diagnostic = DecisionDiagnostic(
        code="owner_evidence_not_current", stage="candidate_admission", kind="validation",
        owner="orchestration.central_state", cause_scope="evidence_validity",
        evidence_ids=["ev-1"], repair_condition="Refresh the evidence.",
        summary="Evidence is stale.", location="$.updates", retryable=True,
        trace=[{"attempt": index, "stage": "owned"} for index in range(20)],
        failure_counts={"owned": 4},
    )
    budget = _budget()
    failure = await _finalize(_MemoryState(_Tx(error)), budget)

    diagnostic = failure.diagnostic
    assert diagnostic.code == "owner_evidence_not_current"
    assert diagnostic.owner == "orchestration.central_state"
    assert diagnostic.location == "$.updates" and diagnostic.cause_scope == "evidence_validity"
    assert diagnostic.stage == "hypothesis_planning" and diagnostic.retryable is False
    assert diagnostic.failure_counts == {"owned": 4}
    assert diagnostic.trace[:-1] == [{"attempt": index, "stage": "owned"} for index in range(1, 20)]
    assert _origin_row(diagnostic)[0]["function"] == "commit"
    assert len(diagnostic.trace) == 20


def test_budget_trace_fills_an_empty_attached_trace():
    budget = _budget()
    budget.record("hypothesis_planning", "first")
    diagnostic = _admission_rejection_diagnostic(StateConflict(RAW), budget)
    assert diagnostic.trace[0]["outcome"] == "first"
    assert diagnostic.trace[-1]["outcome"] == "admission_exception_origin"


@pytest.mark.asyncio
async def test_real_sqlite_transaction_rolls_back_and_charges_nothing(tmp_path):
    path = tmp_path / "origin.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE probe_writes (note TEXT)")
    db.commit()
    db.close()

    tx = _Tx(StateConflict(RAW), cause=_caught_cause(_trusted_boom()))
    budget = _budget()
    used_before = budget.used
    failure = await _finalize(_SqliteState(path, tx), budget)

    db = sqlite3.connect(path)
    writes = db.execute("SELECT COUNT(*) FROM probe_writes").fetchone()[0]
    db.close()
    assert writes == 0
    assert budget.used == used_before == failure.diagnostic.attempts
    assert failure.diagnostic.error_type == "StateConflict"
    assert tx.finished == []


def test_cause_cycle_terminates_and_keeps_only_reachable_trusted_origins():
    trusted = _caught_cause(_trusted_boom())
    partner = StateConflict(RAW)
    trusted.__cause__ = partner
    partner.__cause__ = trusted

    origins = exception_origins_for(trusted)
    assert [(origin["scope"], origin["function"]) for origin in origins] == [("current", "boom")]
    assert exception_chain_for(trusted) == ["StateConflict", "StateConflict"]


def test_suppressed_context_is_excluded_and_unsuppressed_context_is_kept():
    trusted = _caught_cause(_trusted_boom())
    outer = StateConflict(RAW)
    outer.__context__ = trusted
    outer.__suppress_context__ = True
    assert exception_origins_for(outer) == []
    assert exception_chain_for(outer) == ["StateConflict"]

    outer.__suppress_context__ = False
    assert [origin["scope"] for origin in exception_origins_for(outer)] == ["cause"]
    assert exception_chain_for(outer) == ["StateConflict", "StateConflict"]
