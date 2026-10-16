"""Explicit common-worker assembly with real native loops and synthetic providers."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from redteam.app.central_development import build_central_development
from redteam.app.central_run import open_central_run
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.development import DevelopmentToolGateway
from redteam.core.config import AppConfig, EngagementConfig
from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.core.contracts.execution import ExecutionState
from redteam.core.enums import ApprovalStatus, TaskStatus, ToolHealthStatus
from redteam.core.ports.llm import LLMProviderFailure, NativeCompletion, NativeToolCall
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.agents.tool_access import eligible_role_tool_ids
from redteam.orchestration.central_runtime import ACTOR_RETURNED, ready_workers
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.development_jobs import DevelopmentJobs
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.graph_recording import GraphRecordingHandler
from redteam.orchestration.llm_requests import RequestAdmissionError
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.metered_llm import llm_request_scope
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.probes import context_for_probe
from redteam.orchestration.specialist_tasks import HypothesisProbeProposal, register_probe_tasks
from redteam.orchestration.worker_session import WorkerSession
from tests.fakes.worker_input import worker_input
from tests.integration.test_central_completion import OriginalProtocolDouble
from tests.integration.test_central_development import (
    HOST,
    TARGET,
    TOOL,
    call,
    discover,
    gateway,
    inspect,
    turn,
)
from tests.integration.test_central_parallel import limits

PROFILE = Path(__file__).parents[2] / "config/agents/native-probes.yaml"


@asynccontextmanager
async def selection(tmp_path, *, tools=None, llm=None, jobs=None, profile=PROFILE):
    original, configured = limits()
    original.metadata["success_criteria"] = ["Assess the original response"]
    configured = configured.with_overrides(specialist_profile_path=str(profile))
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    llm = llm or FakeLLMClient()
    tools = tools or OriginalProtocolDouble(gateway())
    if jobs is not None:
        async with build_central_development(
            original, llm=llm, tools=tools, settings=configured, jobs=jobs,
        ) as session:
            yield session, llm, tools
        return
    async with open_central_run(
        config, directory=tmp_path / "run", settings=configured, llm=llm,
        tool_gateway=tools, tool_catalog=tools.catalog,
    ) as session:
        yield session, llm, tools


async def register(
    session, key, *, actions=1, requests=8, hypotheses=None, updates=(),
    objective="Observe one response",
):
    state = session.deps.central_state
    service = AttackGraphService(session.deps.graph_store)
    mission = await state.get_mission(session.runtime.mission_id)
    subjects = {TARGET, *("hypothesis:" + identity for identity in (hypotheses or {}))}
    subjects.update("hypothesis:" + update["hypothesis"]["hypothesis_id"]
                    for update in updates if update.get("expected_revision") is not None)
    read = {ref: content_hash(await service.get_node(ref)) for ref in subjects}
    proposal = HypothesisProbeProposal.model_validate({"updates": list(updates), "tasks": [{
        "task_key": key, "title": "Short response probe", "objective": objective,
        "role_id": "probe-worker", "target_node_ids": [TARGET],
        "completion_criteria": [{"kind": "observation", "description": "Inspect the response"}],
        "hypothesis_revisions": hypotheses or {},
        "probe_limits": {"max_actions": actions, "max_model_requests": requests},
    }]})
    receipt = await register_probe_tasks(
        session.deps, proposal_id=key, proposal=proposal, expected_revision=mission.revision,
        expected_binding_hash=await state.transaction(lambda tx: tx.binding()[1], write=False),
        graph_read=read, known_targets={TARGET}, known_principals=set(), max_tasks=4,
    )
    return receipt["task_ids"][0]


async def drive(session):
    workers = await ready_workers(session.deps, session.runtime)
    return [result async for result in session.driver.drive(workers)]


async def test_common_worker_uses_current_provider_selection_and_health(tmp_path):
    original = gateway().catalog.all_descriptors()[0]
    mobile = original.model_copy(update={"tool_id": "mobile.inspect", "server_id": "other",
                                          "capability_tags": ["mobile"]})
    unhealthy = mobile.model_copy(update={"tool_id": "mobile.unavailable",
                                           "health_status": ToolHealthStatus.UNHEALTHY})
    unselected = original.model_copy(update={"tool_id": "cloud.inspect",
                                              "capability_tags": ["cloud"]})
    tools = DevelopmentToolGateway([original, mobile, unhealthy, unselected], [])
    async with selection(tmp_path, tools=tools) as (session, llm, tools):
        assert await eligible_role_tool_ids(session.deps, "probe-worker") == {
            TOOL, "mobile.inspect",
        }
        assert not llm.requests and not tools.invocations


async def test_probe_can_choose_unvisited_url_on_its_assigned_host(tmp_path):
    original = gateway()
    descriptor = original.catalog.all_descriptors()[0].model_copy(update={
        "target_types": ["url"], "argument_bindings": {"url": "url"},
        "input_schema": {"type": "object", "required": ["url"],
                         "properties": {"url": {"type": "string"}}},
    })
    tools = OriginalProtocolDouble(DevelopmentToolGateway([descriptor], original.responses))
    async with selection(tmp_path, tools=tools) as (session, llm, tools):
        identity = await register(session, "new-path")
        address = "http://" + HOST + "/previously-unvisited"
        llm.native_responses_by_task[identity] = [discover(TOOL), turn(call(
            "probe-path", TOOL, {"url": address},
        ))]
        result, = await drive(session)
        assert result.kind == "completed", result.error
        assert len(tools.inner.invocations) == 1
        assert tools.inner.invocations[0].arguments["url"] == address


async def test_common_pool_stops_extra_calls_and_isolates_worker_context(tmp_path):
    async with selection(tmp_path) as (session, llm, tools):
        ids = [await register(session, key) for key in ("a", "b")]
        for index, identity in enumerate(ids):
            llm.native_responses_by_task[identity] = [
                discover(TOOL), turn(inspect("first", account=str(index)),
                                    inspect("excess", account=str(index))),
                NativeCompletion(text="Must not need another request", total_tokens=1),
            ]
        results = await drive(session)
        assert len(results) == 2 and all(item.kind == "completed" for item in results)
        assert len({item.runtime.instance_id for item in results}) == 2
        sessions = []
        for outcome in results:
            runner = session.driver.runner_for(outcome.runtime)
            snapshot = await runner._graph.aget_state(runner.config())
            saved = WorkerSession.model_validate_json(json.dumps(snapshot.values["worker_session"]))
            assert saved.binding.task_id == str(outcome.runtime.active_task_id)
            assert saved.binding.instance_id == str(outcome.runtime.instance_id)
            sessions.append(saved.binding.identity)
        assert sessions[0] != sessions[1]
        executions = await session.deps.central_state.list_executions()
        assert len(executions) == 2
        assert {item.model_tool_call_id for item in executions} == {"first"}
        assert len(tools.inner.invocations) == 2
        for identity in ids:
            task = await session.deps.central_state.get_task(identity)
            assert task.status is TaskStatus.COMPLETED and task.result.outcome == "inconclusive"
            assert "probe_action_limit_exhausted" in task.result.limitations
            usage = await session.deps.central_state.transaction(
                lambda tx, identity=identity: tx.probe_usage(identity), write=False,
            )
            assert usage["actions"] == 1 and usage["model_requests"] == 2
            assert usage["external_tool_decisions"] == 1
        assert len(llm.requests) == 4
        payloads = [worker_input(item["messages"]) for item in llm.requests]
        assert all(len(payload["investigation_tasks"]) == 1 for payload in payloads)
        assert {payload["active_task"]["assigned_agent"] for payload in payloads} == {
            "probe-worker",
        }
        assert all(payload["probe"]["candidates"] == {} for payload in payloads)
        assert await AttackGraphService(session.deps.graph_store).list_findings() == []


async def test_provider_failure_circuit_survives_new_worker_and_same_purpose_task(tmp_path):
    class FailingThenTextModel(FakeLLMClient):
        def __init__(self):
            super().__init__()
            self.native_calls = 0

        async def complete_native(self, *args, **kwargs):
            self.native_calls += 1
            if self.native_calls <= 2:
                raise LLMProviderFailure(kind="server", http_status=503, retryable=True)
            return NativeCompletion(text="provider recovered")

    llm = FailingThenTextModel()
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        first = await register(session, "provider-failures")
        first_result, = await drive(session)
        assert first_result.runtime.planning_diagnostic.code == (
            "probe_provider_failure_circuit_open"
        )
        assert llm.native_calls == 2
        first_usage = await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(first), write=False,
        )
        assert first_usage["provider_failures"] == 2
        assert first_usage["transport_retries"] == 1
        assert first_usage["actions"] == 0

        # Limits remain part of resume/completion binding, but changing only a
        # limit must not create a fresh provider-failure circuit.
        clone = await register(session, "same-purpose-clone", requests=7)
        clone_result, = await drive(session)
        assert clone_result.runtime.planning_diagnostic.code == (
            "probe_provider_failure_circuit_open"
        )
        assert llm.native_calls == 2
        assert (await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(clone), write=False,
        ))["model_requests"] == 0

        different = await register(
            session, "different-purpose", objective="Observe a different response property",
        )
        different_result, = await drive(session)
        assert different_result.runtime.termination_reason == "investigation_held"
        assert llm.native_calls == 3
        assert (await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(different), write=False,
        ))["text_only_held"] == 1


async def test_task_repair_cap_is_shared_with_total_request_ledger(tmp_path):
    async with selection(tmp_path) as (session, llm, tools):
        identity = await register(session, "repair-cap")
        worker, = await ready_workers(session.deps, session.runtime)
        ledger = session.deps.central_state.request_ledger()
        for index in range(2):
            request = await ledger.reserve(
                str(worker.run_id), owner=worker.work_owner, kind="complete_native",
                task_id=identity, stage="native_action", purpose="worker_decision",
                attempt_kind="format_repair",
            )
            await ledger.dispatch(str(worker.run_id), request)
            await ledger.settle_provider_outcome(
                str(worker.run_id), request, total_tokens=index + 1,
            )
            await ledger.classify_decision(
                str(worker.run_id), request, outcome="format_rejected", valid=False,
                made_progress=False,
            )
        with pytest.raises(RequestAdmissionError) as caught:
            await ledger.reserve(
                str(worker.run_id), owner=worker.work_owner, kind="complete_native",
                task_id=identity, stage="native_action", purpose="worker_decision",
                attempt_kind="format_repair",
            )
        assert caught.value.reason_code == "probe_repair_request_limit_exhausted"
        usage = await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(identity), write=False,
        )
        assert usage["model_requests"] == usage["repairs"] == 2
        assert usage["request_outcomes"]["format_rejected"] == 2


async def test_invalid_arguments_unknown_tool_and_settlement_are_distinct_outcomes(tmp_path):
    async with selection(tmp_path) as (session, llm, tools):
        identity = await register(session, "native-outcomes")
        llm.native_responses_by_task[identity] = [
            turn(NativeToolCall(
                call_id="invalid-arguments",
                name=exposed_tool_name(TOOL),
                arguments={},
                argument_error="arguments did not match the advertised schema",
            )),
            turn(call("unknown", "unregistered.tool", {})),
            turn(call("settle", "task.settle", {
                "outcome": "inconclusive",
                "reason": "No valid external action was selected",
                "evidence_ids": [],
            })),
        ]

        outcome, = await drive(session)
        assert outcome.kind == "completed", outcome.error
        usage = await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(identity), write=False,
        )
        assert usage["request_outcomes"]["invalid_tool_arguments"] == 1
        assert usage["request_outcomes"]["unknown_tool"] == 1
        assert usage["request_outcomes"]["accepted"] == 1
        assert usage["settlements"] == 1
        assert usage["external_tool_decisions"] == usage["actions"] == 0


async def test_model_boundary_returns_without_another_request_and_budget_never_resets(tmp_path):
    async with selection(tmp_path) as (session, llm, tools):
        identity = await register(session, "one-model", requests=1)
        llm.native_responses_by_task[identity] = [discover(TOOL), turn(inspect("must-not-run"))]
        result, = await drive(session)
        assert result.kind == "completed", result.error
        task = await session.deps.central_state.get_task(identity)
        assert task.status is TaskStatus.COMPLETED and task.result.outcome == "inconclusive"
        assert len(llm.requests) == 1 and not tools.inner.invocations
        ledger = session.deps.central_state.request_ledger()
        with llm_request_scope(str(session.runtime.run_id), "different-owner", task_id=identity):
            with pytest.raises(RequestAdmissionError, match="probe_task_not_running"):
                await ledger.reserve(str(session.runtime.run_id), owner="different-owner",
                                     kind="complete_native", task_id=identity)
        assert (await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(identity), write=False,
        ))["model_requests"] == 1


async def test_task_request_admission_is_atomic_and_limits_cannot_expand(tmp_path):
    async with selection(tmp_path) as (session, llm, tools):
        with pytest.raises(StateConflict, match="probe_override_exceeds_profile_limits"):
            await register(session, "oversized", actions=5)
        assert not await session.deps.central_state.list_tasks()
        identity = await register(session, "reservation", requests=1)
        worker, = await ready_workers(session.deps, session.runtime)
        ledger = session.deps.central_state.request_ledger()
        request = await ledger.reserve(str(worker.run_id), owner=worker.work_owner,
                                       kind="complete_native", task_id=identity)
        with pytest.raises(RequestAdmissionError, match="probe_model_request_limit_exhausted"):
            await ledger.reserve(str(worker.run_id), owner="reviewer", kind="complete_structured",
                                 task_id=identity)
        assert (await ledger.snapshot(str(worker.run_id)))["reserved"] == 1
        await ledger.release_undispatched(str(worker.run_id), request)
        await ledger.reserve(str(worker.run_id), owner=worker.work_owner,
                             kind="complete_native", task_id=identity)
        task = await session.deps.central_state.get_task(identity)
        changed = task.model_copy(update={"probe_limits": task.probe_limits.model_copy(
            update={"max_model_requests": 2},
        )})
        with pytest.raises(StateConflict, match="task_inputs_or_owned_lifecycle_changed"):
            await session.deps.central_state.transaction(lambda tx: tx.update_task(
                changed, owner=worker.work_owner, version=task.ownership_version,
                expected_hash=content_hash(task),
            ))
        assert not llm.requests and not tools.inner.invocations


@pytest.mark.parametrize("mode", ["approval", "unknown"])
async def test_boundaries_preserve_pending_originals_and_resume_exact_calls(tmp_path, mode):
    tools = OriginalProtocolDouble(gateway(risk="high" if mode == "approval" else "low",
                                          unknown=mode == "unknown"))
    async with selection(tmp_path, tools=tools) as (session, llm, tools):
        identity = await register(session, "pending")
        llm.native_responses_by_task[identity] = [discover(TOOL), turn(inspect("original-call"))]
        result, = await drive(session)
        assert result.kind == "completed", result.error
        original, = await session.deps.central_state.list_executions()
        task = await session.deps.central_state.get_task(identity)
        assert task.status is TaskStatus.BLOCKED and task.result.outcome == "blocked"
        # Current execution waits live in the ledger; immutable gaps keep the history.
        assert task.result.observations == [] and task.result.limitations == []
        original_result_id = task.result.result_id
        historical_refs = list(task.result.evidence_ids)
        gap = await session.deps.evidence_store.get(historical_refs[-1])
        assert gap.metadata["diagnostic"]["trace"][0]["invocation_id"] == str(
            original.invocation_id,
        )
        assert task.occupant_owner == result.runtime.work_owner
        assert await session.deps.central_state.transaction(
            lambda tx: tx.active_activation_count(), write=False,
        ) == 0
        assert original.state in {ExecutionState.PENDING, ExecutionState.UNKNOWN}
        before = await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(identity), write=False,
        )
        if mode == "approval":
            await session.deps.control_plane.resolve_approval(
                result.runtime.pending_approval_id, ApprovalStatus.APPROVED, "fixture:operator",
            )
            recovered, outcome = await session.driver.recover_central_instance(result.runtime)
            assert outcome.ready, outcome.errors
            await session.driver.runner_for(recovered).resume_recovered(recovered)
            updated = await session.deps.central_state.get_execution(str(original.invocation_id))
            assert updated.state is ExecutionState.SUCCEEDED
            assert len(tools.inner.invocations) == 1
            assert tools.inner.invocations[0].invocation_id == original.invocation_id
            completed = await session.deps.central_state.get_task(identity)
            assert completed.status is TaskStatus.COMPLETED
            assert completed.result.result_id == original_result_id
            assert set(historical_refs) <= set(completed.result.evidence_ids)
            assert completed.result.observations == []
            assert "approval_pending" not in completed.result.limitations
            assert not updated.wait_reason and updated.result_evidence_id
        else:
            recovered, outcome = await session.driver.recover_central_instance(result.runtime)
            assert not outcome.ready
            assert outcome.pending_invocations == [str(original.invocation_id)]
            current = await session.deps.central_state.get_task(identity)
            assert current.result == task.result
            assert len(tools.inner.invocations) == 1
        after = await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(identity), write=False,
        )
        assert before == after and len(llm.requests) == 2


class ReviewedObservationModel(FakeLLMClient):
    """Scripted review; the production origin/conditions checker still runs."""

    async def complete_native(self, messages, tools, system=None, **kwargs):
        payload = worker_input(messages)
        if payload["probe"]["usage"]["actions"]:
            evidence = payload["probe"]["original_evidence_ids"]
            self._canned_structured = {
                "accepted": True, "reason": "Original response assessed",
                "claims": [{"kind": "observation", "description": "Original response observed",
                            "evidence_ids": evidence}],
                "criterion_results": [{
                    "criterion_id": criterion_id(CompletionCriterion.model_validate(item)),
                    "met": True, "evidence_ids": evidence, "reason": "Response observed",
                } for item in payload["active_task"]["completion_criteria"]],
            }
            self.native_responses_by_task[payload["active_task"]["task_id"]] = [turn(call(
                "settle", "task.settle", {"outcome": "success", "reason": "Response observed",
                                         "evidence_ids": evidence},
            ))]
        return await super().complete_native(messages, tools, system=system, **kwargs)


async def test_short_observation_can_finish_review_without_confirming_a_candidate(tmp_path):
    llm = ReviewedObservationModel()
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        identity = await register(session, "reviewed", actions=2)
        llm.native_responses_by_task[identity] = [discover(TOOL), turn(inspect("observe"))]
        result, = await drive(session)
        assert result.kind == "completed", result.error
        task = await session.deps.central_state.get_task(identity)
        assert task.status is TaskStatus.COMPLETED
        assert task.result.outcome == "confirmed", (
            task.result.summary,
            [json.loads(item["content"]).get("error")
             for item in llm.last_native_request["messages"] if item["role"] == "tool"],
        )
        assert task.result.completion_receipt_id is not None
        assert (await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(identity), write=False,
        ))["model_requests"] == 4
        assert await AttackGraphService(session.deps.graph_store).list_findings() == []


async def test_probe_job_collection_does_not_resubmit_or_reset_limits(tmp_path):
    tools, jobs = gateway(), DevelopmentJobs(output={"synthetic": "collected"})
    async with selection(tmp_path, tools=tools, jobs=jobs) as (session, llm, tools):
        identity = await register(session, "job")
        llm.native_responses_by_task[identity] = [discover(TOOL), turn(inspect("original-job"))]
        result, = await drive(session)
        assert result.kind == "completed", result.error
        original, = await session.deps.central_state.list_executions()
        assert original.job_ref and original.state is ExecutionState.UNKNOWN
        assert len(jobs.submissions) == 1
        blocked, outcome = await session.driver.recover_central_instance(result.runtime,
                                                                         transfer_owner=True)
        assert not outcome.ready
        jobs.states[original.job_ref] = ExecutionState.SUCCEEDED
        resumed, outcome = await session.driver.recover_central_instance(blocked)
        assert outcome.ready, outcome.errors
        await session.driver.runner_for(resumed).resume_recovered(resumed)
        collected = await session.deps.central_state.get_execution(str(original.invocation_id))
        assert collected.state is ExecutionState.SUCCEEDED
        assert not collected.resource_execution_pending
        assert len(jobs.submissions) == 1 and not tools.invocations
        usage = await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(identity), write=False,
        )
        assert usage["actions"] == 1 and usage["model_requests"] == 2
        assert (await session.deps.central_state.get_task(identity)).status is TaskStatus.COMPLETED


async def test_probe_keeps_admitted_candidate_snapshot_after_planner_update(tmp_path):
    async with selection(tmp_path) as (session, llm, tools):
        update = {"expected_revision": None, "hypothesis": {
            "contract_version": 2, "hypothesis_id": "candidate", "description": "Initial premise",
            "target_node_ids": [TARGET], "reason": "Observe the response",
        }}
        identity = await register(session, "initial", hypotheses={"candidate": 1}, updates=[update])
        worker, = await ready_workers(session.deps, session.runtime)
        update["expected_revision"] = 1
        update["hypothesis"]["description"] = "Revised premise"
        await register(session, "updated", hypotheses={"candidate": 2}, updates=[update])
        context = await InvestigationEngine(session.deps).context(worker, None)
        context["knowledge"]["provided"]["claims"].append({"node_id": "host:unrelated"})
        context["knowledge"]["historical"] = {"facts": [{"node_id": "host:other-run"}]}
        payload = await context_for_probe(session.deps, worker, context)
        assert payload["probe"]["candidates"]["candidate"]["revision"] == 1
        assert payload["probe"]["candidates"]["candidate"]["description"] == "Initial premise"
        assert "hypotheses" not in payload["knowledge"]
        assert "historical" not in payload["knowledge"]
        assert all(item["node_id"] != "host:unrelated"
                   for item in payload["knowledge"]["provided"]["claims"])
        assert [task["task_id"] for task in payload["investigation_tasks"]] == [identity]
        saved = await AttackGraphService(session.deps.graph_store).get_hypothesis("candidate")
        assert saved.revision == 2 and saved.description == "Revised premise"
        assert not llm.requests and not tools.inner.invocations
        refused = await GraphRecordingHandler()(
            {"hypotheses": [{"statement": "Start a different investigation"}]},
            LocalToolContext(session.deps, worker),
        )
        assert not refused.ok
        assert refused.payload["diagnostic"]["code"] == "probe_candidate_change_requires_planner"
        assert len(await AttackGraphService(session.deps.graph_store).list_hypotheses()) == 1


async def test_probe_resource_wait_automatically_resumes_original_at_boundary(tmp_path):
    tools = OriginalProtocolDouble(gateway(resource=True))
    tools.inner.release.clear()
    async with selection(tmp_path, tools=tools) as (session, llm, tools):
        returned = asyncio.Event()

        async def on_return(_):
            returned.set()

        await session.deps.event_bus.subscribe(ACTOR_RETURNED, on_return)
        ids = [await register(session, key) for key in ("first", "waiting")]
        for identity in ids:
            llm.native_responses_by_task[identity] = [discover(TOOL), turn(inspect("shared-call"))]

        async def drain():
            return [result async for result in session.driver.drive_ready_tasks(
                session.runtime, max_instances=2,
            )]

        running = asyncio.create_task(drain())
        try:
            await asyncio.wait_for(tools.inner.wait_for_invocations(1), 5)
            await asyncio.wait_for(returned.wait(), 5)
            originals = await session.deps.central_state.list_executions()
            assert len(originals) == 2 and any(item.wait_reason == "resource" for item in originals)
        finally:
            tools.inner.release.set()
            results = await asyncio.wait_for(running, 10)
        final = await session.deps.central_state.list_executions()
        assert all(result.kind == "completed" for result in results)
        assert {item.invocation_id for item in final} == {item.invocation_id for item in originals}
        assert all(item.state is ExecutionState.SUCCEEDED for item in final)
        assert len(tools.inner.invocations) == 2 and len(llm.requests) == 4
        for identity in ids:
            usage = await session.deps.central_state.transaction(
                lambda tx, identity=identity: tx.probe_usage(identity), write=False,
            )
            assert usage["actions"] == 1 and usage["model_requests"] == 2
            task = await session.deps.central_state.get_task(identity)
            assert task.status is TaskStatus.COMPLETED
