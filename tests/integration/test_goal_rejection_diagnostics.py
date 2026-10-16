"""Goal rejection keeps safe owner diagnostics through terminal reporting."""

from contextlib import asynccontextmanager

from redteam.app.central_run import open_central_run
from redteam.app.central_views import inspect_central_run
from redteam.app.cli.commands.report import _render_markdown
from redteam.capability.development import DevelopmentResponse
from redteam.core.config import AppConfig, EngagementConfig
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_completion import goal_inputs
from redteam.orchestration.completion import CompletionIssues
from redteam.orchestration.state import TerminationReason
from tests.integration.test_central_completion import (
    OriginalProtocolDouble,
    finish_inconclusive,
    review,
)
from tests.integration.test_central_development import engagement, gateway
from tests.integration.test_central_parallel import limits

SECRET_MARKER = "unmapped-original-body-marker"


@asynccontextmanager
async def selected_run(tmp_path, *, mapped_effects=True, llm=None):
    original = engagement()
    original.metadata["success_criteria"] = ["Assess the original response"]
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    configured = limits()[1]
    inner = gateway()
    if not mapped_effects:
        inner.responses[0] = DevelopmentResponse(
            output={"status": 200, "body": SECRET_MARKER},
            metadata={},
        )
    tools = OriginalProtocolDouble(inner)
    llm = llm or FakeLLMClient()
    directory = tmp_path / "run"
    async with open_central_run(
        config,
        directory=directory,
        settings=configured,
        llm=llm,
        tool_gateway=tools,
        tool_catalog=tools.catalog,
    ) as session:
        yield session, llm, tools, config, configured, directory


async def test_unmapped_effects_hold_and_survive_diagnostic_report_reopen(tmp_path):
    async with selected_run(tmp_path, mapped_effects=False) as (
        session, llm, tools, config, configured, directory,
    ):
        await finish_inconclusive(session, llm)
        errors = (await goal_inputs(session.deps, session.runtime))[3]
        assert isinstance(errors, CompletionIssues)
        assert "resource_effects_unmapped_cleanup_unverified" in errors
        diagnostic, = errors.diagnostics
        assert diagnostic.code == "resource_effects_unmapped_cleanup_unverified"
        assert diagnostic.location == "$.executions.resource_effects"
        assert diagnostic.owner == "orchestration.central_completion"
        assert diagnostic.cause_scope == "runtime_contract"
        assert diagnostic.repair_condition
        assert SECRET_MARKER not in diagnostic.model_dump_json()

        requests_before_settle = len(llm.requests)
        actions_before_settle = len(tools.inner.invocations)
        final = await session.settle(session.runtime)
        assert final.termination_reason == TerminationReason.INVESTIGATION_HELD
        assert final.planning_diagnostic == diagnostic
        assert final.planning_diagnostic_history == [diagnostic]
        assert len(llm.requests) == requests_before_settle
        assert len(tools.inner.invocations) == actions_before_settle == 1
        mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
        assert mission.status.value == "active"
        run_id = str(final.run_id)
        engagement_id = str(final.engagement_id)
        state_path = session.deps.central_state.path

    _, _, report = await inspect_central_run(state_path, run_id)
    assert report.planning_diagnostic == diagnostic
    assert report.planning_diagnostic_history == [diagnostic]
    rendered = _render_markdown(report)
    assert "resource_effects_unmapped_cleanup_unverified" in rendered
    assert SECRET_MARKER not in report.model_dump_json()
    assert SECRET_MARKER not in rendered

    async with open_central_run(
        config,
        directory=directory,
        settings=configured,
        resume=True,
        run_id=run_id,
        engagement_id=engagement_id,
        llm=llm,
        tool_gateway=tools,
        tool_catalog=tools.catalog,
    ) as resumed:
        saved = await resumed.deps.central_state.transaction(
            lambda tx: tx.planning_diagnostics(), write=False,
        )
        assert saved == (diagnostic, [diagnostic])


async def test_mapped_empty_effects_can_complete_without_stale_diagnostic(tmp_path):
    async with selected_run(tmp_path) as (session, llm, tools, *_):
        originals = await finish_inconclusive(session, llm)
        assert await review(session, llm, originals) == []
        final = await session.settle(session.runtime)
        assert final.termination_reason == TerminationReason.GOAL_ACHIEVED
        assert final.planning_diagnostic is None
        assert final.planning_diagnostic_history == []
        assert len(tools.inner.invocations) == 1
        state = session.deps.central_state
        assert await state.transaction(lambda tx: tx.planning_diagnostics(), write=False) == (
            None, [],
        )


async def test_plain_completion_errors_get_only_a_safe_generic_diagnostic(tmp_path, monkeypatch):
    async with selected_run(tmp_path) as (session, llm, tools, *_):
        async def reject(_deps, _runtime):
            return [SECRET_MARKER, "untrusted error text"]

        monkeypatch.setattr(
            "redteam.orchestration.central_completion.complete_goal", reject,
        )
        final = await session.settle(session.runtime)

        diagnostic = final.planning_diagnostic
        assert final.termination_reason == TerminationReason.INVESTIGATION_HELD
        assert diagnostic is not None
        assert diagnostic.code == "goal_completion_rejected"
        assert diagnostic.stage == "goal_completion"
        assert diagnostic.owner == "app.central_run"
        assert diagnostic.cause_scope == "unknown"
        assert "2 untyped issue(s)" in diagnostic.summary
        assert SECRET_MARKER not in diagnostic.model_dump_json()
        assert SECRET_MARKER not in str(final.planning_diagnostic_history)
        assert len(llm.requests) == 0 and not tools.inner.invocations


async def test_partially_typed_completion_errors_cover_untyped_count_safely(tmp_path, monkeypatch):
    async with selected_run(tmp_path) as (session, llm, tools, *_):
        async def reject(_deps, _runtime):
            issues = CompletionIssues()
            issues.add(
                "Safe owner issue",
                code="fixture_completion_issue",
                summary="A typed fixture blocker.",
                owner="orchestration.fixture",
            )
            issues.append(SECRET_MARKER)
            return issues

        monkeypatch.setattr(
            "redteam.orchestration.central_completion.complete_goal", reject,
        )
        final = await session.settle(session.runtime)

        typed, generic = final.planning_diagnostic_history
        assert typed.code == "fixture_completion_issue"
        assert generic == final.planning_diagnostic
        assert generic.code == "goal_completion_rejected"
        assert generic.summary == "Goal completion was rejected with 1 untyped issue(s)."
        assert SECRET_MARKER not in generic.model_dump_json()
        assert SECRET_MARKER not in str(final.planning_diagnostic_history)
        assert len(llm.requests) == 0 and not tools.inner.invocations
