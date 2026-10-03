"""Production composition and MCP stdio to a real local HTTP target.

Only the model is scripted. Control, gateway, MCP subprocess, HTTP requests,
evidence, path revisions, mission gates and terminal reports are production code.
This proves transport/runtime wiring, not the quality of a live model's decisions.
"""

from __future__ import annotations

import asyncio
import copy
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
from redteam.core.enums import GraphNodeType, MissionStatus
from redteam.orchestration.paths.sqlite import SQLitePathStore
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from tests.integration import _engine_llm_harness as h
from tests.integration.test_second_principal_mission import _CompareLLM


class _TransportLLM(_CompareLLM):
    """Discover registered references, revise, then decide from real receipts."""

    def _draft(self, host_id: str) -> dict[str, Any]:
        draft = super()._draft(host_id)
        draft["steps"][0].update(
            objective="discover operator-provided principal references",
            principal_refs=[],
        )
        return draft

    async def complete_structured(
        self, *, messages: Any, response_schema: dict[str, Any], system: Any = None, **_: Any
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        if route != "next_action":
            return await super().complete_structured(
                messages=messages, response_schema=response_schema, system=system
            )
        self.calls.append({"route": route, "messages": messages, "system": system})
        self._next_calls += 1
        payload = self._payload(messages)
        run_id = payload["active_path"]["run_id"]
        records = await self._evidence.list_for_run(run_id)
        host_id = await self._host_id()

        def receipts(step_id: str, tool_id: str) -> list[str]:
            return [
                str(r.evidence_id) for r in records
                if r.metadata.get("path_step_id") == step_id
                and r.source_id == tool_id and isinstance(r.metadata.get("tool_result"), dict)
            ]

        if self._next_calls == 1:
            turn = h.turn_actions(host_id)
            turn["actions"][0].update(
                agent_id="web-api-agent", capability="web", operation="response_analysis",
                tool_id="web.session_list", arguments={},
                reason="discover supplied opaque sessions",
            )
            return turn
        if self._next_calls == 2:
            found = receipts("step-1", "web.session_list")
            assert found, "session discovery must produce a real receipt"
            return h.turn_complete_step(found)
        if self._next_calls == 3:
            revised = copy.deepcopy(payload["active_path"])
            compare = super()._draft(host_id)["steps"][0]
            compare.update(step_id="step-2", depends_on=["step-1"])
            revised["steps"].append(compare)
            turn = h.turn_complete_goal("use discovered sessions to revise the path")
            turn.update(mode="revise_path", path=revised)
            return turn
        if self._next_calls == 4:
            turn = h.turn_actions(host_id)
            turn["actions"][0].update(
                path_step_id="step-2", agent_id="web-api-agent", principal_ref="alice",
                capability="web", operation="response_analysis",
                tool_id="web.http_compare_sessions",
                arguments={
                    "target_ref": "bank", "path": "/orders/1234",
                    "first_session_ref": "alice", "second_session_ref": "bob",
                },
                reason="compare discovered principals on the approved object",
            )
            return turn
        if self._next_calls == 5:
            if not await self._comparison_conclusive():
                turn = h.turn_complete_goal("second principal could not be authenticated")
                turn.update(mode="hold")
                return turn
            found = receipts("step-2", "web.http_compare_sessions")
            assert found, "comparison must produce a real receipt"
            return {**h.turn_complete_step(found), "path_step_id": "step-2"}
        return h.turn_complete_goal()


@pytest.mark.parametrize("case", ["success", "unauthenticated", "missing_credential"])
async def test_principal_discovery_revision_and_completion_over_real_mcp(
    case: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    requests: list[tuple[str, str, int]] = []

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).decode()
            method, path, _ = headers.split("\r\n", 1)[0].split()
            bearer = next((
                line.split(":", 1)[1].strip() for line in headers.split("\r\n")[1:]
                if line.lower().startswith("authorization:")
            ), "")
            principal = "alice" if bearer == "Bearer e2e-synthetic-alice" else "bob"
            status = 401 if case == "unauthenticated" else (200 if principal == "alice" else 403)
            requests.append((method, path, status))
            body = b'{"account":"synthetic-alice","balance":1000}' if status == 200 else b'{}'
            writer.write(
                f"HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        server = await asyncio.start_server(respond, "127.0.0.1", 0)
    except PermissionError:
        pytest.skip("sandbox does not permit binding a local E2E target port")
    async with server, AsyncExitStack() as stack:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.chdir(tmp_path)
        policy = tmp_path / "policy.json"
        policy.write_text(json.dumps({
            "targets": {"bank": {
                "origin": f"http://127.0.0.1:{port}", "allowed_paths": ["/orders/1234"],
            }},
            "allowed_networks": ["127.0.0.1/32"],
            "sessions": {ref: {
                "target_ref": "bank", "principal_ref": f"{ref}-principal",
                "bearer_env": f"E2E_SYNTHETIC_{ref.upper()}",
            } for ref in ("alice", "bob")},
        }))
        prefix = "REDTEAM_MCP_PRINCIPAL_E2E"
        for key, value in {
            "KIND": "web_api", "TRANSPORT": "stdio", "COMMAND": sys.executable,
            "ARGS_JSON": json.dumps([
                "-m", "redteam.capability.servers", "web-http", "--policy", str(policy),
            ]),
            "PASSTHROUGH_ENV": (
                "E2E_SYNTHETIC_ALICE" if case == "missing_credential"
                else "E2E_SYNTHETIC_ALICE,E2E_SYNTHETIC_BOB"
            ),
            "ALLOWED_TOOLS_JSON": json.dumps(["session_list", "http_compare_sessions"]),
            "TOOL_OPERATIONS_JSON": json.dumps({
                "session_list": ["response_analysis"],
                "http_compare_sessions": ["response_analysis"],
            }),
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
                    "auth_sessions": {"127.0.0.1": [
                        {"session_ref": ref, "paths": ["/orders/1234"]}
                        for ref in ("alice", "bob")
                    ]},
                },
            ),
            capability=CapabilityConfig(mcp_servers=["principal-e2e"]),
        )
        task_db = tmp_path / "tasks.sqlite3"
        settings = OrchestrationSettings(
            max_steps=30, decision_max_attempts=6,
            task_graph_backend="sqlite", task_graph_path=str(task_db), path_backend="sqlite",
            operator_intent_backend="sqlite",
            operator_intent_path=str(tmp_path / "intents.sqlite3"),
            attack_graph_backend="sqlite", attack_graph_path=str(tmp_path / "graph.sqlite3"),
        )
        llm = _TransportLLM()
        engagement, deps = await build_application(
            config, mcp_stack=stack, llm=llm, orchestration_settings=settings,
            evidence_dir=tmp_path / "evidence", audit_dir=tmp_path / "audit",
            approval_dir=tmp_path / "approvals",
        )
        llm._graph, llm._evidence = deps.graph_store, deps.evidence_store
        graph = AttackGraphService(deps.graph_store)
        await graph.upsert_node(
            GraphNodeType.ENDPOINT,
            node_id=endpoint_node_id("GET", "/orders/1234", host="127.0.0.1"),
            facts={
                "host": "127.0.0.1", "port": port, "path": "/orders/1234", "method": "GET",
                "target_ref": "bank", "path_parameters": ["orders_id"],
            },
        )
        objective = "Compare operator-provided principals on the synthetic account"
        events: list[EventEnvelope] = []

        async def collect(event: EventEnvelope) -> None:
            events.append(event)

        await deps.event_bus.subscribe("*", collect)
        final = await run_engagement(engagement, deps=deps, objective=objective)

        # Reopen durable domain stores rather than relying on live objects.
        assert final.mission_id is not None
        mission = await SQLiteTaskGraphStore(task_db).get_mission(final.mission_id)
        path = await SQLitePathStore(task_db).get_active(str(final.run_id))
        versions = await SQLitePathStore(task_db).list_paths(str(final.run_id))
        report = load_terminal_report(final.run_id)
        assert mission is not None and mission.objective == objective
        assert path is not None and path.version == 2
        assert {v.version for v in versions} == {1, 2}
        assert path.steps[0].evidence_ids and path.steps[0].status == "completed"
        assert path.steps[1].depends_on == ["step-1"]
        assert report is not None and report.mission is not None
        assert report.mission.status == mission.status.value
        finished = next(e for e in events if e.event_type == EventType.RUN_FINISHED)
        assert finished.payload["termination_reason"] == final.termination_reason
        assert finished.payload["actions_executed"] == final.actions_executed
        records = await deps.evidence_store.list_for_run(final.run_id)
        comparisons = [r for r in records if r.source_id == "web.http_compare_sessions"]
        assert comparisons and comparisons[0].metadata["path_version"] == 2
        assert comparisons[0].metadata["path_step_id"] == "step-2"
        serialized = (
            json.dumps([r.model_dump(mode="json") for r in records]) + report.model_dump_json()
        )
        assert "e2e-synthetic-alice" not in serialized
        assert "e2e-synthetic-bob" not in serialized
        assert await graph.list_findings() == [], "HTTP execution alone proves no vulnerability"
        assert all(method == "GET" and target == "/orders/1234" for method, target, _ in requests)
        if case == "success":
            assert requests == [("GET", "/orders/1234", 200), ("GET", "/orders/1234", 403)]
            assert final.termination_reason is TerminationReason.GOAL_ACHIEVED
            assert mission.status is MissionStatus.COMPLETED
            assert mission.criterion_results and all(
                r.met and r.evidence_ids for r in mission.criterion_results
            )
            assert path.steps[1].status == "completed" and path.steps[1].evidence_ids
            assert all(c.met and c.evidence_count for c in report.mission.success_criteria)
        else:
            # Capability preflights BOTH credentials before either HTTP request.
            # Missing Bob's credential must not leave a partial Alice-only probe.
            expected_count = 2 if case == "unauthenticated" else 0
            assert len(requests) == expected_count
            if case == "missing_credential":
                assert comparisons[0].metadata["tool_result"]["success"] is False
            assert final.termination_reason is TerminationReason.INVESTIGATION_HELD
            assert mission.status is MissionStatus.BLOCKED
            assert mission.criterion_results and not all(r.met for r in mission.criterion_results)
            assert not all(c.met for c in report.mission.success_criteria)
