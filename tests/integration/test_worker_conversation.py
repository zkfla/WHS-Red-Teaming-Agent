"""Real native/adapter/Control/checkpoint flow; explicit supervisor/model doubles.

No Docker, paid model, or target traffic. Scripts are fixture labels and never
execute. Their stdout is untrusted program output, not vulnerability proof.
"""

import json
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import datetime, timezone

import pytest

from redteam.app.central_run import open_central_run
from redteam.app.web.graph_read import read_graph
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_manifest import SHELL_SERVER_ID, build_shell_descriptors
from redteam.capability.shell_workroom import ExecResult
from redteam.core.config import AppConfig, EngagementConfig
from redteam.core.contracts.mission import OperatorIntent, Provenance
from redteam.core.enums import OperatorIntentType, TaskStatus
from redteam.core.ids import OperatorIntentId
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.native_result_delivery import ORIGINAL_PREVIEW_CHARS
from redteam.orchestration.nodes import react
from redteam.orchestration.operator_intents.models import IntentRecord
from redteam.orchestration.reporting import ReportingAgent
from redteam.orchestration.worker_session import WorkerSession
from tests.integration.test_central_development import call, turn
from tests.integration.test_central_parallel import limits
from tests.integration.test_investigation_context_baseline import _compare_requests
from tests.integration.test_probe_workers import PROFILE, drive, register
from tests.integration.test_program_probe_delivery import ScriptedWorkroom


class SuccessfulWorkroom(ScriptedWorkroom):
    async def exec_script(self, key, language, script, timeout_seconds=None, **kwargs):
        self.executions.append((key, language, script, kwargs))
        return ExecResult(
            exit_code=0, stdout="synthetic-observation " * 600, stderr="fixture warning",
            timed_out=False, truncated=True, duration_ms=3, workroom_key=key,
            container_name="fixture-only",
        )


class ConversationModel(FakeLLMClient):
    def __init__(self, *, executions, reads, opaque=True, repeat_reads=False):
        super().__init__()
        self.envelopes = []
        self.executions, self.reads = executions, reads
        self.opaque = opaque
        self.repeat_reads = repeat_reads
        self.on_second_request: Callable[[], Awaitable[None]] | None = None

    async def complete_native(self, messages, tools, system=None, **kwargs):
        self.envelopes.append(json.loads(json.dumps({
            "messages": messages, "tools": [asdict(tool) for tool in tools], "system": system,
        })))
        index = len(self.envelopes)
        if index == 2 and self.on_second_request is not None:
            await self.on_second_request()
        originals = [json.loads(message["content"]) for message in messages
                     if message["role"] == "tool"]
        evidence = list(dict.fromkeys(ref for body in originals if body.get("executed")
                                     for ref in body["evidence_ids"]))
        if index == 1 or self.reads + 1 < index <= self.reads + self.executions:
            assert exposed_tool_name("analysis.shell_run") in {tool.name for tool in tools}
            response = turn(call(f"bash-{index}", "analysis.shell_run", {
                "language": "bash", "script": f"printf fixture-{index}",
            }))
        elif index <= 1 + self.reads:
            response = turn(call(f"read-{index}", "evidence.read", {
                "evidence_id": evidence[0],
                "offset": 0 if self.repeat_reads else index - 1,
                "limit": 500,
            }))
        else:
            response = turn(call("return", "task.settle", {
                "outcome": "inconclusive", "reason": "Return bounded program observations",
                "evidence_ids": evidence,
            }))
        self.native_responses = [replace(response, continuation=({
            "type": "thinking", "thinking": "", "signature": f"opaque-fixture-{index}",
        },) if self.opaque else ())]
        return await super().complete_native(messages, tools, system=system, **kwargs)


async def test_worker_receives_invalid_shell_context_and_repairs_without_restart(tmp_path):
    """Explicit model/supervisor doubles; real binding, recovery, Control and state."""
    from redteam.core.contracts.execution import ExecutionState

    class RepairingModel(FakeLLMClient):
        def __init__(self):
            super().__init__()
            self.envelopes = []

        async def complete_native(self, messages, tools, system=None, **kwargs):
            self.envelopes.append(json.loads(json.dumps(messages)))
            for tool in tools:
                if tool.name.startswith("analysis_shell_"):
                    assert "workroom_key" not in tool.input_schema["properties"]
            index = len(self.envelopes)
            if index == 1:
                # Simulate the actual model bug, including an unadvertised field.
                response = turn(call("bad-key", "analysis.shell_run", {
                    "workroom_key": "run_only", "language": "bash", "script": "first",
                }))
            elif index == 2:
                reply = json.loads(messages[-1]["content"])
                assert reply["ok"] is False and reply["executed"] is False
                assert "shell_context_mismatch" in messages[-1]["content"]
                response = turn(call("fixed-key", "analysis.shell_run", {
                    "language": "bash", "script": "corrected",
                }))
            else:
                assert index == 3
                reply = json.loads(messages[-1]["content"])
                assert reply["ok"] is True and reply["executed"] is True
                response = turn(call("return", "task.settle", {
                    "outcome": "inconclusive", "reason": "Return local execution observations",
                    "evidence_ids": reply["evidence_ids"],
                }))
            self.native_responses = [response]
            return await super().complete_native(messages, tools, system=system, **kwargs)

    original, settings = limits()
    settings = settings.with_overrides(specialist_profile_path=str(PROFILE))
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    manager = SuccessfulWorkroom()
    registry = MCPToolRegistry()
    for descriptor in build_shell_descriptors(manager.policy):
        registry.register(descriptor)
    catalog = MCPToolCatalog(registry)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    gateway = MCPToolGateway(registry, {SHELL_SERVER_ID: adapter}, health_refresh_enabled=False)
    llm = RepairingModel()
    async with open_central_run(
        config, directory=tmp_path / "run", settings=settings, llm=llm,
        tool_gateway=gateway, tool_catalog=catalog, shell_programs=manager,
    ) as session:
        identity = await register(session, "repair-shell-context", actions=4, requests=5)
        outcome, = await drive(session)
        assert outcome.kind == "completed", outcome.error
        task = await session.deps.central_state.get_task(identity)
        assert task.status is TaskStatus.COMPLETED
        executions = await session.deps.central_state.list_executions(task_id=identity)
        assert {item.state for item in executions} == {
            ExecutionState.FAILED, ExecutionState.SUCCEEDED,
        }
        assert len(executions) == 2 and len(manager.executions) == 1
        assert manager.executions[0][2] == "corrected"
        assert len(llm.envelopes) == 3
        assert llm.envelopes[1][:len(llm.envelopes[0])] == llm.envelopes[0]


@pytest.mark.parametrize(
    "executions,reads,repeat_reads",
    [(3, 1, False), (1, 5, False), (1, 2, True)],
)
async def test_worker_executes_bash_and_keeps_every_display_byte_identical(
    tmp_path, monkeypatch, executions, reads, repeat_reads,
):
    calls = Counter()
    original_context = InvestigationEngine.context

    async def measured_context(self, runtime, *args, **kwargs):
        calls[str(runtime.active_task_id)] += 1
        return await original_context(self, runtime, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("normal Worker turns must not rewrite or trim previous messages")

    monkeypatch.setattr(InvestigationEngine, "context", measured_context)
    monkeypatch.setattr(react, "_compact_context_messages", forbidden, raising=False)
    monkeypatch.setattr(react, "_message_history", forbidden, raising=False)
    original, settings = limits()
    settings = settings.with_overrides(specialist_profile_path=str(PROFILE), max_steps=20)
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    manager = SuccessfulWorkroom()
    registry = MCPToolRegistry()
    for descriptor in build_shell_descriptors(manager.policy):
        registry.register(descriptor)
    catalog = MCPToolCatalog(registry)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    gateway = MCPToolGateway(registry, {SHELL_SERVER_ID: adapter}, health_refresh_enabled=False)
    llm = ConversationModel(
        executions=executions, reads=reads, repeat_reads=repeat_reads,
    )
    async with open_central_run(
        config, directory=tmp_path / "run", settings=settings, llm=llm,
        tool_gateway=gateway, tool_catalog=catalog, shell_programs=manager,
    ) as session:
        identity = await register(session, "append-only", actions=4, requests=8)
        if executions == 3:
            async def guidance() -> None:
                await session.deps.central_state.append(IntentRecord(
                    run_id=session.runtime.run_id, correlation_id="operator-change",
                    intent=OperatorIntent(
                        intent_id=OperatorIntentId("operator-change"),
                        mission_id=session.runtime.mission_id,
                        intent_type=OperatorIntentType.GUIDANCE,
                        instruction="Consider the new synthetic premise",
                        provenance=Provenance(source="test"), created_at=datetime.now(timezone.utc),
                    ),
                ))
            llm.on_second_request = guidance
        outcome, = await drive(session)
        assert outcome.kind == "completed", outcome.error
        task = await session.deps.central_state.get_task(identity)
        assert task.status is TaskStatus.COMPLETED and task.result.outcome == "inconclusive"
        assert len(manager.executions) == executions
        assert len(llm.envelopes) == executions + reads + 1
        assert calls[identity] == 1  # No complete DB/context reconstruction on subsequent turns.
        initial = llm.envelopes[0]
        initial_context = json.loads(initial["messages"][0]["content"])
        assert initial_context["program_execution_environment"]["routes"][0]["status"] == (
            "local_only"
        )
        assert initial_context["probe"]["remaining"] == {
            "actions": 4,
            "model_requests": 8,
            "repairs": 8,
        }
        for previous, current in zip(llm.envelopes, llm.envelopes[1:], strict=False):
            count = len(previous["messages"])
            assert current["messages"][:count] == previous["messages"]
            assert current["system"] == initial["system"]
            assert current["tools"] == initial["tools"]
        final = llm.envelopes[-1]["messages"]
        starts = [message for message in final if message["role"] == "user"
                  and "active_task" in json.loads(message["content"])]
        assert len(starts) == 1
        if executions == 3:
            notices = [json.loads(message["content"]).get("worker_updates", {})
                       for message in final if message["role"] == "user"]
            assert sum("operator:operator-change" in notice for notice in notices) == 1
        assert not any("Earlier context snapshot omitted" in str(message) for message in final)
        windows = [view for message in final if message["role"] == "tool"
                   for view in json.loads(message["content"]).get("original_results", [])]
        assert len(windows) == executions and all(
            view["limit"] == ORIGINAL_PREVIEW_CHARS for view in windows
        )
        assert all(view["program_output"]["stderr"] == "fixture warning" for view in windows)
        assert all(view["execution"]["accounting"]["exit_code"] == 0 for view in windows)
        assert all(view["provenance"]["invocation_id"] for view in windows)
        stored_executions = await session.deps.central_state.list_executions(task_id=identity)
        assert len(stored_executions) == executions
        assert all(item.selection.route == "local" for item in stored_executions)
        assert all(item.selection.route_status == "local_only" for item in stored_executions)
        assert all(item.outcome.result_kind == "generic_program_output"
                   for item in stored_executions)
        assert all(item.outcome.success is True for item in stored_executions)
        web = await read_graph(session.deps.central_state.path, str(session.runtime.run_id))
        projected = [item for item in web["executions"] if item["task_id"] == identity]
        assert len(projected) == executions
        assert all(item["route"] == "local" for item in projected)
        assert all(item["result_kind"] == "generic_program_output" for item in projected)
        delivered_reads = [message for message in final if message["role"] == "tool"
                           and json.loads(message["content"]).get("evidence_read_request")]
        assert len(delivered_reads) == reads
        assert all(json.loads(message["content"])["payload"]["limit"] == 500
                   for message in delivered_reads)
        if repeat_reads:
            payloads = [json.loads(item["content"])["payload"] for item in delivered_reads]
            assert payloads == [payloads[0], payloads[0]]
        runner = session.driver.runner_for(outcome.runtime)
        snapshot = await runner._graph.aget_state(runner.config())
        saved = WorkerSession.model_validate_json(json.dumps(snapshot.values["worker_session"]))
        assert saved.status == "closed" and saved.binding.task_id == identity
        assert saved.conversation()[:len(final)] == final
        assert snapshot.values["native_messages"] == []
        assistants = [message for message in saved.conversation() if message["role"] == "assistant"]
        assert assistants[0]["provider_continuation"][0]["signature"] == "opaque-fixture-1"
        requests = await session.deps.central_state.request_ledger().snapshot(
            str(session.runtime.run_id),
        )
        assert requests["spent"] == executions + reads + 1
        task_usage = requests["by_task"][identity]
        assert task_usage["local_reads"] == reads
        assert task_usage["no_progress"] == (1 if repeat_reads else 0)
        if repeat_reads:
            report = await ReportingAgent().project(
                outcome.runtime,
                graph=AttackGraphService(session.deps.graph_store),
                evidence_store=session.deps.evidence_store,
                work_store=None,
                central_state=session.deps.central_state,
            )
            effectiveness, = [
                item for item in report.task_request_effectiveness
                if item["task_id"] == identity
            ]
            assert effectiveness == {
                "task_id": identity,
                "model_requests": 4,
                "known_tokens": 40,
                "unknown_usage": 0,
                "external_actions": 1,
                "local_reads": 2,
                "repairs": 0,
                "transport_retries": 0,
                "provider_failures": 0,
                "settlements": 1,
                "text_only_held": 0,
                "external_actions_per_model_request": 0.25,
            }
        sizes = [len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode())
                 for envelope in llm.envelopes]
        comparisons = [_compare_requests(envelope, llm.envelopes[index - 1] if index else None)
                       for index, envelope in enumerate(llm.envelopes)]
        (tmp_path / "conversation-metrics.json").write_text(json.dumps({
            "model_requests": len(sizes), "actions": executions, "extra_reads": reads,
            "input_bytes": sizes, "cumulative_bytes": sum(sizes),
            "common_prefix_bytes": [item["common_prefix_bytes"] for item in comparisons],
            "first_changed_previous_message": [item["first_changed_previous_message"]
                                               for item in comparisons],
            "usage_fixture": requests["usage"], "actual_provider_cost": None,
        }))
