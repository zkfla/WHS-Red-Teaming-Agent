"""10d lifecycle through real storage/Control and explicit model/tool doubles.

No Docker, target traffic or paid provider calls. Size measurements contain
scalars only and demonstrate application envelopes, not actual cache hits/cost.
"""

import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from redteam.app.central_run import open_central_run
from redteam.app.central_views import inspect_central_run
from redteam.app.web.graph_read import read_graph
from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_manifest import SHELL_SERVER_ID, build_shell_descriptors
from redteam.core.config import AppConfig, EngagementConfig
from redteam.core.contracts.execution import ExecutionState
from redteam.core.enums import ApprovalStatus
from redteam.core.ports.llm import LLMUsage, NativeCompletion
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_runtime import ready_workers
from redteam.orchestration.coordination import CoordinationError, content_hash
from redteam.orchestration.development_jobs import DevelopmentJobs
from redteam.orchestration.nodes import react
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.tool_discovery import NATIVE_INPUT_LIMIT, NativeInputLimit
from redteam.orchestration.worker_session import WorkerSession
from tests.integration.test_central_completion import OriginalProtocolDouble
from tests.integration.test_central_development import call, gateway, inspect, turn
from tests.integration.test_central_parallel import limits
from tests.integration.test_hypothesis_planner import probe
from tests.integration.test_investigation_context_baseline import _compare_requests
from tests.integration.test_native_schema_delivery import SchemaSensitiveProbe, provider
from tests.integration.test_planner_request_prefix import RequestDouble
from tests.integration.test_probe_workers import PROFILE, drive, register
from tests.integration.test_worker_conversation import ConversationModel, SuccessfulWorkroom


@asynccontextmanager
async def shell_session(tmp_path, model, *, profile=PROFILE):
    original, settings = limits()
    settings = settings.with_overrides(specialist_profile_path=str(profile), max_steps=20)
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    manager = SuccessfulWorkroom()
    registry = MCPToolRegistry()
    for descriptor in build_shell_descriptors(manager.policy):
        registry.register(descriptor)
    catalog = MCPToolCatalog(registry)
    gateway = MCPToolGateway(registry, {SHELL_SERVER_ID: BuiltinShellAdapter(
        manager.policy, workroom=manager,
    )}, health_refresh_enabled=False)
    async with open_central_run(
        config, directory=tmp_path / "run", settings=settings, llm=model,
        tool_gateway=gateway, tool_catalog=catalog, shell_programs=manager,
    ) as session:
        yield session, manager


async def saved_session(session, runtime):
    runner = session.driver.runner_for(runtime)
    snapshot = await runner._latest_snapshot()
    return WorkerSession.model_validate_json(json.dumps(snapshot.values["worker_session"]))


async def test_independent_planner_worker_bash_source_and_new_task_session(tmp_path):
    first = probe("A")
    first["probe_limits"] = {"max_actions": 3, "max_model_requests": 8}

    def followup(payload):
        source, = payload["investigations"]
        child = probe("B", source=source)
        child["probe_limits"] = first["probe_limits"]
        return {"tasks": [child], "reason": "Assess an explicit follow-up source"}

    class Cycle(RequestDouble):
        def __init__(self):
            super().__init__([{"tasks": [first], "reason": "Inspect a synthetic uncertainty"},
                              followup])
            self.worker_requests = {}

        async def complete_native(self, messages, tools, system=None, **kwargs):
            initial = json.loads(messages[0]["content"])
            identity = initial["active_task"]["task_id"]
            history = self.worker_requests.setdefault(identity, [])
            history.append(json.loads(json.dumps(messages)))
            index = len(history)
            refs = list(dict.fromkeys(ref for message in messages if message["role"] == "tool"
                                      for ref in json.loads(message["content"]).get(
                                          "evidence_ids", [])))
            if index in {1, 3}:
                response = turn(call(f"bash-{index}", "analysis.shell_run", {
                    "language": "bash", "script": f"printf fixture-{index}",
                }))
            elif index == 2:
                response = turn(call("read", "evidence.read", {
                    "evidence_id": refs[0], "offset": 5, "limit": 300,
                }))
            else:
                response = turn(call("return", "task.settle", {
                    "outcome": "inconclusive", "reason": "Return synthetic observations",
                    "evidence_ids": refs,
                }))
            self.native_responses = [replace(response, total_tokens=12,
                usage=LLMUsage(10, 2, 12, 4, 1) if index == 2 else None,
                continuation=({"type": "thinking", "thinking": "",
                               "signature": identity + ":opaque"},))]
            return await super().complete_native(messages, tools, system=system, **kwargs)

    model = Cycle()
    async with shell_session(tmp_path, model, profile=PROFILE.with_name(
        "native-probes-legacy.yaml",
    )) as (session, manager):
        planner = SpecialistPlanner(session.deps)
        ids, sessions = [], []
        previous_runtime = None
        for _ in range(2):
            identity, = await planner.advance(session.runtime)
            ids.append(identity)
            if previous_runtime is None:
                outcome, = await drive(session)
            else:
                workers = await ready_workers(session.deps, previous_runtime)
                worker, = workers
                assert worker.local_tool_result_ids == worker.evidence_reads == []
                assert worker.task_owner is None and worker.model_requests_used == 0
                outcome, = [item async for item in session.driver.drive(workers)]
            assert outcome.kind == "completed", outcome.error
            previous_runtime = outcome.runtime
            sessions.append(await saved_session(session, outcome.runtime))
        a, b = sessions
        assert a.status == b.status == "closed" and a.binding.identity != b.binding.identity
        assert a.binding.prefix_identity == b.binding.prefix_identity
        assert len(manager.executions) == 4
        second = json.loads(b.messages[0].message()["content"])
        assert second["source_result"]["task_id"] == ids[0]
        assert second["source_result"]["status"] == "available"
        assert second["source_result"]["result"]["outcome"] == "inconclusive"
        assert model.worker_requests[ids[1]][0] == [b.messages[0].message()]
        assert ids[0] + ":opaque" not in json.dumps(b.conversation())
        assert model.envelopes[0]["system"] == model.envelopes[1]["system"]
        assert all(len(request["messages"]) == 1 for request in model.envelopes)
        usage = await session.deps.central_state.request_ledger().snapshot(session.runtime.run_id)
        assert usage["spent"] == 10  # 2 independent judgments + 2 * 4 Worker turns.
        for identity in ids:
            measured = usage["by_task"][identity]
            assert measured["spent"] == 4
            assert measured["usage"]["cache_read_tokens"] == {"known": 4, "unknown": 3}
            assert measured["usage"]["cache_write_tokens"] == {"known": 1, "unknown": 3}
        view = await read_graph(session.deps.central_state.path, str(session.runtime.run_id))
        for task in view["tasks"]:
            assert task["status"] == "completed"
            assert task["usage"]["usage"]["cache_read_tokens"] == {"known": 4, "unknown": 3}
        for worker in view["workers"]:
            assert worker["usage"]["usage"]["cache_write_tokens"] == {"known": 1, "unknown": 3}


@pytest.mark.parametrize("opaque", [False, True])
async def test_actual_input_boundary_archives_displays_or_holds_opaque_state(
    tmp_path, monkeypatch, opaque,
):
    class BoundedConversation(ConversationModel):
        async def complete_native(self, messages, tools, system=None, **kwargs):
            response = await super().complete_native(messages, tools, system, **kwargs)
            if len(self.envelopes) == 3:
                size = NATIVE_INPUT_LIMIT.size(messages=messages, tools=tools, system=system)
                monkeypatch.setattr(react, "NATIVE_INPUT_LIMIT", NativeInputLimit(
                    max_bytes=size + 1500,
                ))
            return response

    model = BoundedConversation(executions=3, reads=1, opaque=opaque)
    async with shell_session(tmp_path, model) as (session, manager):
        task_id = await register(session, "bounded", actions=4, requests=16)
        outcome, = await drive(session)
        assert outcome.kind == "completed", outcome.error
        saved = await saved_session(session, outcome.runtime)
        if opaque:
            assert saved.generation == 0 and len(model.envelopes) == 3
            assert outcome.runtime.planning_diagnostic.code == "worker_session_compaction_held"
        else:
            assert len(model.envelopes) == 5 and len(manager.executions) == 3, (
                outcome.runtime.planning_diagnostic.summary
            )
            assert saved.generation >= 1
            assert saved.archives[0].reason == "input_limit"
            assert saved.archives[0].messages[0].message() == model.envelopes[0]["messages"][0]
            assert saved.archives[0].input_bytes_after < saved.archives[0].input_bytes_before
            assert saved.archives[0].messages[-1].message()["role"] == "tool"
            boundary = json.loads(saved.messages[0].message()["content"])["conversation_boundary"]
            assert boundary["reason"] == "input_limit"
            assert saved.status == "closed"
            assert all(spec.name for spec in saved.tools())
        usage = await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(task_id), write=False,
        )
        assert usage["actions"] == len(manager.executions)
        assert usage["model_requests"] == len(model.envelopes)  # No compaction LLM/reset.
        (tmp_path / "boundary-metrics.json").write_text(json.dumps({
            "requests": len(model.envelopes), "actions": len(manager.executions),
            "generations": saved.generation,
            "bytes_before_after": [[a.input_bytes_before, a.input_bytes_after]
                                   for a in saved.archives],
            "provider_cache_usage": None,
            "request_bytes": [len(json.dumps(item, ensure_ascii=False).encode())
                              for item in model.envelopes],
            "first_changed_previous_message": [_compare_requests(
                item, model.envelopes[index - 1] if index else None,
            )["first_changed_previous_message"] for index, item in enumerate(model.envelopes)],
        }))


async def test_changed_owned_purpose_gets_explicit_generation_and_keeps_original_budget(tmp_path):
    model = ConversationModel(executions=3, reads=1, opaque=False)
    async with shell_session(tmp_path, model) as (session, manager):
        identity = await register(session, "purpose", actions=4, requests=16)

        async def change_purpose():
            state = session.deps.central_state
            task = await state.get_task(identity)
            await state.transaction(lambda tx: tx.update_task(
                task.model_copy(update={"objective": "Assess the changed synthetic premise"}),
                owner=task.occupant_owner, version=task.ownership_version,
                expected_hash=content_hash(task),
            ))

        model.on_second_request = change_purpose
        outcome, = await drive(session)
        assert outcome.kind == "completed", outcome.error
        saved = await saved_session(session, outcome.runtime)
        assert saved.generation == 1 and saved.archives[0].reason == "purpose_changed"
        assert saved.binding.purpose_hash != saved.archives[0].binding.purpose_hash
        initial = json.loads(saved.messages[0].message()["content"])
        assert initial["active_task"]["objective"] == "Assess the changed synthetic premise"
        assert len(model.envelopes) == 5 and len(manager.executions) == 3
        assert (await session.deps.central_state.request_ledger().snapshot(
            session.runtime.run_id,
        ))["spent"] == 5
        recovered, proof = await session.recover(outcome.runtime.instance_id)
        assert proof.ready, proof.errors  # Completed receipts retain their original purpose.
        await session.driver.runner_for(recovered).resume_recovered(recovered)
        assert len(manager.executions) == 3 and len(model.envelopes) == 5


@pytest.mark.parametrize("corruption", [None, "version", "tools", "pairing"])
async def test_approval_process_reopen_preserves_session_or_refuses_before_execution(
    tmp_path, corruption,
):
    original, settings = limits()
    settings = settings.with_overrides(specialist_profile_path=str(PROFILE))
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    tools, model = provider(risk="high"), SchemaSensitiveProbe()
    root = tmp_path / "approval"
    async with open_central_run(
        config, directory=root, settings=settings, llm=model,
        tool_gateway=tools, tool_catalog=tools.catalog,
    ) as session:
        await register(session, "approval", actions=2)
        outcome, = await drive(session)
        runtime = outcome.runtime
        runner = session.driver.runner_for(runtime)
        snapshot = await runner._latest_snapshot()
        saved = await saved_session(session, runtime)
        assert saved.status == "waiting" and saved.wait_reason == "approval_pending"
        original_call, = snapshot.values["native_calls"]
        if corruption:
            raw = saved.checkpoint()
            if corruption == "version":
                raw["version"] = 1
            elif corruption == "tools":
                specs = json.loads(raw["tools_json"])
                specs[0]["description"] += "changed"
                raw["tools_json"] = json.dumps(specs)
            else:
                raw["messages"][-1]["display_json"] = json.dumps({
                    "role": "assistant", "content": "missing original call",
                })
            await runner._graph.aupdate_state(runner.config(), {"worker_session": raw},
                                             as_node="settle")
        assert not tools.inner.invocations
    async with open_central_run(
        config, directory=root, settings=settings, llm=model, resume=True,
        run_id=runtime.run_id, engagement_id=runtime.engagement_id,
        tool_gateway=tools, tool_catalog=tools.catalog,
    ) as session:
        await session.deps.control_plane.resolve_approval(
            runtime.pending_approval_id, ApprovalStatus.APPROVED, "fixture:operator",
        )
        if corruption:
            with pytest.raises(CoordinationError, match="worker_session"):
                await session.recover(runtime.instance_id)
            assert not tools.inner.invocations and len(model.envelopes) == 1
            assert await session.deps.central_state.get_task(saved.binding.task_id) is not None
            _, _, report = await inspect_central_run(
                session.deps.central_state.path, runtime.run_id,
            )
            assert report.run_id == runtime.run_id and report.evidence_count > 0
            return
        recovered, proof = await session.recover(runtime.instance_id)
        assert proof.ready, proof.errors
        with pytest.raises(CoordinationError, match="requires_original_resume"):
            await session.driver.runner_for(recovered).run(recovered)
        returned = await session.driver.runner_for(recovered).resume_recovered(recovered)
        after = await saved_session(session, returned)
        assert after.status == "closed"
        assert after.conversation()[:len(saved.messages)] == saved.conversation()
        assert len(tools.inner.invocations) == 1
        assert (tools.inner.invocations[0].invocation_id
                == original_call.dispatch.invocation.invocation_id)
        results = [message for message in after.conversation() if message["role"] == "tool"
                   and message.get("tool_call_id") == original_call.call_id]
        assert len(results) == 1
        usage = await session.deps.central_state.request_ledger().snapshot(runtime.run_id)
        (tmp_path / "resume-metrics.json").write_text(json.dumps({
            "native_requests": len(model.envelopes), "total_requests": usage["spent"],
            "request_bytes": [NATIVE_INPUT_LIMIT.size(**item) for item in model.envelopes],
            "restored_prefix_messages": len(saved.messages),
            "original_tool_submissions": len(tools.inner.invocations),
            "usage_fixture": usage["usage"], "actual_provider_cost": None,
        }))


@pytest.mark.parametrize("boundary", ["bash", "read", "settle"])
async def test_crash_after_original_before_display_restores_without_reexecution(
    tmp_path, monkeypatch, boundary,
):
    model = ConversationModel(executions=3, reads=1)
    original = react._result_messages
    failed = False

    async def interrupt(messages, **kwargs):
        nonlocal failed
        bodies = [json.loads(message["content"]) for message in messages]
        matches = {"bash": any(body.get("executed") for body in bodies),
                   "read": any(body.get("evidence_read_request") for body in bodies),
                   "settle": any(body.get("runtime_outcome") == "task_completed"
                                 for body in bodies)}
        if not failed and matches[boundary]:
            failed = True
            raise RuntimeError("synthetic crash before first display checkpoint")
        return await original(messages, **kwargs)

    monkeypatch.setattr(react, "_result_messages", interrupt)
    async with shell_session(tmp_path, model) as (session, manager):
        identity = await register(session, "crash", actions=4, requests=16)
        outcome, = await drive(session)
        assert outcome.kind == "failed" and failed
        before = await saved_session(session, outcome.runtime)
        recovered, proof = await session.recover(outcome.runtime.instance_id)
        assert proof.ready, proof.errors
        returned = await session.driver.runner_for(recovered).resume_recovered(recovered)
        after = await saved_session(session, returned)
        assert after.status == "closed"
        assert after.conversation()[:len(before.messages)] == before.conversation()
        assert len(manager.executions) == 3 and len(model.envelopes) == 5
        calls = [message["tool_call_id"] for message in after.conversation()
                 if message["role"] == "tool"]
        assert len(calls) == len(set(calls)) == 5
        snapshot = await session.driver.runner_for(returned)._latest_snapshot()
        assert snapshot.next == ()
        usage = await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(identity), write=False,
        )
        assert usage["actions"] == 3 and usage["model_requests"] == 5


async def test_unknown_local_submission_stays_pending_without_replay(tmp_path, monkeypatch):
    model = ConversationModel(executions=3, reads=1)
    async with shell_session(tmp_path, model) as (session, manager):
        await register(session, "local-unknown", actions=4, requests=16)
        original = session.deps.local_tools.call
        submissions = []

        async def interrupt(tool_id, arguments, context):
            result = await original(tool_id, arguments, context)
            if tool_id == "evidence.read":
                submissions.append(tool_id)
                raise RuntimeError("synthetic loss after local effect before outcome receipt")
            return result

        monkeypatch.setattr(session.deps.local_tools, "call", interrupt)
        outcome, = await drive(session)
        assert outcome.kind == "failed" and submissions == ["evidence.read"]
        recovered, proof = await session.recover(outcome.runtime.instance_id)
        assert proof.ready, proof.errors  # No unknown external invocation is being replayed.
        returned = await session.driver.runner_for(recovered).resume_recovered(recovered)
        saved = await saved_session(session, returned)
        assert saved.status == "waiting"
        assert returned.planning_diagnostic.code == "worker_session_local_outcome_unknown"
        assert submissions == ["evidence.read"] and len(manager.executions) == 1
        assert len(model.envelopes) == 2
        view = await read_graph(session.deps.central_state.path, str(returned.run_id))
        assert view["workers"][0]["phase"] == "waiting_review"
        assert view["tasks"][0]["result_outcome"] == "blocked"


async def test_lost_model_reply_keeps_the_pre_dispatch_display_and_request_charge(
    tmp_path, monkeypatch,
):
    class Model(FakeLLMClient):
        def __init__(self):
            super().__init__()
            self.envelopes = []

        async def complete_native(self, messages, tools, system=None, **kwargs):
            self.envelopes.append(json.loads(json.dumps(messages)))
            refs = [ref for message in messages if message["role"] == "tool"
                    for ref in json.loads(message["content"]).get("evidence_ids", [])]
            self.native_responses = [turn(call("return", "task.settle", {
                "outcome": "inconclusive", "reason": "Return synthetic observations",
                "evidence_ids": refs,
            })) if refs else turn(call("first", "analysis.shell_run", {
                "language": "bash", "script": "printf synthetic-first",
            }))]
            return await super().complete_native(messages, tools, system=system, **kwargs)

    original = react._echo_provider_continuation
    failed = False

    def interrupt(response):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("synthetic crash after response before assistant checkpoint")
        return original(response)

    monkeypatch.setattr(react, "_echo_provider_continuation", interrupt)
    model = Model()
    async with shell_session(tmp_path, model) as (session, manager):
        identity = await register(session, "lost-reply", actions=3)
        outcome, = await drive(session)
        assert outcome.kind == "failed" and not manager.executions
        before = await saved_session(session, outcome.runtime)
        assert before.conversation() == model.envelopes[0]
        recovered, proof = await session.recover(outcome.runtime.instance_id)
        assert proof.ready, proof.errors
        returned = await session.driver.runner_for(recovered).resume_recovered(recovered)
        assert (await saved_session(session, returned)).status == "closed"
        assert model.envelopes[0] == model.envelopes[1]
        assert len(model.envelopes) == 3 and len(manager.executions) == 1
        usage = await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(identity), write=False,
        )
        assert usage["model_requests"] == 3 and usage["actions"] == 1


@pytest.mark.parametrize("wait", ["job", "unknown"])
async def test_original_job_or_unknown_survives_process_reopen_without_new_submission(
    tmp_path, wait,
):
    original, settings = limits()
    settings = settings.with_overrides(specialist_profile_path=str(PROFILE))
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    model = FakeLLMClient()
    tools = OriginalProtocolDouble(gateway(resource=True, unknown=wait == "unknown"))
    jobs = DevelopmentJobs() if wait == "job" else None
    options = {"llm": model, "tool_gateway": tools, "tool_catalog": tools.catalog,
               "long_job_adapter": jobs}
    root = tmp_path / "pending"
    async with open_central_run(config, directory=root, settings=settings, **options) as session:
        identity = await register(session, "pending", actions=2)
        model.native_responses_by_task[identity] = [
            turn(inspect("original")), NativeCompletion(text="Return preserved observation"),
        ]
        outcome, = await drive(session)
        saved = await saved_session(session, outcome.runtime)
        assert saved.status == "waiting"
        execution, = await session.deps.central_state.list_executions()
        run_id, engagement_id = session.runtime.run_id, session.runtime.engagement_id
    async with open_central_run(
        config, directory=root, settings=settings, resume=True,
        run_id=run_id, engagement_id=engagement_id, **options,
    ) as session:
        recovered, proof = await session.recover(outcome.runtime.instance_id)
        assert not proof.ready and proof.pending_invocations == [str(execution.invocation_id)]
        assert await saved_session(session, recovered) == saved
        assert len(model.requests) == 1
        if wait == "job":
            assert jobs is not None and len(jobs.submissions) == 1
            jobs.states[execution.job_ref] = ExecutionState.SUCCEEDED
            recovered, proof = await session.recover(outcome.runtime.instance_id)
            assert proof.ready, proof.errors
            returned = await session.driver.runner_for(recovered).resume_recovered(recovered)
            after = await saved_session(session, returned)
            assert after.status == "closed"
            assert after.conversation()[:len(saved.messages)] == saved.conversation()
            assert len(jobs.submissions) == 1 and not tools.inner.invocations
        else:
            assert len(tools.inner.invocations) == 1
            assert (await session.deps.central_state.get_execution(
                str(execution.invocation_id),
            )).state is ExecutionState.UNKNOWN
