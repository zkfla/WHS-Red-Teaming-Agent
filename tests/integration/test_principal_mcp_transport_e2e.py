"""Production composition and MCP stdio to a real local HTTP target.

Only the model is scripted. Control, gateway, MCP subprocess, HTTP requests,
evidence, the native tool loop, mission gates and terminal reports are
production code. This proves transport/runtime wiring, not the quality of a
live model's decisions.

Native-loop migration: the scripted model answers complete_native only and
follows the real sequence -- discover the operator-registered sessions with the
web.session_list tool, then compare them with web.http_compare_sessions using
the discovered session refs, then settle through the genuine task/goal
settlement tools. The retired structured path/mode plan is gone, so there is no
fabricated path revision to assert; the durable proof is the executed
discovery/comparison evidence plus its work/task/call provenance. Any
complete_structured call is limited to the genuine review routes (analysis
verification plus task/goal settlement reviews); legacy strategy routes raise.
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest

from redteam.app.bootstrap import build_application, run_engagement
from redteam.app.report_store import load_terminal_report
from redteam.attack_graph.identity import endpoint_node_id
from redteam.attack_graph.service import AttackGraphService
from redteam.core.config import AppConfig, CapabilityConfig, EngagementConfig
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.enums import GraphNodeType, MissionStatus, TaskStatus
from redteam.core.ids import ToolId
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.native_local_tools import (
    GOAL_SETTLE_TOOL_ID,
    TASK_SETTLE_TOOL_ID,
)
from redteam.orchestration.paths.sqlite import SQLitePathStore
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from redteam.orchestration.work.models import WorkStatus
from redteam.orchestration.work.sqlite import SQLiteWorkStore
from tests.integration.test_second_principal_mission import (
    _CompareNativeLLM,
    _exposed_names,
    _tool_result,
)

COMPARE_PATH = "/orders/1234"
DISCOVERY_CALL_ID = "call-discovery"
COMPARE_CALL_ID = "call-compare"


def _executed_evidence(messages: list[dict[str, Any]], call_id: str) -> list[str]:
    """Executed evidence ids the loop returned for one model tool call."""
    body = _tool_result(messages, call_id)
    if not body or body.get("ok") is not True or body.get("executed") is not True:
        return []
    return [str(item) for item in body.get("evidence_ids") or []]


def _discovered_sessions(
    messages: list[dict[str, Any]], call_id: str
) -> list[dict[str, Any]]:
    """Sessions the real session_list tool returned to the model as originals."""
    body = _tool_result(messages, call_id)
    if not body or body.get("ok") is not True:
        return []
    for original in body.get("original_results") or []:
        raw = original.get("text") if isinstance(original, dict) else None
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        output = parsed.get("output") if isinstance(parsed, dict) else None
        rows = output.get("sessions") if isinstance(output, dict) else None
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


class _TransportLLM(_CompareNativeLLM):
    """Native model: discover the supplied sessions, then compare and settle.

    The compare call is built from the sessions the real session_list receipt
    returned, so the two-session comparison uses the registered sessions and
    their actual results. Analysis and settlement reviews come from the shared
    native fixture; no legacy strategy route is answered.
    """

    _catalog: Any

    def _remote_name(self, remote_tool_name: str) -> str:
        for descriptor in self._catalog.all_descriptors():
            if descriptor.tool_name == remote_tool_name:
                return exposed_tool_name(str(descriptor.tool_id))
        raise AssertionError(f"no registered tool named {remote_tool_name!r}")

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        names = _exposed_names(tools)
        discovery = self._remote_name("session_list")
        compare = self._remote_name("http_compare_sessions")
        task_tool = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_tool = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert discovery in names, "native model needs the registered discovery tool"
        assert compare in names, "native model needs the registered compare tool"
        assert task_tool in names, "native model needs the task settlement helper"
        assert goal_tool in names, "native model needs the goal settlement helper"
        self.turns += 1
        if self.turns == 1:
            self.calls.append({"turn": 1, "tool_calls": [discovery]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=DISCOVERY_CALL_ID, name=discovery, arguments={}
                    ),
                ),
                text="",
            )
        sessions = _discovered_sessions(messages, DISCOVERY_CALL_ID)
        if self.turns == 2:
            assert len(sessions) >= 2, "discovery must return the registered sessions"
            first, second = sessions[0], sessions[1]
            assert first["session_ref"] != second["session_ref"]
            self.calls.append({"turn": 2, "tool_calls": [compare]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=COMPARE_CALL_ID,
                        name=compare,
                        arguments={
                            "target_ref": first["target_ref"],
                            "path": COMPARE_PATH,
                            "first_session_ref": first["session_ref"],
                            "second_session_ref": second["session_ref"],
                        },
                    ),
                ),
                text="",
            )
        # Only a conclusive comparison settles the task and the goal;
        # inconclusive or failed receipts hold so the mission stays blocked.
        evidenced = _executed_evidence(messages, COMPARE_CALL_ID)
        if not evidenced:
            return NativeCompletion(tool_calls=(), text="comparison did not execute")
        if not await self._comparison_conclusive():
            return NativeCompletion(tool_calls=(), text="comparison inconclusive")
        if self._task_call is None:
            self._task_call = "call-task"
            self.calls.append({"turn": self.turns, "tool_calls": [task_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._task_call,
                        name=task_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "discovered comparison evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        seen = _tool_result(messages, self._task_call)
        if seen is None or seen.get("ok") is not True:
            return NativeCompletion(tool_calls=(), text="awaiting task settlement")
        if self._goal_call is None:
            self._goal_call = "call-goal"
            self.calls.append({"turn": self.turns, "tool_calls": [goal_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._goal_call,
                        name=goal_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "discovered comparison evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="")


def _records_for_tool(records: list[Any], tool_id: str) -> list[Any]:
    return [
        record
        for record in records
        if record.source_id == tool_id
        and isinstance(record.metadata.get("tool_result"), dict)
    ]


@pytest.mark.parametrize("case", ["success", "unauthenticated", "missing_credential"])
async def test_principal_discovery_and_completion_over_real_mcp(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, str, int]] = []

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).decode()
            method, path, _ = headers.split("\r\n", 1)[0].split()
            bearer = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in headers.split("\r\n")[1:]
                    if line.lower().startswith("authorization:")
                ),
                "",
            )
            principal = "alice" if bearer == "Bearer e2e-synthetic-alice" else "bob"
            status = 401 if case == "unauthenticated" else (200 if principal == "alice" else 403)
            requests.append((method, path, status))
            body = b'{"account":"synthetic-alice","balance":1000}' if status == 200 else b"{}"
            writer.write(
                f"HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    async with server, AsyncExitStack() as stack:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.chdir(tmp_path)
        policy = tmp_path / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "targets": {
                        "bank": {
                            "origin": f"http://127.0.0.1:{port}",
                            "allowed_paths": ["/orders/1234"],
                        }
                    },
                    "allowed_networks": ["127.0.0.1/32"],
                    "sessions": {
                        ref: {
                            "target_ref": "bank",
                            "principal_ref": f"{ref}-principal",
                            "bearer_env": f"E2E_SYNTHETIC_{ref.upper()}",
                        }
                        for ref in ("alice", "bob")
                    },
                }
            )
        )
        prefix = "REDTEAM_MCP_PRINCIPAL_E2E"
        for key, value in {
            "KIND": "web_api",
            "TRANSPORT": "stdio",
            "COMMAND": sys.executable,
            "ARGS_JSON": json.dumps(
                [
                    "-m",
                    "redteam.capability.servers",
                    "web-http",
                    "--policy",
                    str(policy),
                ]
            ),
            "PASSTHROUGH_ENV": (
                "E2E_SYNTHETIC_ALICE"
                if case == "missing_credential"
                else "E2E_SYNTHETIC_ALICE,E2E_SYNTHETIC_BOB"
            ),
            "ALLOWED_TOOLS_JSON": json.dumps(["session_list", "http_compare_sessions"]),
            "TOOL_OPERATIONS_JSON": json.dumps(
                {
                    "session_list": ["response_analysis"],
                    "http_compare_sessions": ["response_analysis"],
                }
            ),
        }.items():
            monkeypatch.setenv(f"{prefix}_{key}", value)
        monkeypatch.setenv("E2E_SYNTHETIC_ALICE", "e2e-synthetic-alice")
        if case == "missing_credential":
            monkeypatch.delenv("E2E_SYNTHETIC_BOB", raising=False)
        else:
            monkeypatch.setenv("E2E_SYNTHETIC_BOB", "e2e-synthetic-bob")

        config = AppConfig(
            engagement=EngagementConfig(
                name="Synthetic financial principal E2E",
                scope=EngagementScope(allowed_hosts=["127.0.0.1"]),
                execution_budget=ExecutionBudget(max_actions=8),
                rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
                approval_policy=ApprovalPolicy(auto_approve=True),
                metadata={
                    "success_criteria": [
                        "서로 다른 두 테스트 세션으로 동일 객체에 접근한 결과가 저장된다",
                        "두 principal 비교 결과가 종료 보고서에 포함된다",
                    ],
                    "target_refs": {"127.0.0.1": "bank"},
                    "auth_sessions": {
                        "127.0.0.1": [
                            {"session_ref": ref, "paths": ["/orders/1234"]}
                            for ref in ("alice", "bob")
                        ]
                    },
                },
            ),
            capability=CapabilityConfig(mcp_servers=["principal-e2e"]),
        )
        task_db = tmp_path / "tasks.sqlite3"
        work_db = tmp_path / "work.sqlite3"
        settings = OrchestrationSettings(
            max_steps=30,
            decision_max_attempts=6,
            task_graph_backend="sqlite",
            task_graph_path=str(task_db),
            work_backend="sqlite",
            work_path=str(work_db),
            path_backend="sqlite",
            operator_intent_backend="sqlite",
            operator_intent_path=str(tmp_path / "intents.sqlite3"),
            attack_graph_backend="sqlite",
            attack_graph_path=str(tmp_path / "graph.sqlite3"),
        )
        llm = _TransportLLM()
        engagement, deps = await build_application(
            config,
            mcp_stack=stack,
            llm=llm,
            orchestration_settings=settings,
            evidence_dir=tmp_path / "evidence",
            audit_dir=tmp_path / "audit",
            approval_dir=tmp_path / "approvals",
        )
        llm._graph, llm._evidence = deps.graph_store, deps.evidence_store
        assert deps.tool_catalog is not None
        llm._catalog = deps.tool_catalog
        graph = AttackGraphService(deps.graph_store)
        await graph.upsert_node(
            GraphNodeType.ENDPOINT,
            node_id=endpoint_node_id("GET", "/orders/1234", host="127.0.0.1"),
            facts={
                "host": "127.0.0.1",
                "port": port,
                "path": "/orders/1234",
                "method": "GET",
                "target_ref": "bank",
                "path_parameters": ["orders_id"],
            },
        )
        objective = "Compare operator-provided principals on the synthetic account"
        events: list[EventEnvelope] = []

        async def collect(event: EventEnvelope) -> None:
            events.append(event)

        await deps.event_bus.subscribe("*", collect)
        final = await run_engagement(engagement, deps=deps, objective=objective)

        # Reopen the durable domain stores rather than relying on live objects.
        assert final.mission_id is not None
        reopened_tasks = SQLiteTaskGraphStore(task_db)
        mission = await reopened_tasks.get_mission(final.mission_id)
        tasks = await reopened_tasks.list_tasks(mission_id=final.mission_id)
        report = load_terminal_report(final.run_id)
        assert mission is not None and mission.objective == objective
        assert len(tasks) == 1, "the native loop settles the single bootstrap task"
        task = tasks[0]
        assert task.objective == mission.objective
        assert report is not None and report.mission is not None
        assert report.mission.status == mission.status.value
        finished = next(e for e in events if e.event_type == EventType.RUN_FINISHED)
        assert finished.payload["termination_reason"] == final.termination_reason
        assert finished.payload["actions_executed"] == final.actions_executed

        # No plan was fabricated: the native loop records no path at all.
        assert await SQLitePathStore(task_db).get_active(str(final.run_id)) is None
        assert await SQLitePathStore(task_db).list_paths(str(final.run_id)) == []

        assert deps.tool_catalog is not None
        discovery_tool = deps.tool_catalog.get(ToolId("web.session_list"))
        compare_tool = deps.tool_catalog.get(ToolId("web.http_compare_sessions"))
        assert discovery_tool is not None and compare_tool is not None
        # Reopen the shared filesystem evidence store as well: the durable
        # proof must not come from the live in-process object.
        records = await FilesystemEvidenceStore(tmp_path / "evidence").list_for_run(
            final.run_id
        )
        discovery_records = _records_for_tool(records, str(discovery_tool.tool_id))
        compare_records = _records_for_tool(records, str(compare_tool.tool_id))
        assert discovery_records and compare_records

        # Durable work/task/call provenance for both real tool executions,
        # verified through reopened stores rather than the live objects.
        reopened_work = SQLiteWorkStore(work_db)
        for record, call_id, tool in (
            (discovery_records[0], DISCOVERY_CALL_ID, discovery_tool),
            (compare_records[0], COMPARE_CALL_ID, compare_tool),
        ):
            metadata = record.metadata
            assert metadata["tool_id"] == str(tool.tool_id)
            assert metadata["model_tool_call_id"] == call_id
            assert metadata["mission_task_id"] == str(task.task_id)
            assert metadata["execution_origin_version"] == 1
            work = await reopened_work.get(str(metadata["work_id"]))
            assert work is not None
            assert work.mission_task_id == str(task.task_id)
            assert work.model_tool_call_id == call_id
        discovery_work = await reopened_work.get(str(discovery_records[0].metadata["work_id"]))
        assert discovery_work is not None and discovery_work.status is WorkStatus.SUCCEEDED
        compare_work = await reopened_work.get(str(compare_records[0].metadata["work_id"]))
        assert compare_work is not None

        # The compared refs and per-request receipts are the actual originals.
        discovery_output = discovery_records[0].metadata["tool_result"]["output"]
        assert {row["session_ref"] for row in discovery_output["sessions"]} == {"alice", "bob"}
        comparison_output = compare_records[0].metadata["tool_result"]["output"]
        serialized = (
            json.dumps([r.model_dump(mode="json") for r in records]) + report.model_dump_json()
        )
        assert "e2e-synthetic-alice" not in serialized
        assert "e2e-synthetic-bob" not in serialized
        assert await graph.list_findings() == [], "HTTP execution alone proves no vulnerability"
        assert all(method == "GET" and target == "/orders/1234" for method, target, _ in requests)
        if case == "success":
            assert requests == [("GET", "/orders/1234", 200), ("GET", "/orders/1234", 403)]
            assert final.termination_reason is TerminationReason.GOAL_ACHIEVED, json.dumps(
                llm.reviews
            )
            assert mission.status is MissionStatus.COMPLETED
            assert task.status is TaskStatus.COMPLETED
            assert mission.criterion_results and all(
                r.met and r.evidence_ids for r in mission.criterion_results
            )
            assert all(c.met and c.evidence_count for c in report.mission.success_criteria)
            assert comparison_output["first"]["actual_session_ref"] == "alice"
            assert comparison_output["second"]["actual_session_ref"] == "bob"
            assert compare_work.status is WorkStatus.SUCCEEDED
            assert {"task_completion_review", "goal_completion_review"} <= set(llm.reviews)
        else:
            # The capability preflights BOTH credentials before either HTTP
            # request, so a missing credential leaves no partial probe.
            expected_count = 2 if case == "unauthenticated" else 0
            assert len(requests) == expected_count
            if case == "missing_credential":
                assert compare_records[0].metadata["tool_result"]["success"] is False
                assert compare_work.status is not WorkStatus.SUCCEEDED
            if case == "unauthenticated":
                # The real transport still ran both requests; both denied.
                assert comparison_output["first"]["status_code"] == 401
                assert comparison_output["second"]["status_code"] == 401
            assert final.termination_reason is TerminationReason.INVESTIGATION_HELD
            assert mission.status is MissionStatus.BLOCKED
            assert task.status is not TaskStatus.COMPLETED
            assert mission.criterion_results and not all(r.met for r in mission.criterion_results)
            assert not all(c.met for c in report.mission.success_criteria)
