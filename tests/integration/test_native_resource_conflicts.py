"""Real native bind/Control/execute/update with shared-resource work leases.

Only model and gateway are synthetic. No bank/device/browser is contacted.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.tool import ToolResourceBinding
from redteam.core.convert import build_tool_invocation
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import RunId
from redteam.core.ports.llm import NativeCompletion
from redteam.core.ports.tool_gateway import ToolStartRejected
from redteam.orchestration.checkpointing import build_checkpointer
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.nodes.execute import _invoke_with_semaphore
from redteam.orchestration.nodes.react import _bind_call, tools_node
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.state import NativeCallState, TerminationReason, initial_graph_state
from redteam.orchestration.work.models import WorkStatus
from redteam.orchestration.work.sqlite import SQLiteWorkStore
from tests.integration import _engine_llm_harness as h
from tests.integration.test_instance_driver_react import (
    _engagement,
    _react_deps,
    _runtime,
    _TimingGateway,
)
from tests.unit.test_resource_conflicts import resource_work


class ResourceGateway(_TimingGateway):
    def __init__(self, *, uncertain=False):
        super().__init__(dwell=0.25)
        tool = h.inventory_descriptor()
        tool.input_schema["properties"]["profile"] = {"type": "string"}
        tool.resource_bindings = [
            ToolResourceBinding(namespace="browser.profile", argument="profile")
        ]
        self._tools = [tool]
        self.uncertain = uncertain

    async def invoke(self, invocation):
        result = await super().invoke(invocation)
        if self.uncertain:
            return result.model_copy(
                update={
                    "success": False,
                    "metadata": {
                        "execution_started": True,
                        "execution_uncertain": True,
                    },
                }
            )
        return result


async def world(tmp_path, backend, *, uncertain=False, wait=2.0):
    engagement = _engagement("a.local", "b.local", "c.local")
    gateway = ResourceGateway(uncertain=uncertain)
    deps = _react_deps(engagement, gateway)
    deps = replace(
        deps,
        settings=deps.settings.model_copy(
            update={
                "resource_wait_timeout_seconds": wait,
            }
        ),
    )
    if backend == "sqlite":
        deps = replace(deps, work_store=SQLiteWorkStore(tmp_path / "resource-work.sqlite3"))
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    return deps, gateway


async def admitted(deps, instance, host, profile="shared"):
    runtime = _runtime(
        "run-resources", str(deps.engagement.engagement_id), instance, "observe the approved target"
    )
    call = NativeCallState(
        call_id="original-" + instance,
        tool_id=h.INVENTORY_TOOL,
        name=exposed_tool_name(h.INVENTORY_TOOL),
        arguments={"host": host, "profile": profile},
    )
    await _bind_call(
        call,
        deps=deps,
        runtime=runtime,
        context={"tools_by_role": {"recon-agent": [h.INVENTORY_TOOL]}},
    )
    return {"runtime": runtime, "native_calls": [call], "native_messages": []}


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_same_profile_serializes_different_targets_without_model_retries(tmp_path, backend):
    deps, gateway = await world(tmp_path, backend)
    states = [await admitted(deps, "a", "a.local"), await admitted(deps, "b", "b.local")]
    results = await asyncio.gather(*(tools_node(state, deps=deps) for state in states))
    assert len(gateway.invocations) == 2
    assert len(gateway.intervals) == 2
    first, second = sorted(gateway.intervals)
    assert first[1] <= second[0]
    assert deps.llm_client.native_turns == 0  # no repeated model decisions while waiting
    for result in results:
        assert result["runtime"].termination_reason is None
        assert result["native_calls"] == []
    works = await deps.work_store.list_items(run_id=RunId("run-resources"))
    assert len(works) == 2
    assert {work.model_tool_call_id for work in works} == {"original-a", "original-b"}
    assert all(
        work.status is WorkStatus.SUCCEEDED and not work.resource_execution_pending
        for work in works
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_independent_profiles_progress_while_shared_profile_waits(tmp_path, backend):
    deps, gateway = await world(tmp_path, backend)
    states = [
        await admitted(deps, "a", "a.local"),
        await admitted(deps, "b", "b.local"),
        await admitted(deps, "c", "c.local", "independent"),
    ]
    await asyncio.gather(*(tools_node(state, deps=deps) for state in states))
    assert len(gateway.invocations) == 3
    # One independent execution overlapped the first shared-profile execution.
    first, independent, last = sorted(gateway.intervals)
    assert independent[0] < first[1] <= last[0]
    assert deps.llm_client.native_turns == 0


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_unknown_result_holds_next_original_request_instead_of_unlocking(tmp_path, backend):
    deps, gateway = await world(tmp_path, backend, uncertain=True)
    a = await admitted(deps, "a", "a.local")
    b = await admitted(deps, "b", "b.local")
    await tools_node(a, deps=deps)
    result = await tools_node(b, deps=deps)
    assert len(gateway.invocations) == 1
    assert result["runtime"].termination_reason == TerminationReason.RESOURCE_WAIT
    (call,) = result["native_calls"]
    assert not call.completed and call.call_id == "original-b" and call.dispatch is not None
    assert "unresolved original" in call.feedback["error"]
    assert (await deps.work_store.get(call.dispatch.work_id)).status is WorkStatus.PENDING


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_wait_limit_preserves_call_and_explicit_retry_uses_same_work(tmp_path, backend):
    deps, gateway = await world(tmp_path, backend, wait=0.05)
    state = await admitted(deps, "a", "a.local")
    original_id = state["native_calls"][0].dispatch.work_id
    work = await deps.work_store.get(original_id)
    blocker = resource_work("other-owner", key=work.resource_keys[0], run="other-run")
    await deps.work_store.add(blocker)
    now = datetime.now(timezone.utc)
    await deps.work_store.acquire(
        blocker.work_id, owner="other-run", now=now, ttl=timedelta(seconds=5)
    )
    held = await tools_node(state, deps=deps)
    assert held["runtime"].termination_reason == TerminationReason.RESOURCE_WAIT
    assert not gateway.invocations
    assert not held["native_calls"][0].completed
    assert await deps.work_store.release(blocker.work_id, owner="other-run", now=now)
    # Explicit guarded-resume preparation, never a new bind/claim/model call.
    held["runtime"].termination_reason = None
    result = await tools_node(held, deps=deps)
    assert len(gateway.invocations) == 1 and not result["native_calls"]
    works = await deps.work_store.list_items(run_id=RunId("run-resources"))
    assert [row.work_id for row in works] == [original_id]


async def test_pending_approval_releases_unstarted_resource_without_autoapproval(tmp_path):
    deps, gateway = await world(tmp_path, "memory")
    state = await admitted(deps, "a", "a.local")
    deps.control_plane.default_verdict = PolicyVerdict.REQUIRE_APPROVAL
    result = await tools_node(state, deps=deps)
    assert not gateway.invocations
    work = await deps.work_store.get(result["native_calls"][0].dispatch.work_id)
    assert work.status is WorkStatus.PENDING and not work.resource_execution_pending


async def test_resource_metadata_drift_is_rejected_before_provider_entry(tmp_path):
    deps, gateway = await world(tmp_path, "memory")
    state = await admitted(deps, "a", "a.local")
    gateway._tools[0].resource_bindings = [
        ToolResourceBinding(namespace="browser.profile", key="different-backend")
    ]
    await tools_node(state, deps=deps)
    assert not gateway.invocations


@pytest.mark.parametrize("work_id", [None, "missing-work"])
async def test_declared_resource_without_work_ledger_never_enters_provider(tmp_path, work_id):
    deps, gateway = await world(tmp_path, "memory")
    state = await admitted(deps, "a", "a.local")
    dispatch = state["native_calls"][0].dispatch
    proposal = dispatch.proposal
    decision = PolicyDecision(action_id=proposal.action_id, verdict=PolicyVerdict.ALLOW,
                              evaluated_at=datetime.now(timezone.utc))
    invocation = build_tool_invocation(proposal, decision, tool_id=proposal.tool_id)
    with pytest.raises(ToolStartRejected, match="resource work unavailable"):
        await _invoke_with_semaphore(
            deps, invocation, runtime=state["runtime"], step=dispatch.plan_step,
            proposal=proposal, decision=decision, work_id=work_id,
        )
    assert not gateway.invocations


async def test_cancellation_while_waiting_does_not_create_or_execute_another_call(tmp_path):
    deps, gateway = await world(tmp_path, "memory")
    state = await admitted(deps, "a", "a.local")
    original_id = state["native_calls"][0].dispatch.work_id
    work = await deps.work_store.get(original_id)
    blocker = resource_work("other-owner", key=work.resource_keys[0], run="other-run")
    await deps.work_store.add(blocker)
    await deps.work_store.acquire(
        blocker.work_id, owner="other-run", now=datetime.now(timezone.utc), ttl=timedelta(seconds=5)
    )
    waiting = asyncio.create_task(tools_node(state, deps=deps))
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not gateway.invocations
    (pending,) = await deps.work_store.list_items(run_id=RunId("run-resources"))
    assert pending.work_id == original_id and pending.attempt == 0
    assert pending.status is WorkStatus.PENDING


class HoldAfterOriginal:
    """A fresh MODEL is allowed only AFTER the restored original executed."""

    def __init__(self, gateway):
        self.gateway = gateway
        self.native_turns = 0

    async def complete_native(self, **kwargs):
        assert len(self.gateway.invocations) == 1
        self.native_turns += 1
        return NativeCompletion(tool_calls=(), text="holding: original result collected")


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("disposition", ["released", "cancelled_request", "unknown_blocker"])
async def test_real_checkpoint_resume_executes_original_batch_not_fresh_model(
    tmp_path, backend, disposition,
):
    deps, gateway = await world(tmp_path, backend, wait=0.05)
    model = HoldAfterOriginal(gateway)
    deps = replace(deps, llm_client=model)
    if backend == "sqlite":
        deps = replace(deps, settings=deps.settings.model_copy(update={
            "checkpoint_backend": "sqlite", "checkpoint_path": str(tmp_path / "checkpoints.db"),
        }))
    state = await admitted(deps, "a", "a.local")
    call = state["native_calls"][0]
    original = call.dispatch
    work = await deps.work_store.get(original.work_id)
    blocker = resource_work("other-owner", key=work.resource_keys[0], run="other-run")
    await deps.work_store.add(blocker)
    now = datetime.now(timezone.utc)
    await deps.work_store.acquire(blocker.work_id, owner="other-run", now=now,
                                  ttl=timedelta(seconds=5))
    shared = (build_checkpointer(enabled=True, registry=deps.state_codecs)
              if backend == "memory" else None)
    async with AgentLoopRunner(deps, checkpointer=shared) as runner:
        assert await runner._binding_for(state["runtime"], fresh=True)
        seeded = initial_graph_state(state["runtime"])
        seeded["native_calls"] = state["native_calls"]
        held = await runner._run_state(state["runtime"], seeded)
    assert held.termination_reason == TerminationReason.RESOURCE_WAIT
    assert not gateway.invocations and model.native_turns == 0
    if disposition == "unknown_blocker":
        assert await deps.work_store.mark_execution_started(
            blocker.work_id, owner="other-run", now=now,
        )
        assert await deps.work_store.complete(
            blocker.work_id, owner="other-run", status=WorkStatus.FAILED, now=now,
        )
    else:
        assert await deps.work_store.release(blocker.work_id, owner="other-run", now=now)
    if disposition == "cancelled_request":
        await deps.work_store.abandon_run(
            held.run_id, owner=held.work_owner, now=now, reason="explicit_test_cancellation",
        )
    # A new runner loads an actual serde checkpoint, not a hand-built resume state.
    async with AgentLoopRunner(deps, checkpointer=shared) as restarted:
        recovered = await restarted.recover_runtime(held.run_id, instance_id=held.instance_id)
        assert recovered is not None
        final = await restarted.resume_recovered(recovered)
    if disposition != "released":
        assert final.termination_reason == TerminationReason.RESOURCE_WAIT
        assert not gateway.invocations and model.native_turns == 0
        return
    assert final.actions_executed == 1 and model.native_turns == 1
    assert len(gateway.invocations) == 1
    assert gateway.invocations[0].action_id == original.proposal.action_id
    (row,) = await deps.work_store.list_items(run_id=held.run_id)
    assert row.work_id == original.work_id and row.model_tool_call_id == "original-a"
    assert row.status is WorkStatus.SUCCEEDED and not row.resource_execution_pending
