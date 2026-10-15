"""2c: exact approvals, original jobs, conservative recovery, no live providers."""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from redteam.app.central_development import build_central_development
from redteam.app.central_run import open_central_run
from redteam.core.config import AppConfig, EngagementConfig
from redteam.core.contracts.execution import ExecutionState
from redteam.core.contracts.mission import OperatorIntent, Provenance
from redteam.core.enums import ApprovalStatus
from redteam.core.ports.llm import NativeCompletion
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_runtime import ready_workers, runtime_token
from redteam.orchestration.coordination import CoordinationError, StateConflict
from redteam.orchestration.development_jobs import DevelopmentJobs
from redteam.orchestration.operator_intents.models import IntentRecord
from redteam.orchestration.state import TerminationReason
from tests.integration.test_central_development import (
    TOOL,
    discover,
    engagement,
    gateway,
    inspect,
    register,
    turn,
)
from tests.integration.test_central_parallel import limits


async def start(selection, llm, count=1, *, registration_prefix="registration"):
    for index in range(count):
        await register(selection, f"{registration_prefix}-{index}")
    tasks = await selection.deps.central_state.list_tasks()
    for task in tasks:
        llm.native_responses_by_task[str(task.task_id)] = [
            discover(TOOL), turn(inspect(f"call-{task.task_id}")),
            NativeCompletion(text="Original response inspected", total_tokens=1),
        ]
    workers = await ready_workers(selection.deps, selection.runtime)
    results = []
    async for result in selection.driver.drive(workers):
        assert result.kind == "completed", result.error
        results.append(result.runtime)
    return results


@pytest.mark.parametrize("second_decision", [ApprovalStatus.APPROVED, ApprovalStatus.REJECTED])
async def test_approval_batch_resumes_prepared_sibling_without_replaying_first(second_decision):
    llm, tools = FakeLLMClient(), gateway(risk="high")
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=limits()[1],
    ) as selection:
        await register(selection)
        task, = await selection.deps.central_state.list_tasks()
        llm.native_responses_by_task[str(task.task_id)] = [
            discover(TOOL), turn(inspect("first"), inspect("second", account="b")),
            NativeCompletion(text="Original responses inspected", total_tokens=1),
        ]
        workers = await ready_workers(selection.deps, selection.runtime)
        outcomes = [event async for event in selection.driver.drive(workers)]
        original = outcomes[0].runtime
        runner = selection.driver.runner_for(original)
        snapshot = await runner._latest_snapshot()
        planned = [call.dispatch.invocation for call in snapshot.values["native_calls"]]
        assert len(planned) == 2 and len(await selection.deps.central_state.list_executions()) == 1
        assert tools.invocations == []
        await selection.deps.control_plane.resolve_approval(
            original.pending_approval_id, ApprovalStatus.APPROVED, "fixture:operator",
        )
        recovered, proof = await selection.driver.recover_central_instance(
            original, transfer_owner=False,
        )
        assert proof.ready, proof.errors
        second_wait = await runner.resume_recovered(recovered)
        assert len(tools.invocations) == 1
        assert tools.invocations[0].invocation_id == planned[0].invocation_id
        assert second_wait.pending_approval_id != original.pending_approval_id
        second = await selection.deps.central_state.get_execution(str(planned[1].invocation_id))
        assert second.approval_ref == second_wait.pending_approval_id
        await selection.deps.control_plane.resolve_approval(
            second_wait.pending_approval_id, second_decision, "fixture:operator",
        )
        recovered, proof = await selection.driver.recover_central_instance(
            second_wait, transfer_owner=False,
        )
        assert proof.ready, proof.errors
        await runner.resume_recovered(recovered)
        assert [call.invocation_id for call in tools.invocations] == [
            call.invocation_id for call in planned[:2 if second_decision is ApprovalStatus.APPROVED
                                                 else 1]
        ]
        assert await selection.remaining_budget() == (
            4 if second_decision is ApprovalStatus.APPROVED else 5
        )


@pytest.mark.parametrize("corruption", ["missing_intent", "prepared_identity", "admitted_sibling"])
async def test_recovery_does_not_treat_damaged_originals_as_prepared_calls(corruption):
    from redteam.core.contracts.policy import ControlContext
    llm, tools = FakeLLMClient(), gateway(risk="high")
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=limits()[1],
    ) as selection:
        await register(selection)
        task, = await selection.deps.central_state.list_tasks()
        llm.native_responses_by_task[str(task.task_id)] = [
            discover(TOOL), turn(inspect("first"), inspect("second", account="b")),
        ]
        workers = await ready_workers(selection.deps, selection.runtime)
        outcomes = [event async for event in selection.driver.drive(workers)]
        original = outcomes[0].runtime
        runner = selection.driver.runner_for(original)
        snapshot = await runner._latest_snapshot()
        if corruption == "missing_intent":
            execution, = await selection.deps.central_state.list_executions()
            def remove(tx):
                for table in ("state_execution_resources", "state_executions"):
                    tx.db.execute("DELETE FROM " + table + " WHERE run_id=? AND invocation_id=?",
                                  (tx.run_id, str(execution.invocation_id)))
            await selection.deps.central_state.transaction(remove)
        elif corruption == "prepared_identity":
            calls = snapshot.values["native_calls"]
            calls[1].dispatch.invocation = calls[1].dispatch.invocation.model_copy(update={
                "arguments": {"host": "unit.local", "account": "changed"},
            })
            await runner._graph.aupdate_state(runner.config(), {"native_calls": calls},
                                             as_node="settle")
        else:
            proposal = snapshot.values["native_calls"][1].dispatch.proposal
            await selection.deps.control_plane.evaluate(
                proposal, ControlContext(engagement_id=original.engagement_id,
                                         selected_tool_id=proposal.tool_id),
            )
        _, proof = await selection.driver.recover_central_instance(original, transfer_owner=False)
        assert not proof.ready and proof.errors
        assert tools.invocations == []
        final = await selection.resume(transfer_owner=False)
        assert final.planning_diagnostic.code == "central_execution_recovery_held"
        assert final.planning_diagnostic.validation_issues
        assert tools.invocations == []


async def test_multiple_exact_approvals_deny_transfer_and_stale_owner():
    llm, tools = FakeLLMClient(), gateway(risk="high")
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=limits()[1],
    ) as selection:
        originals = await start(selection, llm, count=2)
        assert len({item.pending_approval_id for item in originals}) == 2
        assert all(item.termination_reason == TerminationReason.APPROVAL_PENDING
                   for item in originals)
        approve, deny = originals
        await selection.deps.control_plane.resolve_approval(
            approve.pending_approval_id, ApprovalStatus.APPROVED, "fixture:operator",
        )
        await selection.deps.control_plane.resolve_approval(
            deny.pending_approval_id, ApprovalStatus.REJECTED, "fixture:operator",
        )
        original_executions = await selection.deps.central_state.list_executions()
        for original in originals:
            recovered, outcome = await selection.driver.recover_central_instance(
                original, transfer_owner=True,
            )
            assert outcome.ready
            assert recovered.instance_id == original.instance_id
            assert recovered.task_ownership_version == original.task_ownership_version + 1
            with pytest.raises(StateConflict, match="stale_task_ownership"):
                await runtime_token(selection.deps, original)
            result = await selection.driver.runner_for(recovered).resume_recovered(recovered)
            assert result.termination_reason == TerminationReason.INVESTIGATION_HELD
        assert len(tools.invocations) == 1
        executed = tools.invocations[0]
        allowed = next(item for item in original_executions
                       if item.instance_id == approve.instance_id)
        assert executed.invocation_id == allowed.invocation_id
        assert executed.action_id == allowed.action_id
        assert executed.arguments == {"host": "unit.local", "account": "a"}
        final = await selection.deps.central_state.list_executions()
        assert {item.state for item in final} == {ExecutionState.SUCCEEDED,
                                               ExecutionState.CANCELLED}
        assert all(item.ownership_version == 1 for item in final)
        assert await selection.remaining_budget() == 5


async def test_original_job_status_and_result_continue_without_resubmission():
    from unittest.mock import AsyncMock

    from redteam.app.cli.commands.run import wait_for_central_suspensions
    llm, tools, jobs = FakeLLMClient(), gateway(resource=True), DevelopmentJobs()
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=limits()[1], jobs=jobs,
    ) as selection:
        original, = await start(selection, llm)
        execution, = await selection.deps.central_state.list_executions()
        assert execution.job_ref and execution.state is ExecutionState.UNKNOWN
        held, outcome = await selection.driver.recover_central_instance(original)
        assert outcome.pending_invocations == [str(execution.invocation_id)]
        assert held == original
        assert len(jobs.submissions) == 1
        held, outcome = await selection.driver.recover_central_instance(
            original, transfer_owner=True,
        )
        assert not outcome.ready and held.instance_id == original.instance_id
        assert held.task_ownership_version == original.task_ownership_version + 1
        assert len(jobs.submissions) == 1
        jobs.states[execution.job_ref] = ExecutionState.SUCCEEDED
        recovered, outcome = await selection.driver.recover_central_instance(
            held,
        )
        assert outcome.ready, outcome.errors
        result = await selection.driver.runner_for(recovered).resume_recovered(recovered)
        assert result.termination_reason == TerminationReason.INVESTIGATION_HELD
        settled = await selection.deps.central_state.get_execution(str(execution.invocation_id))
        assert settled.state is ExecutionState.SUCCEEDED
        assert not settled.resource_execution_pending and settled.resource_lease_until == 0
        assert len(jobs.submissions) == 1 and tools.invocations == []
        assert await selection.remaining_budget() == 5
        selection.resume = AsyncMock(side_effect=AssertionError("terminal job was polled again"))
        assert await wait_for_central_suspensions(
            selection, result, input_live=lambda: True, echo=lambda text: None,
        ) is result
        selection.resume.assert_not_awaited()


async def test_competing_recovery_of_one_epoch_succeeds_once():
    llm, tools = FakeLLMClient(), gateway(risk="high")
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=limits()[1],
    ) as selection:
        original, = await start(selection, llm)
        results = await asyncio.gather(
            *(selection.driver.recover_central_instance(original, transfer_owner=True)
              for _ in range(2)), return_exceptions=True,
        )
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert any(isinstance(result, (CoordinationError, StateConflict)) for result in results)
        assert tools.invocations == []


async def test_unknown_execution_and_lost_model_dispatch_remain_charged():
    llm, tools = FakeLLMClient(), gateway(unknown=True, resource=True)
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=limits()[1],
    ) as selection:
        original, = await start(selection, llm)
        ledger = selection.deps.central_state.request_ledger()
        identity = await ledger.reserve(str(original.run_id), owner=original.work_owner,
                                        kind="fixture:lost-dispatch")
        await ledger.dispatch(str(original.run_id), identity)
        recovered, outcome = await selection.driver.recover_central_instance(
            original, transfer_owner=True,
        )
        assert outcome.pending_invocations and recovered == original
        usage = await ledger.snapshot(str(original.run_id))
        assert usage["unknown_dispatch"] == 1 and usage["reserved"] == 0
        assert len(tools.invocations) == 1
        execution, = await selection.deps.central_state.list_executions()
        assert execution.state is ExecutionState.UNKNOWN and execution.resource_execution_pending


async def test_explicit_selection_reopens_original_checkpoint_and_control_budget(tmp_path):
    config = AppConfig(engagement=EngagementConfig.model_validate(
        engagement(actions=1).model_dump(exclude={"engagement_id", "created_at"}),
    ))
    llm, tools = FakeLLMClient(), gateway(risk="high")
    root = tmp_path / "selected"
    async with open_central_run(
        config, directory=root, settings=limits()[1],
        llm=llm, tool_gateway=tools, tool_catalog=tools.catalog,
    ) as selection:
        original, = await start(selection, llm)
        run_id, engagement_id = original.run_id, original.engagement_id
        approval_id = original.pending_approval_id
        ledger = selection.deps.central_state.request_ledger()
        lost = await ledger.reserve(str(run_id), owner="specialist_planning", kind="fixture:lost")
        await ledger.dispatch(str(run_id), lost)
        assert tools.invocations == []
    async with open_central_run(
        config, directory=root, settings=limits()[1], resume=True,
        run_id=run_id, engagement_id=engagement_id,
        llm=llm, tool_gateway=tools, tool_catalog=tools.catalog,
    ) as selection:
        assert (await selection.status())["usage"]["unknown_dispatch"] == 1
        await selection.deps.control_plane.resolve_approval(
            approval_id, ApprovalStatus.APPROVED, "fixture:operator",
        )
        recovered, outcome = await selection.recover(original.instance_id, transfer_owner=True)
        assert outcome.ready, outcome.errors
        result = await selection.driver.runner_for(recovered).resume_recovered(recovered)
        assert result.termination_reason == TerminationReason.INVESTIGATION_HELD
        assert len(tools.invocations) == 1
        assert await selection.remaining_budget() == 0
        spent = (await selection.status())["usage"]["spent"]
    async with open_central_run(
        config, directory=root, settings=limits()[1], resume=True,
        run_id=run_id, engagement_id=engagement_id,
        llm=llm, tool_gateway=tools, tool_catalog=tools.catalog,
    ) as selection:
        recovered, outcome = await selection.recover(original.instance_id)
        assert outcome.ready
        assert len(tools.invocations) == 1
        assert (await selection.status())["usage"]["spent"] == spent
        accounting = await selection.deps.control_plane.rehydrate_run(
            engagement_id, run_id, started_at=recovered.started_at,
        )
        assert accounting.budget_remaining == 0
    assert not list(root.rglob("network_grants.sqlite3"))
    assert not list(root.rglob("task_graph.sqlite3"))
    assert not list(root.rglob("work.sqlite3"))


async def test_explicit_selection_refuses_missing_original_or_changed_binding(tmp_path):
    config = AppConfig(engagement=EngagementConfig.model_validate(
        engagement().model_dump(exclude={"engagement_id", "created_at"}),
    ))
    with pytest.raises(ValueError, match="original_ids"):
        async with open_central_run(config, directory=tmp_path, settings=limits()[1], resume=True):
            pass

    llm, tools = FakeLLMClient(), gateway()
    async with open_central_run(
        config, directory=tmp_path, settings=limits()[1],
        llm=llm, tool_gateway=tools, tool_catalog=tools.catalog,
    ) as selection:
        original, = await start(selection, llm)
    with pytest.raises(StateConflict, match="binding_changed"):
        async with open_central_run(
            config.model_copy(update={
                "llm": config.llm.model_copy(update={"default_model": "changed"}),
            }),
            directory=tmp_path, settings=limits()[1], resume=True,
            run_id=original.run_id, engagement_id=original.engagement_id,
            llm=llm, tool_gateway=tools, tool_catalog=tools.catalog,
        ):
            pass


async def test_stop_does_not_wait_for_blocked_planner_response():
    llm, tools = FakeLLMClient(), gateway()
    llm.request_release.clear()
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=limits()[1],
    ) as selection:
        running = asyncio.create_task(selection.run())
        await asyncio.wait_for(llm.wait_for_requests(1), timeout=2)
        await selection.deps.central_state.append(IntentRecord(
            run_id=selection.runtime.run_id, correlation_id="stop-planner",
            intent=OperatorIntent(
                intent_id="stop-planner", mission_id=selection.runtime.mission_id,
                intent_type="stop", instruction="Stop fixture",
                created_at=datetime.now(timezone.utc), provenance=Provenance(source="operator"),
            ),
        ))
        result = await asyncio.wait_for(running, timeout=2)
        assert result.termination_reason == TerminationReason.OPERATOR_STOPPED
        assert tools.invocations == []
        usage = (await selection.status())["usage"]
        assert usage["unknown_dispatch"] == 1 and usage["reserved"] == 0


async def test_selected_network_ledger_keeps_pool_window_sequence_on_reopen(tmp_path):
    from redteam.app.bootstrap import build_central_storage
    from redteam.capability.gated_activation import GatedProviderConfig, configure_gated
    from redteam.capability.gated_network_policy import GatedNetworkPolicy
    from redteam.capability.shell_runtime import build_shell_runtime
    from redteam.core.contracts.network import (
        GrantRefused,
        NetworkGrant,
        NetworkPurpose,
    )
    from tests.fakes import FakeEvidenceStore
    from tests.unit.test_network_ledger import budget, caps
    from tests.unit.test_shell_runtime_008 import _policy_file

    # Direct target is the default selection and owns no ledger. This ledger
    # belongs to the dormant gated provider, so it is selected explicitly.
    provider = GatedProviderConfig(network_policy=GatedNetworkPolicy(
        budget=budget(), default_target_caps=caps(),
        default_install_caps=caps(purpose=NetworkPurpose.INSTALL),
    ))
    engagement_model = engagement()
    run_id = "ledger-run"
    state_path = tmp_path / "central" / "state.sqlite3"
    shell = build_shell_runtime(_policy_file(tmp_path))
    assert shell is not None

    async def open_ledger():
        _, ledger = await build_central_storage(
            engagement_model, run_id=run_id, path=state_path, network_budget=budget(),
        )
        assert ledger is not None
        return ledger

    original = await open_ledger()
    assembly = configure_gated(
        shell, engagement_model.scope, original, FakeEvidenceStore(), provider_config=provider,
    )
    assert assembly.descriptors
    window = await original.request_window(run_id=run_id, instance_id="original-holder",
                                           epoch=1, caps=caps())
    assert isinstance(window, NetworkGrant)
    assert (await original.admit_delivery(window_id=window.window_id,
        daemon_session_id="original-daemon", seq=1, flow_key="original-flow")).charged_delivery
    args = dict(window_id=window.window_id, meter=None, verified_closed=False)
    settled = await original.settle_window(**args)
    assert settled.released_bytes == settled.released_deliveries == 0
    with sqlite3.connect(state_path) as db:
        before = {table: db.execute(f"SELECT * FROM {table}").fetchall()
                  for table in ("network_pools", "network_windows", "network_seq_log")}
    reopened = await open_ledger()
    assert await reopened.settle_window(**args) == settled
    refused = await reopened.request_window(run_id=run_id, instance_id="original-holder",
                                            epoch=2, caps=caps())
    assert isinstance(refused, GrantRefused) and refused.code == "quarantined"
    with sqlite3.connect(state_path) as db:
        after = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in before}
    assert before == after
    assert not list(tmp_path.rglob("network_grants.sqlite3"))


async def test_stop_drains_parallel_calls_and_keeps_unconfirmed_resources():
    llm = FakeLLMClient(structured_responses=[
        {"questions": [], "reason": "Tasks already registered"} for _ in range(8)
    ])
    tools = gateway(resource=True)
    tools.release.clear()
    async with build_central_development(
        limits()[0], llm=llm, tools=tools, settings=limits()[1],
    ) as selection:
        for index in range(2):
            await register(selection, f"registration-{index}")
        for index, task in enumerate(await selection.deps.central_state.list_tasks()):
            llm.native_responses_by_task[str(task.task_id)] = [
                discover(TOOL), turn(inspect(f"call-{index}", account=f"session-{index}")),
            ]
        running = asyncio.create_task(selection.run())
        await asyncio.wait_for(tools.wait_for_invocations(2), timeout=2)
        await selection.deps.central_state.append(IntentRecord(
            run_id=selection.runtime.run_id, correlation_id="stop-workers",
            intent=OperatorIntent(
                intent_id="stop-workers", mission_id=selection.runtime.mission_id,
                intent_type="stop", instruction="Stop fixture workers",
                created_at=datetime.now(timezone.utc), provenance=Provenance(source="operator"),
            ),
        ))
        result = await asyncio.wait_for(running, timeout=2)
        assert result.termination_reason == TerminationReason.OPERATOR_STOPPED
        executions = await selection.deps.central_state.list_executions()
        assert len(executions) == 2
        assert all(item.state is ExecutionState.UNKNOWN and item.resource_execution_pending
                   for item in executions)
        assert await selection.remaining_budget() == limits()[0].execution_budget.max_actions - 2
        assert all(row["launch_stage"] == "returned"
                   for row in await selection.deps.central_state.activation_inventory())


async def test_never_entered_recovery_keeps_original_actor_and_checks_any_activity():
    llm, tools = FakeLLMClient(), gateway()
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=limits()[1],
    ) as selection:
        await register(selection)
        workers = await ready_workers(selection.deps, selection.runtime)
        originals = await selection.driver.recover_unstarted_activations(
            str(selection.runtime.run_id), checkpoint_instances=set(),
        )
        assert len(originals) == 1 and originals[0].instance_id == workers[0].instance_id
        ledger = selection.deps.central_state.request_ledger()
        request = await ledger.reserve(str(workers[0].run_id), owner=workers[0].work_owner,
                                       kind="fixture:reserved")
        await ledger.release_undispatched(str(workers[0].run_id), request)
        with pytest.raises(StateConflict, match="never_entered_recovery_not_proven"):
            await selection.driver.recover_unstarted_activations(
                str(selection.runtime.run_id), checkpoint_instances=set(),
            )
        assert tools.invocations == []


@pytest.mark.parametrize("corruption", ["proposal", "evidence"])
async def test_recovery_refuses_changed_proposal_or_damaged_evidence(corruption):
    from redteam.orchestration.central_recovery import resume_intent

    llm, tools = FakeLLMClient(), gateway(risk="high")
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=limits()[1],
    ) as selection:
        original, = await start(selection, llm)
        await selection.deps.control_plane.resolve_approval(
            original.pending_approval_id, ApprovalStatus.APPROVED, "fixture:operator",
        )
        execution, = await selection.deps.central_state.list_executions()
        if corruption == "proposal":
            snapshot = await selection.driver.runner_for(original)._latest_snapshot()
            dispatch = snapshot.values["native_calls"][0].dispatch
            local = {
                "runtime": original, "action_proposal": dispatch.proposal.model_copy(update={
                    "arguments": {"host": "unit.local", "account": "changed"},
                }), "plan_step": dispatch.plan_step, "prepared_invocation": dispatch.invocation,
            }
            with pytest.raises(StateConflict, match="approval_original_proposal_mismatch"):
                await resume_intent(selection.deps, local, execution)
        else:
            path = selection.deps.central_state.path.parent / "evidence" / (
                f"{original.run_id}.jsonl"
            )
            records = [json.loads(line) for line in path.read_text().splitlines()]
            for record in records:
                if record["evidence_id"] == str(execution.proposal_evidence_id):
                    record["summary"] = "Changed fixture original"
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            _, outcome = await selection.driver.recover_central_instance(
                original, transfer_owner=True,
            )
            assert not outcome.ready
            assert outcome.errors[str(execution.invocation_id)] == (
                "evidence_file_index_disagreement"
            )
        assert tools.invocations == []
        task = await selection.deps.central_state.get_task(str(original.active_task_id))
        assert task.ownership_version == original.task_ownership_version
