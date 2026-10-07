"""CLI persistence and mandatory-model contracts through real commands (native loop)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.app.cli.commands import run as run_command
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.service import DefaultControlPlane
from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import ApprovalPolicy
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import ApprovalStatus, PolicyVerdict
from redteam.core.ids import ActionId, AgentId, RunId
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.local_tools import LocalToolRegistry
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.native_local_tools import (
    GOAL_SETTLE_TOOL_ID,
    TASK_SETTLE_TOOL_ID,
    goal_settle_descriptor,
    goal_settle_handler,
    task_settle_descriptor,
    task_settle_handler,
)
from redteam.orchestration.paths import InMemoryPathStore
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from tests.integration import _engine_llm_harness as h

_ENGAGEMENT = """
name: CLI Smoke
scope:
  allowed_hosts: [host-01]
execution_budget:
  max_actions: 5
rate_limit_policy:
  max_actions_per_minute: 30
approval_policy:
  require_approval_for: []
"""


def _native_tool_registry() -> LocalToolRegistry:
    registry = LocalToolRegistry()
    registry.register(task_settle_descriptor(), task_settle_handler)
    registry.register(goal_settle_descriptor(), goal_settle_handler)
    return registry


def _exposed_names(tools: list[Any]) -> set[str]:
    return {str(getattr(spec, "name", "")) for spec in tools}


def _remote_executed_evidence(messages: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for message in messages:
        if message.get("role") != "tool" or not message.get("tool_call_id"):
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            continue
        if not isinstance(body, dict) or body.get("ok") is not True:
            continue
        if body.get("executed") is not True:
            continue
        for identity in body.get("evidence_ids") or []:
            text = str(identity)
            if text and text not in found:
                found.append(text)
    return found


def _tool_result(messages: list[dict[str, Any]], call_id: str) -> dict[str, Any] | None:
    for message in messages:
        if message.get("role") != "tool" or message.get("tool_call_id") != call_id:
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            return None
        return body if isinstance(body, dict) else None
    return None


class _CliCountingGateway:
    """Tool gateway double that counts real invocations and answers success."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def invoke(self, invocation: Any) -> ToolResult:
        self.calls.append(invocation)
        now = h.utcnow()
        return ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            success=True,
            output={"inventory": "ok", "host": h.HOST},
            started_at=now,
            finished_at=now,
            metadata={},
        )


class _CliNativeLLM:
    """Native scripted model: one inventory call, then task/goal settlement."""

    def __init__(self, *, graph_store: Any, evidence_store: Any) -> None:
        self._graph = graph_store
        self._evidence = evidence_store
        self.calls: list[dict[str, Any]] = []
        self.reviews: list[str] = []
        self.turns = 0
        self._task_call: str | None = None
        self._goal_call: str | None = None

    @staticmethod
    def _route_key(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        if "criteria" in props:
            return "goal_completion_review"
        if "claims" in props:
            return "task_completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        return "unknown"

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        num_retries: int = 0,
        **_: Any,
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.reviews.append(route)
        if route == "analysis_verify":
            return h.meaning("success", True)
        if route == "task_completion_review":
            payload = json.loads(messages[0]["content"])
            receipts = list(payload["verified_source_ids"])
            assert receipts, "task review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "scripted semantic accept",
                "claims": [
                    {
                        "description": "inventory tool result observed",
                        "kind": "observation",
                        "evidence_ids": list(receipts),
                        "target_node_ids": [],
                        "principal_refs": [],
                    }
                ],
            }
        if route == "goal_completion_review":
            payload = json.loads(messages[0]["content"])
            criteria = payload["mission"]["success_criteria"]
            receipts = list(payload["verified_source_ids"])
            assert receipts, "goal review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "scripted goal accept",
                "criteria": [
                    {
                        "criterion": item,
                        "met": True,
                        "evidence_ids": list(receipts),
                        "claims": [
                            {
                                "description": "inventory tool result observed",
                                "kind": "observation",
                                "evidence_ids": list(receipts),
                                "target_node_ids": [],
                                "principal_refs": [],
                            }
                        ],
                    }
                    for item in criteria
                ],
            }
        raise AssertionError(
            f"legacy strategy route is not part of the native loop: {route!r}"
        )

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        return ""

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        names = _exposed_names(tools)
        inventory = exposed_tool_name(h.INVENTORY_TOOL)
        task_tool = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_tool = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert inventory in names, "native model needs the registered inventory tool"
        assert task_tool in names, "native model needs the task settlement helper"
        assert goal_tool in names, "native model needs the goal settlement helper"
        self.turns += 1
        evidenced = _remote_executed_evidence(messages)
        self.calls.append({"turn": self.turns, "evidenced": list(evidenced)})
        if self.turns == 1:
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(call_id="call-cli-1", name=inventory, arguments={}),
                ),
                text="",
            )
        if self._task_call is None:
            assert evidenced, "task settlement needs the executed call evidence"
            self._task_call = "call-cli-task"
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._task_call,
                        name=task_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "task evidence ready",
                            "evidence_ids": list(evidenced),
                        },
                    ),
                ),
                text="",
            )
        seen = _tool_result(messages, self._task_call)
        if seen is None or seen.get("ok") is not True:
            return NativeCompletion(tool_calls=(), text="")
        if self._goal_call is None:
            self._goal_call = "call-cli-goal"
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._goal_call,
                        name=goal_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "goal evidence ready",
                            "evidence_ids": list(evidenced),
                        },
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="")


def _native_world():
    engagement = h.make_engagement(engagement_id="eng_e2e")
    llm = _CliNativeLLM(
        graph_store=InMemoryAttackGraphStore(), evidence_store=h.FakeEvidenceStore()
    )
    gateway = _CliCountingGateway()
    catalog = h.FakeCatalog([h.inventory_descriptor()])
    deps = RuntimeDeps(
        control_plane=DefaultControlPlane(
            {engagement.engagement_id: engagement},
            outbound_descriptor_resolver=catalog.get,
        ),
        tool_gateway=gateway,
        event_bus=InMemoryEventBus(),
        graph_store=llm._graph,
        evidence_store=llm._evidence,
        llm_client=llm,
        engagement=engagement,
        agents=default_registry(),
        tool_catalog=catalog,
        local_tools=_native_tool_registry(),
        work_store=InMemoryWorkStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        operator_intent_store=None,
        settings=OrchestrationSettings(
            max_steps=8, checkpointing=False, decision_max_attempts=6
        ),
    )
    return engagement, deps, gateway


async def _run_cli(*args: str, cwd: Path) -> tuple[int, str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("REDTEAM_")
    }
    environment["REDTEAM_LLM_PROVIDER"] = "fake"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "redteam.app.cli",
        *args,
        cwd=str(cwd),
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode()


async def test_init_validate_and_persisted_status_findings_report(tmp_path, monkeypatch):
    code, out, err = await _run_cli("init", "--output", "engagement.yaml", cwd=tmp_path)
    assert code == 0, err
    assert "Created engagement config" in out
    code, out, err = await _run_cli("validate", "engagement.yaml", cwd=tmp_path)
    assert code == 0, err
    assert "Configuration valid." in out
    monkeypatch.chdir(tmp_path)
    engagement, deps, gateway = _native_world()
    final = await run_engagement(engagement, deps)
    assert len(gateway.calls) == 1
    code, out, err = await _run_cli("status", str(final.run_id), cwd=tmp_path)
    assert code == 0, err
    assert "FINISHED" in out
    code, _, err = await _run_cli("findings", "list", str(final.run_id), cwd=tmp_path)
    assert code == 0, err
    code, out, err = await _run_cli("report", "generate", str(final.run_id), cwd=tmp_path)
    assert code == 0, err
    assert "Report generated" in out
    assert (tmp_path / "reports" / str(final.run_id) / "report.md").exists()


@pytest.mark.parametrize("json_mode", [False, True])
async def test_fake_provider_is_rejected_before_run(tmp_path, json_mode):
    (tmp_path / "engagement.yaml").write_text(_ENGAGEMENT, encoding="utf-8")
    options = ["--json"] if json_mode else []
    code, out, err = await _run_cli(
        "run", "engagement.yaml", "--non-interactive", *options, cwd=tmp_path
    )
    assert code != 0
    assert "fake" in err and "Traceback" not in err
    assert "ToolExecutionStarted" not in out
    assert not (tmp_path / "data" / "reports").exists()


async def test_json_command_streams_actual_events(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "litellm")
    monkeypatch.delenv("REDTEAM_MCP_SERVERS", raising=False)
    path = tmp_path / "engagement.yaml"
    path.write_text(_ENGAGEMENT, encoding="utf-8")
    engagement, deps, gateway = _native_world()

    async def build(config, **kwargs):
        return engagement, deps

    monkeypatch.setattr(run_command, "build_application", build)
    code = await run_command.run(
        argparse.Namespace(
            engagement_path=str(path),
            non_interactive=True,
            json=True,
            max_steps=None,
            log_level=None,
            objective=None,
        )
    )
    assert code == 0 and len(gateway.calls) == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert events and events[-1]["event_type"] == "RunFinished"
    assert any(event["event_type"] == "ToolExecutionFinished" for event in events)


@pytest.mark.parametrize(
    "command,status",
    [
        ("approve", ApprovalStatus.APPROVED),
        ("deny", ApprovalStatus.REJECTED),
    ],
)
async def test_approval_command_resolves_durable_policy_request(tmp_path, command, status):
    engagement = h.make_engagement().model_copy(
        update={
            "approval_policy": ApprovalPolicy(require_approval_for=["recon"]),
        }
    )
    approvals = ApprovalService(FilesystemApprovalStore(tmp_path / "data" / "approvals"))
    control = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        approvals=approvals,
        audit_log=AuditLog(FilesystemAuditStore(tmp_path / "data" / "audit")),
    )
    proposal = ActionProposal(
        action_id=ActionId("action-cli"),
        run_id=RunId("run-cli"),
        engagement_id=engagement.engagement_id,
        agent_id=AgentId(h.RECON_AGENT),
        capability="recon",
        operation="service_discovery",
        target=HostTarget(host=h.HOST),
        arguments={},
        reason="inspect declared target",
        created_at=h.utcnow(),
    )
    decision = await control.evaluate(
        proposal, ControlContext(engagement_id=engagement.engagement_id)
    )
    assert decision.verdict is PolicyVerdict.REQUIRE_APPROVAL
    code, out, err = await _run_cli("approvals", command, str(decision.approval_id), cwd=tmp_path)
    assert code == 0, err
    assert status.value in out
    saved = await approvals.get(decision.approval_id)
    assert saved is not None and saved.status is status
    code, _, _ = await _run_cli("approvals", command, str(decision.approval_id), cwd=tmp_path)
    assert code != 0


async def test_no_subcommand_dispatches_to_interactive_not_run(tmp_path):
    code, out, _ = await _run_cli(cwd=tmp_path)
    assert "REDTEAM" in out
    assert "no command handler registered" not in out
    assert code == 2
