"""Plan 4e central flows with explicit doubles; no Docker/target/paid LLM.

Handlers are called directly with a session-assembled store set plus an
explicit synthetic fixture-runner fake. The blob store comes from the
application default location; only model answers and fixture outcomes are
doubled, and every double is labelled.
"""

import json
from dataclasses import replace

import pytest

from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ports.code_fixtures import CodeFixtureResult
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.code_artifacts import (
    _capture,
    _commit,
    _reserve_fixture,
    check_fixture_admissions,
    inspect_artifact,
    prepare,
    read_fixture_admissions,
    validate_fixture,
)
from redteam.orchestration.cognition import DecisionBudget
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.local_tools import LocalToolContext
from tests.integration.test_central_completion import selection
from tests.integration.test_central_recovery import start

PROGRAM_SOURCE = "def evaluate(value):\n    return {\"observed\": bool(value.get(\"n\", 0))}"
PROGRAM = {
    "language": "python",
    "source": PROGRAM_SOURCE,
    "cases": [{"name": "one", "input": {"n": 1}, "expected": {"observed": True}}],
}
PURPOSE = "Assess the observed response"


class CodeDouble(FakeLLMClient):
    """Explicit scripted author/reviewer; records structured requests."""

    def __init__(self, program, *, review="accept"):
        super().__init__()
        self.program = program
        self.review = review

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        await self._wait_response("complete_structured", messages)
        properties = response_schema.get("properties", {})
        if {"language", "source", "cases"} <= properties.keys():
            return dict(self.program)
        if {"accepted", "artifact_digest", "reason"} <= properties.keys():
            try:
                body = json.loads(messages[0]["content"].split("\n\nPrevious attempt failed")[0])
                digest = body.get("artifact_digest", "0" * 64)
            except (ValueError, AttributeError, KeyError, IndexError):
                digest = "0" * 64
            if self.review == "reject":
                return {"accepted": False, "artifact_digest": digest,
                        "reason": "Not relevant."}
            if self.review == "mismatch":
                return {"accepted": True, "artifact_digest": "0" * 64,
                        "reason": "Wrong digest."}
            return {"accepted": True, "artifact_digest": digest,
                    "reason": "Synthetic-only design matches."}
        raise AssertionError("unexpected structured stage in code-artifact test")


class FakeRunner:
    """Explicit synthetic-only fixture double; never a target shell."""

    fingerprint = "e" * 64

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def run(self, request):
        self.calls.append(request)
        assert request.source.startswith("def evaluate(")
        assert len(request.cases) >= 1
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


def passed(n=1):
    return CodeFixtureResult(state="passed", reason_code="fixture_cases_passed",
                             cases_checked=n, cases_passed=n, cleanup_verified=True)


def failed(reason):
    return CodeFixtureResult(state="failed", reason_code=reason,
                             cases_checked=1, cases_passed=0, cleanup_verified=True)


def held(reason):
    return CodeFixtureResult(state="held", reason_code=reason,
                             cases_checked=0, cases_passed=0, cleanup_verified=False)


def context(session, runtime, *, runner=None, budget=None, purpose=PURPOSE):
    deps = replace(session.deps, code_fixture_runner=runner)
    return LocalToolContext(
        deps, runtime, purpose=purpose,
        budget=budget if budget is not None else DecisionBudget(8),
    )


async def originals_for(session, runtime):
    return [str(record.evidence_id)
            for record in await session.deps.evidence_store.list_for_run(runtime.run_id)
            if isinstance(record.metadata.get("tool_result"), dict)
            and record.metadata.get("mission_task_id") == str(runtime.active_task_id)]


async def tool_result_count(session, runtime):
    return len(await originals_for(session, runtime))


async def begin(session, llm, *, program=PROGRAM, review="accept"):
    runtimes = await start(session, llm)
    runtime = runtimes[0]
    originals = await originals_for(session, runtime)
    assert originals, "harness produced no proven tool originals"
    double = CodeDouble(program, review=review)
    llm.complete_structured = double.complete_structured
    return runtime, originals, double


async def test_prepare_inspect_validate_pass_and_reuse(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, llm=llm) as (session, _):
        runtime, originals, double = await begin(session, llm)
        store = session.deps.artifact_store
        assert store is not None and len(store.fingerprint) == 64
        before_tools = await tool_result_count(session, runtime)
        before_all = len(await _records(session, runtime))
        made = await prepare({"objective": "Summarise the observed flag format",
                              "source_evidence_ids": originals},
                             context(session, runtime, runner=FakeRunner([passed()])))
        assert made.ok, made.error
        assert len(double.requests) == 2
        records = await session.deps.evidence_store.list_for_run(runtime.run_id)
        assert len(records) == before_all + 1
        assert await tool_result_count(session, runtime) == before_tools
        meta = [record for record in records
                if record.metadata.get("record_kind") == "reviewed_code_artifact_v1"]
        assert len(meta) == 1
        assert meta[0].metadata["writer"] != meta[0].metadata["reviewer"]
        seen = await inspect_artifact(
            {"artifact_evidence_id": made.payload["artifact_evidence_id"]},
            context(session, runtime, runner=FakeRunner([passed()])))
        assert seen.ok, seen.error
        assert seen.payload["artifact_digest"] == made.payload["artifact_digest"]
        assert seen.payload["program"]["source"] == PROGRAM_SOURCE
        assert seen.payload["fixture_only"] is True
        assert seen.payload["target_execution_enabled"] is False
        first = await validate_fixture(
            {"artifact_evidence_id": made.payload["artifact_evidence_id"]},
            context(session, runtime, runner=(runner := FakeRunner([passed()]))))
        assert first.ok, first.error
        assert first.payload["cases_checked"] == first.payload["cases_passed"] == 1
        assert first.refs == (first.payload["fixture_evidence_id"],)
        again = await validate_fixture(
            {"artifact_evidence_id": made.payload["artifact_evidence_id"]},
            context(session, runtime, runner=runner))
        assert again.ok, again.error
        assert len(runner.calls) == 1
        assert runner.calls[0].source == PROGRAM_SOURCE
        assert [str(identity) for identity in runner.calls[0].cases[0].input] == ["n"]
        task = await session.deps.central_state.get_task(str(runtime.active_task_id))
        assert task.status is TaskStatus.RUNNING
        mission = await session.deps.central_state.get_mission(runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE


async def _records(session, runtime):
    return await session.deps.evidence_store.list_for_run(runtime.run_id)


async def test_writer_cannot_review_own_artifact(tmp_path):
    from types import SimpleNamespace

    llm = FakeLLMClient()
    async with selection(tmp_path, llm=llm) as (session, _):
        runtime, originals, double = await begin(session, llm)
        solo = SimpleNamespace(role_id="solo", version="9", contract_version="1",
                               sha256="s" * 64)
        deps = replace(session.deps, agent_prompts=SimpleNamespace(
            binding=lambda stage: solo))
        made = await prepare({"objective": "Summarise", "source_evidence_ids": originals},
                             LocalToolContext(deps, runtime, purpose=PURPOSE,
                                              budget=DecisionBudget(4)))
        assert made.ok is False
        assert made.error == "code_artifact_input_or_integrity_failed"
        assert len(double.requests) == 0


async def test_review_rejection_and_digest_mismatch_are_structured(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, llm=llm) as (session, _):
        runtime, originals, _ = await begin(session, llm)
        before = len(await _records(session, runtime))
        llm.complete_structured = CodeDouble(PROGRAM, review="reject").complete_structured
        refused = await prepare({"objective": "Summarise", "source_evidence_ids": originals},
                                context(session, runtime))
        assert refused.ok is False and refused.error == "code_review_rejected"
        assert len(await _records(session, runtime)) == before
    llm = FakeLLMClient()
    async with selection(tmp_path / "second", llm=llm) as (session, _):
        runtime, originals, double = await begin(session, llm)
        before = len(await _records(session, runtime))
        mismatch_double = CodeDouble(PROGRAM, review="mismatch")
        llm.complete_structured = mismatch_double.complete_structured
        forged = await prepare({"objective": "Summarise", "source_evidence_ids": originals},
                               context(session, runtime, budget=DecisionBudget(3)))
        assert forged.ok is False
        assert len(mismatch_double.requests) == 3
        assert len(await _records(session, runtime)) == before


async def test_budget_exhaustion_is_structured_without_records(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, llm=llm) as (session, _):
        runtime, originals, double = await begin(session, llm)
        before = len(await _records(session, runtime))
        spent = await prepare({"objective": "Summarise", "source_evidence_ids": originals},
                              context(session, runtime, budget=DecisionBudget(1)))
        assert spent.ok is False and spent.error
        assert len(double.requests) == 1
        assert len(await _records(session, runtime)) == before


async def test_unproven_sources_and_unknown_artifacts_rejected(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, llm=llm) as (session, _):
        runtime, originals, _ = await begin(session, llm)
        ctx = context(session, runtime)
        assert (await prepare({"objective": "Summarise",
                               "source_evidence_ids": ["missing-original"]}, ctx)).ok is False
        assert (await prepare({"objective": "Summarise",
                               "source_evidence_ids": originals + ["missing-original"]},
                              ctx)).ok is False
        assert (await inspect_artifact({"artifact_evidence_id": "code_artifact_absent"},
                                      ctx)).ok is False
        assert (await validate_fixture({"artifact_evidence_id": "code_artifact_absent"},
                                       ctx)).ok is False


async def test_changed_purpose_owner_and_tampered_blob_rejected(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, llm=llm) as (session, _):
        runtime, originals, _ = await begin(session, llm)
        runner = FakeRunner([passed()])
        made = await prepare({"objective": "Summarise", "source_evidence_ids": originals},
                             context(session, runtime, runner=runner))
        assert made.ok, made.error
        identity = made.payload["artifact_evidence_id"]
        moved = await inspect_artifact(
            {"artifact_evidence_id": identity},
            context(session, runtime, runner=runner, purpose=PURPOSE + " changed"))
        assert moved.ok is False
        assert moved.error == "code_artifact_input_or_integrity_failed"
        foreign = runtime.model_copy(update={"instance_id": "foreign-instance"})
        assert (await inspect_artifact(
            {"artifact_evidence_id": identity},
            context(session, foreign, runner=runner))).ok is False
        record = await session.deps.evidence_store.get(identity)
        blob = next((tmp_path / "run").rglob(record.artifact_ref))
        blob.write_bytes(b"changed-by-test")
        assert (await inspect_artifact({"artifact_evidence_id": identity},
                                       context(session, runtime, runner=runner))).ok is False
        assert await session.deps.artifact_store.verify(record.artifact_ref,
                                                        record.digest) is False
        assert await session.deps.evidence_store.get(identity) is not None


async def test_fixture_mismatch_timeout_and_held_without_promotion(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, llm=llm) as (session, _):
        runtime, originals, _ = await begin(session, llm)

        async def checked(objective, result):
            runner = FakeRunner([result])
            made = await prepare({"objective": objective, "source_evidence_ids": originals},
                                 context(session, runtime, runner=runner))
            assert made.ok, made.error
            target = {"artifact_evidence_id": made.payload["artifact_evidence_id"]}
            outcome = await validate_fixture(target, context(session, runtime, runner=runner))
            return runner, made, outcome

        runner, _, mismatch = await checked("Summarise one", failed("fixture_case_mismatch"))
        assert mismatch.ok is False and mismatch.error == "fixture_case_mismatch"
        assert len(runner.calls) == 1
        runner, made, timeout = await checked("Summarise two", failed("fixture_timeout"))
        assert timeout.ok is False and timeout.error == "fixture_timeout"
        assert len(runner.calls) == 1
        runner, made, stalled = await checked("Summarise three", held("fixture_cleanup_unverified"))
        assert stalled.ok is False and stalled.error == "fixture_cleanup_unverified"
        assert len(runner.calls) == 1
        target = {"artifact_evidence_id": made.payload["artifact_evidence_id"]}
        repeated = await validate_fixture(target, context(session, runtime, runner=runner))
        assert repeated.ok is False
        assert repeated.payload["fixture_evidence_id"] == stalled.payload["fixture_evidence_id"]
        assert len(runner.calls) == 1
        task = await session.deps.central_state.get_task(str(runtime.active_task_id))
        assert task.status is TaskStatus.RUNNING
        mission = await session.deps.central_state.get_mission(runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE

async def test_fixture_unconfigured_and_changed_inputs_do_not_rerun(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, llm=llm) as (session, _):
        runtime, originals, _ = await begin(session, llm)
        runner = FakeRunner([passed()])
        made = await prepare({"objective": "Summarise", "source_evidence_ids": originals},
                             context(session, runtime, runner=runner))
        assert made.ok, made.error
        identity = made.payload["artifact_evidence_id"]
        assert (await validate_fixture(
            {"artifact_evidence_id": identity},
            context(session, runtime, runner=None))).error == "fixture_runner_unconfigured"
        assert len(runner.calls) == 0
        first = await validate_fixture({"artifact_evidence_id": identity},
                                       context(session, runtime, runner=runner))
        assert first.ok, first.error
        assert len(runner.calls) == 1
        stale = await validate_fixture(
            {"artifact_evidence_id": identity},
            context(session, runtime, runner=runner, purpose=PURPOSE + " changed"))
        assert stale.ok is False
        assert len(runner.calls) == 1



async def _rewrite_task(session, runtime, **updates):
    state = session.deps.central_state
    task = await state.get_task(str(runtime.active_task_id))
    changed = task.model_copy(update=updates)

    def write(tx):
        tx.db.execute(
            "UPDATE state_tasks SET payload=? WHERE run_id=? AND task_id=?",
            (changed.model_dump_json(), tx.run_id, str(changed.task_id)),
        )

    await state.transaction(write)


async def _rewrite_mission_objective(session, runtime, suffix):
    state = session.deps.central_state
    mission = await state.get_mission(runtime.mission_id)
    changed = mission.model_copy(update={"objective": mission.objective + suffix})

    def write(tx):
        tx.db.execute(
            "UPDATE state_missions SET payload=? WHERE mission_id=?",
            (changed.model_dump_json(), str(changed.mission_id)),
        )

    await state.transaction(write)


async def _admission_rows(session, runtime):
    return await session.deps.central_state.transaction(
        lambda tx: tx.db.execute(
            "SELECT identity FROM state_requests WHERE run_id=? AND kind=?",
            (tx.run_id, "code_fixture_admission"),
        ).fetchall(),
        write=False,
    )


async def test_sql_commit_rejects_owner_status_and_goal_changes(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, llm=llm) as (session, _):
        runtime, originals, _ = await begin(session, llm)
        made = await prepare({"objective": "Summarise race", "source_evidence_ids": originals},
                             context(session, runtime, runner=FakeRunner([passed()])))
        assert made.ok, made.error
        identity = made.payload["artifact_evidence_id"]
        state = session.deps.central_state
        scope, _, validity = await _capture(
            context(session, runtime, runner=FakeRunner([passed()])), originals)

        ctx = context(session, runtime)

        def commit(tx):
            return _commit(tx, ctx, scope, validity)

        await _rewrite_task(session, runtime, occupant_owner="run:instance:foreign",
                            ownership_version=99)
        with pytest.raises(StateConflict):
            await state.transaction(commit)
        assert (await inspect_artifact({"artifact_evidence_id": identity},
                                      context(session, runtime))).ok is False

        await _rewrite_task(session, runtime, occupant_owner=runtime.work_owner,
                            ownership_version=0, status=TaskStatus.BLOCKED, blocking_reason="race")
        with pytest.raises(StateConflict):
            await state.transaction(commit)

        await _rewrite_task(session, runtime, status=TaskStatus.RUNNING, blocking_reason=None)
        await _rewrite_mission_objective(session, runtime, " plus late scope")
        with pytest.raises(StateConflict):
            await state.transaction(commit)
        assert (await inspect_artifact({"artifact_evidence_id": identity},
                                      context(session, runtime))).ok is False
        assert await session.deps.evidence_store.get(identity) is not None


async def test_fixture_reserve_refuses_stale_scope_without_a_row(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, llm=llm) as (session, _):
        runtime, originals, _ = await begin(session, llm)
        ctx = context(session, runtime, runner=FakeRunner([passed()]))
        scope, _, validity = await _capture(ctx, originals)
        await _rewrite_task(session, runtime, occupant_owner="run:instance:foreign",
                            ownership_version=7)
        binding = {"schema_version": 1, "scope": scope}
        with pytest.raises(StateConflict):
            await session.deps.central_state.transaction(
                lambda tx: (_commit(tx, ctx, scope, validity),
                            _reserve_fixture(tx, "f" * 64, binding))[1])
        assert await _admission_rows(session, runtime) == []


async def test_goal_gate_blocks_held_and_uncollected_but_permits_cleaned(tmp_path):
    from redteam.orchestration.central_completion import goal_inputs

    async def gated(directory, objective, result, extra_reservation=False):
        llm = FakeLLMClient()
        async with selection(directory, llm=llm) as (session, _):
            runtime, originals, _ = await begin(session, llm)
            runner = FakeRunner([result])
            made = await prepare({"objective": objective, "source_evidence_ids": originals},
                                 context(session, runtime, runner=runner))
            assert made.ok, made.error
            target = {"artifact_evidence_id": made.payload["artifact_evidence_id"]}
            outcome = await validate_fixture(target,
                                             context(session, runtime, runner=runner))
            assert outcome.ok is False
            if extra_reservation:
                state = session.deps.central_state
                binding = {"schema_version": 1,
                           "scope": {"run_id": str(runtime.run_id),
                                     "engagement_id": str(runtime.engagement_id)}}

                def reserve(tx):
                    tx.db.execute("INSERT INTO state_requests VALUES(?,?,?,?)",
                                  (tx.run_id, "code_fixture_admission", "a" * 64,
                                   json.dumps(binding)))

                await state.transaction(reserve)
            return (await goal_inputs(session.deps, session.runtime))[3]

    errors = await gated(tmp_path / "held", "Gate held", held("fixture_cleanup_unverified"))
    assert "fixture_cleanup_unverified" in errors
    errors = await gated(tmp_path / "cleaned", "Gate cleaned", failed("fixture_case_mismatch"))
    assert not [code for code in errors if code.startswith("fixture_")]
    errors = await gated(tmp_path / "uncollected", "Gate collected",
                         failed("fixture_case_mismatch"), extra_reservation=True)
    assert "fixture_result_uncollected" in errors


async def test_goal_mapping_never_counts_fixture_results(tmp_path):
    from redteam.orchestration.central_completion import goal_inputs
    from tests.integration.test_plan4a_completion import task_review

    llm = FakeLLMClient()
    native_structured = llm.complete_structured
    async with selection(tmp_path, llm=llm) as (session, _):
        runtime, originals, _ = await begin(session, llm)
        runner = FakeRunner([failed("fixture_case_mismatch")])
        made = await prepare({"objective": "Gate scoped", "source_evidence_ids": originals},
                             context(session, runtime, runner=runner))
        assert made.ok, made.error
        target = {"artifact_evidence_id": made.payload["artifact_evidence_id"]}
        outcome = await validate_fixture(target,
                                        context(session, runtime, runner=runner))
        assert outcome.ok is False
        llm.complete_structured = native_structured
        assert await task_review(session, llm, runtime, originals, entry="public") == []
        _, _, _, errors, mapping, excerpts, _ = await goal_inputs(session.deps, session.runtime)
        assert not [code for code in errors if code.startswith("fixture_")]
        flat = [ref for refs in mapping.values() for ref in refs]
        assert flat
        assert all(not ref.startswith("code_fixture_") and not ref.startswith("code_artifact_")
                   for ref in flat)
        assert not any("code_fixture_" in excerpt for excerpt in excerpts)


async def test_final_commit_rejects_fixture_admission_drift(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, llm=llm) as (session, _):
        runtime, originals, _ = await begin(session, llm)
        runner = FakeRunner([passed()])
        made = await prepare({"objective": "Gate drift", "source_evidence_ids": originals},
                             context(session, runtime, runner=runner))
        assert made.ok, made.error
        target = {"artifact_evidence_id": made.payload["artifact_evidence_id"]}
        assert (await validate_fixture(target, context(session, runtime, runner=runner))).ok
        state = session.deps.central_state
        snapshot = await read_fixture_admissions(session.deps, str(runtime.run_id))
        assert len(snapshot) == 1

        def extra(tx):
            tx.db.execute("INSERT INTO state_requests VALUES(?,?,?,?)",
                          (tx.run_id, "code_fixture_admission", "b" * 64,
                           json.dumps({"schema_version": 1})))

        await state.transaction(extra)
        with pytest.raises(StateConflict):
            await state.transaction(lambda tx: check_fixture_admissions(tx, snapshot))
        assert len(await read_fixture_admissions(session.deps, str(runtime.run_id))) == 2
