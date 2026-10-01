"""CLI decisions and configuration/approval boundaries use production session code."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from redteam.app.cli.commands import interactive
from redteam.app.cli.conversation import ConversationAssistant
from redteam.app.cli.interactive_commands import InteractiveSession
from redteam.control.stores.filesystem import FilesystemApprovalStore
from redteam.core.config import CapabilityConfig, LLMConfig, LoggingConfig, OrchestrationConfig
from redteam.core.contracts.policy import Approval
from redteam.core.enums import AgentPhase, ApprovalStatus
from redteam.core.ids import ActionId, ApprovalId, EngagementId, RunId
from redteam.intake.serializer import dump_engagement_yaml
from redteam.orchestration.state import RuntimeState
from tests.integration.test_cli_session import _FakeConsole
from tests.unit.test_cli_runtime_config import _engagement


class ConversationModel:
    def __init__(self, response, mutate=None):
        self.response = response
        self.mutate = mutate
        self.payloads = []

    async def complete_structured(self, *, messages, **kwargs):
        payload = json.loads(messages[0]["content"].split("\n\nPrevious attempt")[0])
        self.payloads.append(payload)
        if self.mutate is not None and len(self.payloads) == 1:
            self.mutate()
        if isinstance(self.response, Exception):
            raise self.response
        mode = self.response[min(len(self.payloads) - 1, len(self.response) - 1)]
        return {"mode": mode, "response": "정리했습니다.", "objective": "assess scope"}

    async def complete(self, **kwargs):
        raise AssertionError("structured decision required")


async def session_run(tmp_path, monkeypatch, model):
    path = tmp_path / "engagement.yaml"
    if not path.exists():
        path.write_text(dump_engagement_yaml(_engagement()), encoding="utf8")
    session = InteractiveSession(_engagement(), LLMConfig(), engagement_path=path)
    execute = AsyncMock(
        return_value=RuntimeState(
            run_id=RunId("run_schema"),
            engagement_id=EngagementId("eng_schema"),
            phase=AgentPhase.FINISHED,
        )
    )
    monkeypatch.setattr(interactive.run_command, "execute", execute)
    monkeypatch.setattr(
        interactive.run_command,
        "build",
        AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace())),
    )
    result = await interactive._session_loop(
        _FakeConsole(["요청을 처리해", "/exit"]),
        session,
        llm_config=LLMConfig(),
        orchestration_config=OrchestrationConfig(),
        capability_config=CapabilityConfig(),
        logging_config=LoggingConfig(),
        verbose=False,
        conversation=ConversationAssistant(model),
    )
    assert result == 0
    return execute, session


@pytest.mark.parametrize("mode", ["answer", "clarify", "guidance"])
async def test_nonexecution_modes_never_start_a_mission(tmp_path, monkeypatch, mode):
    execute, _ = await session_run(tmp_path, monkeypatch, ConversationModel([mode]))
    execute.assert_not_awaited()


async def test_model_failure_never_becomes_an_execution(tmp_path, monkeypatch):
    model = ConversationModel(RuntimeError("provider unavailable"))
    execute, _ = await session_run(tmp_path, monkeypatch, model)
    execute.assert_not_awaited()
    assert len(model.payloads) == 3


@pytest.mark.parametrize("new_mode,executions", [("answer", 0), ("execute", 1)])
async def test_changed_scope_requires_a_new_model_decision(
    tmp_path, monkeypatch, new_mode, executions
):
    path = tmp_path / "engagement.yaml"
    new = _engagement("new.example", budget=2)
    model = ConversationModel(
        ["execute", new_mode], lambda: path.write_text(dump_engagement_yaml(new), encoding="utf8")
    )
    execute, session = await session_run(tmp_path, monkeypatch, model)
    assert [p["scope"] for p in model.payloads] == [["old.example"], ["new.example"]]
    assert execute.await_count == executions
    assert session.engagement_config == new
    if executions:
        assert execute.call_args.kwargs["config"].engagement == new
        assert execute.call_args.kwargs["objective"] == "assess scope"


@pytest.mark.parametrize("case", ["foreign", "resolved", "pending"])
async def test_idle_approval_checks_actual_status_and_original_run(tmp_path, monkeypatch, case):
    monkeypatch.chdir(tmp_path)
    store = FilesystemApprovalStore("data/approvals")
    record = Approval(
        approval_id=ApprovalId("approval_original"),
        action_id=ActionId("action_original"),
        status=ApprovalStatus.APPROVED if case == "resolved" else ApprovalStatus.PENDING,
        requested_at=datetime.now(timezone.utc),
    )
    await store.save(record)
    monkeypatch.setattr(
        interactive.AuditLog,
        "find_approval_request",
        AsyncMock(
            return_value=SimpleNamespace(
                run_id="run_foreign" if case == "foreign" else "run_original"
            )
        ),
    )
    approve = AsyncMock(return_value=0)
    resume = AsyncMock(return_value=0)
    final = RuntimeState(
        run_id=RunId("run_original"),
        engagement_id=EngagementId("eng_original"),
        phase=AgentPhase.FINISHED,
        actions_executed=1,
    )

    async def resume_original(args, *, on_finished):
        assert args.run_id == "run_original"
        on_finished(final)
        return 0

    resume.side_effect = resume_original
    monkeypatch.setattr(interactive.approvals_command, "run_approve", approve)
    monkeypatch.setattr(interactive.resume_command, "run", resume)
    session = InteractiveSession(
        _engagement(),
        LLMConfig(),
        last_run_id=RunId("run_original"),
        last_pending_approval_id="approval_original",
    )
    await interactive._handle_idle_approval(_FakeConsole([]), session, "approve", "approve")
    if case == "pending":
        assert approve.await_count == resume.await_count == 1
        assert approve.call_args.args[0].approval_id == "approval_original"
        assert session.last_pending_approval_id is None
        assert session.last_actions_executed == 1
    else:
        approve.assert_not_awaited()
        resume.assert_not_awaited()
        assert session.last_pending_approval_id == "approval_original"
