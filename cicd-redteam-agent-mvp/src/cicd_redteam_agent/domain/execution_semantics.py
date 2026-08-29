from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from cicd_redteam_agent.domain.condition_semantics import (
    ConditionRuntimeContext,
    evaluate_condition,
)
from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.investigation import (
    EventPrincipalEvidence,
    ForkPullRequestPolicyEvidence,
    RepositoryActorPermissionEvidence,
    RepositoryMetadataEvidence,
    WorkflowRunEventLinkageEvidence,
    WorkflowRunIdentityEvidence,
    WorkflowRunJobEvidence,
    WorkflowRunJobsEvidence,
    WorkflowRunStatusEvidence,
    WorkflowRunStepEvidence,
)
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    AttackerProfile,
    ConditionEvaluation,
    EffectivePermissionSet,
    EventPrincipalRole,
    ExecutionResult,
    Job,
    JobExecutionContext,
    PermissionLevel,
    Relation,
    RepositoryWorkflowPermissionDefault,
    Step,
    TriggerExecutionContext,
    TriState,
    Workflow,
)

MAX_REUSABLE_DEPTH = 10
SENSITIVE_PERMISSION_SCOPES = {
    "actions",
    "contents",
    "deployments",
    "id-token",
    "packages",
    "pull-requests",
}
CACHE_READ_WRITE_EVENTS = {
    "delete",
    "page_build",
    "pull_request",
    "registry_package",
    "release",
    "repository_dispatch",
    "schedule",
    "push",
    "workflow_dispatch",
}
CACHE_READ_ONLY_DEFAULT_BRANCH_EVENTS = {
    "issue_comment",
    "pull_request_target",
    "workflow_run",
}


@dataclass(frozen=True)
class ExecutionSemanticsResult:
    trigger_contexts: list[TriggerExecutionContext]
    job_contexts: list[JobExecutionContext]
    condition_evaluations: list[ConditionEvaluation]
    relations: list[Relation]


def _relation(
    source_id: str,
    relation_type: str,
    target_id: str,
    evidence_ids: list[str],
    *,
    category: str,
    verdict: TriState = TriState.TRUE,
    missing_evidence: list[str] | None = None,
    evidence_requirements=None,
) -> Relation:
    return Relation(
        source_id=source_id,
        relation_type=relation_type,
        target_id=target_id,
        evidence_ids=sorted(set(evidence_ids)),
        category=category,
        verdict=verdict,
        missing_evidence=sorted(set(missing_evidence or [])),
        evidence_requirements=list(evidence_requirements or []),
    )


def _permission_level(value: object) -> PermissionLevel:
    lowered = str(value).strip().lower()
    if lowered == "write":
        return PermissionLevel.WRITE
    if lowered == "read":
        return PermissionLevel.READ
    if lowered == "none":
        return PermissionLevel.NONE
    return PermissionLevel.UNKNOWN


def _declared_permission_set(
    workflow_permissions: dict[str, str] | str | None,
    job_permissions: dict[str, str] | str | None,
    repository_default: RepositoryWorkflowPermissionDefault | None = None,
) -> EffectivePermissionSet:
    declaration = job_permissions if job_permissions is not None else workflow_permissions
    provenance = ["job-permissions"] if job_permissions is not None else []
    if job_permissions is None and workflow_permissions is not None:
        provenance = ["workflow-permissions"]
    if declaration is None:
        if repository_default is RepositoryWorkflowPermissionDefault.READ:
            return EffectivePermissionSet(
                levels={
                    "contents": PermissionLevel.READ,
                    "packages": PermissionLevel.READ,
                },
                default_level=PermissionLevel.NONE,
                provenance=["repository-default-observed:read"],
            )
        if repository_default is RepositoryWorkflowPermissionDefault.WRITE:
            return EffectivePermissionSet(
                levels={"id-token": PermissionLevel.NONE},
                default_level=PermissionLevel.WRITE,
                provenance=["repository-default-observed:write"],
            )
        return EffectivePermissionSet(
            default_level=PermissionLevel.UNKNOWN,
            provenance=["repository-default-unknown"],
            missing_evidence=["repository default GITHUB_TOKEN permissions"],
        )
    if isinstance(declaration, str):
        lowered = declaration.strip().lower()
        if lowered == "write-all":
            return EffectivePermissionSet(
                default_level=PermissionLevel.WRITE,
                provenance=provenance,
            )
        if lowered == "read-all":
            return EffectivePermissionSet(
                default_level=PermissionLevel.READ,
                provenance=provenance,
            )
        return EffectivePermissionSet(
            default_level=PermissionLevel.UNKNOWN,
            provenance=provenance + ["unsupported-permissions-scalar"],
            missing_evidence=[f"unsupported permissions declaration: {declaration}"],
        )
    return EffectivePermissionSet(
        levels={name: _permission_level(value) for name, value in declaration.items()},
        default_level=PermissionLevel.NONE,
        provenance=provenance,
    )


def _cap_permission(
    requested: PermissionLevel,
    ceiling: PermissionLevel,
) -> PermissionLevel:
    if requested is PermissionLevel.NONE or ceiling is PermissionLevel.NONE:
        return PermissionLevel.NONE
    if requested is PermissionLevel.UNKNOWN or ceiling is PermissionLevel.UNKNOWN:
        return PermissionLevel.UNKNOWN
    rank = {
        PermissionLevel.NONE: 0,
        PermissionLevel.READ: 1,
        PermissionLevel.WRITE: 2,
    }
    return requested if rank[requested] <= rank[ceiling] else ceiling


def _cap_permission_set(
    requested: EffectivePermissionSet,
    ceiling: EffectivePermissionSet,
) -> EffectivePermissionSet:
    scopes = set(requested.levels) | set(ceiling.levels) | SENSITIVE_PERMISSION_SCOPES
    return EffectivePermissionSet(
        levels={
            scope: _cap_permission(requested.get(scope), ceiling.get(scope))
            for scope in sorted(scopes)
        },
        default_level=_cap_permission(requested.default_level, ceiling.default_level),
        provenance=[*requested.provenance, "reusable-caller-ceiling", *ceiling.provenance],
        missing_evidence=sorted(set(requested.missing_evidence + ceiling.missing_evidence)),
    )


def _apply_event_permission_adjustment(
    permission_set: EffectivePermissionSet,
    *,
    event_name: str,
    profile: AttackerProfile,
    fork_policy: ForkPullRequestPolicyEvidence | None = None,
) -> EffectivePermissionSet:
    if profile is not AttackerProfile.FORK_PR or event_name != "pull_request":
        return permission_set

    write_setting = (
        fork_policy.send_write_tokens_to_workflows
        if fork_policy is not None and fork_policy.private_repo_settings_applicable
        else None
    )

    def adjust(scope: str, level: PermissionLevel) -> PermissionLevel:
        if level is not PermissionLevel.WRITE:
            return level
        if scope == "id-token":
            return PermissionLevel.UNKNOWN
        if write_setting is True:
            return PermissionLevel.WRITE
        if write_setting is False:
            return PermissionLevel.READ
        return PermissionLevel.UNKNOWN

    missing = list(permission_set.missing_evidence)
    write_scopes = {
        scope
        for scope in SENSITIVE_PERMISSION_SCOPES
        if permission_set.get(scope) is PermissionLevel.WRITE
    }
    if write_scopes - {"id-token"} and write_setting is None:
        missing.append("fork pull-request write-token repository setting")
    if "id-token" in write_scopes:
        missing.append("fork pull-request OIDC token availability")
    levels = {
        scope: adjust(scope, level) for scope, level in permission_set.levels.items()
    }
    default_level = permission_set.default_level
    if default_level is PermissionLevel.WRITE:
        if write_setting is True:
            default_level = PermissionLevel.WRITE
        elif write_setting is False:
            default_level = PermissionLevel.READ
        else:
            default_level = PermissionLevel.UNKNOWN
    observed_provenance = (
        [f"fork-write-token-observed:{str(write_setting).lower()}"]
        if write_setting is not None
        else []
    )
    return EffectivePermissionSet(
        levels=levels,
        default_level=default_level,
        provenance=[
            *permission_set.provenance,
            "fork-pull-request-adjustment",
            *observed_provenance,
        ],
        missing_evidence=sorted(set(missing)),
    )


def resolve_effective_permissions(
    workflow: Workflow,
    job: Job,
    *,
    event_name: str,
    profile: AttackerProfile,
    caller_ceiling: EffectivePermissionSet | None = None,
    repository_default: RepositoryWorkflowPermissionDefault | None = None,
    fork_policy: ForkPullRequestPolicyEvidence | None = None,
) -> EffectivePermissionSet:
    permissions = _declared_permission_set(
        workflow.permissions,
        job.permissions_declared,
        repository_default,
    )
    permissions = _apply_event_permission_adjustment(
        permissions,
        event_name=event_name,
        profile=profile,
        fork_policy=fork_policy,
    )
    if caller_ceiling is not None:
        permissions = _cap_permission_set(permissions, caller_ceiling)
    return permissions


def cache_write_access_for_event(event_name: str) -> tuple[TriState, list[str]]:
    """Model GitHub.com's June 2026 cache-token policy for known event scopes.

    `pull_request` and `release` retain read-write cache access because they use a
    non-default-branch scope. The listed trusted/default-branch events retain write
    access. Low-trust default-branch events are read-only. Events whose branch/scope
    semantics are not modeled remain UNKNOWN instead of being guessed.
    """

    if event_name in CACHE_READ_WRITE_EVENTS:
        return TriState.TRUE, []
    if event_name in CACHE_READ_ONLY_DEFAULT_BRANCH_EVENTS:
        return TriState.FALSE, []
    return TriState.UNKNOWN, ["GitHub Actions cache scope and trigger write policy"]


def _tri_and(left: TriState, right: TriState) -> TriState:
    if left is TriState.FALSE or right is TriState.FALSE:
        return TriState.FALSE
    if left is TriState.TRUE and right is TriState.TRUE:
        return TriState.TRUE
    return TriState.UNKNOWN


def _tri_or(left: TriState, right: TriState) -> TriState:
    if left is TriState.TRUE or right is TriState.TRUE:
        return TriState.TRUE
    if left is TriState.FALSE and right is TriState.FALSE:
        return TriState.FALSE
    return TriState.UNKNOWN


def _split_top_level(expression: str, operator: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(expression):
        char = expression[index]
        if quote:
            if char == quote and (index == 0 or expression[index - 1] != "\\"):
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif depth == 0 and expression.startswith(operator, index):
            parts.append(expression[start:index].strip())
            start = index + len(operator)
            index = start
            continue
        index += 1
    if not parts:
        return [expression]
    parts.append(expression[start:].strip())
    return parts


def evaluate_job_condition(
    condition: str | None,
    *,
    event_name: str,
    profile: AttackerProfile,
) -> TriState:
    return evaluate_condition(
        condition,
        target_id="compatibility-job",
        target_kind="job",
        trigger_context_id="compatibility-trigger",
        context=ConditionRuntimeContext(event_name=event_name, profile=profile),
    ).verdict


def _checkout_step_targets_fork_pr(step) -> bool:
    if not step.uses or not step.uses.startswith("actions/checkout@"):
        return False
    serialized = " ".join(str(value) for value in step.with_.values()).casefold()
    markers = (
        "github.event.pull_request.head.sha",
        "github.event.pull_request.head.repo.full_name",
        "refs/pull/",
        "pull_request.head",
        "head.sha",
    )
    return any(marker in serialized for marker in markers)


def _checkout_builtin_protection(step) -> TriState:
    """Model the 2026 actions/checkout fork-PR checkout protection.

    Floating supported major tags receive the backported protection. An explicit
    allow-unsafe-pr-checkout opt-out re-enables the risky checkout. For pinned
    SHAs/minor/patch versions, the repository alone cannot prove whether the
    selected checkout revision contains the protection.
    """

    if not step.uses or not step.uses.startswith("actions/checkout@"):
        return TriState.FALSE
    allow_unsafe = step.with_.get("allow-unsafe-pr-checkout")
    if str(allow_unsafe).strip().casefold() == "true":
        return TriState.FALSE
    version = step.uses.split("@", 1)[1].strip()
    if re.fullmatch(r"v(?:[2-9]|[1-9][0-9]+)", version, flags=re.IGNORECASE):
        return TriState.TRUE
    if re.fullmatch(r"v(?:[7-9]|[1-9][0-9]+)\.[0-9]+(?:\.[0-9]+)?", version, flags=re.IGNORECASE):
        return TriState.TRUE
    return TriState.UNKNOWN


def fork_pr_checkout_execution_verdict(
    job: Job,
    *,
    event_name: str,
    profile: AttackerProfile,
) -> tuple[TriState, list[str]]:
    """Return whether fork-controlled code is known to become executable in a trusted PR context."""

    if profile is not AttackerProfile.FORK_PR or event_name not in {
        "pull_request_target",
        "workflow_run",
    }:
        return TriState.FALSE, []

    manual_fetch = any(
        step.run
        and re.search(
            r"(?:git\s+(?:fetch|checkout|switch)|gh\s+pr\s+checkout)[^\r\n]*"
            r"(?:pull_request|refs/pull/|head\.sha|head_ref)",
            step.run,
            flags=re.IGNORECASE,
        )
        for step in job.steps
    )
    if manual_fetch:
        return TriState.TRUE, []

    targeted = [step for step in job.steps if _checkout_step_targets_fork_pr(step)]
    if not targeted:
        return TriState.FALSE, []
    protections = [_checkout_builtin_protection(step) for step in targeted]
    if any(value is TriState.FALSE for value in protections):
        return TriState.TRUE, []
    if all(value is TriState.TRUE for value in protections):
        return TriState.FALSE, []
    return TriState.UNKNOWN, ["actions/checkout fork-PR protection for pinned revision"]


def _root_trigger_semantics(
    event_name: str,
    profile: AttackerProfile,
) -> tuple[TriState, TriState, TriState, list[str]]:
    if profile is AttackerProfile.FORK_PR:
        if event_name == "pull_request":
            return TriState.TRUE, TriState.TRUE, TriState.UNKNOWN, []
        if event_name == "pull_request_target":
            return TriState.TRUE, TriState.TRUE, TriState.TRUE, []
        if event_name in {"push", "workflow_dispatch"}:
            return TriState.FALSE, TriState.FALSE, TriState.TRUE, []
        if event_name == "issue_comment":
            return (
                TriState.UNKNOWN,
                TriState.TRUE,
                TriState.TRUE,
                ["comment author authorization"],
            )
        if event_name == "workflow_run":
            return (
                TriState.UNKNOWN,
                TriState.UNKNOWN,
                TriState.TRUE,
                ["upstream workflow triggerability"],
            )
    if profile is AttackerProfile.REPO_WRITER:
        if event_name in {"push", "workflow_dispatch"}:
            return TriState.TRUE, TriState.TRUE, TriState.TRUE, []
        if event_name in {"pull_request", "pull_request_target", "issue_comment"}:
            return TriState.TRUE, TriState.TRUE, TriState.TRUE, []
        if event_name == "workflow_run":
            return (
                TriState.UNKNOWN,
                TriState.UNKNOWN,
                TriState.TRUE,
                ["upstream workflow triggerability"],
            )
    return (
        TriState.UNKNOWN,
        TriState.UNKNOWN,
        TriState.UNKNOWN,
        ["runtime event provenance"],
    )


def _subject_assessment_verdict(
    state: AnalysisState,
    subject_id: str,
) -> tuple[TriState, list[str], list[str]]:
    assessment = next(
        (item for item in state.assessments if item.subject_id == subject_id),
        None,
    )
    if assessment is None:
        return TriState.FALSE, [], []
    return assessment.verdict, assessment.evidence_ids, assessment.missing_evidence


def _assessment_verdict(state: AnalysisState, job: Job) -> tuple[TriState, list[str], list[str]]:
    assessments = {item.subject_id: item for item in state.assessments}
    relevant = [
        assessment
        for subject_id in [job.job_id, *(step.step_id for step in job.steps)]
        if (assessment := assessments.get(subject_id)) is not None
    ]
    if any(item.verdict is TriState.TRUE for item in relevant):
        return (
            TriState.TRUE,
            sorted({evidence for item in relevant for evidence in item.evidence_ids}),
            [],
        )
    if any(item.verdict is TriState.UNKNOWN for item in relevant):
        return (
            TriState.UNKNOWN,
            sorted({evidence for item in relevant for evidence in item.evidence_ids}),
            sorted({missing for item in relevant for missing in item.missing_evidence}),
        )
    return TriState.FALSE, [], []


def _local_reusable_path(value: str | None) -> str | None:
    if not value or not value.startswith("./") or "${{" in value:
        return None
    return str(PurePosixPath(value[2:]))


def exact_push_branches(workflow: Workflow) -> tuple[str, ...]:
    """Return exact push branch filters that can be checked with GitHub's branch rules REST API.

    Wildcard/negated filters are intentionally excluded because the REST endpoint accepts only a
    concrete branch name. A push workflow without an exact allowlist remains branch-rule agnostic:
    a repository writer may choose another branch, so default-branch protection must not be treated
    as a global blocker.
    """

    config = workflow.triggers.get("push")
    if not isinstance(config, dict):
        return ()
    raw = config.get("branches")
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [str(item) for item in raw]
    else:
        return ()
    branches: list[str] = []
    for value in values:
        branch = value.strip()
        if (
            not branch
            or "${{" in branch
            or branch.startswith("!")
            or any(character in branch for character in "*?[")
        ):
            return ()
        branches.append(branch)
    return tuple(dict.fromkeys(branches))


def _workflow_run_names(workflow: Workflow) -> set[str]:
    config = workflow.triggers.get("workflow_run")
    if not isinstance(config, dict):
        return set()
    names = config.get("workflows") or []
    if isinstance(names, str):
        return {names}
    return {str(name) for name in names}


def _trigger_context(
    workflow: Workflow,
    *,
    event_name: str,
    activation_type: str,
    profile: AttackerProfile,
    fork_policy: ForkPullRequestPolicyEvidence | None = None,
    attacker_can_trigger: TriState | None = None,
    payload_control: TriState | None = None,
    workflow_revision_trusted: TriState | None = None,
    caller_job_context_id: str | None = None,
    call_depth: int = 0,
    missing_evidence: list[str] | None = None,
) -> TriggerExecutionContext:
    root = _root_trigger_semantics(event_name, profile)
    root_trigger = root[0]
    root_missing = list(root[3])
    if (
        profile is AttackerProfile.FORK_PR
        and event_name == "pull_request"
        and fork_policy is not None
        and fork_policy.private_repo_settings_applicable
        and fork_policy.run_workflows_from_fork_pull_requests is False
    ):
        root_trigger = TriState.FALSE
        root_missing = []
    return TriggerExecutionContext(
        context_id=stable_id(
            "trigger-context",
            workflow.workflow_id,
            event_name,
            activation_type,
            caller_job_context_id or "root",
        ),
        workflow_id=workflow.workflow_id,
        event_name=event_name,
        activation_type=activation_type,
        attacker_can_trigger=attacker_can_trigger or root_trigger,
        event_payload_attacker_controlled=payload_control or root[1],
        workflow_revision_trusted=workflow_revision_trusted or root[2],
        caller_job_context_id=caller_job_context_id,
        call_depth=call_depth,
        evidence_ids=workflow.evidence_ids,
        missing_evidence=sorted(set((missing_evidence or []) + root_missing)),
    )


def _execution_result(value: str | None) -> ExecutionResult:
    if value is None:
        return ExecutionResult.UNKNOWN
    try:
        return ExecutionResult(value.strip().casefold())
    except ValueError:
        return ExecutionResult.UNKNOWN


def _normalized_workflow_path(value: str) -> str:
    return value.replace("\\", "/").removeprefix("./")


def _runtime_workflow(
    state: AnalysisState,
    workflow_run_identity: WorkflowRunIdentityEvidence | None,
) -> Workflow | None:
    if workflow_run_identity is None or not workflow_run_identity.workflow_path:
        return None
    expected = _normalized_workflow_path(workflow_run_identity.workflow_path)
    matches = [
        workflow
        for workflow in state.workflows
        if _normalized_workflow_path(workflow.path) == expected
    ]
    return matches[0] if len(matches) == 1 else None


def _runtime_job_results(
    state: AnalysisState,
    workflow_run_jobs: WorkflowRunJobsEvidence | None,
    workflow_run_identity: WorkflowRunIdentityEvidence | None,
) -> dict[str, WorkflowRunJobEvidence]:
    runtime_workflow = _runtime_workflow(state, workflow_run_identity)
    if workflow_run_jobs is None or runtime_workflow is None:
        return {}
    static_by_name: dict[str, list[Job]] = {}
    for workflow in (runtime_workflow,):
        for job in workflow.jobs:
            raw_name = job.name if job.name is not None else job.job_key
            if not isinstance(raw_name, str):
                continue
            name = raw_name.strip()
            if not name or "${{" in name:
                continue
            static_by_name.setdefault(name, []).append(job)
    runtime_by_name: dict[str, list[WorkflowRunJobEvidence]] = {}
    for runtime_job in workflow_run_jobs.jobs:
        runtime_by_name.setdefault(runtime_job.name, []).append(runtime_job)
    return {
        jobs[0].job_id: runtime_by_name[name][0]
        for name, jobs in static_by_name.items()
        if len(jobs) == 1 and len(runtime_by_name.get(name, ())) == 1
    }


def _job_status_context(
    job: Job,
    jobs_by_key: dict[str, Job],
    results_by_key: dict[str, ExecutionResult],
) -> tuple[TriState, TriState, TriState, TriState, dict[str, ExecutionResult]]:
    direct_results: dict[str, ExecutionResult] = {}
    unresolved = False
    for need in job.needs:
        key = need.casefold()
        if key not in jobs_by_key:
            unresolved = True
            direct_results[key] = ExecutionResult.UNKNOWN
        else:
            direct_results[key] = results_by_key.get(key, ExecutionResult.UNKNOWN)

    ancestor_keys: set[str] = set()
    visiting: set[str] = set()
    cycle = False

    def visit(key: str) -> None:
        nonlocal cycle
        if key in ancestor_keys:
            return
        if key in visiting:
            cycle = True
            return
        dependency = jobs_by_key.get(key)
        if dependency is None:
            return
        visiting.add(key)
        ancestor_keys.add(key)
        for parent in dependency.needs:
            visit(parent.casefold())
        visiting.remove(key)

    for need in job.needs:
        visit(need.casefold())
    ancestor_results = [results_by_key.get(key, ExecutionResult.UNKNOWN) for key in ancestor_keys]
    dependency_gate = TriState.UNKNOWN if unresolved or cycle else TriState.TRUE
    if not ancestor_results:
        return (
            dependency_gate,
            TriState.TRUE,
            TriState.FALSE,
            TriState.FALSE,
            direct_results,
        )
    if any(result is ExecutionResult.FAILURE for result in ancestor_results):
        failure = TriState.TRUE
    elif any(result is ExecutionResult.UNKNOWN for result in ancestor_results):
        failure = TriState.UNKNOWN
    else:
        failure = TriState.FALSE
    if any(result is ExecutionResult.CANCELLED for result in ancestor_results):
        cancelled = TriState.TRUE
    elif any(result is ExecutionResult.UNKNOWN for result in ancestor_results):
        cancelled = TriState.UNKNOWN
    else:
        cancelled = TriState.FALSE
    if all(result is ExecutionResult.SUCCESS for result in ancestor_results):
        success = TriState.TRUE
    elif any(result is ExecutionResult.UNKNOWN for result in ancestor_results):
        success = TriState.UNKNOWN
    else:
        success = TriState.FALSE
    return dependency_gate, success, failure, cancelled, direct_results


def _runtime_step_matches(
    job: Job,
    runtime_job: WorkflowRunJobEvidence | None,
) -> dict[str, WorkflowRunStepEvidence]:
    if runtime_job is None:
        return {}
    static_by_name: dict[str, list[Step]] = {}
    for step in job.steps:
        if not isinstance(step.name, str):
            continue
        name = step.name.strip()
        if not name or "${{" in name:
            continue
        static_by_name.setdefault(name, []).append(step)
    runtime_by_name: dict[str, list[WorkflowRunStepEvidence]] = {}
    for runtime_step in runtime_job.steps:
        runtime_by_name.setdefault(runtime_step.name, []).append(runtime_step)
    return {
        steps[0].step_id: runtime_by_name[name][0]
        for name, steps in static_by_name.items()
        if len(steps) == 1 and len(runtime_by_name.get(name, ())) == 1
    }


def _step_runtime_results(
    step: Step,
    runtime_step: WorkflowRunStepEvidence | None,
    reachable: TriState,
) -> tuple[ExecutionResult, ExecutionResult]:
    if runtime_step is not None and runtime_step.status.casefold() == "completed":
        conclusion = _execution_result(runtime_step.conclusion)
        if step.continue_on_error in {None, False}:
            return conclusion, conclusion
        if step.continue_on_error is True:
            if conclusion in {ExecutionResult.CANCELLED, ExecutionResult.SKIPPED}:
                return conclusion, conclusion
            if conclusion is ExecutionResult.SUCCESS:
                return ExecutionResult.UNKNOWN, conclusion
        return ExecutionResult.UNKNOWN, conclusion
    if reachable is TriState.FALSE:
        return ExecutionResult.SKIPPED, ExecutionResult.SKIPPED
    return ExecutionResult.UNKNOWN, ExecutionResult.UNKNOWN


def _job_context(
    state: AnalysisState,
    workflow: Workflow,
    job: Job,
    trigger: TriggerExecutionContext,
    profile: AttackerProfile,
    *,
    caller_ceiling: EffectivePermissionSet | None = None,
    repository_default: RepositoryWorkflowPermissionDefault | None = None,
    fork_policy: ForkPullRequestPolicyEvidence | None = None,
    condition_runtime: ConditionRuntimeContext,
    condition_evaluations: list[ConditionEvaluation],
    dependency_gate: TriState = TriState.TRUE,
    runtime_result: ExecutionResult = ExecutionResult.UNKNOWN,
) -> JobExecutionContext:
    condition_evaluation = evaluate_condition(
        job.condition,
        target_id=job.job_id,
        target_kind="job",
        trigger_context_id=trigger.context_id,
        context=condition_runtime,
    )
    condition_evaluations.append(condition_evaluation)
    condition = condition_evaluation.verdict
    # Reachability answers whether this configured execution may run. Dependency eligibility and
    # expression truth remain separate so status-check functions can bypass the implicit success
    # gate without bypassing an invalid/unresolved dependency graph.
    reachable = _tri_and(dependency_gate, condition)
    attacker_code, assessment_evidence, assessment_missing = _assessment_verdict(state, job)
    attacker_code = _tri_and(trigger.attacker_can_trigger, _tri_and(reachable, attacker_code))
    permissions = resolve_effective_permissions(
        workflow,
        job,
        event_name=trigger.event_name,
        profile=profile,
        caller_ceiling=caller_ceiling,
        repository_default=repository_default,
        fork_policy=fork_policy,
    )
    missing = list(trigger.missing_evidence)
    if dependency_gate is TriState.UNKNOWN:
        missing.append("workflow job dependency graph")
    if condition is TriState.UNKNOWN:
        missing.extend(condition_evaluation.missing_evidence or ["job condition result"])
    missing.extend(assessment_missing)
    missing.extend(permissions.missing_evidence)
    return JobExecutionContext(
        context_id=stable_id("job-context", trigger.context_id, job.job_id),
        trigger_context_id=trigger.context_id,
        workflow_id=workflow.workflow_id,
        job_id=job.job_id,
        reachable=reachable,
        dependency_gate=dependency_gate,
        predicted_result=(
            runtime_result
            if runtime_result is not ExecutionResult.UNKNOWN
            else ExecutionResult.SKIPPED
            if reachable is TriState.FALSE
            else ExecutionResult.UNKNOWN
        ),
        effective_permissions=permissions,
        attacker_code_execution=attacker_code,
        condition_evaluation_id=condition_evaluation.evaluation_id,
        evidence_ids=sorted(set(trigger.evidence_ids + job.evidence_ids + assessment_evidence)),
        missing_evidence=sorted(set(missing)),
    )


def _effective_write_token_verdict(
    permissions: EffectivePermissionSet,
) -> tuple[TriState, list[str]]:
    scopes = SENSITIVE_PERMISSION_SCOPES - {"id-token"}
    levels = [permissions.get(scope) for scope in scopes]
    if any(level is PermissionLevel.WRITE for level in levels):
        return TriState.TRUE, []
    if any(level is PermissionLevel.UNKNOWN for level in levels):
        return TriState.UNKNOWN, permissions.missing_evidence
    return TriState.FALSE, []


def _semantic_relations_for_context(
    state: AnalysisState,
    trigger: TriggerExecutionContext,
    workflow: Workflow,
    job: Job,
    job_context: JobExecutionContext,
    profile: AttackerProfile,
    condition_runtime: ConditionRuntimeContext,
    condition_evaluations: list[ConditionEvaluation],
    runtime_job: WorkflowRunJobEvidence | None = None,
) -> list[Relation]:
    job_condition = next(
        item
        for item in reversed(condition_evaluations)
        if item.target_id == job.job_id and item.trigger_context_id == trigger.context_id
    )
    attacker_id = f"attacker:{profile.value}"
    relations = [
        _relation(
            attacker_id,
            "can_trigger_event_context",
            trigger.context_id,
            trigger.evidence_ids,
            category="source",
            verdict=trigger.attacker_can_trigger,
            missing_evidence=trigger.missing_evidence,
        ),
        _relation(
            trigger.context_id,
            "event_starts_workflow",
            workflow.workflow_id,
            trigger.evidence_ids,
            category="execution",
        ),
        _relation(
            trigger.context_id,
            "event_reaches_job_context",
            job_context.context_id,
            job_context.evidence_ids,
            category="execution",
            verdict=job_context.reachable,
            missing_evidence=job_context.missing_evidence,
            evidence_requirements=job_condition.evidence_requirements,
        ),
        _relation(
            job_context.context_id,
            "executes_job_definition",
            job.job_id,
            job_context.evidence_ids,
            category="context",
            verdict=job_context.reachable,
            missing_evidence=job_context.missing_evidence,
            evidence_requirements=job_condition.evidence_requirements,
        ),
    ]
    token_id = f"credential:github-token:{job_context.context_id}"
    write_token_verdict, write_token_missing = _effective_write_token_verdict(
        job_context.effective_permissions
    )
    relations.append(
        _relation(
            job_context.context_id,
            "job_receives_github_token",
            token_id,
            job_context.evidence_ids,
            category="credential",
            verdict=job_context.github_token_available,
        )
    )

    scopes = set(job_context.effective_permissions.levels) | SENSITIVE_PERMISSION_SCOPES
    for scope in sorted(scopes):
        level = job_context.effective_permissions.get(scope)
        if level not in {PermissionLevel.WRITE, PermissionLevel.UNKNOWN}:
            continue
        missing = (
            job_context.effective_permissions.missing_evidence
            if level is PermissionLevel.UNKNOWN
            else []
        )
        relations.append(
            _relation(
                token_id,
                "grants_effective_permission",
                f"permission:github-token:{scope}:{level.value}",
                job_context.evidence_ids,
                category="credential",
                verdict=TriState.UNKNOWN if level is PermissionLevel.UNKNOWN else TriState.TRUE,
                missing_evidence=missing,
            )
        )

    runtime_steps = _runtime_step_matches(job, runtime_job)
    prior_step_results: dict[str, tuple[ExecutionResult, ExecutionResult]] = {}
    current_job_result = (
        ExecutionResult.SKIPPED
        if job_context.reachable is TriState.FALSE
        else ExecutionResult.SUCCESS
    )
    implicit_step_gate = (
        TriState.FALSE if job_context.reachable is TriState.FALSE else TriState.TRUE
    )

    for step in job.steps:
        execution_id = stable_id("step-execution", job_context.context_id, step.step_id)
        status_success = (
            TriState.TRUE
            if current_job_result is ExecutionResult.SUCCESS
            else TriState.FALSE
            if current_job_result
            in {ExecutionResult.FAILURE, ExecutionResult.CANCELLED, ExecutionResult.SKIPPED}
            else TriState.UNKNOWN
        )
        status_failure = (
            TriState.TRUE
            if current_job_result is ExecutionResult.FAILURE
            else TriState.FALSE
            if current_job_result is not ExecutionResult.UNKNOWN
            else TriState.UNKNOWN
        )
        status_cancelled = (
            TriState.TRUE
            if current_job_result is ExecutionResult.CANCELLED
            else TriState.FALSE
            if current_job_result is not ExecutionResult.UNKNOWN
            else TriState.UNKNOWN
        )
        step_runtime = replace(
            condition_runtime,
            implicit_success_gate=implicit_step_gate,
            status_success=status_success,
            status_failure=status_failure,
            status_cancelled=status_cancelled,
            step_results=dict(prior_step_results),
        )
        step_evaluation = evaluate_condition(
            step.condition,
            target_id=step.step_id,
            target_kind="step",
            trigger_context_id=trigger.context_id,
            context=step_runtime,
        )
        condition_evaluations.append(step_evaluation)
        step_condition = step_evaluation.verdict
        step_reachable = _tri_and(job_context.reachable, step_condition)
        step_missing = list(job_context.missing_evidence)
        if step_condition is TriState.UNKNOWN:
            step_missing.extend(step_evaluation.missing_evidence or ["step condition result"])
        assessment_verdict, assessment_evidence, assessment_missing = _subject_assessment_verdict(
            state, step.step_id
        )
        controlled_execution = _tri_and(
            trigger.attacker_can_trigger,
            _tri_and(step_reachable, assessment_verdict),
        )
        control_missing = sorted(set(step_missing + assessment_missing))
        relations.append(
            _relation(
                job_context.context_id,
                "job_reaches_step_execution",
                execution_id,
                step.evidence_ids + job_context.evidence_ids,
                category="execution",
                verdict=step_reachable,
                missing_evidence=step_missing,
                evidence_requirements=step_evaluation.evidence_requirements,
            )
        )
        relations.append(
            _relation(
                execution_id,
                "executes_step_definition",
                step.step_id,
                step.evidence_ids,
                category="context",
                verdict=step_reachable,
                missing_evidence=step_missing,
                evidence_requirements=step_evaluation.evidence_requirements,
            )
        )
        relations.append(
            _relation(
                step.step_id,
                "step_definition_may_execute_as_context",
                execution_id,
                step.evidence_ids + job_context.evidence_ids,
                category="execution",
                verdict=step_reachable,
                missing_evidence=step_missing,
            )
        )
        relations.append(
            _relation(
                attacker_id,
                "controls_step_execution_context",
                execution_id,
                step.evidence_ids + job_context.evidence_ids + assessment_evidence,
                category="source",
                verdict=controlled_execution,
                missing_evidence=control_missing,
            )
        )
        relations.append(
            _relation(
                execution_id,
                "executes_with_github_token",
                token_id,
                step.evidence_ids + job_context.evidence_ids,
                category="credential",
                verdict=step_reachable,
                missing_evidence=step_missing,
            )
        )
        effective_write = _tri_and(step_reachable, write_token_verdict)
        if effective_write is not TriState.FALSE:
            relations.append(
                _relation(
                    execution_id,
                    "executes_with_effective_write_github_token",
                    token_id,
                    step.evidence_ids + job_context.evidence_ids,
                    category="credential",
                    verdict=effective_write,
                    missing_evidence=sorted(set(step_missing + write_token_missing)),
                )
            )

        outcome, conclusion = _step_runtime_results(
            step,
            runtime_steps.get(step.step_id),
            step_reachable,
        )
        if step.step_key:
            prior_step_results[step.step_key.casefold()] = (outcome, conclusion)
        if conclusion is ExecutionResult.FAILURE:
            current_job_result = ExecutionResult.FAILURE
            implicit_step_gate = TriState.FALSE
        elif conclusion is ExecutionResult.CANCELLED:
            current_job_result = ExecutionResult.CANCELLED
            implicit_step_gate = TriState.FALSE
        elif conclusion is ExecutionResult.UNKNOWN:
            current_job_result = ExecutionResult.UNKNOWN
        elif conclusion is ExecutionResult.SUCCESS:
            if current_job_result is ExecutionResult.SUCCESS:
                current_job_result = ExecutionResult.SUCCESS
        # SKIPPED does not alter the current job result.
    return relations


def derive_execution_semantics(
    state: AnalysisState,
    profile: AttackerProfile,
    *,
    repository_default: RepositoryWorkflowPermissionDefault | None = None,
    repository_metadata: RepositoryMetadataEvidence | None = None,
    fork_policy: ForkPullRequestPolicyEvidence | None = None,
    workflow_run_identity: WorkflowRunIdentityEvidence | None = None,
    actor_permissions: dict[str, RepositoryActorPermissionEvidence] | None = None,
    event_linkage: WorkflowRunEventLinkageEvidence | None = None,
    event_principals: dict[EventPrincipalRole, EventPrincipalEvidence] | None = None,
    workflow_run_jobs: WorkflowRunJobsEvidence | None = None,
    workflow_run_status: WorkflowRunStatusEvidence | None = None,
    github_run_id: int | None = None,
) -> ExecutionSemanticsResult:
    workflows_by_id = {workflow.workflow_id: workflow for workflow in state.workflows}
    workflows_by_path = {workflow.path: workflow for workflow in state.workflows}
    trigger_contexts: dict[str, TriggerExecutionContext] = {}
    job_contexts: dict[str, JobExecutionContext] = {}
    condition_evaluations: list[ConditionEvaluation] = []
    relations: dict[tuple[str, str, str], Relation] = {}
    runtime_jobs_by_static_id = _runtime_job_results(
        state,
        workflow_run_jobs,
        workflow_run_identity,
    )

    def add_relation(relation: Relation) -> None:
        key = (relation.source_id, relation.relation_type, relation.target_id)
        existing = relations.get(key)
        if existing is None:
            relations[key] = relation
            return
        existing.evidence_ids = sorted(set(existing.evidence_ids + relation.evidence_ids))
        existing.missing_evidence = sorted(
            set(existing.missing_evidence + relation.missing_evidence)
        )
        existing.evidence_requirements = sorted(
            {
                (item.kind, item.provider, item.subject_id, item.qualifier): item
                for item in [
                    *existing.evidence_requirements,
                    *relation.evidence_requirements,
                ]
            }.values(),
            key=lambda item: (item.kind.value, item.subject_id, item.qualifier or ""),
        )
        if relation.verdict is TriState.TRUE:
            existing.verdict = TriState.TRUE
            existing.missing_evidence = []
        elif existing.verdict is TriState.FALSE and relation.verdict is TriState.UNKNOWN:
            existing.verdict = TriState.UNKNOWN

    queue: list[tuple[TriggerExecutionContext, EffectivePermissionSet | None]] = []
    for workflow in state.workflows:
        for event_name in workflow.triggers:
            if event_name == "workflow_call":
                continue
            context = _trigger_context(
                workflow,
                event_name=event_name,
                activation_type=event_name,
                profile=profile,
                fork_policy=fork_policy,
            )
            trigger_contexts[context.context_id] = context
            queue.append((context, None))

    # workflow_run is a new privileged execution context but attacker triggerability is inherited
    # only through a locally matched producer workflow. External producer state remains UNKNOWN.
    for consumer in state.workflows:
        if "workflow_run" not in consumer.triggers:
            continue
        names = _workflow_run_names(consumer)
        matched = [
            workflow
            for workflow in state.workflows
            if workflow is not consumer and workflow.name and workflow.name in names
        ]
        root = next(
            (
                context
                for context in trigger_contexts.values()
                if context.workflow_id == consumer.workflow_id
                and context.activation_type == "workflow_run"
            ),
            None,
        )
        if root is None:
            continue
        producer_contexts = [
            context
            for producer in matched
            for context in trigger_contexts.values()
            if context.workflow_id == producer.workflow_id
            and context.activation_type != "workflow_run"
        ]
        if producer_contexts:
            if any(context.attacker_can_trigger is TriState.TRUE for context in producer_contexts):
                root.attacker_can_trigger = TriState.TRUE
                root.missing_evidence = []
            elif any(
                context.attacker_can_trigger is TriState.UNKNOWN for context in producer_contexts
            ):
                root.attacker_can_trigger = TriState.UNKNOWN
            else:
                root.attacker_can_trigger = TriState.FALSE
            for producer in matched:
                add_relation(
                    _relation(
                        producer.workflow_id,
                        "workflow_run_may_trigger",
                        root.context_id,
                        producer.evidence_ids + consumer.evidence_ids,
                        category="execution",
                        verdict=root.attacker_can_trigger,
                        missing_evidence=root.missing_evidence,
                    )
                )

    processed_triggers: set[str] = set()
    while queue:
        trigger, caller_ceiling = queue.pop(0)
        if trigger.context_id in processed_triggers:
            continue
        processed_triggers.add(trigger.context_id)
        workflow = workflows_by_id[trigger.workflow_id]
        condition_runtime = ConditionRuntimeContext(
            event_name=trigger.event_name,
            profile=profile,
            repository_full_name=(
                f"{repository_metadata.owner}/{repository_metadata.repository}"
                if repository_metadata
                else None
            ),
            repository_owner=(repository_metadata.owner if repository_metadata else None),
            default_branch=(repository_metadata.default_branch if repository_metadata else None),
            trigger_config=workflow.triggers,
            repository_id=state.repository.repository_id,
            github_run_id=github_run_id,
            actor_login=(workflow_run_identity.actor_login if workflow_run_identity else None),
            actor_id=(workflow_run_identity.actor_id if workflow_run_identity else None),
            triggering_actor_login=(
                workflow_run_identity.triggering_actor_login
                if workflow_run_identity
                else None
            ),
            triggering_actor_id=(
                workflow_run_identity.triggering_actor_id
                if workflow_run_identity
                else None
            ),
            run_event_name=(
                workflow_run_identity.event_name if workflow_run_identity else None
            ),
            actor_permissions={
                login.casefold(): (evidence.permission, evidence.actor_id)
                for login, evidence in (actor_permissions or {}).items()
            },
            event_linkage=(
                event_linkage
                if event_linkage is not None
                and event_linkage.event_name.casefold() == trigger.event_name.casefold()
                else None
            ),
            event_principals=(
                {
                    role: evidence
                    for role, evidence in (event_principals or {}).items()
                    if evidence.event_name.casefold() == trigger.event_name.casefold()
                }
                if event_linkage is not None
                and event_linkage.event_name.casefold() == trigger.event_name.casefold()
                else {}
            ),
        )
        jobs_by_key = {
            job.job_key.casefold(): job
            for job in workflow.jobs
            if isinstance(job.job_key, str) and job.job_key.strip()
        }
        results_by_key: dict[str, ExecutionResult] = {}

        def materialize_job(
            job: Job,
            *,
            active_workflow: Workflow,
            active_trigger: TriggerExecutionContext,
            active_caller_ceiling: EffectivePermissionSet | None,
            job_condition_runtime: ConditionRuntimeContext,
            dependency_gate: TriState,
            runtime_result: ExecutionResult,
        ) -> JobExecutionContext:
            context = _job_context(
                state,
                active_workflow,
                job,
                active_trigger,
                profile,
                caller_ceiling=active_caller_ceiling,
                repository_default=repository_default,
                fork_policy=fork_policy,
                condition_runtime=job_condition_runtime,
                condition_evaluations=condition_evaluations,
                dependency_gate=dependency_gate,
                runtime_result=runtime_result,
            )
            job_contexts[context.context_id] = context
            if active_trigger.caller_job_context_id is not None:
                caller_context = job_contexts.get(active_trigger.caller_job_context_id)
                if caller_context is not None:
                    add_relation(
                        _relation(
                            caller_context.context_id,
                            "reusable_workflow_permission_ceiling_applied",
                            context.context_id,
                            caller_context.evidence_ids + context.evidence_ids,
                            category="credential",
                        )
                    )
            for relation in _semantic_relations_for_context(
                state,
                active_trigger,
                active_workflow,
                job,
                context,
                profile,
                job_condition_runtime,
                condition_evaluations,
                runtime_job=runtime_jobs_by_static_id.get(job.job_id),
            ):
                add_relation(relation)

            reusable_path = _local_reusable_path(job.uses)
            if reusable_path is None or active_trigger.call_depth >= MAX_REUSABLE_DEPTH - 1:
                return context
            callee = workflows_by_path.get(reusable_path)
            if callee is None or "workflow_call" not in callee.triggers:
                return context
            if any(
                existing.workflow_id == callee.workflow_id
                and existing.caller_job_context_id == context.context_id
                for existing in trigger_contexts.values()
            ):
                return context
            callee_trigger = _trigger_context(
                callee,
                event_name=active_trigger.event_name,
                activation_type="workflow_call",
                profile=profile,
                attacker_can_trigger=_tri_and(
                    active_trigger.attacker_can_trigger,
                    context.reachable,
                ),
                payload_control=active_trigger.event_payload_attacker_controlled,
                workflow_revision_trusted=TriState.TRUE,
                caller_job_context_id=context.context_id,
                call_depth=active_trigger.call_depth + 1,
                missing_evidence=context.missing_evidence,
            )
            trigger_contexts[callee_trigger.context_id] = callee_trigger
            add_relation(
                _relation(
                    context.context_id,
                    "calls_reusable_workflow_context",
                    callee_trigger.context_id,
                    job.evidence_ids + callee.evidence_ids,
                    category="execution",
                    verdict=context.reachable,
                    missing_evidence=context.missing_evidence,
                )
            )
            queue.append((callee_trigger, context.effective_permissions))
            return context

        pending = list(workflow.jobs)
        while pending:
            progressed = False
            for job in list(pending):
                waiting = [
                    need.casefold()
                    for need in job.needs
                    if need.casefold() in jobs_by_key and need.casefold() not in results_by_key
                ]
                if waiting:
                    continue
                dependency_gate, success, failure, cancelled, direct_results = _job_status_context(
                    job,
                    jobs_by_key,
                    results_by_key,
                )
                runtime_job = runtime_jobs_by_static_id.get(job.job_id)
                runtime_result = (
                    _execution_result(runtime_job.conclusion)
                    if runtime_job is not None and runtime_job.status.casefold() == "completed"
                    else ExecutionResult.UNKNOWN
                )
                job_runtime = replace(
                    condition_runtime,
                    implicit_success_gate=success,
                    status_success=success,
                    status_failure=failure,
                    status_cancelled=cancelled,
                    needs_results=direct_results,
                )
                context = materialize_job(
                    job,
                    active_workflow=workflow,
                    active_trigger=trigger,
                    active_caller_ceiling=caller_ceiling,
                    job_condition_runtime=job_runtime,
                    dependency_gate=dependency_gate,
                    runtime_result=runtime_result,
                )
                if job.job_key:
                    results_by_key[job.job_key.casefold()] = context.predicted_result
                pending.remove(job)
                progressed = True
            if progressed:
                continue

            # Cycles or unresolved dependency ordering are not guessed. Materialize the
            # remaining jobs with an UNKNOWN dependency gate so downstream Frontiers preserve
            # missing evidence.
            for job in list(pending):
                direct_results = {
                    need.casefold(): results_by_key.get(
                        need.casefold(), ExecutionResult.UNKNOWN
                    )
                    for need in job.needs
                }
                job_runtime = replace(
                    condition_runtime,
                    implicit_success_gate=TriState.UNKNOWN,
                    status_success=TriState.UNKNOWN,
                    status_failure=TriState.UNKNOWN,
                    status_cancelled=TriState.UNKNOWN,
                    needs_results=direct_results,
                )
                context = materialize_job(
                    job,
                    active_workflow=workflow,
                    active_trigger=trigger,
                    active_caller_ceiling=caller_ceiling,
                    job_condition_runtime=job_runtime,
                    dependency_gate=TriState.UNKNOWN,
                    runtime_result=ExecutionResult.UNKNOWN,
                )
                if job.job_key:
                    results_by_key[job.job_key.casefold()] = context.predicted_result
                pending.remove(job)
            break

    return ExecutionSemanticsResult(
        trigger_contexts=list(trigger_contexts.values()),
        job_contexts=list(job_contexts.values()),
        condition_evaluations=condition_evaluations,
        relations=list(relations.values()),
    )
