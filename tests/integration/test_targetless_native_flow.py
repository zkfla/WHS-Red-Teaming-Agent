"""Target-free calls retain actual execution lineage and bounded feedback."""

import json
from dataclasses import replace

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.control.service import DefaultControlPlane
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import RunId
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.orchestration.completion import verify_stored_origin
from redteam.orchestration.graph import build_graph
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.result_claims import check_result_review
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.semantic_completion import verify_single_origin
from redteam.orchestration.state import initial_graph_state
from tests.fakes import DummyToolGateway
from tests.integration.test_native_react_loop import (
    RAW_MARKER,
    ScriptedNativeLLM,
    _engagement,
    _native_deps,
    deps_runtime,
)


async def test_target_free_call_executes_without_inventing_graph_or_path():
    engagement = _engagement()
    llm = ScriptedNativeLLM([])
    deps = _native_deps(engagement, llm)
    original = deps.tool_catalog.all_descriptors()[0]
    descriptor = original.model_copy(update={
        "requires_target": False,
        "input_schema": {
            "type": "object", "properties": {"query": {"type": "string"}},
            "required": ["query"], "additionalProperties": False,
        },
        "argument_bindings": {},
    })
    gateway = DummyToolGateway(tools=[descriptor], output={"value": RAW_MARKER})
    deps = replace(
        deps, tool_gateway=gateway, tool_catalog=gateway,
        control_plane=DefaultControlPlane(
            {engagement.engagement_id: engagement}, outbound_descriptor_resolver=gateway.get,
        ),
    )
    llm._native = [NativeCompletion(tool_calls=(NativeToolCall(
        call_id="target-free-call", name=exposed_tool_name(str(descriptor.tool_id)),
        arguments={"query": "public reference"},
    ),))]
    run_id = RunId("run_target_free_native")
    service = AttackGraphService(deps.graph_store)
    await seed_from_engagement(service, engagement, run_id=run_id)
    before = await deps.graph_store.list_nodes()
    final = await build_graph(deps).ainvoke(initial_graph_state(deps_runtime(run_id, engagement)))
    assert len(gateway.invocations) == 1
    assert gateway.invocations[0].arguments == {"query": "public reference"}
    records = await deps.evidence_store.list_for_run(run_id)
    raw_records = [r for r in records if r.source_type is EvidenceSourceType.TOOL]
    assert len(raw_records) == 1
    record = raw_records[0]
    assert record.metadata["proposal"]["target"] is None
    assert "target_node_id" not in record.metadata
    assert record.metadata["mapping_status"] == "not_applicable"
    work = (await deps.work_store.list_items(run_id=run_id))[0]
    assert work.target_node_ids == [] and work.conflict_keys == []
    assert await verify_stored_origin(
        path_store=deps.path_store, run_id=str(run_id), m_path_id="", m_version=None,
        m_step_id="", metadata=record.metadata, record=record,
        work_store=deps.work_store, task_graph_store=deps.task_graph_store,
    ) == []
    origin, errors = await verify_single_origin(
        evidence_store=deps.evidence_store, path_store=deps.path_store,
        run_id=str(run_id), engagement_id=str(engagement.engagement_id), mission_id=None,
        evidence_id=str(record.evidence_id), work_store=deps.work_store,
        task_graph_store=deps.task_graph_store,
    )
    assert origin == record and errors == []
    assert await check_result_review(
        claims=[{"description": "A public reference was returned", "kind": "semantic",
                 "evidence_ids": [str(record.evidence_id)]}],
        evidence_ids=[str(record.evidence_id)], evidence_store=deps.evidence_store,
        graph_service=service, path_store=deps.path_store, run_id=str(run_id), mission_id=None,
        work_store=deps.work_store, task_graph_store=deps.task_graph_store,
    ) == []
    assert await deps.path_store.list_paths(str(run_id)) == []
    assert {n.id for n in await deps.graph_store.list_nodes()} == {n.id for n in before}
    assert RAW_MARKER in json.dumps(llm.native_turns[1]["messages"])
    assert RAW_MARKER not in json.dumps(final["native_messages"])
    from redteam.orchestration.recovery import reprocess_evidence

    class NoObservationReview:
        async def verify(self, request):
            raise AssertionError("a preserved observation needs no new meaning review")

    replayed = await reprocess_evidence(
        evidence_id=str(record.evidence_id), run_id=str(run_id),
        evidence_store=deps.evidence_store, graph_service=service,
        verifier=NoObservationReview(), result_mappers=deps.result_mappers,
    )
    assert replayed["status"] == "ok"
    assert len(gateway.invocations) == 1

    # A registered backend result can be collected through the same local
    # tool contract, with no submission or second gateway invocation.
    from redteam.core.contracts.tool import ToolResult
    from redteam.orchestration.job_local_tools import job_reconcile_handler
    from redteam.orchestration.local_tools import LocalToolContext
    from redteam.orchestration.long_jobs import LongJobState, LongJobStatus
    from redteam.orchestration.work.memory import InMemoryWorkStore
    from redteam.orchestration.work.models import WorkStatus

    class CompletedJob:
        reads = 0

        async def status(self, job_id):
            return LongJobStatus(job_id=job_id, state=LongJobState.SUCCEEDED)

        async def result(self, job_id):
            self.reads += 1
            return ToolResult.model_validate(record.metadata["tool_result"])

    backend = CompletedJob()
    job_store = InMemoryWorkStore()
    await job_store.add(work.model_copy(update={"status": WorkStatus.FAILED, "job_id": "job-one"}))
    job_deps = replace(deps, work_store=job_store, long_job_adapter=backend)
    context = LocalToolContext(deps=job_deps, runtime=final["runtime"])
    class UnrelatedJob(CompletedJob):
        async def result(self, job_id):
            return (await super().result(job_id)).model_copy(update={"action_id": "foreign-action"})

    unrelated = await job_reconcile_handler(
        {"work_id": work.work_id},
        LocalToolContext(
            deps=replace(job_deps, long_job_adapter=UnrelatedJob()), runtime=final["runtime"],
        ),
    )
    assert not unrelated.ok and unrelated.payload["status"] == "unrelated_job_result"
    assert (await job_store.get(work.work_id)).status is WorkStatus.FAILED
    assert len(gateway.invocations) == 1
    collected = await job_reconcile_handler({"work_id": work.work_id}, context)
    assert collected.ok and collected.payload["status"] == "collected"
    assert collected.refs and collected.last_event_id is not None
    assert backend.reads == 1 and len(gateway.invocations) == 1
    again = await job_reconcile_handler({"work_id": work.work_id}, context)
    assert again.ok and again.payload["status"] == "already_collected"
    assert backend.reads == 1 and len(gateway.invocations) == 1


@pytest.mark.parametrize("target_free", [False, True])
async def test_uncertain_execution_cannot_be_resubmitted_with_new_call_id(target_free):
    """A new provider call ID does not make an uncertain side effect safe to repeat."""
    from redteam.core.contracts.tool import ToolError
    from tests.integration import _engine_llm_harness as h

    class UncertainGateway(DummyToolGateway):
        async def invoke(self, invocation):
            result = await super().invoke(invocation)
            return result.model_copy(update={
                "success": False,
                "metadata": {"orchestration_exception": True},
                "error": ToolError(code="gateway_exception:TimeoutError", message="unknown"),
            })

    engagement = _engagement()
    descriptor = h.inventory_descriptor().model_copy(update={"requires_target": not target_free})
    llm = ScriptedNativeLLM([
        NativeCompletion(tool_calls=(NativeToolCall(
            call_id=identity, name=exposed_tool_name(str(descriptor.tool_id)), arguments={},
        ),)) for identity in ("first-call", "retry-call")
    ])
    deps = _native_deps(engagement, llm)
    gateway = UncertainGateway(tools=[descriptor])
    deps = replace(deps, tool_gateway=gateway, tool_catalog=gateway)
    run_id = RunId("run_uncertain_" + str(target_free))
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement, run_id=run_id)
    final = await build_graph(deps).ainvoke(initial_graph_state(deps_runtime(run_id, engagement)))
    assert len(gateway.invocations) == 1
    assert len(await deps.work_store.list_items(run_id=run_id)) == 1
    assert "uncertain" in json.dumps(final["native_messages"])


@pytest.mark.parametrize("revoke_targetless", [False, True])
async def test_target_free_approval_resume_retains_input_and_rechecks_descriptor(revoke_targetless):
    from redteam.core.contracts.engagement import ApprovalPolicy
    from redteam.core.enums import ApprovalStatus
    from redteam.orchestration.runner import AgentLoopRunner
    from redteam.orchestration.settings import OrchestrationSettings
    from redteam.orchestration.state import TerminationReason

    engagement = _engagement()
    llm = ScriptedNativeLLM([])
    deps = _native_deps(
        engagement, llm, settings=OrchestrationSettings(max_steps=8, checkpointing=True),
    )
    descriptor = deps.tool_catalog.all_descriptors()[0].model_copy(update={
        "requires_target": False, "argument_bindings": {},
        "input_schema": {
            "type": "object", "properties": {"query": {"type": "string"}},
            "required": ["query"], "additionalProperties": False,
        },
    })
    engagement.approval_policy = ApprovalPolicy(require_approval_for=["recon"])
    gateway = DummyToolGateway(tools=[descriptor], output={"value": RAW_MARKER})
    control = DefaultControlPlane(
        {engagement.engagement_id: engagement}, outbound_descriptor_resolver=gateway.get,
    )
    deps = replace(deps, tool_catalog=gateway, tool_gateway=gateway, control_plane=control)
    inputs = {"query": "public reference"}
    llm._native = [NativeCompletion(tool_calls=(NativeToolCall(
        call_id="target-free-held", name=exposed_tool_name(str(descriptor.tool_id)),
        arguments=inputs,
    ),))]
    run_id = RunId("run_target_free_approval_" + str(revoke_targetless))
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement, run_id=run_id)
    runner = AgentLoopRunner(deps, settings=deps.settings)
    try:
        stopped = await runner.run(deps_runtime(run_id, engagement))
        assert stopped.termination_reason == TerminationReason.APPROVAL_PENDING, (
            stopped.termination_reason, stopped.planning_feedback,
        )
        assert stopped.pending_approval_id is not None and not gateway.invocations
        assert runner._graph is not None
        snapshot = await runner._graph.aget_state(runner.config())
        held = snapshot.values["native_calls"][0]
        assert held.dispatch.proposal.target is None
        assert held.dispatch.proposal.arguments == inputs
        assert held.dispatch.plan_step.arguments == inputs
        original_action = held.dispatch.proposal.action_id
        if revoke_targetless:
            # The registered descriptor is current authority even after approval.
            gateway.get(descriptor.tool_id).requires_target = True
        await control.resolve_approval(
            stopped.pending_approval_id, ApprovalStatus.APPROVED, "operator",
        )
        resumed = await runner.reconcile_once(stopped, timeout_seconds=0.05)
        assert resumed.resumed
        if revoke_targetless:
            assert not gateway.invocations
        else:
            assert len(gateway.invocations) == 1
            assert gateway.invocations[0].action_id == original_action
            assert gateway.invocations[0].arguments == inputs
            originals = [r for r in await deps.evidence_store.list_for_run(run_id)
                         if r.source_type is EvidenceSourceType.TOOL]
            assert len(originals) == 1 and "target_node_id" not in originals[0].metadata
        assert await deps.path_store.list_paths(str(run_id)) == []
    finally:
        await runner.aclose()
