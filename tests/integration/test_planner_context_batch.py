"""Planner batches independent captured reads in one normal model round trip."""

from redteam.orchestration.planner_context import PlannerContextRequest, PlannerPassContext
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_planner_context_lookup import seed_history
from tests.integration.test_probe_workers import selection
from tests.integration.test_v3_atomization import SplitDouble


def _read(identity):
    return {"context": {"kind": "task", "mode": "read",
                        "refs": [{"kind": "task", "identity": identity}]}}


async def test_independent_task_reads_batch_and_keep_same_related_readset(tmp_path):
    batch = {"reason": "Read both independent originals", "questions": [],
             "termination": "continue", "context_request": {
                 "version": 2, "queries": [_read("z-source"), _read("history-0000")]}}

    def finish(payload):
        envelope, = payload["context_lookup_results"]
        results = envelope["result"]["results"]
        assert [item["result"]["records"][0]["identity"] for item in results] == [
            "z-source", "history-0000"]
        return {"questions": [], "reason": "No additional work is proposed",
                "termination": "held",
                "resume_conditions": ["A new independent observation becomes available"],
                "reviewed_task_ids": ["z-source", "history-0000"]}

    llm = SplitDouble([batch, finish], [])
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        await seed_history(session.deps.central_state)
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.plan_inputs) == 2  # One lookup round plus the final decision.
        assert not llm.atom_inputs
        cursor = await session.deps.central_state.transaction(
            lambda tx: tx.hypothesis_planner_cursor(), write=False)
        assert cursor is not None
        assert len(await session.deps.central_state.list_tasks()) == 101
        ledger = await session.deps.central_state.request_ledger().snapshot(session.runtime.run_id)
        assert ledger["spent"] == 2


async def test_batch_valid_item_error_is_isolated_and_invalid_batch_repaired(tmp_path):
    malformed = {"reason": "Try invalid batch shape", "questions": [], "termination": "continue",
                 "context_request": {"version": 2, "queries": [
                     _read("z-source"), {"context": {"mode": "read"}}]}}
    valid = {"reason": "Read one captured record", "questions": [], "termination": "continue",
             "context_request": {"version": 2, "queries": [
                 _read("missing"), _read("z-source")]}}

    def finish(payload):
        results = payload["context_lookup_results"][0]["result"]["results"]
        assert results[0]["result"]["status"] == "error"
        assert results[1]["result"]["status"] == "ok"
        return {"questions": [], "reason": "Wait for new observations", "termination": "held",
                "resume_conditions": ["A new independent observation becomes available"]}

    llm = SplitDouble([malformed, valid, finish], [])
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        await seed_history(session.deps.central_state)
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.plan_inputs) == 3
        assert "context_lookup_results" in llm.plan_inputs[-1]


def _window(index, visible=()):
    request = {"version": 1, "context": {"kind": "task", "mode": "list",
                                           "text": f"query-{index}"},
               "evidence": None, "queries": None}
    result = {"status": "ok", "records": [], "marker": index}
    public = {"request": request, "result": result, "retention": "recent"}
    return {"request": request, "result": result, "public": public,
            "_visible": set(visible)}


def _pass_context_for_retention():
    context = object.__new__(PlannerPassContext)
    context.seen = {kind: set() for kind in ("task", "candidate", "execution", "node", "edge")}
    context._baseline_seen = {kind: set() for kind in context.seen}
    context._baseline_seen["task"].add("overlap")
    context.seen["task"].add("overlap")
    context.recent_windows = []
    context._recent_seen = []
    context._lookup_visible = set()
    context.used_evidence = set()
    context.consumed = {kind: set() for kind in context.seen}
    context.cache = {}
    context._retained_all_v1 = True
    return context


def test_oversized_batch_window_is_rejected_without_visibility_or_read_indexes():
    context = _pass_context_for_retention()
    request = PlannerContextRequest(version=2, queries=[_read("candidate-hidden")])
    base = {"required": "base"}
    result = {"version": 2, "status": "ok", "results": [{
        "result": {"status": "ok", "records": [{"identity": "candidate-hidden"}],
                   "body": "x" * 70000}}]}
    assert not context.retain_lookup_window(
        base, request.model_dump(mode="json"), result, {("task", "candidate-hidden")})
    assert context.seen["task"] == {"overlap"}
    assert context._lookup_visible == set()
    assert context.recent_windows == []
    assert context.cache == {}
    assert context.consumed == {kind: set() for kind in context.seen}
    assert context.used_evidence == set()


def test_recent_window_eviction_preserves_baseline_overlap_and_drops_old_refs():
    context = _pass_context_for_retention()
    context.recent_windows = [_window(i, {("task", "overlap"), ("task", f"old-{i}")})
                              for i in range(9)]
    context._lookup_visible = set().union(*(item["_visible"] for item in context.recent_windows))
    context.seen["task"].update(identity for _, identity in context._lookup_visible)
    next_window = _window(9, {("task", "new-9")})
    request = next_window["request"]
    assert context.retain_lookup_window({"base": True}, request, next_window["result"],
                                        next_window["_visible"])
    assert len(context.recent_windows) == 8
    assert "overlap" in context.seen["task"]  # Baseline visibility survives lookup eviction.
    assert "old-0" not in context.seen["task"]
    assert "new-9" in context.seen["task"]


async def test_oversize_lookup_retries_narrowly_and_rolls_back_indexes(tmp_path, monkeypatch):
    request = {"reason": "Read an original", "questions": [], "termination": "continue",
               "context_request": {"version": 2, "queries": [_read("z-source")]}}

    def narrow(payload):
        result = payload["context_lookup_results"][0]["result"]
        assert result["status"] == "error"
        assert result["code"] == "context_batch_budget_exceeded"
        return {"questions": [], "reason": "Wait for another observation", "termination": "held",
                "resume_conditions": ["A new independent observation becomes available"]}

    llm = SplitDouble([request, narrow], [])
    seen_contexts = []
    original_lookup = PlannerPassContext.lookup

    async def oversized(self, selected, **kwargs):
        result, visible = await original_lookup(self, selected, **kwargs)
        seen_contexts.append(self)
        return {"version": 2, "status": "ok", "results": [{
            "query": {}, "result": {"status": "ok", "records": [], "data": "x" * 70000}}]}, visible

    monkeypatch.setattr(PlannerPassContext, "lookup", oversized)
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        await seed_history(session.deps.central_state)
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
    assert len(llm.plan_inputs) == 2
    pass_context, = seen_contexts
    assert pass_context.cache == {}
    assert all(not identities for identities in pass_context.consumed.values())
    assert pass_context.used_evidence == set()
    assert "z-source" not in pass_context.seen["task"]


async def test_guard_halt_between_batch_items_prevents_second_backend_read():
    from redteam.orchestration.cognition import DecisionFailure

    context = object.__new__(PlannerPassContext)
    reads = []

    async def lookup_item(self, item):
        reads.append(item)
        return {"status": "ok"}, set()

    context._lookup_item = lookup_item.__get__(context, PlannerPassContext)
    request = PlannerContextRequest(version=2, queries=[
        {"context": {"kind": "task"}}, {"context": {"kind": "candidate"}}])
    guards = 0

    async def guard():
        nonlocal guards
        guards += 1
        if guards == 2:
            raise DecisionFailure("hypothesis_planning", "budget", 0)

    try:
        await context.lookup(request, before_each=guard)
    except DecisionFailure:
        pass
    else:
        raise AssertionError("guard halt should stop the in-flight batch")
    assert len(reads) == 1
