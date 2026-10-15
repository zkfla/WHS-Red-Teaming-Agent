"""4b: real SQLite/SDK/Control with explicit synthetic model/tool doubles only."""

import asyncio
import sqlite3
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from redteam.app.central_development import build_central_development
from redteam.app.central_run import open_central_run
from redteam.app.central_views import inspect_central_run
from redteam.core.config import AppConfig, EngagementConfig, OrchestrationConfig
from redteam.core.contracts.execution import ExecutionState
from redteam.core.enums import AgentPhase, MissionStatus, TaskStatus
from redteam.core.ports.llm import NativeCompletion
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_runtime import ACTOR_RETURNED, ready_workers
from redteam.orchestration.central_state import CentralStateStore, execution_identity_hash
from redteam.orchestration.coordination import CoordinationError, StateConflict
from redteam.orchestration.state import TerminationReason
from tests.integration.test_central_completion import (
    OriginalProtocolDouble,
    finish_inconclusive,
    selection,
)
from tests.integration.test_central_development import (
    TOOL,
    discover,
    gateway,
    inspect,
    register,
    turn,
)
from tests.integration.test_central_parallel import drain, limits, release_and_join
from tests.integration.test_central_recovery import start


async def scripted_task(session, llm, identity, *, account="a"):
    task_id, = await register(session, identity)
    llm.native_responses_by_task[task_id] = [
        discover(TOOL), turn(inspect("original-call", account)),
        NativeCompletion(text="Original recorded; investigation remains open", total_tokens=1),
    ]
    return task_id


@pytest.mark.parametrize("declared_resource", [False, True])
@pytest.mark.parametrize("notify", [True, False])
async def test_resource_release_continues_exact_call_without_recharge(declared_resource, notify):
    selected, configured = limits()
    llm, tools = FakeLLMClient(), gateway(resource=declared_resource)
    tools.release.clear()
    async with build_central_development(
        selected, settings=configured, llm=llm, tools=tools,
    ) as session:
        returned = asyncio.Event()

        async def changed(_):
            returned.set()

        await session.deps.event_bus.subscribe(ACTOR_RETURNED, changed)
        identities = [await scripted_task(session, llm, str(i)) for i in range(2)]
        # Drop/duplicate actual delivery hints, preserving Control/accounting.
        bus = session.deps.event_bus.bus
        publish = bus.publish
        async def altered_delivery(event):
            if event.event_type == ACTOR_RETURNED:
                if not notify:
                    return None
                await publish(event)
            return await publish(event)
        bus.publish = altered_delivery
        results = []
        running = asyncio.create_task(drain(session, results))
        try:
            await asyncio.wait_for(tools.wait_for_invocations(1), 10)
            if notify:
                await asyncio.wait_for(returned.wait(), 10)
            else:
                # Result events are a second wake source, independent of the bus.
                for _ in range(100):
                    if results:
                        break
                    await asyncio.sleep(0.01)
                assert results
            state = session.deps.central_state
            await asyncio.sleep(0.05)
            originals = await state.list_executions()
            waiting_calls = [e for e in originals if e.wait_reason == "resource"]
            assert len(waiting_calls) == 1, [
                (e.state, e.input_digest, e.resource_keys, e.wait_reason) for e in originals
            ]
            waiting, = waiting_calls
            assert waiting.state is ExecutionState.PENDING
            assert await session.remaining_budget() == 10
            owner = await state.get_task(str(waiting.task_id))
            with pytest.raises(StateConflict, match="stale_task_ownership"):
                await state.transaction(lambda tx: tx.reserve_resource_actor(
                    str(waiting.task_id), owner="obsolete-owner", version=owner.ownership_version,
                    instance_id=str(waiting.instance_id), now=0, max_instances=2,
                    max_calls=2, per_target=2,
                ))
        finally:
            await release_and_join(tools, running)
        final = await state.list_executions()
        assert len(final) == len(tools.invocations) == 2
        assert {e.invocation_id for e in final} == {e.invocation_id for e in originals}
        assert {e.action_id for e in final} == {e.action_id for e in originals}
        assert {e.instance_id for e in final} == {e.instance_id for e in originals}
        assert all(e.state is ExecutionState.SUCCEEDED for e in final)
        assert {str(e.task_id) for e in final} == set(identities)
        assert await session.remaining_budget() == 10
        assert (await state.request_ledger().snapshot(state.run_id))["spent"] == 6
        assert await state.transaction(lambda tx: tx.active_activation_count(), write=False) == 0


@pytest.mark.parametrize("retry_safe", [False, True])
@pytest.mark.parametrize("historical", [False, True])
async def test_unknown_exact_physical_call_new_ids_and_independent_context(retry_safe, historical):
    selected, configured = limits(workers=4, calls=4, target=4)
    llm, tools = FakeLLMClient(), gateway(unknown=True)
    # Explicit provider contract, never inferred from LOW risk.
    descriptor = tools.catalog.get(TOOL)
    if retry_safe:
        from redteam.capability.development import DevelopmentToolGateway
        tools = DevelopmentToolGateway(
            [descriptor.model_copy(update={"retry_safe": True})], tools.responses,
        )
    async with build_central_development(
        selected, settings=configured, llm=llm, tools=tools,
    ) as session:
        await scripted_task(session, llm, "first")
        await drain(session)
        state = session.deps.central_state
        original, = await state.list_executions()
        assert original.state is ExecutionState.UNKNOWN
        if historical and not retry_safe:
            legacy = original.model_copy(update={"resource_keys": [],
                                                 "resource_execution_pending": False})
            with sqlite3.connect(state.path) as db:
                db.execute("DELETE FROM state_execution_resources WHERE run_id=?",
                           (state.run_id,))
                db.execute("UPDATE state_executions SET payload=?,identity_hash=? WHERE run_id=?",
                           (legacy.model_dump_json(), execution_identity_hash(legacy),
                            state.run_id))
        # Reopen the state connection: no in-memory attempted-operation set.
        reopened = CentralStateStore(state.path, run_id=state.run_id,
                                     engagement_id=state.engagement_id)
        await reopened.setup()
        assert await reopened.get_execution(str(original.invocation_id)) is not None
        await scripted_task(session, llm, "same")
        await drain(session)
        assert len(tools.invocations) == (2 if retry_safe else 1)
        if not retry_safe:
            waiting, = [e for e in await reopened.list_executions() if e.wait_reason == "resource"]
            assert waiting.task_id != original.task_id
            assert waiting.input_digest == original.input_digest
            if historical:
                legacy_pending = waiting.model_copy(update={"resource_keys": []})
                with sqlite3.connect(state.path) as db:
                    db.execute("DELETE FROM state_execution_resources WHERE run_id=? "
                               "AND invocation_id=?", (state.run_id, str(waiting.invocation_id)))
                    db.execute("UPDATE state_executions SET payload=?,identity_hash=? "
                               "WHERE run_id=? AND invocation_id=?",
                               (legacy_pending.model_dump_json(),
                                execution_identity_hash(legacy_pending), state.run_id,
                                str(waiting.invocation_id)))
                # Old pending rows get comparisons from their verified receipt,
                # at both automatic readiness and explicit original entry.
                assert await drain(session) == []
                runner = session.driver.runner_for(session.runtime.model_copy(
                    update={"instance_id": waiting.instance_id},
                ))
                actor = await runner.original_central_runtime(state.run_id, waiting.instance_id)
                resumed = await runner.resume_resource(actor)
                assert resumed.termination_reason == TerminationReason.RESOURCE_WAIT
                assert len(tools.invocations) == 1
                unchanged = await state.get_execution(str(waiting.invocation_id))
                assert execution_identity_hash(unchanged) == execution_identity_hash(legacy_pending)
        await scripted_task(session, llm, "independent", account="separate-account")
        await drain(session)
        assert len(tools.invocations) == (3 if retry_safe else 2)
        assert tools.invocations[-1].arguments["account"] == "separate-account"


async def test_resolved_call_allows_new_investigation():
    selected, configured = limits()
    llm, tools = FakeLLMClient(), gateway()
    async with build_central_development(
        selected, settings=configured, llm=llm, tools=tools,
    ) as session:
        for i in range(2):
            await scripted_task(session, llm, str(i))
            await drain(session)
        assert len(tools.invocations) == 2
        assert len({item.invocation_id for item in tools.invocations}) == 2


async def test_unknown_duplicate_protection_survives_original_api_restart(tmp_path):
    selected, configured = limits()
    config = AppConfig(engagement=EngagementConfig.model_validate(
        selected.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    llm, tools = FakeLLMClient(), OriginalProtocolDouble(gateway(unknown=True))
    options = {"llm": llm, "tool_gateway": tools, "tool_catalog": tools.catalog}
    root = tmp_path / "run"
    async with open_central_run(config, directory=root, settings=configured, **options) as session:
        await scripted_task(session, llm, "first")
        await drain(session)
        run_id, engagement_id = session.runtime.run_id, session.runtime.engagement_id
        original, = await session.deps.central_state.list_executions()
    async with open_central_run(
        config, directory=root, settings=configured, resume=True,
        run_id=run_id, engagement_id=engagement_id, **options,
    ) as session:
        await scripted_task(session, llm, "new-id")
        await drain(session)
        assert len(tools.inner.invocations) == 1
        held = await session.deps.central_state.get_execution(str(original.invocation_id))
        assert held.state is ExecutionState.UNKNOWN and held.resource_execution_pending
        pending, = [e for e in await session.deps.central_state.list_executions()
                    if e.wait_reason == "resource"]
        assert pending.input_digest == original.input_digest
        assert await session.remaining_budget() == 10


async def test_wait_returns_slot_for_independent_work_and_resumes_within_bounds():
    selected, configured = limits()
    llm, tools = FakeLLMClient(), gateway(resource=True)
    tools.release.clear()
    async with build_central_development(
        selected, settings=configured, llm=llm, tools=tools,
    ) as session:
        await scripted_task(session, llm, "first")
        await scripted_task(session, llm, "waiting")
        await scripted_task(session, llm, "independent", account="other")
        running = asyncio.create_task(drain(session))
        try:
            await asyncio.wait_for(tools.wait_for_invocations(2), 10)
            assert {call.arguments["account"] for call in tools.invocations} == {"a", "other"}
            state = session.deps.central_state
            active = await state.transaction(lambda tx: tx.active_activation_count(), write=False)
            assert active <= 2
            assert len([e for e in await state.list_executions()
                        if e.state is ExecutionState.RUNNING]) <= 2
        finally:
            results = await release_and_join(tools, running)
        assert len({r.instance_id for r in results}) == 3
        assert len(tools.invocations) == 3
        assert await session.remaining_budget() == 9


@pytest.mark.parametrize("operation", ["pause", "stop"])
async def test_operator_closes_admission_before_resource_continuation(operation):
    from datetime import datetime, timezone

    from redteam.core.contracts.mission import OperatorIntent, Provenance
    from redteam.orchestration.central_runtime import process_controls
    from redteam.orchestration.operator_intents.models import IntentRecord

    selected, configured = limits()
    llm, tools = FakeLLMClient(), gateway(resource=True)
    tools.release.clear()
    async with build_central_development(
        selected, settings=configured, llm=llm, tools=tools,
    ) as session:
        await scripted_task(session, llm, "first")
        await scripted_task(session, llm, "waiting")
        returned = asyncio.Event()
        async def changed(_):
            returned.set()
        await session.deps.event_bus.subscribe(ACTOR_RETURNED, changed)
        running = asyncio.create_task(drain(session))
        try:
            await asyncio.wait_for(tools.wait_for_invocations(1), 10)
            await asyncio.wait_for(returned.wait(), 10)
            await session.deps.central_state.append(IntentRecord(
                intent=OperatorIntent(
                    intent_id="operator-intent", mission_id=session.runtime.mission_id,
                    intent_type=operation, instruction=operation,
                    created_at=datetime.now(timezone.utc),
                    provenance=Provenance(source="fixture:operator"),
                ), run_id=session.runtime.run_id, correlation_id="operator-intent",
            ))
            await process_controls(session.deps)
        finally:
            await release_and_join(tools, running)
        assert len(tools.invocations) == 1
        mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
        assert mission.status is (
            MissionStatus.PAUSED if operation == "pause" else MissionStatus.CANCELLED
        )


async def test_selected_completed_actor_preserves_ready_sibling_and_goal(tmp_path):
    async with selection(tmp_path) as (session, llm):
        await finish_inconclusive(session, llm)
        state = session.deps.central_state
        completed, = await state.activation_inventory()
        assert (await state.get_task(completed["task_id"])).status is TaskStatus.COMPLETED
        sibling, = await register(session, "sibling")
        before = await state.activation_inventory()
        usage = await state.request_ledger().snapshot(state.run_id)
        # A completed selection does not even inspect a missing SDK thread.
        async def unexpected(*args, **kwargs):
            raise AssertionError("terminal selection opened SDK or settled whole goal")
        session.driver.recover_unstarted_activations = unexpected
        session.settle = unexpected
        final = await session.resume(instance_id=completed["instance_id"])
        assert str(final.instance_id) == completed["instance_id"]
        assert (await state.get_task(sibling)).status is TaskStatus.READY
        assert await state.activation_inventory() == before
        assert await state.request_ledger().snapshot(state.run_id) == usage
        assert (await state.get_mission(session.runtime.mission_id)).status is MissionStatus.ACTIVE


async def test_cli_completed_selection_returns_without_opening_missing_sdk(tmp_path, monkeypatch,
                                                                        capsys):
    from redteam.app.cli.commands import resume, run
    from tests.integration.test_central_development import engagement

    async with selection(tmp_path) as (session, llm):
        await finish_inconclusive(session, llm)
        state = session.deps.central_state
        completed, = await state.activation_inventory()
        sibling, = await register(session, "ready-sibling")
        _, saved, _ = await inspect_central_run(state.path, state.run_id)
        used = await state.request_ledger().snapshot(state.run_id)
        tools = session.deps.tool_gateway
    # A completed selection uses canonical records; it never creates an empty
    # replacement SDK when the actor checkpoint file is unavailable.
    from pathlib import Path
    sdk = Path(saved["storage_refs"]["checkpoints"])
    sdk.rename(sdk.with_suffix(".preserved"))
    raw = engagement()
    raw.metadata["success_criteria"] = ["Assess the original response"]
    config = AppConfig(engagement=EngagementConfig.model_validate(
        raw.model_dump(exclude={"engagement_id", "created_at"}),
    ), orchestration=OrchestrationConfig(runtime_backend="central",
                                       central_state_path=str(state.path)))
    real_build = run.build_application
    async def assemble(config, **kwargs):
        return await real_build(config, llm=llm, tool_gateway=tools,
                                tool_catalog=tools.catalog, **kwargs)
    monkeypatch.setattr(run, "build_application", assemble)
    monkeypatch.setattr(run, "prepare_shell_runtime", AsyncMock(return_value=None))
    args = SimpleNamespace(run_id=state.run_id, instance=completed["instance_id"],
                           json=True, workers=None, max_steps=None)
    async with AsyncExitStack() as stack:
        assert await resume._run_central(args, config, mcp_stack=stack) == 0
    assert not sdk.exists()
    assert await state.request_ledger().snapshot(state.run_id) == used
    assert (await state.get_task(sibling)).status is TaskStatus.READY
    assert len(await state.activation_inventory()) == 1
    assert (await state.get_mission(session.runtime.mission_id)).status is MissionStatus.ACTIVE
    capsys.readouterr()


@pytest.mark.parametrize("mode", ["normal", "approval", "unknown"])
async def test_selected_unstarted_actor_does_not_read_corrupt_foreign_checkpoint(
    tmp_path, mode,
):
    approval = mode == "approval"
    tools = OriginalProtocolDouble(gateway(risk="high" if approval else "low"))
    if mode == "unknown":
        tools.inner.responses[0].metadata["execution_uncertain"] = True
    async with selection(tmp_path, tools=tools) as (session, llm):
        first, = await start(session, llm)
        sibling = await scripted_task(session, llm, "selected", account="independent")
        selected, = await ready_workers(session.deps, session.runtime)
        await register(session, "ready-sibling")
        foreign = session.driver.runner_for(first)
        async def corrupt(*args):
            raise CoordinationError("foreign_checkpoint_corrupt")
        foreign.original_central_runtime = corrupt
        final = await session.resume(instance_id=selected.instance_id)
        assert str(final.active_task_id) == sibling
        inventory = await session.deps.central_state.activation_inventory()
        assert len(inventory) == 2
        remaining = [task for task in await session.deps.central_state.list_tasks()
                     if str(task.task_id) not in {str(first.active_task_id), sibling}]
        assert len(remaining) == 1 and remaining[0].status is TaskStatus.READY
        if approval:
            assert not tools.inner.invocations
            executions = await session.deps.central_state.list_executions()
            assert len({e.approval_ref for e in executions}) == 2
        else:
            # The production fixture has one tool slot. A sibling UNKNOWN still
            # occupies it; selection bypasses foreign SDK damage, never capacity.
            assert len(tools.inner.invocations) == (1 if mode == "unknown" else 2)
        if mode == "unknown":
            originals = await session.deps.central_state.list_executions()
            assert next(e for e in originals if e.instance_id == first.instance_id).state \
                is ExecutionState.UNKNOWN
        with pytest.raises(CoordinationError, match="foreign_checkpoint_corrupt"):
            await session.resume()


async def test_active_status_uses_current_actor_and_keeps_previous_reason_readonly(tmp_path):
    async with selection(tmp_path) as (session, llm):
        await session.settle(session.runtime.model_copy(update={
            "phase": AgentPhase.FINISHED,
            "termination_reason": TerminationReason.INVESTIGATION_HELD,
        }))
        state = session.deps.central_state
        await register(session)
        await state.transaction(lambda tx: tx.set_settlement_gate(False))
        llm.request_release.clear()
        running = asyncio.create_task(drain(session))
        try:
            await asyncio.wait_for(llm.wait_for_requests(1), 10)
            before = await state.get_mission(session.runtime.mission_id)
            _, _, report = await inspect_central_run(state.path, state.run_id)
            assert report.phase == AgentPhase.OBSERVE
            assert report.run_outcome == "in_progress"
            assert report.termination_reason is None
            assert report.previous_termination_reason == TerminationReason.INVESTIGATION_HELD
            assert await state.get_mission(session.runtime.mission_id) == before
        finally:
            llm.request_release.set()
            await asyncio.wait_for(running, 10)


@pytest.mark.parametrize("changed_limit", [False, True])
async def test_cli_preserves_nondefault_passive_identity_and_rejects_actual_override(
    tmp_path, monkeypatch, capsys, changed_limit,
):
    from redteam.app.cli.commands import resume, run

    selected, configured = limits()
    configured = configured.with_overrides(max_no_progress_turns=1)
    config = AppConfig(engagement=EngagementConfig.model_validate(
        selected.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    tools, llm = OriginalProtocolDouble(gateway(risk="high")), FakeLLMClient()
    async with open_central_run(
        config, directory=tmp_path / "run", settings=configured,
        llm=llm, tool_gateway=tools, tool_catalog=tools.catalog,
    ) as session:
        original, = await start(session, llm)
        state = session.deps.central_state
        path = str(state.path)
        before = await state.activation_inventory()
        used = await state.request_ledger().snapshot(state.run_id)
    config.orchestration = OrchestrationConfig(runtime_backend="central", central_state_path=path)
    captured = []
    real_build = run.build_application
    async def assemble(config, **kwargs):
        result = await real_build(config, llm=llm, tool_gateway=tools,
                                  tool_catalog=tools.catalog, **kwargs)
        captured.append(result[1])
        return result
    monkeypatch.setattr(run, "build_application", assemble)
    monkeypatch.setattr(run, "prepare_shell_runtime", AsyncMock(return_value=None))
    args = SimpleNamespace(run_id=str(original.run_id), instance=str(original.instance_id),
                           json=True, workers=None, max_steps=99 if changed_limit else None)
    async with AsyncExitStack() as stack:
        result = await resume._run_central(args, config, mcp_stack=stack)
    output = capsys.readouterr()
    assert not tools.inner.invocations
    assert captured[0].settings.central_payload()["max_no_progress_turns"] == 1
    if changed_limit:
        assert result == 1 and "resume refused" in output.err
        assert await state.activation_inventory() == before
        assert await state.request_ledger().snapshot(state.run_id) == used
    else:
        assert result == 0, output.err
        assert "resume refused" not in output.err
        assert (await state.list_executions())[0].state is ExecutionState.PENDING
