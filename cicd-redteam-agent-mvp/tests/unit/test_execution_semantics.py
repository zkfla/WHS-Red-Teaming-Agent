from pathlib import Path

import pytest

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.condition_semantics import (
    ConditionRuntimeContext,
    evaluate_condition,
)
from cicd_redteam_agent.domain.execution_semantics import (
    cache_write_access_for_event,
    derive_execution_semantics,
    evaluate_job_condition,
)
from cicd_redteam_agent.domain.investigation import (
    EventPrincipalEvidence,
    WorkflowRunEventLinkageEvidence,
    WorkflowRunIdentityEvidence,
    WorkflowRunJobEvidence,
    WorkflowRunJobsEvidence,
    WorkflowRunStatusEvidence,
    WorkflowRunStepEvidence,
)
from cicd_redteam_agent.domain.models import (
    AttackerProfile,
    ConditionSecurityEffect,
    EventPrincipalRole,
    EvidenceRequirementKind,
    ExecutionResult,
    PermissionLevel,
    TriState,
)

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


@pytest.mark.parametrize(
    ("expression", "effect", "verdict"),
    [
        ("false && fromJSON('[]')", ConditionSecurityEffect.BLOCKS, TriState.FALSE),
        ("true || fromJSON('[]')", ConditionSecurityEffect.ALWAYS_TRUE, TriState.TRUE),
        (
            "contains(github.event.comment.body, '/deploy')",
            ConditionSecurityEffect.ATTACKER_SATISFIABLE,
            TriState.UNKNOWN,
        ),
        (
            "github.actor == github.repository_owner",
            ConditionSecurityEffect.TRUST_REQUIRED,
            TriState.UNKNOWN,
        ),
        (
            "contains(github.event.pull_request.labels.*.name, 'safe')",
            ConditionSecurityEffect.TRUST_REQUIRED,
            TriState.UNKNOWN,
        ),
    ],
)
def test_p4_condition_security_effect_is_independent_from_runtime_truth(
    expression: str,
    effect: ConditionSecurityEffect,
    verdict: TriState,
) -> None:
    evaluation = evaluate_condition(
        expression,
        target_id="job:p4",
        target_kind="job",
        trigger_context_id="trigger:p4",
        context=ConditionRuntimeContext(
            event_name="issue_comment" if "comment" in expression else "pull_request",
            profile=AttackerProfile.FORK_PR,
            repository_full_name="example/project",
            repository_owner="example",
        ),
    )
    assert evaluation.security_effect is effect
    assert evaluation.verdict is verdict


def test_p4_label_trust_does_not_claim_exact_revision_binding() -> None:
    evaluation = evaluate_condition(
        "contains(github.event.pull_request.labels.*.name, 'safe')",
        target_id="job:label",
        target_kind="job",
        trigger_context_id="trigger:p4",
        context=ConditionRuntimeContext(
            event_name="pull_request_target",
            profile=AttackerProfile.FORK_PR,
        ),
    )
    assert evaluation.security_effect is ConditionSecurityEffect.TRUST_REQUIRED
    assert evaluation.trusted_principal_required is TriState.TRUE
    assert evaluation.revision_bound is TriState.UNKNOWN
    assert "condition approval is not bound to the exact workflow revision" in (
        evaluation.security_missing_evidence
    )


def test_p4_event_aware_symbolic_ref_functions_are_bounded() -> None:
    pull_request = evaluate_condition(
        "startsWith(github.ref, 'refs/pull/') && endsWith(github.ref, '/merge')",
        target_id="job:ref",
        target_kind="job",
        trigger_context_id="trigger:p4",
        context=ConditionRuntimeContext(
            event_name="pull_request",
            profile=AttackerProfile.FORK_PR,
        ),
    )
    push = evaluate_condition(
        "github.ref == 'refs/heads/main' && github.ref_name == 'main'",
        target_id="job:push-ref",
        target_kind="job",
        trigger_context_id="trigger:p4-push",
        context=ConditionRuntimeContext(
            event_name="push",
            profile=AttackerProfile.REPO_WRITER,
            trigger_config={"push": {"branches": ["main"]}},
        ),
    )
    assert pull_request.verdict is TriState.TRUE
    assert push.verdict is TriState.TRUE


def test_p4_actor_and_triggering_actor_are_not_equivalent_authority() -> None:
    owner_guard = evaluate_condition(
        "github.actor == github.repository_owner",
        target_id="job:owner",
        target_kind="job",
        trigger_context_id="trigger:p4-owner",
        context=ConditionRuntimeContext(
            event_name="pull_request",
            profile=AttackerProfile.FORK_PR,
            repository_owner="example",
        ),
    )
    rerun_guard = evaluate_condition(
        "github.triggering_actor == github.repository_owner",
        target_id="job:rerun",
        target_kind="job",
        trigger_context_id="trigger:p4-rerun",
        context=ConditionRuntimeContext(
            event_name="pull_request",
            profile=AttackerProfile.FORK_PR,
            repository_owner="example",
        ),
    )
    assert owner_guard.security_effect is ConditionSecurityEffect.TRUST_REQUIRED
    assert owner_guard.revision_bound is TriState.TRUE
    assert rerun_guard.security_effect is ConditionSecurityEffect.UNKNOWN


def test_typed_condition_evaluator_preserves_precedence_and_github_string_case() -> None:
    context = ConditionRuntimeContext(
        event_name="pull_request",
        profile=AttackerProfile.FORK_PR,
    )
    evaluation = evaluate_condition(
        "github.event_name == 'PULL_REQUEST' && (false || !false)",
        target_id="job:test",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=context,
    )
    assert evaluation.verdict is TriState.TRUE
    assert evaluation.referenced_contexts == ["github.event_name"]
    assert evaluation.missing_evidence == []


def test_status_functions_do_not_treat_success_as_static_true() -> None:
    context = ConditionRuntimeContext(
        event_name="push",
        profile=AttackerProfile.REPO_WRITER,
    )
    success = evaluate_condition(
        "success()",
        target_id="job:success",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=context,
    )
    always = evaluate_condition(
        "always()",
        target_id="job:always",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=context,
    )
    assert success.verdict is TriState.UNKNOWN
    assert success.missing_evidence == ["runtime status result for success()"]
    assert always.verdict is TriState.TRUE


def test_malformed_condition_fails_closed_without_crashing() -> None:
    evaluation = evaluate_condition(
        "github.event_name == 'push' && (true",
        target_id="job:malformed",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=ConditionRuntimeContext(
            event_name="push",
            profile=AttackerProfile.REPO_WRITER,
        ),
    )
    assert evaluation.verdict is TriState.UNKNOWN
    assert evaluation.missing_evidence == [
        "unsupported condition syntax: missing closing parenthesis"
    ]


def test_unsupported_function_still_extracts_actor_evidence_requirement() -> None:
    evaluation = evaluate_condition(
        "contains(github.event.comment.body, '/deploy') "
        "&& github.actor != 'dependabot[bot]'",
        target_id="job:command",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=ConditionRuntimeContext(
            event_name="issue_comment",
            profile=AttackerProfile.FORK_PR,
            repository_id="github:example/repo",
            github_run_id=42,
        ),
    )
    assert evaluation.verdict is TriState.UNKNOWN
    assert "github.actor" in evaluation.referenced_contexts
    assert {item.kind for item in evaluation.evidence_requirements} == {
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY,
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
    }


def test_event_principal_reference_requires_exact_event_linkage_first() -> None:
    evaluation = evaluate_condition(
        "github.event.comment.user.login == 'maintainer'",
        target_id="job:command",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=ConditionRuntimeContext(
            event_name="issue_comment",
            profile=AttackerProfile.FORK_PR,
            repository_id="github:example/repo",
            github_run_id=42,
        ),
    )

    assert evaluation.verdict is TriState.UNKNOWN
    assert {item.kind for item in evaluation.evidence_requirements} == {
        EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE
    }
    assert evaluation.principal_assessments[0].principal_role is EventPrincipalRole.COMMENT_AUTHOR
    assert "workflow run event resource linkage" in evaluation.missing_evidence


def test_exact_linkage_compiles_comment_principal_requirement() -> None:
    linkage = WorkflowRunEventLinkageEvidence(
        run_id=42,
        event_name="issue_comment",
        action="created",
        binding="exact",
        source="operator_event_payload",
        issue_number=7,
        comment_id=99,
        sender_login="outsider",
        sender_id=8,
    )
    evaluation = evaluate_condition(
        "github.event.comment.user.login == 'maintainer'",
        target_id="job:command",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=ConditionRuntimeContext(
            event_name="issue_comment",
            profile=AttackerProfile.FORK_PR,
            repository_id="github:example/repo",
            github_run_id=42,
            event_linkage=linkage,
        ),
    )

    assert {item.kind for item in evaluation.evidence_requirements} == {
        EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL
    }
    assert evaluation.principal_assessments[0].missing_evidence == [
        "event principal identity: comment_author"
    ]


def test_event_principal_current_permission_is_separate_from_historical_authorization() -> None:
    linkage = WorkflowRunEventLinkageEvidence(
        run_id=42,
        event_name="issue_comment",
        action="created",
        binding="exact",
        source="operator_event_payload",
        issue_number=7,
        comment_id=99,
        sender_login="maintainer",
        sender_id=7,
    )
    principal = EventPrincipalEvidence(
        run_id=42,
        event_name="issue_comment",
        event_action="created",
        principal_role=EventPrincipalRole.COMMENT_AUTHOR,
        login="maintainer",
        actor_id=7,
        account_type="User",
        author_association="MEMBER",
        resource_id="id:99",
    )
    evaluation = evaluate_condition(
        "github.event.comment.user.login == 'maintainer'",
        target_id="job:command",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=ConditionRuntimeContext(
            event_name="issue_comment",
            profile=AttackerProfile.FORK_PR,
            repository_id="github:example/repo",
            github_run_id=42,
            event_linkage=linkage,
            event_principals={EventPrincipalRole.COMMENT_AUTHOR: principal},
            actor_permissions={"maintainer": ("write", 7)},
        ),
    )

    assessment = evaluation.principal_assessments[0]
    assert evaluation.verdict is TriState.TRUE
    assert assessment.current_repository_authorization is TriState.TRUE
    assert assessment.event_action_observed is TriState.TRUE
    assert assessment.historical_repository_authorization is TriState.UNKNOWN
    assert assessment.missing_evidence == ["historical event principal authorization"]


def test_author_association_truth_does_not_become_repository_permission() -> None:
    linkage = WorkflowRunEventLinkageEvidence(
        run_id=42,
        event_name="issue_comment",
        action="created",
        binding="exact",
        source="operator_event_payload",
        issue_number=7,
        comment_id=99,
        sender_login="contributor",
        sender_id=11,
    )
    principal = EventPrincipalEvidence(
        run_id=42,
        event_name="issue_comment",
        event_action="created",
        principal_role=EventPrincipalRole.COMMENT_AUTHOR,
        login="contributor",
        actor_id=11,
        author_association="MEMBER",
        resource_id="id:99",
    )
    evaluation = evaluate_condition(
        "github.event.comment.author_association == 'MEMBER'",
        target_id="job:command",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=ConditionRuntimeContext(
            event_name="issue_comment",
            profile=AttackerProfile.FORK_PR,
            repository_id="github:example/repo",
            github_run_id=42,
            event_linkage=linkage,
            event_principals={EventPrincipalRole.COMMENT_AUTHOR: principal},
        ),
    )

    assessment = evaluation.principal_assessments[0]
    assert evaluation.verdict is TriState.TRUE
    assert assessment.current_repository_authorization is TriState.UNKNOWN
    assert "current repository event principal authorization" in assessment.missing_evidence
    assert "historical event principal authorization" in assessment.missing_evidence
    assert any(
        item.kind is EvidenceRequirementKind.GITHUB_REPOSITORY_ACTOR_PERMISSION
        for item in evaluation.evidence_requirements
    )


def test_review_author_and_event_sender_are_distinct_principals() -> None:
    linkage = WorkflowRunEventLinkageEvidence(
        run_id=42,
        event_name="pull_request_review",
        action="dismissed",
        binding="exact",
        source="operator_event_payload",
        pull_number=5,
        review_id=13,
        sender_login="maintainer-b",
        sender_id=22,
    )
    review_author = EventPrincipalEvidence(
        run_id=42,
        event_name="pull_request_review",
        event_action="dismissed",
        principal_role=EventPrincipalRole.REVIEW_AUTHOR,
        login="reviewer-a",
        actor_id=21,
        resource_id="id:13",
    )
    sender = EventPrincipalEvidence(
        run_id=42,
        event_name="pull_request_review",
        event_action="dismissed",
        principal_role=EventPrincipalRole.EVENT_SENDER,
        login="maintainer-b",
        actor_id=22,
        resource_id="run:42:sender",
    )
    evaluation = evaluate_condition(
        "github.event.review.user.login != github.event.sender.login",
        target_id="job:review",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=ConditionRuntimeContext(
            event_name="pull_request_review",
            profile=AttackerProfile.FORK_PR,
            repository_id="github:example/repo",
            github_run_id=42,
            event_linkage=linkage,
            event_principals={
                EventPrincipalRole.REVIEW_AUTHOR: review_author,
                EventPrincipalRole.EVENT_SENDER: sender,
            },
        ),
    )

    assert evaluation.verdict is TriState.TRUE
    assert {item.principal_role for item in evaluation.principal_assessments} == {
        EventPrincipalRole.REVIEW_AUTHOR,
        EventPrincipalRole.EVENT_SENDER,
    }


def test_actor_condition_separates_expression_truth_from_current_authorization() -> None:
    context = ConditionRuntimeContext(
        event_name="issue_comment",
        profile=AttackerProfile.FORK_PR,
        repository_id="github:example/repo",
        github_run_id=42,
        actor_login="Maintainer",
        actor_id=7,
        triggering_actor_login="rerunner",
        triggering_actor_id=8,
        run_event_name="issue_comment",
        actor_permissions={"maintainer": ("write", 7)},
    )
    actor = evaluate_condition(
        "github.actor == 'maintainer' && github.actor_id == 7",
        target_id="job:actor",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=context,
    )
    triggering = evaluate_condition(
        "github.triggering_actor == 'rerunner'",
        target_id="job:rerun",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=context,
    )
    assert actor.verdict is TriState.TRUE
    assert actor.current_actor_authorization is TriState.TRUE
    assert actor.authorization_scope == "current"
    assert triggering.verdict is TriState.TRUE
    assert triggering.current_actor_authorization is TriState.UNKNOWN
    assert triggering.authorization_missing_evidence == [
        "github.triggering_actor is not workflow privilege authority"
    ]


@pytest.mark.parametrize(
    ("permission", "permission_actor_id", "expected", "missing"),
    [
        ("read", 7, TriState.FALSE, []),
        ("none", 7, TriState.FALSE, []),
        ("write", 99, TriState.UNKNOWN, ["actor permission identity mismatch"]),
    ],
)
def test_actor_authorization_is_current_and_identity_bound(
    permission: str,
    permission_actor_id: int,
    expected: TriState,
    missing: list[str],
) -> None:
    evaluation = evaluate_condition(
        "github.actor == 'maintainer'",
        target_id="job:actor",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=ConditionRuntimeContext(
            event_name="push",
            profile=AttackerProfile.REPO_WRITER,
            repository_id="github:example/repo",
            github_run_id=42,
            actor_login="maintainer",
            actor_id=7,
            run_event_name="push",
            actor_permissions={"maintainer": (permission, permission_actor_id)},
        ),
    )
    assert evaluation.verdict is TriState.TRUE
    assert evaluation.current_actor_authorization is expected
    assert evaluation.authorization_scope == "current"
    assert evaluation.authorization_missing_evidence == missing


def test_actor_condition_requires_same_event_scoped_run_identity() -> None:
    evaluation = evaluate_condition(
        "github.actor == 'maintainer'",
        target_id="job:actor",
        target_kind="job",
        trigger_context_id="trigger:test",
        context=ConditionRuntimeContext(
            event_name="issue_comment",
            profile=AttackerProfile.FORK_PR,
            repository_id="github:example/repo",
            github_run_id=42,
            actor_login="maintainer",
            actor_id=7,
            run_event_name="workflow_dispatch",
        ),
    )
    assert evaluation.verdict is TriState.UNKNOWN
    assert "workflow run event does not match trigger context" in evaluation.missing_evidence
    assert evaluation.evidence_requirements == []


@pytest.mark.asyncio
async def test_actor_condition_propagates_typed_run_identity_requirement(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    workflows = repository / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "comment.yml").write_text(
        "on: issue_comment\njobs:\n  deploy:\n"
        "    if: github.actor == 'maintainer'\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: echo deploy\n",
        encoding="utf-8",
    )
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.FORK_PR,
        ),
    )
    execution = derive_execution_semantics(
        report.state,
        AttackerProfile.FORK_PR,
        github_run_id=42,
    )
    evaluation = next(
        item
        for item in execution.condition_evaluations
        if item.normalized_expression == "github.actor == 'maintainer'"
    )
    assert evaluation.verdict is TriState.UNKNOWN
    assert {
        item.kind for item in evaluation.evidence_requirements
    } == {EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY}
    reaches_job = next(
        relation
        for relation in execution.relations
        if relation.relation_type == "event_reaches_job_context"
    )
    assert reaches_job.verdict is TriState.UNKNOWN
    assert {
        item.kind for item in reaches_job.evidence_requirements
    } == {EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY}


@pytest.mark.asyncio
async def test_false_step_condition_blocks_only_that_step(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    workflows = repository / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "steps.yml").write_text(
        "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: skipped\n        if: false\n        run: echo skipped\n"
        "      - name: active\n        if: true\n        run: echo active\n",
        encoding="utf-8",
    )
    report, _ = await analyze_repository(
        repository,
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.REPO_WRITER,
        ),
    )
    workflow = report.state.workflows[0]
    job = workflow.jobs[0]
    steps = {step.name: step for step in job.steps}
    verdicts = {
        relation.target_id: relation.verdict
        for relation in report.state.relations
        if relation.relation_type == "executes_step_definition"
    }
    assert report.state.job_execution_contexts[0].reachable is TriState.TRUE
    assert verdicts[steps["skipped"].step_id] is TriState.FALSE
    assert verdicts[steps["active"].step_id] is TriState.TRUE
    assert not any(
        relation.relation_type == "execution_condition_present"
        and relation.source_id in {steps["skipped"].step_id, steps["active"].step_id}
        for relation in report.state.relations
    )


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


@pytest.mark.asyncio
async def test_p2_static_needs_semantics_separates_raw_condition_from_implicit_success(
    tmp_path: Path,
) -> None:
    report, _ = await analyze_repository(
        FIXTURES / "status-semantics",
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.REPO_WRITER,
        ),
    )
    jobs = {
        job.job_key: job
        for workflow in report.state.workflows
        for job in workflow.jobs
        if job.job_key
    }
    contexts = {context.job_id: context for context in report.state.job_execution_contexts}
    evaluations = {item.target_id: item for item in report.state.condition_evaluations}

    skipped = contexts[jobs["skipped"].job_id]
    implicit = contexts[jobs["implicit-after-skip"].job_id]
    always = contexts[jobs["always-after-skip"].job_id]
    failure_handler = contexts[jobs["failure-handler"].job_id]
    on_cancel = contexts[jobs["on-cancel"].job_id]

    assert skipped.reachable is TriState.FALSE
    assert skipped.predicted_result is ExecutionResult.SKIPPED
    assert evaluations[jobs["implicit-after-skip"].job_id].raw_expression_verdict is TriState.TRUE
    assert evaluations[jobs["implicit-after-skip"].job_id].status_gate_verdict is TriState.FALSE
    assert implicit.reachable is TriState.FALSE
    assert always.reachable is TriState.TRUE
    assert failure_handler.reachable is TriState.UNKNOWN
    # A root job with cancelled() is false at initial scheduling; a final cancelled run must not
    # retroactively rewrite that evaluation without a cancelled dependency/timing link.
    assert on_cancel.reachable is TriState.FALSE


@pytest.mark.asyncio
async def test_p2_runtime_job_results_close_failure_needs_and_ancestor_status(
    tmp_path: Path,
) -> None:
    report, _ = await analyze_repository(
        FIXTURES / "status-semantics",
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.REPO_WRITER,
        ),
    )
    runtime = WorkflowRunJobsEvidence(
        run_id=42,
        jobs=[
            WorkflowRunJobEvidence(
                github_job_id=1,
                name="Source",
                status="completed",
                conclusion="failure",
            ),
            WorkflowRunJobEvidence(
                github_job_id=2,
                name="Middle",
                status="completed",
                conclusion="success",
            ),
        ],
    )
    result = derive_execution_semantics(
        report.state,
        AttackerProfile.REPO_WRITER,
        workflow_run_identity=WorkflowRunIdentityEvidence(
            run_id=42,
            event_name="workflow_dispatch",
            workflow_path=".github/workflows/status.yml",
            actor_login="tester",
            actor_id=1,
        ),
        workflow_run_jobs=runtime,
        github_run_id=42,
    )
    jobs = {
        job.job_key: job
        for workflow in report.state.workflows
        for job in workflow.jobs
        if job.job_key
    }
    contexts = {context.job_id: context for context in result.job_contexts}
    evaluations = {item.target_id: item for item in result.condition_evaluations}

    assert contexts[jobs["failure-handler"].job_id].reachable is TriState.TRUE
    assert contexts[jobs["explicit-result"].job_id].reachable is TriState.TRUE
    assert contexts[jobs["implicit-after-failure"].job_id].reachable is TriState.FALSE
    assert contexts[jobs["ancestor-failure-handler"].job_id].reachable is TriState.TRUE
    assert (
        evaluations[jobs["implicit-after-failure"].job_id].raw_expression_verdict
        is TriState.TRUE
    )
    assert (
        evaluations[jobs["implicit-after-failure"].job_id].status_gate_verdict
        is TriState.FALSE
    )


@pytest.mark.asyncio
async def test_p2_step_conclusion_does_not_invent_continue_on_error_outcome(
    tmp_path: Path,
) -> None:
    report, _ = await analyze_repository(
        FIXTURES / "status-semantics",
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.REPO_WRITER,
        ),
    )
    runtime = WorkflowRunJobsEvidence(
        run_id=42,
        jobs=[
            WorkflowRunJobEvidence(
                github_job_id=10,
                name="Soft fail job",
                status="completed",
                conclusion="success",
                steps=[
                    WorkflowRunStepEvidence(
                        number=1,
                        name="Soft fail",
                        status="completed",
                        conclusion="success",
                    ),
                    WorkflowRunStepEvidence(
                        number=2,
                        name="Outcome check",
                        status="completed",
                        conclusion="success",
                    ),
                    WorkflowRunStepEvidence(
                        number=3,
                        name="Conclusion check",
                        status="completed",
                        conclusion="success",
                    ),
                ],
            )
        ],
    )
    result = derive_execution_semantics(
        report.state,
        AttackerProfile.REPO_WRITER,
        workflow_run_identity=WorkflowRunIdentityEvidence(
            run_id=42,
            event_name="workflow_dispatch",
            workflow_path=".github/workflows/status.yml",
            actor_login="tester",
            actor_id=1,
        ),
        workflow_run_jobs=runtime,
        github_run_id=42,
    )
    soft_fail_job = next(
        job
        for workflow in report.state.workflows
        for job in workflow.jobs
        if job.job_key == "soft-fail"
    )
    steps = {step.name: step for step in soft_fail_job.steps if step.name}
    evaluations = {item.target_id: item for item in result.condition_evaluations}

    outcome_eval = evaluations[steps["Outcome check"].step_id]
    conclusion_eval = evaluations[steps["Conclusion check"].step_id]
    assert outcome_eval.verdict is TriState.UNKNOWN
    assert "prior step outcome: softfail" in outcome_eval.missing_evidence
    assert conclusion_eval.verdict is TriState.TRUE


@pytest.mark.asyncio
async def test_p2_cancelled_uses_cancelled_dependency_not_final_run_status_alone(
    tmp_path: Path,
) -> None:
    report, _ = await analyze_repository(
        FIXTURES / "status-live",
        AnalysisConfig(
            output_dir=tmp_path / "out",
            attacker_profile=AttackerProfile.REPO_WRITER,
        ),
    )
    result = derive_execution_semantics(
        report.state,
        AttackerProfile.REPO_WRITER,
        workflow_run_identity=WorkflowRunIdentityEvidence(
            run_id=42,
            event_name="workflow_dispatch",
            workflow_path=".github/workflows/cancel.yml",
            actor_login="tester",
            actor_id=1,
        ),
        workflow_run_jobs=WorkflowRunJobsEvidence(
            run_id=42,
            jobs=[
                WorkflowRunJobEvidence(
                    github_job_id=20,
                    name="Long running",
                    status="completed",
                    conclusion="cancelled",
                ),
                WorkflowRunJobEvidence(
                    github_job_id=21,
                    name="On cancel",
                    status="completed",
                    conclusion="success",
                ),
                WorkflowRunJobEvidence(
                    github_job_id=22,
                    name="Always after cancel",
                    status="completed",
                    conclusion="success",
                ),
            ],
        ),
        workflow_run_status=WorkflowRunStatusEvidence(
            run_id=42,
            status="completed",
            conclusion="cancelled",
        ),
        github_run_id=42,
    )
    cancel_workflow = next(
        workflow for workflow in report.state.workflows if workflow.path.endswith("cancel.yml")
    )
    jobs = {job.job_key: job for job in cancel_workflow.jobs if job.job_key}
    contexts = {context.job_id: context for context in result.job_contexts}
    assert contexts[jobs["on_cancel"].job_id].reachable is TriState.TRUE
    assert contexts[jobs["always_after_cancel"].job_id].reachable is TriState.TRUE
