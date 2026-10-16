"""A live Run continues an approved actor while planning and its peer are active."""

import asyncio
import json

from redteam.app.central_development import build_central_development
from redteam.core.contracts.execution import ExecutionState
from redteam.core.enums import ApprovalStatus
from redteam.core.ports.llm import NativeCompletion
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.specialist_planner import SpecialistPlanner
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


async def test_approved_returned_actor_continues_during_blocked_planner(monkeypatch):
    llm = FakeLLMClient()
    tools = gateway(risk="high")
    # Hold the approved call after gateway entry so its original remains visible.
    tools.release.clear()

    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=limits()[1],
    ) as selection:
        approved_task, = await register(selection, "approval-actor")
        peer_task, = await register(selection, "independent-peer")
        llm.native_responses_by_task[approved_task] = [
            discover(TOOL),
            turn(inspect("approval-original")),
            NativeCompletion(text="Original approved result", total_tokens=1),
        ]
        llm.native_responses_by_task[peer_task] = [
            discover(TOOL),
            NativeCompletion(text="Independent peer result", total_tokens=1),
        ]

        planner_entered = asyncio.Event()
        release_planner = asyncio.Event()
        peer_entered = asyncio.Event()
        release_peer = asyncio.Event()
        original_complete_native = llm.complete_native

        async def hold_peer_response(messages, *args, **kwargs):
            for message in reversed(messages):
                if message.get("role") != "user":
                    continue
                try:
                    context = json.loads(message.get("content") or "{}")
                except (TypeError, ValueError):
                    continue
                task_id = (context.get("active_task") or {}).get("task_id")
                if task_id == peer_task:
                    peer_entered.set()
                    await release_peer.wait()
                break
            return await original_complete_native(messages, *args, **kwargs)

        monkeypatch.setattr(llm, "complete_native", hold_peer_response)
        advances = 0

        async def hold_post_start_planner(_planner, runtime):
            nonlocal advances
            advances += 1
            if advances == 1:
                # Tasks are registered before Run starts; skip initial planning.
                return []
            planner_entered.set()
            await release_planner.wait()
            return []

        monkeypatch.setattr(SpecialistPlanner, "advance", hold_post_start_planner)

        returned = []
        drive_ready_tasks = selection.driver.drive_ready_tasks

        async def observe_returns(*args, **kwargs):
            async for result in drive_ready_tasks(*args, **kwargs):
                returned.append(result)
                yield result

        selection.driver.drive_ready_tasks = observe_returns
        running = asyncio.create_task(selection.run())
        try:
            await asyncio.wait_for(
                asyncio.gather(planner_entered.wait(), peer_entered.wait()), timeout=10,
            )
            assert tools.invocations == []

            executions = await selection.deps.central_state.list_executions()
            approved_execution, = [item for item in executions if item.wait_reason == "approval"]
            approved_id = approved_execution.approval_ref
            assert approved_id
            original_result, = [
                item for item in returned
                if str(item.instance_id) == str(approved_execution.instance_id)
            ]
            original_runtime = original_result.runtime
            original_runner = selection.driver.runner_for(original_runtime)
            assert await original_runner.checkpoint_exists(original_runtime)
            original_identity = (
                str(approved_execution.invocation_id),
                approved_execution.ownership_version,
                approved_execution.approval_fingerprint,
            )

            await selection.deps.control_plane.resolve_approval(
                approved_id, ApprovalStatus.APPROVED, "fixture:operator",
            )
            # The planner remains held and the peer remains inside its model call.
            # The exact approved invocation must still resume in this Run.
            await asyncio.wait_for(tools.wait_for_invocations(1), timeout=2)
            assert len(tools.invocations) == 1
            approved_call, = [
                item for item in tools.invocations if str(item.invocation_id)
                == str(approved_execution.invocation_id)
            ]
            assert approved_call.arguments == {"host": "unit.local", "account": "a"}
            current = await selection.deps.central_state.get_execution(
                str(approved_execution.invocation_id),
            )
            assert (
                str(current.invocation_id), current.ownership_version,
                current.approval_fingerprint,
            ) == original_identity
            assert current.state is ExecutionState.RUNNING
            assert await original_runner.checkpoint_exists(original_runtime)
        finally:
            release_planner.set()
            release_peer.set()
            tools.release.set()
            try:
                final = await asyncio.wait_for(running, timeout=15)
            except BaseException:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                raise

        assert final.termination_reason in {
            TerminationReason.INVESTIGATION_HELD,
            TerminationReason.BUDGET_EXHAUSTED,
        }
        assert len(tools.invocations) == 1
        assert len({str(item.invocation_id) for item in tools.invocations}) == 1
        assert sum(
            str(item.invocation_id) == str(approved_execution.invocation_id)
            for item in tools.invocations
        ) == 1
        continued = [
            item for item in returned
            if str(item.instance_id) == str(original_runtime.instance_id)
        ]
        assert len(continued) == 2
        assert all(
            item.runtime.work_owner == original_runtime.work_owner
            and item.runtime.task_ownership_version == original_runtime.task_ownership_version
            for item in continued
        )
