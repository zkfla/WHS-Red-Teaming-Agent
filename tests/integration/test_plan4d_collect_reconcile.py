"""Plan 4d integration (F05/F17): central job lookup/collection and the collect-originals CLI."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

from redteam.app.central_development import build_central_development
from redteam.app.central_run import bind_run_metadata
from redteam.app.cli.commands import resume as resume_command
from redteam.app.cli.commands import run as run_command
from redteam.core.config import AppConfig, EngagementConfig, OrchestrationConfig
from redteam.core.contracts.execution import ExecutionState
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.development_jobs import DevelopmentJobs
from redteam.orchestration.local_tools import LocalToolContext
from tests.integration.test_central_development import engagement, gateway, settings
from tests.integration.test_central_recovery import start


async def test_central_job_reconcile_lists_masks_and_collects_without_resubmit(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    tools, llm, jobs = gateway(resource=True), FakeLLMClient(), DevelopmentJobs()
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=settings(), jobs=jobs,
    ) as selection:
        (original,) = await start(selection, llm)
        state = selection.deps.central_state
        (execution,) = await state.list_executions()
        assert execution.job_ref and execution.state is ExecutionState.UNKNOWN
        tasks_before = [(item.task_id, item.status) for item in await state.list_tasks()]
        mission_before = await state.get_mission(original.mission_id)

        async def call(arguments, runtime=None):
            context = LocalToolContext(deps=selection.deps, runtime=runtime or original)
            return await selection.deps.local_tools.call("job.reconcile", arguments, context)

        listed = await call({})
        assert listed.ok
        (row,) = listed.payload["jobs"]
        assert row["job_ref"] == execution.job_ref
        assert row["invocation_id"] == str(execution.invocation_id)
        assert set(row) == {
            "job_ref", "invocation_id", "action_id", "task_id",
            "instance_id", "state", "wait_reason",
        }

        compat = await call({"work_id": "w-1"})
        assert not compat.ok
        assert compat.payload["status"] == "work_id_not_supported_centrally"
        missing = await call({"invocation_id": "inv-missing"})
        assert not missing.ok and missing.payload["status"] == "no_registered_job"
        foreign = original.model_copy(update={"instance_id": "foreign-instance"})
        refused = await call({"invocation_id": str(execution.invocation_id)}, foreign)
        assert not refused.ok and refused.payload["status"] == "wrong_owner_ref"

        held = await call({"invocation_id": str(execution.invocation_id)})
        assert held.ok and held.payload["status"] in {"pending", "running", "unknown"}
        assert len(jobs.submissions) == 1 and len(tools.invocations) == 0

        jobs.states[execution.job_ref] = ExecutionState.SUCCEEDED
        collected = await call({"job_ref": execution.job_ref})
        assert collected.ok and collected.payload["status"] == "collected"
        assert collected.payload["invocation_id"] == str(execution.invocation_id)
        assert collected.refs == (collected.payload["result_evidence_id"],)
        settled = await state.get_execution(str(execution.invocation_id))
        assert settled.state is ExecutionState.SUCCEEDED
        assert settled.result_evidence_id is not None
        assert len(jobs.submissions) == 1 and len(tools.invocations) == 0
        terminal = {"completed", "cancelled", "failed"}
        live = await state.list_tasks()
        assert {item.task_id for item in live if item.status.value in terminal} == {
            task_id for task_id, status in tasks_before if status.value in terminal
        }
        assert (await state.get_mission(original.mission_id)).status == mission_before.status

        evidence_before = await selection.deps.evidence_store.list_for_run(original.run_id)
        again = await call({"invocation_id": str(execution.invocation_id)})
        assert again.ok and again.payload["status"] == "already_collected"
        assert again.payload["result_evidence_id"] == str(settled.result_evidence_id)
        evidence_after = await selection.deps.evidence_store.list_for_run(original.run_id)
        assert [item.evidence_id for item in evidence_after] == [
            item.evidence_id for item in evidence_before
        ]


async def test_collect_originals_cli_collects_only_and_creates_nothing(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.chdir(tmp_path)
    tools, llm = gateway(), FakeLLMClient()
    async with build_central_development(
        engagement(), llm=llm, tools=tools, settings=settings(),
    ) as selection:
        originals = await start(selection, llm)
        state = selection.deps.central_state
        executions = await state.list_executions()
        assert executions, "expected preserved executions"
        assert all(item.result_evidence_id is not None for item in executions)
        root = Path(selection.deps.settings.checkpoint_path).parent
        state.original_store_paths = {
            "evidence": str(root / "evidence"),
            "audit": str(root / "audit"),
            "approvals": str(root / "approvals"),
            "checkpoints": str(root / "checkpoints.sqlite3"),
        }
        await bind_run_metadata(selection.deps)
        run_id = str(originals[0].run_id)
        tasks_before = [(item.task_id, item.status) for item in await state.list_tasks()]
        mission_before = await state.get_mission(originals[0].mission_id)
        calls_before = len(tools.invocations)
        evidence_store = selection.deps.evidence_store
        evidence_before = [
            item.evidence_id for item in await evidence_store.list_for_run(originals[0].run_id)
        ]
        ledger_before = await state.request_ledger().snapshot(run_id)
        checkpoint_path = Path(selection.deps.settings.checkpoint_path)
        assert checkpoint_path.is_file()
        await selection.driver.close()
        checkpoint_path.unlink()

        async def fake_build(config, **kwargs):
            return None, selection.deps

        monkeypatch.setattr(run_command, "build", fake_build)
        raw = engagement()
        config = AppConfig(
            engagement=EngagementConfig.model_validate(
                raw.model_dump(exclude={"engagement_id", "created_at"}),
            ),
            orchestration=OrchestrationConfig(
                runtime_backend="central", central_state_path=str(state.path),
            ),
        )
        args = SimpleNamespace(
            run_id=run_id, instance=None, json=True,
            state_db=str(state.path), log_level=None,
        )
        async with AsyncExitStack() as stack:
            assert await resume_command._run_collect_central(args, config, mcp_stack=stack) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["run_id"] == run_id
        assert payload["outcome"] == "collected"
        assert payload["replay_permitted"] is False
        assert payload["claim_release_permitted"] is False
        assert len(payload["instances"]) >= 1
        assert all(
            item["originals"] and all(
                original["state"] == "known" for original in item["originals"]
            ) for item in payload["instances"]
        )
        receipts = [
            original["original_receipt_id"]
            for item in payload["instances"] for original in item["originals"]
        ]
        assert receipts and all(receipts)

        async with AsyncExitStack() as stack:
            assert await resume_command._run_collect_central(args, config, mcp_stack=stack) == 0
        again = json.loads(capsys.readouterr().out)
        assert [
            original["original_receipt_id"]
            for item in again["instances"] for original in item["originals"]
        ] == receipts
        assert not checkpoint_path.exists()
        assert len(tools.invocations) == calls_before
        assert [
            item.evidence_id for item in await evidence_store.list_for_run(originals[0].run_id)
        ] == evidence_before
        assert [(item.task_id, item.status) for item in await state.list_tasks()] == tasks_before
        assert (await state.get_mission(originals[0].mission_id)).status == mission_before.status
        assert await state.request_ledger().snapshot(run_id) == ledger_before


async def test_collect_originals_cli_refuses_missing_run_before_assembly(
    tmp_path, monkeypatch, capsys,
):
    async def forbidden(*args, **kwargs):
        raise AssertionError("missing original caused provider assembly")

    monkeypatch.setattr(run_command, "build", forbidden)
    raw = engagement()
    config = AppConfig(
        engagement=EngagementConfig.model_validate(
            raw.model_dump(exclude={"engagement_id", "created_at"}),
        ),
        orchestration=OrchestrationConfig(
            runtime_backend="central", central_state_path=str(tmp_path / "missing.sqlite3"),
        ),
    )
    args = SimpleNamespace(
        run_id="missing-original", instance=None, json=True,
        state_db=None, log_level=None,
    )
    async with AsyncExitStack() as stack:
        assert await resume_command._run_collect_central(args, config, mcp_stack=stack) == 1
    assert not (tmp_path / "missing.sqlite3").exists()
    assert "refused" in capsys.readouterr().err
