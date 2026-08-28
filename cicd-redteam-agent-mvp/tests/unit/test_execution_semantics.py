from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.execution_semantics import (
    cache_write_access_for_event,
    evaluate_job_condition,
)
from cicd_redteam_agent.domain.models import AttackerProfile, PermissionLevel, TriState

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_cache_write_policy_distinguishes_branch_scope_and_low_trust_events() -> None:
    assert cache_write_access_for_event("pull_request") == (TriState.TRUE, [])
    assert cache_write_access_for_event("push") == (TriState.TRUE, [])
    assert cache_write_access_for_event("pull_request_target") == (TriState.FALSE, [])
    assert cache_write_access_for_event("workflow_run") == (TriState.FALSE, [])
    verdict, missing = cache_write_access_for_event("custom-unmodeled-event")
    assert verdict is TriState.UNKNOWN
    assert missing == ["GitHub Actions cache scope and trigger write policy"]


def test_condition_evaluator_handles_event_boolean_subset() -> None:
    assert evaluate_job_condition(
        "github.event_name == 'pull_request'",
        event_name="pull_request",
        profile=AttackerProfile.FORK_PR,
    ) is TriState.TRUE
    assert evaluate_job_condition(
        "github.event_name == 'push'",
        event_name="pull_request",
        profile=AttackerProfile.FORK_PR,
    ) is TriState.FALSE
    assert evaluate_job_condition(
        "contains(github.event.pull_request.labels.*.name, 'safe')",
        event_name="pull_request",
        profile=AttackerProfile.FORK_PR,
    ) is TriState.UNKNOWN


@pytest.mark.asyncio
async def test_explicit_job_mapping_sets_unspecified_permissions_to_none(tmp_path: Path) -> None:
    report, _ = await analyze_repository(
        FIXTURES / "permission-override",
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.REPO_WRITER,
        ),
    )
    context = report.state.job_execution_contexts[0]
    assert context.effective_permissions.get("contents") is PermissionLevel.READ
    assert context.effective_permissions.get("id-token") is PermissionLevel.NONE


@pytest.mark.asyncio
async def test_pull_request_write_permission_becomes_context_unknown(tmp_path: Path) -> None:
    report, _ = await analyze_repository(
        FIXTURES / "oidc-risk",
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.FORK_PR,
        ),
    )
    context = next(
        item
        for item in report.state.job_execution_contexts
        if next(
            trigger
            for trigger in report.state.trigger_contexts
            if trigger.context_id == item.trigger_context_id
        ).event_name
        == "pull_request"
    )
    assert context.effective_permissions.get("id-token") is PermissionLevel.UNKNOWN
    assert "fork pull-request OIDC token availability" in (
        context.effective_permissions.missing_evidence
    )


@pytest.mark.asyncio
async def test_local_reusable_workflow_caps_permissions_at_caller(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    workflows = repository / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "caller.yml").write_text(
        "on: push\npermissions:\n  contents: read\njobs:\n  call:\n"
        "    uses: ./.github/workflows/reusable.yml\n",
        encoding="utf-8",
    )
    (workflows / "reusable.yml").write_text(
        "on: workflow_call\njobs:\n  deploy:\n    permissions:\n      contents: write\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: echo deploy\n",
        encoding="utf-8",
    )
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.REPO_WRITER,
        ),
    )
    reusable_trigger = next(
        item
        for item in report.state.trigger_contexts
        if item.activation_type == "workflow_call"
    )
    callee_context = next(
        item
        for item in report.state.job_execution_contexts
        if item.trigger_context_id == reusable_trigger.context_id
    )
    assert callee_context.effective_permissions.get("contents") is PermissionLevel.READ
    assert "reusable-caller-ceiling" in callee_context.effective_permissions.provenance
    assert any(
        relation.relation_type == "reusable_workflow_permission_ceiling_applied"
        and relation.target_id == callee_context.context_id
        and relation.verdict is TriState.TRUE
        for relation in report.state.relations
    )


@pytest.mark.asyncio
async def test_pull_request_target_checkout_major_blocks_fork_head_unless_opted_out(
    tmp_path: Path,
) -> None:
    blocked, _ = await analyze_repository(
        FIXTURES / "pr-target-blocked",
        AnalysisConfig(
            output_dir=tmp_path / "blocked",
            attacker_profile=AttackerProfile.FORK_PR,
        ),
    )
    controlled, _ = await analyze_repository(
        FIXTURES / "pr-target-controlled",
        AnalysisConfig(
            output_dir=tmp_path / "controlled",
            attacker_profile=AttackerProfile.FORK_PR,
        ),
    )

    blocked_checkout = next(
        relation
        for relation in blocked.state.relations
        if relation.relation_type == "untrusted_input_selects_checkout_revision"
    )
    controlled_checkout = next(
        relation
        for relation in controlled.state.relations
        if relation.relation_type == "untrusted_input_selects_checkout_revision"
    )
    assert blocked_checkout.verdict is TriState.FALSE
    assert controlled_checkout.verdict is TriState.TRUE
    assert not any(
        relation.relation_type == "controls_step_execution_context"
        and relation.verdict is TriState.TRUE
        for relation in blocked.state.relations
    )
    assert any(
        relation.relation_type == "controls_step_execution_context"
        and relation.verdict is TriState.TRUE
        for relation in controlled.state.relations
    )


@pytest.mark.asyncio
async def test_workflow_run_gets_fresh_consumer_context(tmp_path: Path) -> None:
    report, _ = await analyze_repository(
        FIXTURES / "gha-03-artifact-trust" / "vulnerable",
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.FORK_PR,
        ),
    )
    trigger = next(
        item
        for item in report.state.trigger_contexts
        if item.activation_type == "workflow_run"
    )
    assert trigger.attacker_can_trigger is TriState.TRUE
    assert any(
        relation.relation_type == "workflow_run_may_trigger"
        and relation.target_id == trigger.context_id
        for relation in report.state.relations
    )
    assert any(
        item.trigger_context_id == trigger.context_id
        for item in report.state.job_execution_contexts
    )
