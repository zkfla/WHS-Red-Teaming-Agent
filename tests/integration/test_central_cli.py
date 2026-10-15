"""CLI consumers use temporary central originals; no external providers."""

import json
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from redteam.app.cli import _build_parser
from redteam.app.cli.commands import approvals, report, resume
from redteam.core.config import OrchestrationConfig
from redteam.core.enums import ApprovalStatus
from tests.integration.test_central_completion import (
    OriginalProtocolDouble,
    finish_inconclusive,
    review,
    selection,
)
from tests.integration.test_central_development import gateway
from tests.integration.test_central_recovery import start


async def test_status_findings_report_read_current_db_without_saved_export(tmp_path, capsys):
    async with selection(tmp_path) as (session, llm):
        refs = await finish_inconclusive(session, llm)
        assert await review(session, llm, refs) == []
        parser = _build_parser()
        path, run = str(session.deps.central_state.path), str(session.runtime.run_id)
        for command in (["status", run], ["findings", "list", run]):
            args = parser.parse_args([*command, "--state-db", path, "--json"])
            assert await args.handler(args) == 0
            value = json.loads(capsys.readouterr().out)
            if command[0] == "status":
                assert value["ready_for_release"], value["integrity_blockers"]
                assert value["effective_limits"]["workers"] == 2
            else:
                assert value == []
        args = parser.parse_args(["report", "generate", run, "--state-db", path,
                                  "--output-dir", str(tmp_path / "reports")])
        assert await report.run_generate(args) == 0
        text = (tmp_path / "reports" / run / "report.md").read_text(encoding="utf-8")
        assert "Criterion evidence" in text and refs[0] in text
        assert not (tmp_path / "data" / "reports").exists()


async def test_central_approvals_resolve_only_original_run_action(tmp_path, capsys):
    tools = OriginalProtocolDouble(gateway(risk="high"))
    async with selection(tmp_path, tools=tools) as (session, llm):
        original, = await start(session, llm)
        path = str(session.deps.central_state.path)
        parser = _build_parser()
        args = parser.parse_args(["approvals", "list", "--run", str(original.run_id),
                                  "--state-db", path, "--json"])
        assert await approvals.run_list(args) == 0
        pending = json.loads(capsys.readouterr().out)
        assert pending[0]["approval_id"] == original.pending_approval_id
        args = parser.parse_args(["approvals", "approve", str(original.pending_approval_id),
                                  "--run", str(original.run_id), "--state-db", path])
        assert await approvals.run_approve(args) == 0
        assert (await session.deps.control_plane.inspect_approval(
            original.pending_approval_id,
        )).status is ApprovalStatus.APPROVED
        assert tools.inner.invocations == []  # Resolving an approval never runs an action.
        wrong = parser.parse_args(["approvals", "approve", "not-this-run",
                                   "--run", str(original.run_id), "--state-db", path])
        assert await approvals.run_approve(wrong) == 2


async def test_missing_central_run_resume_refuses_before_assembly(tmp_path, monkeypatch, capsys):
    async def forbidden(*args, **kwargs):
        raise AssertionError("missing original caused provider assembly")
    monkeypatch.setattr("redteam.app.cli.commands.run.build", forbidden)
    from redteam.core.config import AppConfig, EngagementConfig
    config = AppConfig(engagement=EngagementConfig(name="Synthetic missing Run",
                                                 execution_budget={"max_actions": 1}),
                      orchestration=OrchestrationConfig(
        runtime_backend="central", central_state_path=str(tmp_path / "missing.sqlite3"),
    ))
    args = SimpleNamespace(run_id="missing-original", instance=None, json=True)
    async with AsyncExitStack() as stack:
        assert await resume._run_central(args, config, mcp_stack=stack) == 1
    assert not (tmp_path / "missing.sqlite3").exists()
    assert "refused" in capsys.readouterr().err


def test_worker_limit_and_backend_are_validated_operator_inputs():
    assert OrchestrationConfig().runtime_backend == "central"
    assert OrchestrationConfig().max_workers == 2
    config = OrchestrationConfig.from_environment({
        "REDTEAM_RUNTIME_BACKEND": "central", "REDTEAM_STATE_DB": "temporary/state.sqlite3",
        "REDTEAM_MAX_WORKERS": "2",
    })
    assert config.runtime_backend == "central" and config.max_workers == 2
    assert config.central_state_path == "temporary/state.sqlite3"
    with pytest.raises(ValueError):
        OrchestrationConfig.from_environment({"REDTEAM_MAX_WORKERS": "0"})


async def test_cli_resume_reuses_original_approval_action_and_budget(tmp_path, monkeypatch, capsys):
    from redteam.app.cli.commands import run as run_command
    from redteam.core.config import AppConfig, EngagementConfig
    from redteam.core.contracts.execution import ExecutionState
    from redteam.llm.fake import FakeLLMClient
    from tests.integration.test_central_development import engagement

    monkeypatch.chdir(tmp_path)
    tools, llm = OriginalProtocolDouble(gateway(risk="high")), FakeLLMClient()
    async with selection(tmp_path, tools=tools, llm=llm) as (session, _):
        original, = await start(session, llm)
        execution, = await session.deps.central_state.list_executions()
        path = str(session.deps.central_state.path)
        await session.deps.control_plane.resolve_approval(
            original.pending_approval_id, ApprovalStatus.APPROVED, "fixture:operator",
        )
        spent = (await session.deps.central_state.request_ledger().snapshot(str(original.run_id)))[
            "spent"
        ]
        # A pre-cleanup central record may carry inert legacy store selectors.
        # Reopening must preserve its original action/accounting without creating
        # those stores or requiring an automatic database migration.
        def historical_configuration(tx):
            row = tx.db.execute(
                "SELECT payload FROM state_requests WHERE run_id=? "
                "AND kind='run_configuration' AND identity=?", (tx.run_id, tx.run_id),
            ).fetchone()
            saved = json.loads(row[0])
            for name in ("work", "task_graph", "attack_graph", "operator_intent"):
                saved["settings"][name + "_backend"] = "sqlite"
                saved["settings"][name + "_path"] = str(tmp_path / ("unused-" + name + ".db"))
            saved["settings"].update(path_backend="sqlite", planner_score_margin=0.15,
                                     planner_rerank_top_k=3, branch_prune_failure_threshold=2,
                                     branch_prune_difficulty_threshold=0.65,
                                     branch_prune_objective_override=0.8)
            tx.db.execute(
                "UPDATE state_requests SET payload=? WHERE run_id=? "
                "AND kind='run_configuration' AND identity=?",
                (json.dumps(saved), tx.run_id, tx.run_id),
            )
        await session.deps.central_state.transaction(historical_configuration)
    raw = engagement()
    raw.metadata["success_criteria"] = ["Assess the original response"]
    config = AppConfig(engagement=EngagementConfig.model_validate(
        raw.model_dump(exclude={"engagement_id", "created_at"}),
    ), orchestration=OrchestrationConfig(runtime_backend="central", central_state_path=path))
    llm._canned_structured = {
        "questions": [], "termination": "exhausted",
        "resume_conditions": ["A relevant new result changes the available evidence"],
        "reason": "Original response assessed, no useful followup",
    }
    real_build = run_command.build_application
    captured = []
    async def with_explicit_test_doubles(config, **kwargs):
        result = await real_build(config, llm=llm, tool_gateway=tools,
                                  tool_catalog=tools.catalog, **kwargs)
        captured.append(result[1])
        return result
    monkeypatch.setattr(run_command, "build_application", with_explicit_test_doubles)
    monkeypatch.setattr(run_command, "prepare_shell_runtime", AsyncMock(return_value=None))
    args = SimpleNamespace(run_id=str(original.run_id), instance=None, json=True,
                           workers=None, max_steps=None)
    async with AsyncExitStack() as stack:
        assert await resume._run_central(args, config, mcp_stack=stack) == 0
    updated, = await captured[0].central_state.list_executions()
    assert updated.invocation_id == execution.invocation_id
    assert updated.state is ExecutionState.SUCCEEDED
    assert tools.inner.invocations[0].action_id == execution.action_id
    assert len(tools.inner.invocations) == 1
    assert not list(tmp_path.glob("unused-*.db"))
    assert captured[0].path_store is None and captured[0].work_store is None
    usage = await captured[0].central_state.request_ledger().snapshot(str(original.run_id))
    assert usage["spent"] >= spent
    assert await captured[0].central_state.transaction(lambda tx: tx.policy_budget_remaining(),
                                                      write=False) == 5
    capsys.readouterr()


async def test_default_application_assembles_only_central_domain_store(tmp_path, monkeypatch):
    from redteam.app.bootstrap import build_application, run_engagement
    from redteam.core.config import AppConfig, EngagementConfig
    from redteam.core.contracts.llm_policy import LLMRequestPolicy
    from redteam.llm.fake import FakeLLMClient
    from redteam.orchestration.settings import OrchestrationSettings
    from tests.integration.test_central_development import PROFILE, engagement

    monkeypatch.chdir(tmp_path)
    raw = engagement()
    config = AppConfig(engagement=EngagementConfig.model_validate(
        raw.model_dump(exclude={"engagement_id", "created_at"}),
    ), orchestration=OrchestrationConfig(
        specialist_profile_path=str(PROFILE),
        llm_request_policy=LLMRequestPolicy(mode="bounded", max_requests=8),
        central_state_path=str(tmp_path / "state.sqlite3"),
    ))
    selected = OrchestrationSettings.from_config(config.orchestration).model_copy(update={
        "checkpoint_path": str(tmp_path / "checkpoints.sqlite3"),
    })
    tools = OriginalProtocolDouble(gateway())
    llm = FakeLLMClient(canned_structured={"questions": [], "reason": "No useful proposal"})
    scope, deps = await build_application(
        config, orchestration_settings=selected, llm=llm,
        tool_gateway=tools, tool_catalog=tools.catalog,
        evidence_dir=tmp_path / "evidence", audit_dir=tmp_path / "audit",
        approval_dir=tmp_path / "approvals",
    )
    assert deps.central_state is not None and deps.specialist_policy.max_instances == 2
    assert deps.central_state.path == tmp_path / "state.sqlite3"
    assert deps.path_store is None and deps.work_store is None and deps.task_graph_store is None
    assert not {"work_backend", "task_graph_backend"} & type(deps.settings).model_fields.keys()
    from redteam.orchestration.graph import build_graph

    assert set(build_graph(deps).get_graph().nodes) == {
        "__start__", "model", "tools", "settle", "__end__",
    }
    await run_engagement(scope, deps=deps)
    assert not list(tmp_path.rglob("task_graph.sqlite3"))
    assert not list(tmp_path.rglob("work.sqlite3"))
    stored = await deps.central_state.transaction(lambda tx: json.loads(tx.db.execute(
        "SELECT payload FROM state_requests WHERE run_id=? AND kind='run_configuration'",
        (tx.run_id,),
    ).fetchone()[0]), write=False)
    legacy_paths = {"work_path", "task_graph_path", "attack_graph_path", "operator_intent_path"}
    assert not legacy_paths.intersection(stored["settings"])


async def test_default_application_assembles_candidate_planner_and_common_worker(tmp_path):
    from redteam.app.bootstrap import build_application
    from redteam.core.config import AppConfig, EngagementConfig
    from redteam.core.contracts.llm_policy import LLMRequestPolicy
    from redteam.llm.fake import FakeLLMClient
    from tests.integration.test_central_development import engagement

    raw = engagement()
    config = AppConfig(
        engagement=EngagementConfig.model_validate(
            raw.model_dump(exclude={"engagement_id", "created_at"})
        ),
        orchestration=OrchestrationConfig(
            llm_request_policy=LLMRequestPolicy(mode="bounded", max_requests=8),
            central_state_path=str(tmp_path / "state.sqlite3"),
        ),
    )
    llm = FakeLLMClient()
    tools = OriginalProtocolDouble(gateway())
    _, deps = await build_application(
        config,
        llm=llm,
        tool_gateway=tools,
        tool_catalog=tools.catalog,
        evidence_dir=tmp_path / "evidence",
        audit_dir=tmp_path / "audit",
        approval_dir=tmp_path / "approvals",
    )

    assert deps.specialist_policy.probe_limits is not None
    assert Path(deps.settings.specialist_profile_path).as_posix().endswith(
        "config/agents/native-probes.yaml")
    assert [str(item.agent_id) for item in deps.agents.definitions() if item.schedulable] == [
        "probe-worker"
    ]
    assert deps.agent_prompts is not None
    assert deps.agent_prompts.binding("hypothesis_planning").role_id == "planner"
    assert deps.specialist_policy.probe_atomization
    assert deps.agent_prompts.binding("task_atomization").role_id != (
        deps.agent_prompts.binding("hypothesis_planning").role_id
    )
    from redteam.orchestration.specialist_planner import specialist_planning_inputs

    assert specialist_planning_inputs(deps)["schema_version"] == 3
    assert deps.component_binding.specialist_fingerprint is not None
    assert llm.requests == [] and tools.inner.invocations == []
