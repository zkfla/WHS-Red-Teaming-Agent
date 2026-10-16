"""Concurrency caps reach real central workers and the in-process MCP providers.

All model and tool outcomes here are explicit local doubles. The tests exercise
the real Control admission, SQLite execution/evidence stores and MCP tool layer;
they make no target network calls.
"""

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from mcp import Client

from redteam.app.central_development import build_central_development
from redteam.capability.servers import recon, web_http
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.network import CapturedResponse
from redteam.core.contracts.engagement import RuleOfEngagement
from redteam.core.contracts.execution import ExecutionState
from redteam.core.ports.llm import NativeCompletion
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_runtime import ready, ready_workers
from redteam.orchestration.central_state import execution_occupies_capacity
from redteam.orchestration.settings import OrchestrationSettings
from tests.integration.test_central_development import (
    HOST,
    PROFILE,
    TOOL,
    call,
    discover,
    engagement,
    gateway,
    inspect,
    register,
    settings,
    turn,
)


def concurrency_setup(tmp_path: Path, *, workers: int = 6):
    """Give the real specialist profile and all run-level caps the same bound."""
    profile_path = tmp_path / "native-specialists-web.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PROFILE.parent / "prompts", profile_path.parent / "prompts")
    profile = PROFILE.read_text(encoding="utf-8")
    assert "max_instances: 2" in profile
    profile_path.write_text(profile.replace("max_instances: 2", f"max_instances: {workers}"),
                            encoding="utf-8")

    configured = settings(requests=100).model_dump()
    configured.update(
        global_concurrency=workers,
        per_target_concurrency=workers,
        resource_wait_timeout_seconds=0.1,
        specialist_profile_path=str(profile_path),
    )
    configured["llm_request_policy"]["max_concurrent_requests"] = workers
    selected = engagement(actions=12)
    rate = selected.rate_limit_policy.model_dump()
    rate["max_concurrent_actions"] = workers
    selected.rate_limit_policy = type(selected.rate_limit_policy).model_validate(rate)
    selected.rules_of_engagement = [RuleOfEngagement(
        name="explicit-synthetic-parallel-cap", max_parallel_actions=workers,
    )]
    return selected, OrchestrationSettings.model_validate(configured)


async def register_tasks(selection, llm: FakeLLMClient, count: int = 6) -> list[str]:
    identities = []
    for index in range(count):
        identity = (await register(selection, f"concurrency-registration-{index}"))[0]
        identities.append(identity)
        llm.native_responses_by_task[identity] = [
            discover(TOOL),
            turn(inspect(f"parallel-call-{index}", account=str(index))),
            NativeCompletion(text="Synthetic execution finished; evidence remains inconclusive."),
        ]
    return identities


async def drain(selection, *, max_instances: int = 6):
    results = []
    async for result in selection.driver.drive_ready_tasks(
        selection.runtime,
        max_instances=max_instances,
    ):
        results.append(result)
    return results


async def release_and_join(tools, running):
    tools.release.set()
    for signal in tools.call_releases.values():
        signal.set()
    return await asyncio.wait_for(running, 15)


def _contexts_for_task(llm: FakeLLMClient, task_id: str) -> list[dict]:
    contexts = []
    for request in llm.requests:
        if request["kind"] != "complete_native":
            continue
        for message in request["messages"]:
            if message.get("role") != "user":
                continue
            try:
                context = json.loads(message.get("content") or "{}")
            except (TypeError, ValueError):
                continue
            active = context.get("active_task") or {}
            if str(active.get("task_id")) == task_id:
                contexts.append(context)
    return contexts


async def test_six_independent_tasks_overlap_and_commit_distinct_original_receipts(tmp_path):
    selected, configured = concurrency_setup(tmp_path)
    llm, tools = FakeLLMClient(), gateway()
    tools.release.clear()
    async with build_central_development(
        selected, llm=llm, tools=tools, settings=configured,
    ) as selection:
        identities = await register_tasks(selection, llm)
        running = asyncio.create_task(drain(selection))
        try:
            await asyncio.wait_for(tools.wait_for_invocations(6), 10)
            state = selection.deps.central_state
            executions = await state.list_executions()
            assert len(executions) == 6
            assert all(item.state is ExecutionState.RUNNING for item in executions)
            assert {str(item.task_id) for item in executions} == set(identities)
            assert len({str(item.instance_id) for item in executions}) == 6
            assert len({str(item.action_id) for item in executions}) == 6
            assert len({str(item.invocation_id) for item in executions}) == 6
            assert len({str(item.proposal_evidence_id) for item in executions}) == 6
        finally:
            results = await release_and_join(tools, running)

        assert len(results) == 6
        completed = await selection.deps.central_state.list_executions()
        assert all(item.state is ExecutionState.SUCCEEDED for item in completed)
        original_ids = [str(item.result_evidence_id) for item in completed]
        assert len(set(original_ids)) == 6
        assert all(item.result_evidence_id is not None for item in completed)
        for identity in original_ids:
            assert await selection.deps.central_state.has_inspected_evidence(identity)
        db = sqlite3.connect(selection.deps.central_state.path)
        try:
            stored = {
                row[0]
                for row in db.execute(
                    "SELECT evidence_id FROM state_evidence_refs WHERE run_id=?",
                    (selection.deps.central_state.run_id,),
                )
            }
        finally:
            db.close()
        assert set(original_ids) <= stored
        assert len(tools.invocations) == 6
        assert {item.arguments["account"] for item in tools.invocations} == set("012345")


async def test_one_unknown_execution_leaves_five_independent_slots_available(tmp_path):
    selected, configured = concurrency_setup(tmp_path)
    llm, tools = FakeLLMClient(), gateway()
    tools.responses[0].metadata = {"execution_uncertain": True}
    async with build_central_development(
        selected, llm=llm, tools=tools, settings=configured,
    ) as selection:
        identities = await register_tasks(selection, llm)
        first = await ready(selection.deps, selection.runtime)
        assert first is not None
        async for result in selection.driver.drive([first]):
            assert result.kind == "completed", result.error

        state = selection.deps.central_state
        held = [item for item in await state.list_executions()
                if execution_occupies_capacity(item)]
        assert len(held) == 1 and held[0].state is ExecutionState.UNKNOWN
        assert held[0].result_evidence_id is not None

        tools.release.clear()
        running = asyncio.create_task(drain(selection))
        try:
            await asyncio.wait_for(tools.wait_for_invocations(6), 10)
            executions = await state.list_executions()
            assert len(executions) == 6
            unknown = [item for item in executions if item.state is ExecutionState.UNKNOWN]
            executing = [item for item in executions if item.state is ExecutionState.RUNNING]
            assert len(unknown) == 1 and len(executing) == 5
            assert len({str(item.task_id) for item in executing}) == 5
        finally:
            results = await release_and_join(tools, running)

        assert len(results) == 5
        final = await state.list_executions()
        assert sum(item.state is ExecutionState.UNKNOWN for item in final) == 1
        assert sum(item.state is ExecutionState.SUCCEEDED for item in final) == 5
        assert len(tools.invocations) == 6
        assert {str(item.task_id) for item in final} == set(identities)


async def test_resource_conflict_blocks_only_the_matching_task_until_release(tmp_path):
    selected, configured = concurrency_setup(tmp_path)
    llm, tools = FakeLLMClient(), gateway(resource=True)
    tools.release.clear()
    async with build_central_development(
        selected, llm=llm, tools=tools, settings=configured,
    ) as selection:
        identities = await register_tasks(selection, llm)
        # The first two Tasks share one concrete resource; the other four do not.
        llm.native_responses_by_task[identities[0]][1] = turn(inspect("shared-a", "shared"))
        llm.native_responses_by_task[identities[1]][1] = turn(inspect("shared-b", "shared"))
        running = asyncio.create_task(drain(selection))
        try:
            await asyncio.wait_for(tools.wait_for_invocations(5), 10)
            await asyncio.sleep(0.05)
            assert sum(item.arguments["account"] == "shared" for item in tools.invocations) == 1
            assert {item.arguments["account"] for item in tools.invocations} == {
                "shared", "2", "3", "4", "5",
            }
        finally:
            results = await release_and_join(tools, running)

        assert {str(item.runtime.active_task_id) for item in results} == set(identities)
        assert len(tools.invocations) == 6
        assert sum(item.arguments["account"] == "shared" for item in tools.invocations) == 2
        executions = await selection.deps.central_state.list_executions()
        assert len(executions) == 6
        assert all(item.state is ExecutionState.SUCCEEDED for item in executions)


async def test_scope_and_approval_still_gate_parallel_tasks(tmp_path):
    selected, configured = concurrency_setup(tmp_path)
    llm, tools = FakeLLMClient(), gateway()
    async with build_central_development(
        selected, llm=llm, tools=tools, settings=configured,
    ) as selection:
        identities = await register_tasks(selection, llm)
        llm.native_responses_by_task[identities[-1]][1] = turn(
            inspect("in-scope-first", "outside-task-first"),
            inspect("outside-scope", "outside", host="outside.local"),
        )
        results = await asyncio.wait_for(drain(selection), 15)
        assert len(results) == 6
        assert len(tools.invocations) == 6
        assert all(item.arguments["host"] == HOST for item in tools.invocations)
        assert not any(item.arguments["account"] == "outside" for item in tools.invocations)
        assert len(await selection.deps.central_state.list_executions(
            task_id=identities[-1],
        )) == 1

    selected, configured = concurrency_setup(tmp_path / "approval")
    llm, tools = FakeLLMClient(), gateway(risk="high")
    async with build_central_development(
        selected, llm=llm, tools=tools, settings=configured,
    ) as selection:
        await register_tasks(selection, llm)
        results = await asyncio.wait_for(drain(selection), 15)
        assert len(results) == 6
        assert not tools.invocations
        executions = await selection.deps.central_state.list_executions()
        assert len(executions) == 6
        assert all(item.wait_reason == "approval" for item in executions)
        assert all(item.approval_ref for item in executions)


async def test_producer_original_receipt_is_in_consumer_model_input_and_result(tmp_path):
    selected, configured = concurrency_setup(tmp_path, workers=2)
    llm, tools = FakeLLMClient(), gateway()
    async with build_central_development(
        selected, llm=llm, tools=tools, settings=configured,
    ) as selection:
        identities = await register_tasks(selection, llm, count=2)
        workers = await ready_workers(selection.deps, selection.runtime, max_instances=2)
        by_task = {str(item.active_task_id): item for item in workers}
        producer_id, consumer_id = identities
        assert producer_id in by_task and consumer_id in by_task

        async for result in selection.driver.drive([by_task[producer_id]]):
            assert result.kind == "completed", result.error
        producer_execution = (await selection.deps.central_state.list_executions(
            task_id=producer_id,
        ))[0]
        original_id = str(producer_execution.result_evidence_id)
        assert producer_execution.state is ExecutionState.SUCCEEDED
        assert await selection.deps.central_state.has_inspected_evidence(original_id)

        consumer = await selection.deps.central_state.get_task(consumer_id)
        llm.native_responses_by_task[consumer_id] = [
            discover(TOOL, "task.publish_result"),
            turn(call("publish-consumed-original", "task.publish_result", {
                "result": {
                    "contract_version": consumer.investigation_contract_version,
                    "result_id": "consumer-original-receipt",
                    "outcome": "inconclusive",
                    "summary": "Uses an original produced by another Task",
                    "evidence_ids": [original_id],
                },
            })),
        ]
        async for result in selection.driver.drive([by_task[consumer_id]]):
            assert result.kind == "completed", result.error

        contexts = _contexts_for_task(llm, consumer_id)
        assert contexts
        assert any(
            original_id in context.get("evidence_index", [])
            and any(item.get("evidence_id") == original_id for item in context.get("evidence", []))
            for context in contexts
        )
        published = await selection.deps.central_state.get_task(consumer_id)
        assert published.result is not None
        assert original_id in list(map(str, published.result.evidence_ids))
        assert await selection.deps.central_state.has_inspected_evidence(original_id)


def provider_policy(max_concurrency: int = 1, *, sessions: bool = False) -> ServerPolicy:
    configured = {
        "targets": {"bank": {
            "origin": "http://127.0.0.1:8080",
            "allowed_paths": ["/account"],
        }},
        "allowed_networks": ["127.0.0.0/8"],
        "max_concurrency": max_concurrency,
    }
    if sessions:
        configured["sessions"] = {
            "alice": {"target_ref": "bank", "principal_ref": "a", "bearer_env": "TOKEN_A"},
            "bob": {"target_ref": "bank", "principal_ref": "b", "bearer_env": "TOKEN_B"},
        }
    return ServerPolicy.model_validate(configured)


def test_server_policy_default_capacity_is_canonical_and_bounded():
    configured = {
        "targets": {"bank": {
            "origin": "http://127.0.0.1:8080",
            "allowed_paths": ["/account"],
        }},
        "allowed_networks": ["127.0.0.0/8"],
    }
    policy = ServerPolicy.model_validate(configured)
    assert policy.max_concurrency == 1
    assert "max_concurrency" not in policy.model_dump(mode="json")
    assert "max_concurrency" not in json.loads(policy.model_dump_json())
    selected = provider_policy(6)
    assert selected.model_dump(mode="json")["max_concurrency"] == 6
    with pytest.raises(ValueError):
        provider_policy(33)


@pytest.mark.parametrize(
    ("module", "tool_name"),
    [(web_http, "http_request"), (recon, "http_probe")],
)
@pytest.mark.parametrize(("capacity", "expected_peak"), [(1, 1), (6, 6)])
async def test_in_process_mcp_provider_enforces_configured_capacity(
    module, tool_name, capacity, expected_peak, monkeypatch,
):
    condition = asyncio.Condition()
    release = asyncio.Event()
    active = peak = started = 0

    async def fake_request(*args, **kwargs):
        nonlocal active, peak, started
        async with condition:
            active += 1
            started += 1
            peak = max(peak, active)
            condition.notify_all()
            await condition.wait_for(lambda: started >= expected_peak)
        await release.wait()
        try:
            return CapturedResponse(200, {"content-type": "text/plain"}, b"synthetic")
        finally:
            async with condition:
                active -= 1
                condition.notify_all()

    monkeypatch.setattr(module, "request", fake_request)
    arguments = {"target_ref": "bank", "path": "/account"}
    async with Client(module.create_server(provider_policy(capacity))) as client:
        calls = [
            asyncio.create_task(client.call_tool(tool_name, arguments))
            for _ in range(6)
        ]
        async with condition:
            await asyncio.wait_for(condition.wait_for(lambda: started >= 1), 5)
        if capacity == 1:
            await asyncio.sleep(0.05)
            assert peak == 1
        else:
            async with condition:
                await asyncio.wait_for(condition.wait_for(lambda: started == 6), 5)
            assert peak == 6
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*calls), 10)
    assert peak == expected_peak
    assert all(not result.is_error for result in results)


async def test_compare_sessions_holds_one_provider_slot_for_both_sequential_requests(
    monkeypatch,
):
    monkeypatch.setenv("TOKEN_A", "synthetic-a")
    monkeypatch.setenv("TOKEN_B", "synthetic-b")
    release_first = asyncio.Event()
    calls = []

    async def fake_request(*args, **kwargs):
        session_ref = kwargs["session_ref"]
        calls.append(session_ref)
        if session_ref == "alice":
            await release_first.wait()
        return CapturedResponse(200, {}, b"synthetic")

    monkeypatch.setattr(web_http, "request", fake_request)
    arguments = {
        "target_ref": "bank",
        "path": "/account",
        "first_session_ref": "alice",
        "second_session_ref": "bob",
    }
    async with Client(web_http.create_server(provider_policy(sessions=True))) as client:
        first = asyncio.create_task(client.call_tool("http_compare_sessions", arguments))
        second = asyncio.create_task(client.call_tool("http_compare_sessions", arguments))
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls == ["alice"]
        release_first.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 10)
    assert all(not result.is_error for result in results)
    assert calls == ["alice", "bob", "alice", "bob"]
