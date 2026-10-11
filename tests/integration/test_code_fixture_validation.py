"""Registered artifact -> fixture port -> immutable original, synthetic only."""

import asyncio
import copy
from datetime import datetime, timezone

import pytest

from redteam.core.enums import TaskStatus
from redteam.core.ports.code_fixtures import CodeFixtureResult
from redteam.orchestration.code_fixture_validation import KIND
from tests.integration.test_code_artifacts import call, configured


class Runner:
    fingerprint = "f" * 64

    def __init__(self):
        self.calls = []
        self.hook = None
        self.result = CodeFixtureResult(state="passed", reason_code="fixture_cases_passed",
                                        cases_checked=1, cases_passed=1, cleanup_verified=True)

    async def run(self, request):
        self.calls.append(request)
        if self.hook is not None:
            await self.hook()
        return self.result


async def prepared(tmp_path):
    world, runtime, path, provider, ledger, context = await configured(tmp_path)
    runner = Runner()
    world.deps.code_fixture_runner = runner
    artifact = await call(context)
    assert artifact.ok, artifact.error
    arguments = {"artifact_evidence_id": artifact.refs[0]}
    return world, runtime, provider, ledger, context, runner, arguments


async def test_registered_fixture_not_target_success_or_extra_model_call(tmp_path):
    world, runtime, provider, ledger, context, runner, arguments = await prepared(tmp_path)
    result = await call(context, "code.validate", arguments)
    assert result.ok, result.error
    assert result.payload["state"] == "passed" and result.runtime_outcome is None
    assert result.payload["target_execution_enabled"] is False
    assert len(runner.calls) == 1 and len(provider.calls) == 2
    assert (await ledger.snapshot(str(runtime.run_id)))["spent"] == 2
    assert (await world.tasks.get_task(runtime.active_task_id)).status is TaskStatus.RUNNING
    original = await world.evidence.get(result.refs[0])
    assert original.metadata["record_kind"] == KIND
    replay = await call(context, "code.validate", arguments)
    assert replay.ok and replay.payload == result.payload and len(runner.calls) == 1


async def test_fixture_original_cannot_support_target_completion(tmp_path):
    from redteam.orchestration.semantic_completion import verify_single_origin

    world, runtime, _, _, context, _, arguments = await prepared(tmp_path)
    result = await call(context, "code.validate", arguments)
    assert result.ok
    _, errors = await verify_single_origin(
        evidence_store=world.evidence, path_store=world.paths,
        run_id=str(runtime.run_id), engagement_id=str(runtime.engagement_id),
        mission_id=str(runtime.mission_id), evidence_id=result.refs[0],
        task_graph_store=world.tasks, work_store=world.work,
    )
    assert errors, "offline fixture SYSTEM record is not an original target response"
    assert (await world.tasks.get_task(runtime.active_task_id)).status is TaskStatus.RUNNING


async def test_unconfigured_means_disabled_not_host_fallback(tmp_path):
    world, runtime, _, _, _, context = await configured(tmp_path)
    artifact = await call(context)
    result = await call(context, "code.validate", {"artifact_evidence_id": artifact.refs[0]})
    assert not result.ok and result.error == "fixture_runner_unconfigured"


@pytest.mark.parametrize("state", ["failed", "held"])
async def test_failed_unknown_immutable_and_not_reexecuted(tmp_path, state):
    _, _, _, _, context, runner, arguments = await prepared(tmp_path)
    runner.result = CodeFixtureResult(state=state, reason_code="fixture_test_failure",
                                      cases_checked=0, cases_passed=0,
                                      cleanup_verified=state != "held")
    first = await call(context, "code.validate", arguments)
    second = await call(context, "code.validate", arguments)
    assert not first.ok and second.payload == first.payload
    assert len(runner.calls) == 1


async def test_concurrent_duplicate_is_held_not_parallel_rerun(tmp_path):
    _, _, _, _, context, runner, arguments = await prepared(tmp_path)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def blocked():
        entered.set()
        await finish.wait()

    runner.hook = blocked
    first = asyncio.create_task(call(context, "code.validate", arguments))
    await entered.wait()
    second = await call(context, "code.validate", arguments)
    assert second.error == "fixture_original_missing_held" and len(runner.calls) == 1
    finish.set()
    assert (await first).ok


async def test_cancelled_admission_has_no_automatic_retry(tmp_path):
    _, _, _, _, context, runner, arguments = await prepared(tmp_path)
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    runner.hook = blocked
    task = asyncio.create_task(call(context, "code.validate", arguments))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await call(context, "code.validate", arguments)
    assert result.error == "fixture_original_missing_held"
    assert len(runner.calls) == 1


async def test_stop_while_validating_preserves_original_without_admitting_current_result(tmp_path):
    world, runtime, _, _, context, runner, arguments = await prepared(tmp_path)

    async def stop():
        async def close(tx):
            tx.records[("binding", str(runtime.run_id))]["closed"] = True
        await world.deps.coordinator.change(close)

    runner.hook = stop
    result = await call(context, "code.validate", arguments)
    assert not result.ok and result.error == "fixture_context_changed_or_write_pending"
    originals = [r for r in await world.evidence.list_for_run(runtime.run_id)
                 if r.metadata.get("record_kind") == KIND]
    assert len(originals) == 1 and len(runner.calls) == 1


@pytest.mark.parametrize("kind", ["blob", "source", "runner", "claim", "result"])
async def test_mutation_rejected_no_rerun(tmp_path, kind):
    world, runtime, _, _, context, runner, arguments = await prepared(tmp_path)
    result = await call(context, "code.validate", arguments)
    assert result.ok
    if kind == "runner":
        runner.fingerprint = "a" * 64
    elif kind == "source":
        (await world.evidence.get("proof-race")).metadata["tool_result"]["success"] = False
    elif kind == "claim":
        task = await world.tasks.get_task(runtime.active_task_id)
        await world.tasks.update_task(task.model_copy(update={"occupant_owner": "other-owner"}))
    elif kind == "blob":
        record = await world.evidence.get(arguments["artifact_evidence_id"])
        record.digest = "0" * 64
    else:
        (await world.evidence.get(result.refs[0])).metadata["result"]["cases_passed"] = 0
    again = await call(context, "code.validate", arguments)
    assert not again.ok and len(runner.calls) == 1


async def test_lost_acknowledgment_reuses_exact_original_without_second_execution(tmp_path):
    world, _, _, _, context, runner, arguments = await prepared(tmp_path)
    evidence = world.deps.evidence_store

    class LostAck:
        def __getattr__(self, name):
            return getattr(evidence, name)

        async def append(self, record):
            await evidence.append(record)
            if record.metadata.get("record_kind") == KIND:
                raise OSError("sensitive-storage-detail")

    world.deps.evidence_store = LostAck()
    first = await call(context, "code.validate", arguments)
    assert not first.ok and "sensitive" not in str(first)
    world.deps.evidence_store = evidence
    second = await call(context, "code.validate", arguments)
    assert second.ok and len(runner.calls) == 1


@pytest.mark.parametrize("cancelled", [False, True])
async def test_durable_admission_and_original_survive_provider_reopen(tmp_path, cancelled):
    """Real SQLite/Filesystem persistence, explicit synthetic model/runner."""
    from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
    from redteam.orchestration.coordination import coordinator_for
    from redteam.orchestration.llm_requests import RunRequestLedger
    from redteam.orchestration.paths.sqlite import SQLitePathStore
    from redteam.orchestration.planning_admission import PlanningAdmission
    from redteam.orchestration.planning_evidence import PlanningEvidenceStore
    from redteam.orchestration.planning_graph import PlanningGraphStore
    from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore

    world, runtime, provider, _, context, runner, arguments = await prepared(tmp_path)
    # Copy the synthetic world through public staged-store operations. No SQL
    # internals, claim repair or production migration fallback is added.
    async def snapshot(tx):
        return copy.deepcopy((await tx.tasks.list_missions(), await tx.tasks.list_tasks(),
                              await tx.paths.list_paths(str(runtime.run_id)), tx.records))

    missions, tasks, paths, records = await world.deps.coordinator.change(snapshot)
    db, evidence_dir = tmp_path / "canonical.sqlite", tmp_path / "evidence"
    original_store = FilesystemEvidenceStore(evidence_dir)
    for record in await world.evidence.list_for_run(runtime.run_id):
        await original_store.append(record)

    def reopen():
        world.tasks, world.paths = SQLiteTaskGraphStore(db), SQLitePathStore(db)
        coordinator = coordinator_for(world.tasks, world.paths)
        world.deps.coordinator = coordinator
        world.deps.task_graph_store, world.deps.path_store = world.tasks, world.paths
        admission = PlanningAdmission(coordinator)
        world.evidence = PlanningEvidenceStore(FilesystemEvidenceStore(evidence_dir), admission)
        world.deps.evidence_store = world.evidence
        graph = world.deps.graph_store
        world.deps.graph_store = PlanningGraphStore(graph.store, admission)
        return coordinator

    coordinator = reopen()

    async def seed(tx):
        for mission in missions:
            await tx.tasks.create_mission(mission)
        for task in tasks:
            await tx.tasks.create_task(task)
        for path in paths:
            await tx.paths.register(path)
        tx.records.update(records)
    await coordinator.change(seed)

    async def cancel():
        raise asyncio.CancelledError
    runner.hook = cancel if cancelled else None
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await call(context, "code.validate", arguments)
    else:
        assert (await call(context, "code.validate", arguments)).ok
    before = (await world.tasks.get_task(runtime.active_task_id)).model_dump(mode="json")
    ledger = await RunRequestLedger(coordinator).snapshot(str(runtime.run_id))
    replacement = Runner()
    world.deps.code_fixture_runner = replacement
    coordinator = reopen()
    result = await call(context, "code.validate", arguments)
    assert result.ok is (not cancelled), result.error
    if cancelled:
        assert result.error == "fixture_original_missing_held"
    assert len(runner.calls) == 1 and not replacement.calls and len(provider.calls) == 2
    assert (await world.tasks.get_task(runtime.active_task_id)).model_dump(mode="json") == before
    assert await RunRequestLedger(coordinator).snapshot(str(runtime.run_id)) == ledger


def test_optional_binding_serialization_keeps_legacy_digest_shape():
    from redteam.orchestration.component_binding import RuntimeComponentBinding
    from redteam.orchestration.state import RuntimeState

    binding = RuntimeComponentBinding()
    assert "code_fixture_fingerprint" not in binding.model_dump(mode="json")
    runtime = RuntimeState(run_id="legacy", engagement_id="eng", component_binding=binding,
                           started_at=datetime.now(timezone.utc))
    assert "code_fixture_fingerprint" not in runtime.model_dump(mode="json")["component_binding"]
    selected = binding.model_copy(update={"code_fixture_fingerprint": "c" * 64})
    assert selected != binding and selected.model_dump()["code_fixture_fingerprint"] == "c" * 64
