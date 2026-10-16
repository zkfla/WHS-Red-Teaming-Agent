"""An unsettled original call must not starve independent ready work on resume."""

from redteam.app.central_development import build_central_development
from redteam.app.central_run import bind_run_metadata
from redteam.core.contracts.execution import ExecutionState
from redteam.core.enums import ApprovalStatus, MissionStatus, TaskStatus
from redteam.core.ports.llm import NativeCompletion
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_runtime import ready_workers, tool_limit
from redteam.orchestration.state import TerminationReason
from tests.integration.test_central_development import (
    TOOL,
    discover,
    gateway,
    inspect,
    register,
    turn,
)
from tests.integration.test_central_parallel import limits


async def test_resume_runs_ready_independent_task_without_replaying_unknown_original():
    selected, configured = limits(calls=2)
    llm = FakeLLMClient(canned_structured={
        "questions": [],
        "reason": "Keep the investigation open while independent work remains.",
        "termination": "held",
        "resume_conditions": ["A ready independent task can run"],
    })
    tools = gateway(risk="high", resource=True)
    # The first approved call remains ambiguous; later independent calls succeed.
    tools.responses[0].metadata["execution_uncertain"] = True

    async with build_central_development(
        selected, llm=llm, tools=tools, settings=configured,
    ) as selection:
        await bind_run_metadata(selection.deps)
        assert tool_limit(selection.deps) == 2
        first_task_id, = await register(selection, "registration-unknown")
        llm.native_responses_by_task[first_task_id] = [
            discover(TOOL),
            turn(inspect("unknown-original", account="session-a")),
            NativeCompletion(text="Inspect the original response", total_tokens=1),
        ]
        first_worker, = await ready_workers(
            selection.deps, selection.runtime, max_instances=1,
        )
        async for event in selection.driver.drive([first_worker]):
            original = event.runtime
            assert event.kind == "completed", event.error

        pending, = await selection.deps.central_state.list_executions()
        original_invocation_id = str(pending.invocation_id)
        original_approval_id = original.pending_approval_id
        assert pending.state is ExecutionState.PENDING
        assert pending.wait_reason == "approval"
        assert original_approval_id == str(pending.approval_ref)
        assert tools.invocations == []

        await selection.deps.control_plane.resolve_approval(
            original_approval_id, ApprovalStatus.APPROVED, "fixture:operator",
        )
        recovered, proof = await selection.driver.recover_central_instance(
            original, transfer_owner=False,
        )
        assert proof.ready, proof.errors
        await selection.driver.runner_for(recovered).resume_recovered(recovered)

        unknown = await selection.deps.central_state.get_execution(original_invocation_id)
        assert unknown is not None
        assert unknown.state is ExecutionState.UNKNOWN
        assert unknown.resource_execution_pending
        assert unknown.approval_ref == original_approval_id
        assert [str(item.invocation_id) for item in tools.invocations] == [
            original_invocation_id,
        ]
        assert tools.invocations[0].timeout_seconds == 20

        second_task_id, = await register(selection, "registration-independent")
        second_task = await selection.deps.central_state.get_task(second_task_id)
        assert second_task.status is TaskStatus.READY
        llm.native_responses_by_task[second_task_id] = [
            discover(TOOL),
            turn(inspect("independent-ready", account="session-b")),
            NativeCompletion(text="Inspect the independent response", total_tokens=1),
        ]

        # Resume should admit the ready task despite the first task's held
        # external resource. The new action must still await its own approval.
        await selection.resume()
        executions = await selection.deps.central_state.list_executions()
        assert len(executions) == 2
        ready_call = next(item for item in executions
                          if str(item.invocation_id) != original_invocation_id)
        ready_invocation_id = str(ready_call.invocation_id)
        ready_approval_id = str(ready_call.approval_ref)
        assert ready_call.state is ExecutionState.PENDING
        assert ready_call.wait_reason == "approval"
        assert ready_approval_id
        assert ready_call.resource_keys
        assert set(unknown.resource_keys).isdisjoint(ready_call.resource_keys)
        assert [str(item.invocation_id) for item in tools.invocations] == [
            original_invocation_id,
        ]
        assert (await selection.deps.control_plane.inspect_approval(
            original_approval_id,
        )).status is ApprovalStatus.APPROVED

        await selection.deps.control_plane.resolve_approval(
            ready_approval_id, ApprovalStatus.APPROVED, "fixture:operator",
        )
        final = await selection.resume()

        # The approved ready call runs under its original invocation and the
        # unresolved original remains held, so the mission cannot be completed.
        unknown = await selection.deps.central_state.get_execution(original_invocation_id)
        completed = await selection.deps.central_state.get_execution(ready_invocation_id)
        assert unknown is not None and unknown.state is ExecutionState.UNKNOWN
        assert unknown.resource_execution_pending
        assert completed is not None and completed.state is ExecutionState.SUCCEEDED
        assert completed.approval_ref == ready_approval_id
        assert [str(item.invocation_id) for item in tools.invocations] == [
            original_invocation_id, ready_invocation_id,
        ]
        assert tools.invocations[1].timeout_seconds == 20
        final = await selection.settle(final)
        mission = await selection.deps.central_state.get_mission(selection.runtime.mission_id)
        assert mission.status is MissionStatus.ACTIVE
        assert final.termination_reason is not TerminationReason.GOAL_ACHIEVED
        assert not await selection.deps.central_state.transaction(
            lambda tx: tx.settlement_closed(), write=False,
        )
