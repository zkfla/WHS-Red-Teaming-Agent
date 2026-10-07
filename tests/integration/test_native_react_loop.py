"""Native ReAct loop integration tests (scripted model, no network).

Drives the actual compiled model -> tools -> settle graph with the fake
real tool gateway (``DummyToolGateway``). Every turn is an explicit
scripted native completion through the production path -- nothing runs
on a JSON/structured fallback: the scripted model answers
``complete_native`` only, and any ``complete_structured`` call outside
the verifier meaning route raises instead of returning a soft answer.

Covered execution contracts (observable semantic outcomes, not
implementation mirrors):
- partial malformed args keep the successful sibling: one call with a
  per-call argument error completes as that call's error without
  executing, while the valid sibling executes through the real
  Control -> Gateway -> evidence path.
- no fabricated PathPlan: the bound execution carrier keeps the
  provider call id and origin version with empty path linkage.
- feedback carries evidence ids, not raw payloads: tool messages cite
  stored evidence; the raw output is recoverable only through the
  evidence reader on the stored record.
- terminal text is not auto goal success: a text-only model answer
  never settles the mission, even when it claims to be finished.
- completed sibling disposal across approval resume: one native response
  carries a successful local sibling plus an approval-held external call.
  The checkpoint keeps only the held call's original envelope while the
  finished sibling keeps identity and result; resuming the approval
  executes the held call once without replaying the finished sibling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.enums import ApprovalStatus, EvidenceSourceType, MissionStatus
from redteam.core.ids import EngagementId, RunId
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.evidence_access import EvidenceReader
from redteam.orchestration.graph import build_graph
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.nodes.react import model_node
from redteam.orchestration.paths import InMemoryPathStore
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason, initial_graph_state
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from tests.fakes import DummyToolGateway, FakeControlPlane, FakeEvidenceStore
from tests.integration import _engine_llm_harness as h

NODE_HOST = "a.local"
RAW_MARKER = "native-loop-raw-payload-marker"


class ScriptedNativeLLM:
    """Answer each native model turn from a queue, in order.

    Only ``complete_native`` drives the loop. ``complete_structured``
    answers the verifier meaning route (the one structured call the
    execution path legitimately makes); any other structured route
    raises so a silent JSON fallback can never stand in for a native
    turn. An exhausted queue falls back to a text-only completion,
    which the loop must treat as a non-settling turn.
    """

    def __init__(self, native_responses: list[NativeCompletion]) -> None:
        self._native = list(native_responses)
        self.native_turns: list[dict[str, Any]] = []

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        self.native_turns.append({"messages": messages, "tools": tools})
        if self._native:
            return self._native.pop(0)
        return NativeCompletion(tool_calls=(), text="no further tool calls")

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        raise AssertionError("native loop must not use free-text completion")

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        props = response_schema.get("properties", {})
        if "claim_supported" in props:
            return {
                "meaning": "scripted",
                "outcome": "success",
                "reason": "scripted meaning",
                "confidence": 0.8,
                "claim_supported": True,
            }
        raise AssertionError("no scripted answer for structured route")


def _engagement() -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_native_loop"),
        name="native-loop",
        scope=EngagementScope(allowed_hosts=[NODE_HOST]),
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


def _native_deps(
    engagement: Engagement,
    llm: ScriptedNativeLLM,
    *,
    settings: OrchestrationSettings | None = None,
) -> RuntimeDeps:
    gateway = DummyToolGateway(
        tools=[h.inventory_descriptor()],
        output={"inventory": "ok", "echo": RAW_MARKER},
    )
    return RuntimeDeps(
        control_plane=FakeControlPlane(),
        tool_gateway=gateway,
        event_bus=InMemoryEventBus(),
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=FakeEvidenceStore(),
        llm_client=llm,  # type: ignore[arg-type]
        engagement=engagement,
        agents=default_registry(),
        tool_catalog=gateway,
        work_store=InMemoryWorkStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        operator_intent_store=None,
        settings=settings or OrchestrationSettings(max_steps=4, checkpointing=False),
    )


def deps_runtime(run_id: RunId, engagement: Engagement) -> RuntimeState:
    """Goal-only runtime: no mission bootstrap, purpose comes from current_goal."""
    return RuntimeState(
        run_id=run_id,
        engagement_id=engagement.engagement_id,
        current_goal="map the in-scope host",
        remaining_budget=engagement.execution_budget.max_actions,
    )


def _inventory_call(call_id: str, **arguments: Any) -> NativeToolCall:
    return NativeToolCall(
        call_id=call_id,
        name=exposed_tool_name(h.INVENTORY_TOOL),
        arguments=dict(arguments),
    )


async def test_rejected_call_does_not_retain_secret_inputs_in_checkpoint() -> None:
    from redteam.orchestration.checkpointing import _serializer, legacy_allowlist

    engagement = _engagement()
    secret = "SyntheticCheckpointPassword"
    llm = ScriptedNativeLLM([
        NativeCompletion(tool_calls=(NativeToolCall(
            call_id="unknown_call", name="unregistered_tool",
            arguments={"password": secret},
        ),))
    ])
    deps = _native_deps(engagement, llm)
    run_id = RunId("run_native_rejected_secret")
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement, run_id=run_id)
    state = await model_node(initial_graph_state(deps_runtime(run_id, engagement)), deps=deps)
    serializer = _serializer(legacy_allowlist())
    stored = serializer.dumps_typed(state)
    restored = serializer.loads_typed(stored)
    assert secret.encode() not in stored[1]
    assert restored["native_calls"][0].arguments is None
    assert restored["native_calls"][0].feedback["ok"] is False
    assert isinstance(deps.tool_gateway, DummyToolGateway)
    assert deps.tool_gateway.invocations == []


async def test_native_target_resolution_preserves_distinct_concrete_resources() -> None:
    from redteam.core.contracts.target import UrlTarget
    from redteam.core.contracts.tool import ToolDescriptor
    from redteam.core.ids import ToolId
    from redteam.orchestration.knowledge_validation import InternalAssessment, InternalTarget
    from redteam.orchestration.native_binding import resolve_native_target

    engagement = _engagement()
    deps = _native_deps(engagement, ScriptedNativeLLM([]))
    first = UrlTarget(url=f"https://{NODE_HOST}/orders/1")
    second = UrlTarget(url=f"https://{NODE_HOST}/orders/2")
    material = InternalAssessment(targets={
        "first": InternalTarget(node_id="first", execution_target=first),
        "second": InternalTarget(node_id="second", execution_target=second),
    })
    tool = ToolDescriptor(
        tool_id=ToolId("synthetic.resource_reader"), server_id="synthetic",
        tool_name="resource_reader", target_types=["url"],
    )
    resolved = await resolve_native_target(
        deps, tool, {"target_ref": first.url}, material,
    )
    assert resolved.url == first.url


async def test_local_tool_duration_halt_reaches_saved_runtime_diagnostic() -> None:
    from redteam.orchestration.cognition import ReasoningHalted
    from redteam.orchestration.local_tools import LocalToolDescriptor

    identity = "synthetic.local_halt"
    async def halt(arguments: Any, context: Any) -> Any:
        raise ReasoningHalted("duration_budget_exhausted", termination_reason="budget_exhausted")

    engagement = _engagement()
    llm = ScriptedNativeLLM([NativeCompletion(tool_calls=(NativeToolCall(
        call_id="duration_call", name=exposed_tool_name(identity), arguments={},
    ),))])
    deps = _native_deps(engagement, llm)
    deps.local_tools.register(LocalToolDescriptor(
        tool_id=identity, description="Synthetic duration halt", input_schema={"type": "object"},
        contribution_id="synthetic-halt", version="1",
    ), halt)
    run_id = RunId("run_native_duration")
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement, run_id=run_id)
    result = await build_graph(deps).ainvoke(
        initial_graph_state(deps_runtime(run_id, engagement)),
        config={"recursion_limit": deps.settings.effective_recursion_limit},
    )
    runtime = result["runtime"]
    assert runtime.termination_reason == TerminationReason.BUDGET_EXHAUSTED
    assert runtime.planning_diagnostic.termination_reason == "budget_exhausted"
    assert runtime.planning_diagnostic_history[-1] == runtime.planning_diagnostic
    assert len(llm.native_turns) == 1
    records = await deps.evidence_store.list_for_run(run_id)
    receipts = [
        r.metadata for r in records if r.metadata.get("record_kind") == "runtime_tool_result"
    ]
    assert receipts[0]["result"]["ok"] is False
    diagnostic = receipts[0]["result"]["payload"]["diagnostic"]
    assert diagnostic["termination_reason"] == "budget_exhausted"


async def test_partial_malformed_args_keeps_successful_sibling_without_path_plan() -> None:
    """One malformed call must not sink its valid sibling or invent a path."""
    engagement = _engagement()
    bad = NativeToolCall(
        call_id="call_bad_1",
        name=exposed_tool_name(h.INVENTORY_TOOL),
        arguments=None,
        argument_error="invalid_arguments",
    )
    llm = ScriptedNativeLLM(
        [NativeCompletion(tool_calls=(bad, _inventory_call("call_good_1")), text="")]
    )
    deps = _native_deps(engagement, llm)
    gateway = deps.tool_gateway
    assert isinstance(gateway, DummyToolGateway)
    run_id = RunId("run_native_partial")
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement, run_id=run_id)
    runtime = deps_runtime(run_id, engagement)
    graph = build_graph(deps)
    final_state = await graph.ainvoke(
        initial_graph_state(runtime),
        config={"recursion_limit": deps.settings.effective_recursion_limit},
    )
    final = final_state["runtime"]
    assert final.termination_reason != TerminationReason.GOAL_ACHIEVED

    # Only the valid sibling reached the real gateway; the malformed call
    # completed as its own error and never executed.
    assert [str(inv.tool_id) for inv in gateway.invocations] == [h.INVENTORY_TOOL]

    # The raw tool output landed in exactly one stored TOOL record.
    tool_records = [
        record
        for record in await deps.evidence_store.list_for_run(final.run_id)
        if record.source_type is EvidenceSourceType.TOOL
        and isinstance(record.metadata.get("tool_result"), dict)
    ]
    assert len(tool_records) == 1
    stored_id = str(tool_records[0].evidence_id)

    # No investigation path was fabricated for the native carrier: the
    # admitted work keeps the provider call id and origin version with
    # empty path linkage.
    items = await deps.work_store.list_items(run_id=final.run_id)
    bound = [item for item in items if item.model_tool_call_id == "call_good_1"]
    assert len(bound) == 1
    assert bound[0].execution_origin_version == 1
    assert not bound[0].path_id and not bound[0].path_step_id
    assert await deps.path_store.list_paths(str(final.run_id)) == []

    # Tool messages cite the stored evidence and feedback, never the raw
    # payload: the raw marker is absent from every tool message while the
    # stored id is cited.
    tool_contents = [
        str(message.get("content", ""))
        for message in final_state.get("native_messages", [])
        if message.get("role") == "tool"
    ]
    assert tool_contents, "executed calls must pair tool messages"
    assert RAW_MARKER not in " ".join(tool_contents)
    assert stored_id in " ".join(tool_contents)

    # The raw payload is recoverable only through the evidence reader on
    # the stored original.
    read = await EvidenceReader(deps.evidence_store).read(
        evidence_id=stored_id, run_id=str(final.run_id)
    )
    assert read["status"] == "ok"
    assert RAW_MARKER in read["text"]
    assert read["provenance"]["evidence_id"] == stored_id

    # The provider-facing delivery carries the same bounded original inline,
    # without an implicit evidence.read call: the second native turn's tool
    # message embeds the raw window plus the stored provenance.
    import json as _json

    assert len(llm.native_turns) >= 2
    delivered = [
        message
        for message in llm.native_turns[1]["messages"]
        if message.get("role") == "tool"
    ]
    assert delivered, "second turn must deliver the executed tool message"
    bodies = [_json.loads(message.get("content") or "{}") for message in delivered]
    assert RAW_MARKER in _json.dumps(bodies)
    originals = [entry for body in bodies for entry in body.get("original_results", [])]
    assert originals and RAW_MARKER in " ".join(entry.get("text", "") for entry in originals)
    assert {entry["provenance"]["evidence_id"] for entry in originals} == {stored_id}
    assert all(len(entry.get("text", "")) <= 4000 for entry in originals)
    assert sum(len(entry.get("text", "")) for entry in originals) <= 8000
    assert final.evidence_reads == []


async def test_terminal_text_is_not_auto_goal_success() -> None:
    """A text-only model answer settles nothing, even when it says finished."""
    engagement = _engagement()
    llm = ScriptedNativeLLM(
        [NativeCompletion(tool_calls=(), text="finished: mission accomplished")]
    )
    deps = _native_deps(
        engagement, llm, settings=OrchestrationSettings(max_steps=3, checkpointing=False)
    )
    gateway = deps.tool_gateway
    assert isinstance(gateway, DummyToolGateway)

    final = await run_engagement(engagement, deps)
    assert final.termination_reason == TerminationReason.INVESTIGATION_HELD
    assert gateway.invocations == []
    assert final.mission_id is not None
    mission = await deps.task_graph_store.get_mission(final.mission_id)
    assert mission is not None and mission.status is not MissionStatus.COMPLETED


async def test_native_message_rounds_stay_user_first_and_paired() -> None:
    """Stored user context keeps the next request user-first with paired tools."""
    engagement = _engagement()
    llm = ScriptedNativeLLM(
        [
            NativeCompletion(tool_calls=(_inventory_call("call_round_1"),), text=""),
            NativeCompletion(tool_calls=(), text="holding for review"),
        ]
    )
    deps = _native_deps(engagement, llm)
    gateway = deps.tool_gateway
    assert isinstance(gateway, DummyToolGateway)
    run_id = RunId("run_native_rounds")
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement, run_id=run_id)
    runtime = deps_runtime(run_id, engagement)
    graph = build_graph(deps)
    final_state = await graph.ainvoke(
        initial_graph_state(runtime),
        config={"recursion_limit": deps.settings.effective_recursion_limit},
    )
    assert [str(inv.tool_id) for inv in gateway.invocations] == [h.INVENTORY_TOOL]
    assert len(llm.native_turns) >= 2
    first = llm.native_turns[0]["messages"]
    assert first and first[0].get("role") == "user"
    second = llm.native_turns[1]["messages"]
    assert second and second[0].get("role") == "user"
    user_indexes = [i for i, m in enumerate(second) if m.get("role") == "user"]
    assert user_indexes and user_indexes[0] == 0
    for i, message in enumerate(second):
        calls = message.get("tool_calls") or []
        if message.get("role") == "assistant" and calls:
            next_user = next((u for u in user_indexes if u > i), len(second))
            for call in calls:
                call_id = call.get("id")
                assert call_id, "assistant tool call must carry a provider call id"
                match = next(
                    (
                        j
                        for j in range(i + 1, next_user)
                        if second[j].get("role") == "tool"
                        and second[j].get("tool_call_id") == call_id
                    ),
                    None,
                )
                assert match is not None, f"unpaired assistant call {call_id!r}"
    assert final_state["runtime"].termination_reason == TerminationReason.INVESTIGATION_HELD


async def test_completed_sibling_disposed_across_approval_checkpoint_resume() -> None:
    """A finished sibling keeps only identity and result across an approval hold."""
    from redteam.orchestration.checkpointing import _serializer, legacy_allowlist
    from redteam.orchestration.local_tools import LocalToolDescriptor, LocalToolResult
    from redteam.orchestration.runner import AgentLoopRunner
    from redteam.orchestration.work.models import WorkStatus

    local_tool_id = "synthetic.local_note"
    local_calls: list[dict[str, Any]] = []

    async def note(arguments: Any, context: Any) -> LocalToolResult:
        local_calls.append(dict(arguments or {}))
        return LocalToolResult(ok=True, payload={"noted": True})

    local_input = {"note": "synthetic-observation"}
    llm = ScriptedNativeLLM([
        NativeCompletion(tool_calls=(
            NativeToolCall(
                call_id="call_local_1",
                name=exposed_tool_name(local_tool_id),
                arguments=dict(local_input),
            ),
            _inventory_call("call_ext_1"),
        ), text=""),
    ])
    engagement = Engagement(
        engagement_id=EngagementId("eng_native_sibling_disposal"),
        name="native-sibling-disposal",
        scope=EngagementScope(allowed_hosts=[NODE_HOST]),
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(require_approval_for=["recon"]),
        created_at=datetime.now(timezone.utc),
    )
    gateway = DummyToolGateway(
        tools=[h.inventory_descriptor()],
        output={"inventory": "ok", "echo": RAW_MARKER},
    )
    control = DefaultControlPlane({engagement.engagement_id: engagement})
    deps = RuntimeDeps(
        control_plane=control,
        tool_gateway=gateway,
        event_bus=InMemoryEventBus(),
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=FakeEvidenceStore(),
        llm_client=llm,  # type: ignore[arg-type]
        engagement=engagement,
        agents=default_registry(),
        tool_catalog=gateway,
        work_store=InMemoryWorkStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        operator_intent_store=None,
        settings=OrchestrationSettings(max_steps=10, checkpointing=True),
    )
    deps.local_tools.register(LocalToolDescriptor(
        tool_id=local_tool_id,
        description="Synthetic successful local sibling",
        input_schema={"type": "object"},
        contribution_id="synthetic-note",
        version="1",
    ), note)
    run_id = RunId("run_native_sibling_disposal")
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement, run_id=run_id)
    runner = AgentLoopRunner(deps, settings=deps.settings)
    try:
        stopped = await runner.run(deps_runtime(run_id, engagement))
        assert stopped.termination_reason == TerminationReason.APPROVAL_PENDING
        assert stopped.pending_approval_id is not None
        assert stopped.pending_action_id is not None
        assert len(local_calls) == 1
        assert gateway.invocations == []

        assert runner._graph is not None
        snapshot = await runner._graph.aget_state(runner.config())
        checkpoint = dict(snapshot.values)
        by_id = {call.call_id: call for call in checkpoint.get("native_calls", [])}
        assert set(by_id) == {"call_local_1", "call_ext_1"}
        done = by_id["call_local_1"]
        held = by_id["call_ext_1"]
        expected_compiled = {"host": NODE_HOST, "target_ref": NODE_HOST}
        assert done.completed is True
        assert done.arguments is None and done.dispatch is None
        assert done.feedback.get("ok") is True
        assert held.completed is False
        assert held.dispatch is not None
        assert str(held.dispatch.proposal.action_id) == str(stopped.pending_action_id)
        assert held.dispatch.plan_step.arguments == expected_compiled
        assert held.dispatch.proposal.arguments == expected_compiled

        serializer = _serializer(legacy_allowlist())
        stored = serializer.dumps_typed(checkpoint)
        restored = serializer.loads_typed(stored)
        revived = {call.call_id: call for call in restored["native_calls"]}
        assert revived["call_local_1"].arguments is None
        assert revived["call_local_1"].dispatch is None
        assert revived["call_local_1"].completed is True
        assert revived["call_ext_1"].completed is False
        assert revived["call_ext_1"].dispatch is not None
        assert str(revived["call_ext_1"].dispatch.proposal.action_id) == str(
            stopped.pending_action_id
        )
        assert revived["call_ext_1"].dispatch.plan_step.arguments == expected_compiled
        assert revived["call_ext_1"].dispatch.proposal.arguments == expected_compiled

        await control.resolve_approval(
            stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator"
        )
        result = await runner.reconcile_once(stopped, timeout_seconds=0.05)
        assert result.resumed is True
        assert [str(inv.tool_id) for inv in gateway.invocations] == [h.INVENTORY_TOOL]
        assert str(gateway.invocations[0].action_id) == str(stopped.pending_action_id)
        assert gateway.invocations[0].arguments == expected_compiled
        assert len(local_calls) == 1
        succeeded = await deps.work_store.list_items(run_id=run_id, status=WorkStatus.SUCCEEDED)
        assert len(succeeded) == 1
        assert succeeded[0].model_tool_call_id == "call_ext_1"
    finally:
        await runner.aclose()
