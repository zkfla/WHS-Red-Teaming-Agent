"""Feedback 05: scripted shell-shaped originals through the common worker chain.

Labeled doubles: SchemaSensitiveProbe and ScriptedNegative are scripted fake
models; the gateway is a synthetic DevelopmentToolGateway returning a shell
execution envelope. No live model, target, or Kali/program execution is
contacted. Envelope exit codes are tool-call data; they are not Findings.
"""

import json

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.capability.development import DevelopmentResponse, DevelopmentToolGateway
from redteam.core.enums import ApprovalStatus, TaskStatus, ToolHealthStatus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_completion import OriginalProtocolDouble
from tests.integration.test_central_development import TOOL, call, gateway, turn
from tests.integration.test_native_schema_delivery import SchemaSensitiveProbe
from tests.integration.test_probe_workers import drive, register, selection

MARKER = "WF05-STDOUT-HEAD"


def shell_envelope(*, exit_code=3, stdout_chars=6000, truncated=True):
    return {
        "exit_code": exit_code,
        "stdout": MARKER + ("x" * stdout_chars),
        "stderr": "fixture-stderr",
        "timed_out": False,
        "truncated": truncated,
        "duration_ms": 1234,
        "script_sha256": "f" * 64,
        "workroom_key": "wf05-workroom",
    }


def shell_gateway(envelope, *, risk="low", unknown=False, extra=()):
    descriptor = gateway(risk=risk).catalog.all_descriptors()[0]
    metadata = {"resource_effects": [], **({"execution_uncertain": True} if unknown else {})}
    return OriginalProtocolDouble(
        DevelopmentToolGateway(
            [descriptor, *extra],
            [DevelopmentResponse(output=envelope, metadata=dict(metadata)) for _ in range(6)],
        )
    )


def delivered_windows(llm):
    windows = []
    for request in llm.envelopes:
        for message in request["messages"]:
            if message.get("role") != "tool":
                continue
            body = json.loads(message.get("content") or "{}")
            if isinstance(body, dict) and body.get("original_results"):
                windows.extend(body["original_results"])
    return windows


class ScriptedNegative(FakeLLMClient):
    """Requests one call the catalog must refuse, then returns no further calls."""

    def __init__(self, tool_id, arguments):
        super().__init__()
        self.tool_id = tool_id
        self.arguments = arguments
        self.envelopes = []
        self.exposed_names = []

    async def complete_native(self, messages, tools, system=None, **kwargs):
        self.envelopes.append({"messages": messages, "tools": tools, "system": system})
        self.exposed_names.append({item.name for item in tools})
        first = len(self.envelopes) == 1
        self.native_responses = [
            turn(call("negative", self.tool_id, self.arguments)) if first else turn()
        ]
        return await super().complete_native(messages, tools, system=system, **kwargs)


async def test_shell_envelope_reaches_worker_bounded_and_never_becomes_a_finding(tmp_path):
    envelope = shell_envelope()
    async with selection(tmp_path, tools=shell_gateway(envelope), llm=SchemaSensitiveProbe()) as (
        session,
        llm,
        tools,
    ):
        await register(session, "shell-feedback", actions=2)
        (outcome,) = await drive(session)
        assert outcome.kind == "completed", outcome.error
        windows = delivered_windows(llm)
        assert any(MARKER in json.dumps(window) for window in windows)
        assert all(len(window.get("text", "")) <= 4000 for window in windows)
        assert any(window.get("truncated") is True for window in windows)
        assert await AttackGraphService(session.deps.graph_store).list_findings() == []


async def test_planner_input_carries_bounded_envelope_summary_without_finding(tmp_path):
    envelope = shell_envelope()
    async with selection(tmp_path, tools=shell_gateway(envelope), llm=SchemaSensitiveProbe()) as (
        session,
        llm,
        tools,
    ):
        await register(session, "planner-capture", actions=2)
        (outcome,) = await drive(session)
        assert outcome.kind == "completed", outcome.error
        await SpecialistPlanner(session.deps).advance(session.runtime)
        planner = [
            item
            for item in llm.requests
            if item.get("kind") == "complete_structured" and MARKER in json.dumps(item["messages"])
        ]
        assert planner, "Planner never received the original-derived content"
        assert all("x" * 4500 not in json.dumps(item["messages"]) for item in planner)
        assert await AttackGraphService(session.deps.graph_store).list_findings() == []


async def test_approval_wait_then_resume_uses_same_invocation_with_envelope(tmp_path):
    envelope = shell_envelope()
    async with selection(
        tmp_path, tools=shell_gateway(envelope, risk="high"), llm=SchemaSensitiveProbe()
    ) as (session, llm, tools):
        identity = await register(session, "approval-envelope", actions=2)
        (outcome,) = await drive(session)
        task = await session.deps.central_state.get_task(identity)
        assert task.status is TaskStatus.BLOCKED and not tools.inner.invocations
        assert len(llm.requests) == 1  # No model polling while approval is pending.
        (original,) = await session.deps.central_state.list_executions()
        await session.deps.control_plane.resolve_approval(
            outcome.runtime.pending_approval_id,
            ApprovalStatus.APPROVED,
            "fixture:operator",
        )
        recovered, recovery = await session.driver.recover_central_instance(outcome.runtime)
        assert recovery.ready, recovery.errors
        await session.driver.runner_for(recovered).resume_recovered(recovered)
        assert len(tools.inner.invocations) == 1
        assert tools.inner.invocations[0].invocation_id == original.invocation_id
        assert any(MARKER in json.dumps(window) for window in delivered_windows(llm))
        assert await AttackGraphService(session.deps.graph_store).list_findings() == []


async def test_unknown_execution_keeps_envelope_as_data_and_creates_no_finding(tmp_path):
    envelope = shell_envelope(exit_code=0, truncated=False)
    async with selection(
        tmp_path, tools=shell_gateway(envelope, unknown=True), llm=SchemaSensitiveProbe()
    ) as (session, llm, tools):
        await register(session, "unknown-envelope", actions=2)
        (outcome,) = await drive(session)
        assert outcome.kind == "completed", outcome.error
        executions = await session.deps.central_state.list_executions()
        assert len(tools.inner.invocations) == 1
        assert not delivered_windows(llm) and len(llm.requests) == 1
        assert all(
            "UNKNOWN" in str(item.state) or "PENDING" in str(item.state) for item in executions
        )
        assert await AttackGraphService(session.deps.graph_store).list_findings() == []


@pytest.mark.parametrize("case", ["unregistered", "unready", "missing_required_argument"])
async def test_negative_tool_requests_are_refused_without_invocation(tmp_path, case):
    extra = ()
    tool_id, arguments = TOOL, {"host": "unscoped.example"}  # Omits required "account".
    if case == "unregistered":
        tool_id, arguments = "web.missing", {"host": "unscoped.example", "account": "a"}
    elif case == "unready":
        descriptor = gateway().catalog.all_descriptors()[0]
        extra = (
            descriptor.model_copy(
                update={"tool_id": "web.unhealthy", "health_status": ToolHealthStatus.UNHEALTHY}
            ),
        )
        tool_id, arguments = "web.unhealthy", {"host": "unscoped.example", "account": "a"}
    tools = shell_gateway(shell_envelope(), extra=extra)
    negative = ScriptedNegative(tool_id, arguments)
    async with selection(tmp_path, tools=tools, llm=negative) as (session, llm, _):
        await register(session, "negative-" + case, actions=2)
        (outcome,) = await drive(session)
        assert not tools.inner.invocations
        # Observed public refusal text, pinned as current behavior. unready and unregistered
        # share one message today; that distinction is an open diagnostic gap for root.
        refusals = [
            json.loads(message["content"])
            for message in llm.envelopes[1]["messages"]
            if message.get("role") == "tool"
        ]
        expected = {
            "unregistered": "unknown tool",
            "unready": "unknown tool",
            "missing_required_argument": "ambiguous_tool_target",
        }[case]
        assert refusals and all(item["ok"] is False for item in refusals)
        assert any(expected in item["error"] for item in refusals)
        if case == "unready":
            assert all(
                exposed_tool_name("web.unhealthy") not in names for names in llm.exposed_names
            )
        assert await AttackGraphService(session.deps.graph_store).list_findings() == []
