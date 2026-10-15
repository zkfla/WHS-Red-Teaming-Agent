"""Real native actors and SQLite, with explicit synthetic providers only."""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from redteam.app.central_development import build_central_development
from redteam.app.central_run import bind_run_metadata
from redteam.core.contracts.execution import ExecutionState
from redteam.core.enums import TaskStatus
from redteam.core.ports.llm import NATIVE_PROTOCOL_EMPTY_RESPONSE, NativeCompletion
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_results import RESULT_UPDATED
from redteam.orchestration.central_runtime import ready_workers
from redteam.orchestration.central_state import CentralStateStore, execution_occupies_capacity
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.development_jobs import DevelopmentJobs
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.specialist_tasks import register_investigation_tasks
from tests.integration.test_central_development import (
    TARGET,
    TOOL,
    call,
    discover,
    engagement,
    gateway,
    inspect,
    proposal,
    register,
    settings,
    turn,
)


def limits(*, workers=2, calls=2, target=2, requests=100, llm_slots=2):
    configured = settings(requests=requests).model_dump()
    configured.update(
        global_concurrency=workers, per_target_concurrency=target, resource_wait_timeout_seconds=0.1
    )
    configured["llm_request_policy"]["max_concurrent_requests"] = llm_slots
    selected = engagement(actions=12)
    rate = selected.rate_limit_policy.model_dump()
    rate["max_concurrent_actions"] = calls
    selected.rate_limit_policy = type(selected.rate_limit_policy).model_validate(rate)
    return selected, OrchestrationSettings.model_validate(configured)


async def tasks(selection, llm, count=2):
    identities = []
    for i in range(count):
        identity = (await register(selection, f"registration-{i}"))[0]
        identities.append(identity)
        llm.native_responses_by_task[identity] = [
            discover(TOOL),
            turn(inspect("original-call", account=str(i))),
            NativeCompletion(text="Further evidence is needed", total_tokens=10),
        ]
    return identities


async def drain(selection, received=None):
    received = received if received is not None else []
    async for result in selection.driver.drive_ready_tasks(
        selection.runtime,
        max_instances=2,
    ):
        received.append(result)
    return received


async def release_and_join(tools, running):
    tools.release.set()
    for signal in tools.call_releases.values():
        signal.set()
    return await asyncio.wait_for(running, 10)


@pytest.mark.parametrize("workers", [1, 2])
async def test_same_role_parallel_entry_and_instance_state_isolation(workers):
    selected, configured = limits(workers=workers)
    llm, tools = FakeLLMClient(), gateway()
    tools.release.clear()
    async with build_central_development(
        selected,
        llm=llm,
        tools=tools,
        settings=configured,
    ) as selection:
        identities = await tasks(selection, llm)
        base = selection.runtime.model_dump()
        running = asyncio.create_task(drain(selection))
        try:
            await asyncio.wait_for(tools.wait_for_invocations(workers), 10)
            executions = await selection.deps.central_state.list_executions()
            assert len(executions) == workers
            assert all(e.state is ExecutionState.RUNNING for e in executions)
            assert (
                await selection.deps.central_state.transaction(
                    lambda tx: tx.active_activation_count(),
                    write=False,
                )
                == workers
            )
            assert len({e.instance_id for e in executions}) == workers
            assert len({e.task_id for e in executions}) == workers
        finally:
            results = await release_and_join(tools, running)
        assert len(results) == 2 and all(e.kind == "completed" for e in results)
        assert {str(e.runtime.active_task_id) for e in results} == set(identities)
        assert {str(e.runtime.current_agent) for e in results} == {"web-executor"}
        assert selection.runtime.model_dump() == base
        assert all(e.runtime.actions_executed == 1 for e in results)
        assert len(tools.invocations) == 2
        state = selection.deps.central_state
        assert await state.transaction(lambda tx: tx.active_activation_count(), write=False) == 0
        assert all(e.state is ExecutionState.SUCCEEDED for e in await state.list_executions())
        for result in results:
            assert await selection.driver.runner_for(result.runtime).checkpoint_exists(
                result.runtime
            )
        assert (await state.request_ledger().snapshot(state.run_id))["spent"] == 6


@pytest.mark.parametrize("waiting", ["approval", "job"])
async def test_waiting_actors_return_slots_and_third_task_runs(waiting):
    selected, configured = limits()
    llm, tools = FakeLLMClient(), gateway(risk="high" if waiting == "approval" else "low")
    jobs = DevelopmentJobs() if waiting == "job" else None
    async with build_central_development(
        selected,
        llm=llm,
        tools=tools,
        settings=configured,
        jobs=jobs,
    ) as selection:
        await tasks(selection, llm, 3)
        identities = [str(task.task_id) for task in await selection.deps.central_state.list_tasks()]
        llm.native_responses_by_task[identities[2]] = [NativeCompletion(text="Review evidence")]
        results = await asyncio.wait_for(drain(selection), 10)
        assert len(results) == 3 and not tools.invocations
        state = selection.deps.central_state
        executions = await state.list_executions()
        assert len(executions) == 2
        assert all(e.wait_reason == waiting for e in executions)
        if waiting == "approval":
            assert all(e.approval_ref and not execution_occupies_capacity(e) for e in executions)
        else:
            assert len(jobs.submissions) == 2
            assert all(e.job_ref and execution_occupies_capacity(e) for e in executions)
        blocked = [
            (await state.get_task(identity)).status is TaskStatus.BLOCKED
            for identity in identities[:2]
        ]
        assert all(blocked)
        assert await ready_workers(selection.deps, selection.runtime) == []
        assert await state.transaction(lambda tx: tx.active_activation_count(), write=False) == 0
        assert (await state.request_ledger().snapshot(state.run_id))["reserved"] == 0


@pytest.mark.parametrize("held", ["job", "unknown", "resource"])
async def test_retained_external_activity_caps_survive_actor_return_and_expiry(held):
    selected, configured = limits(calls=1 if held != "resource" else 2)
    llm, tools = FakeLLMClient(), gateway(unknown=held == "unknown", resource=True)
    jobs = DevelopmentJobs() if held == "job" else None
    async with build_central_development(
        selected,
        llm=llm,
        tools=tools,
        settings=configured,
        jobs=jobs,
    ) as selection:
        identities = await tasks(selection, llm, 3)
        if held == "resource":
            tools.responses[0].metadata = {"execution_uncertain": True}
            # Shared physical resource despite different investigation purposes.
            for identity in identities:
                llm.native_responses_by_task[identity][1] = turn(inspect("original-call", "shared"))
        results = await asyncio.wait_for(drain(selection), 10)
        assert len(results) == 3
        state = selection.deps.central_state
        held_calls = [e for e in await state.list_executions() if execution_occupies_capacity(e)]
        assert len(held_calls) == 1 and held_calls[0].state is ExecutionState.UNKNOWN
        assert held_calls[0].resource_execution_pending
        # Reservations remain charged even though only one provider call ran.
        assert (await selection.status())["remaining_budget"] == 9
        if jobs:
            assert len(jobs.submissions) == 1 and held_calls[0].job_ref in jobs.submissions
        else:
            assert len(tools.invocations) == 1
        original = held_calls[0]
        # Expired reservations never prove external completion.
        # Synthetic persisted expiry; this does not settle the original call.
        with sqlite3.connect(state.path) as db:
            db.execute(
                "UPDATE state_executions SET payload=json_set(payload,'$.resource_lease_until',0) "
                "WHERE run_id=? AND invocation_id=?",
                (state.run_id, str(original.invocation_id)),
            )
        other = CentralStateStore(
            state.path, run_id=state.run_id, engagement_id=state.engagement_id
        )
        await other.setup()
        assert execution_occupies_capacity(await other.get_execution(str(original.invocation_id)))
        assert await state.transaction(lambda tx: tx.active_activation_count(), write=False) == 0
        assert await ready_workers(selection.deps, selection.runtime) == []


async def test_one_worker_failure_does_not_stop_held_sibling_or_next_task():
    selected, configured = limits()
    llm, tools = FakeLLMClient(), gateway()
    tools.release.clear()
    async with build_central_development(
        selected,
        llm=llm,
        tools=tools,
        settings=configured,
    ) as selection:
        identities = await tasks(selection, llm, 3)
        failed_id = str((await selection.deps.central_state.list_tasks())[0].task_id)
        llm.native_responses_by_task[failed_id] = [
            NativeCompletion(protocol_error=NATIVE_PROTOCOL_EMPTY_RESPONSE),
        ]
        received = []
        running = asyncio.create_task(drain(selection, received))
        try:
            # A failed actor frees its slot while its independent sibling is
            # still inside the gateway: the third actor enters too.
            await asyncio.wait_for(tools.wait_for_invocations(2), 10)
            assert any(e.kind == "failed" for e in received)
            expected = {str(i) for i, identity in enumerate(identities) if identity != failed_id}
            assert {e.arguments["account"] for e in tools.invocations} == expected
        finally:
            results = await release_and_join(tools, running)
        assert sum(e.kind == "failed" for e in results) == 1
        assert sum(e.kind == "completed" for e in results) == 2


@pytest.mark.parametrize("limit_kind", ["target", "tool", "llm"])
async def test_independent_limits_are_enforced_under_concurrent_load(limit_kind):
    selected, configured = limits(
        target=1 if limit_kind == "target" else 2,
        calls=1 if limit_kind == "tool" else 2,
        llm_slots=1 if limit_kind == "llm" else 2,
    )
    llm, tools = FakeLLMClient(), gateway()
    async with build_central_development(
        selected,
        llm=llm,
        tools=tools,
        settings=configured,
    ) as selection:
        await tasks(selection, llm, 3)
        if limit_kind == "llm":
            llm.request_release.clear()
        else:
            tools.release.clear()
        running = asyncio.create_task(drain(selection))
        try:
            if limit_kind == "llm":
                await asyncio.wait_for(llm.wait_for_requests(1), 10)
                assert llm.active_requests == 1
                usage = await selection.deps.central_state.request_ledger().snapshot(
                    str(selection.runtime.run_id),
                )
                assert usage["reserved"] == 1
            else:
                await asyncio.wait_for(tools.wait_for_invocations(1), 10)
                first_return = asyncio.Event()

                async def returned(_):
                    first_return.set()

                from redteam.orchestration.central_runtime import ACTOR_RETURNED

                await selection.deps.event_bus.subscribe(ACTOR_RETURNED, returned)
                await asyncio.wait_for(first_return.wait(), 10)
                assert len(tools.invocations) == 1
        finally:
            llm.request_release.set()
            results = await release_and_join(tools, running)
        # A returned resource wait now continues on the SAME actor and emits
        # another return; count identities and executions, not return events.
        assert len({result.instance_id for result in results}) == 3
        assert len(tools.invocations) == 3
        assert all(item.state is ExecutionState.SUCCEEDED
                   for item in await selection.deps.central_state.list_executions())
        if limit_kind == "llm":
            assert llm.peak_requests == 1


async def test_lost_notification_and_reopened_database_scan_do_not_replay_old_work():
    selected, configured = limits()
    llm, tools = FakeLLMClient(), gateway()
    tools.release.clear()
    async with build_central_development(
        selected,
        llm=llm,
        tools=tools,
        settings=configured,
    ) as selection:
        await tasks(selection, llm, 1)
        running = asyncio.create_task(drain(selection))
        try:
            await asyncio.wait_for(tools.wait_for_invocations(1), 10)
            # Register through a reopened connection; do not deliver its outbox.
            state = selection.deps.central_state
            reopened = CentralStateStore(
                state.path, run_id=state.run_id, engagement_id=state.engagement_id
            )
            await reopened.setup()
            identity = (
                await register_investigation_tasks(
                    reopened,
                    proposal_id="silent-registration",
                    proposal=proposal(),
                    agents=selection.deps.agents,
                    known_targets={TARGET},
                    known_principals=set(),
                    max_tasks=8,
                    evidence_store=selection.deps.evidence_store,
                )
            )[0]
            llm.native_responses_by_task[identity] = [
                discover(TOOL),
                turn(inspect("silent", "new")),
            ]
            await asyncio.wait_for(tools.wait_for_invocations(2), 10)
            assert {e.arguments["account"] for e in tools.invocations} == {"0", "new"}
        finally:
            await release_and_join(tools, running)
        before = list(tools.invocations)
        assert await asyncio.wait_for(drain(selection), 10) == []
        assert tools.invocations == before


async def test_stale_ownership_and_exact_result_replay_preserve_originals_and_usage():
    selected, configured = limits()
    llm, tools = FakeLLMClient(), gateway()
    async with build_central_development(
        selected,
        llm=llm,
        tools=tools,
        settings=configured,
    ) as selection:
        await tasks(selection, llm)
        results = await asyncio.wait_for(drain(selection), 10)
        state = selection.deps.central_state
        record = (await state.list_executions())[0]
        owner = next(
            r.runtime.work_owner for r in results if r.runtime.active_task_id == record.task_id
        )
        usage = await state.request_ledger().snapshot(state.run_id)
        with sqlite3.connect(state.path) as db:
            refs = db.execute("SELECT count(*) FROM state_evidence_refs").fetchone()[0]
        assert (
            await state.transaction(
                lambda tx: tx.update_execution(
                    record,
                    owner=owner,
                    now=datetime.now(timezone.utc).timestamp(),
                )
            )
            == record
        )
        with pytest.raises(StateConflict, match="stale_task_ownership"):
            await state.transaction(
                lambda tx: tx.update_execution(
                    record.model_copy(update={"ownership_version": record.ownership_version + 1}),
                    owner=owner,
                    now=datetime.now(timezone.utc).timestamp(),
                )
            )
        assert await state.request_ledger().snapshot(state.run_id) == usage
        with sqlite3.connect(state.path) as db:
            assert db.execute("SELECT count(*) FROM state_evidence_refs").fetchone()[0] == refs


@pytest.mark.parametrize("quota", ["actions", "requests"])
async def test_workers_share_one_budget_without_reset_on_next_task(quota):
    selected, configured = limits(requests=3 if quota == "requests" else 100)
    if quota == "actions":
        selected.execution_budget.max_actions = 1
    llm, tools = FakeLLMClient(), gateway()
    async with build_central_development(
        selected,
        llm=llm,
        tools=tools,
        settings=configured,
    ) as selection:
        await tasks(selection, llm, 3)
        assert len(await asyncio.wait_for(drain(selection), 10)) == 3
        assert len(tools.invocations) <= 1
        if quota == "actions":
            assert not await selection.deps.control_plane.has_tool_call_capacity(
                selection.runtime.engagement_id,
            )
        else:
            usage = await selection.deps.central_state.request_ledger().snapshot(
                str(selection.runtime.run_id),
            )
            assert usage["spent"] == 3 and usage["reserved"] == 0


async def test_shared_graph_updates_rebase_on_latest_row_and_deletion_stays_fenced():
    from redteam.attack_graph.stores.central import CentralAttackGraphStore
    from redteam.orchestration.central_graph_batch import CentralGraphBatch

    selected, configured = limits()
    async with build_central_development(
        selected,
        llm=FakeLLMClient(),
        tools=gateway(),
        settings=configured,
    ) as selection:
        state = selection.deps.central_state
        source = CentralAttackGraphStore(state)
        node = (await source.list_nodes())[0]
        first, second, deletion = [CentralGraphBatch(source, state) for _ in range(3)]
        for batch, label in ((first, "first-observation"), (second, "second-observation")):
            await batch.mutate_node(
                node.id,
                lambda current, label=label: current.model_copy(
                    update={"labels": sorted({*current.labels, label})},
                ),
            )
        await deletion.remove_node(node.id)
        # Both read the SAME old row; commit order is deterministic.
        a, b, removed = (
            await first.prepare("first"),
            await second.prepare("second"),
            await deletion.prepare("delete"),
        )
        await state.transaction(lambda tx: first.commit(tx, *a))
        await state.transaction(lambda tx: second.commit(tx, *b))
        merged = await source.get_node(node.id)
        assert {"first-observation", "second-observation"} <= set(merged.labels)
        with pytest.raises(StateConflict, match="related_graph_row_changed"):
            await state.transaction(lambda tx: deletion.commit(tx, *removed))


async def test_planner_and_peer_read_intermediate_result_before_source_finishes():
    selected, configured = limits()
    llm = FakeLLMClient(canned_structured={
        "questions": [], "reason": "Await investigation", "termination": "held",
        "resume_conditions": ["New worker result is committed"],
    })
    tools = gateway()
    tools.release.clear()
    async with build_central_development(
        selected,
        llm=llm,
        tools=tools,
        settings=configured,
    ) as selection:
        await bind_run_metadata(selection.deps)
        identities = await tasks(selection, llm)
        # Authoritative Task IDs route independent native scripts, not response order.
        published = asyncio.Event()
        followup_registered = asyncio.Event()
        original_ref = None

        async def intermediate(envelope):
            nonlocal original_ref
            task = await selection.deps.central_state.get_task(envelope.payload["task_id"])
            assert task.status is TaskStatus.RUNNING
            original_ref = task.result.evidence_ids[0]
            candidate = proposal(
                source_task_id=str(task.task_id),
                source_result_id=task.result.result_id,
                source_question_id="new-question",
            )
            llm.structured_responses = [
                {
                    "questions": [q.model_dump(mode="json") for q in candidate.questions],
                    "reason": "Follow committed intermediate question",
                },
                {"tasks": [t.model_dump(mode="json") for t in candidate.tasks]},
                *[
                    {
                        "questions": [], "reason": "Await evidence", "termination": "held",
                        "resume_conditions": ["New worker result is committed"],
                    }
                    for _ in range(8)
                ],
            ]
            published.set()

        async def created(envelope):
            task = await selection.deps.central_state.get_task(envelope.payload["task_id"])
            if task.source_task_id:
                source = await selection.deps.central_state.get_task(str(task.source_task_id))
                assert source.status is TaskStatus.RUNNING
                assert task.source_result_id == "intermediate"
                assert task.source_question_id == "new-question"
                followup_registered.set()

        from redteam.core.contracts.events import EventType

        await selection.deps.event_bus.subscribe(RESULT_UPDATED, intermediate)
        await selection.deps.event_bus.subscribe(EventType.TASK_CREATED, created)
        running = asyncio.create_task(selection.run())
        try:
            await asyncio.wait_for(tools.wait_for_invocations(2), 10)
            a = next(e for e in tools.invocations if e.arguments["account"] == "0")
            b = next(e for e in tools.invocations if e.arguments["account"] == "1")
            # Let A commit an observation while B is still executing.
            tools.call_releases[str(b.invocation_id)].clear()
            tools.release.set()
            state = selection.deps.central_state

            async def evidence(envelope):
                if envelope.payload.get("action_id") != str(a.action_id):
                    return
                evidence_id = envelope.payload["evidence_id"]
                llm.native_responses_by_task[identities[0]][:0] = [
                    discover("task.publish_result", TOOL),
                    turn(
                        call(
                            "publish",
                            "task.publish_result",
                            {
                                "result": {
                                    "result_id": "intermediate",
                                    "outcome": "inconclusive",
                                    "summary": "Independent follow-up",
                                    "evidence_ids": [evidence_id],
                                    "questions": [
                                        {
                                            "question_id": "new-question",
                                            "question": "Another view",
                                            "evidence_ids": [evidence_id],
                                        }
                                    ],
                                }
                            },
                        )
                    ),
                    turn(inspect("hold-source", "source-still-running")),
                ]
                # Keep the source Task running until its follow-up is registered.
                tools.release.clear()

            await selection.deps.event_bus.subscribe(EventType.EVIDENCE_CREATED, evidence)
            await asyncio.wait_for(published.wait(), 10)
            try:
                await asyncio.wait_for(followup_registered.wait(), 10)
            except TimeoutError:
                # Surface the planner run outcome (its termination and diagnostic) on failure.
                if not running.done():
                    running.cancel()
                outcome = await asyncio.gather(running, return_exceptions=True)
                raise AssertionError(f"followup not registered; run outcome={outcome!r}") from None
            peer = next(
                task for task in await state.list_tasks() if str(task.task_id) == identities[1]
            )
            runtime = selection.runtime.model_copy(
                update={
                    "instance_id": b.execution_context.instance_id,
                    "active_task_id": peer.task_id,
                    "task_ownership_version": peer.ownership_version,
                    "current_agent": peer.assigned_agent,
                }
            )
            context = await InvestigationEngine(selection.deps).context(runtime, None)
            source = next(
                t for t in context["investigation_tasks"] if t["task_id"] == identities[0]
            )
            assert source["result"]["evidence_ids"] == [original_ref]
            assert any(
                "intermediate" in json.dumps(request["messages"])
                for request in llm.requests
                if request["kind"] == "complete_structured"
            )
        finally:
            final = await release_and_join(tools, running)
        assert final.instance_id is None and final.active_task_id is None
        assert len(selection.instance_results) >= 2
