from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    AttackerProfile,
    EffectivePermissionSet,
    Job,
    JobExecutionContext,
    PermissionLevel,
    Relation,
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
) -> Relation:
    return Relation(
        source_id=source_id,
        relation_type=relation_type,
        target_id=target_id,
        evidence_ids=sorted(set(evidence_ids)),
        category=category,
        verdict=verdict,
        missing_evidence=sorted(set(missing_evidence or [])),
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
) -> EffectivePermissionSet:
    declaration = job_permissions if job_permissions is not None else workflow_permissions
    provenance = ["job-permissions"] if job_permissions is not None else []
    if job_permissions is None and workflow_permissions is not None:
        provenance = ["workflow-permissions"]
    if declaration is None:
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
        missing_evidence=sorted(
            set(requested.missing_evidence + ceiling.missing_evidence)
        ),
    )


def _apply_event_permission_adjustment(
    permission_set: EffectivePermissionSet,
    *,
    event_name: str,
    profile: AttackerProfile,
) -> EffectivePermissionSet:
    if profile is not AttackerProfile.FORK_PR or event_name != "pull_request":
        return permission_set

    def adjust(level: PermissionLevel) -> PermissionLevel:
        if level is PermissionLevel.WRITE:
            return PermissionLevel.UNKNOWN
        return level

    missing = list(permission_set.missing_evidence)
    write_scopes = {
        scope
        for scope, level in permission_set.levels.items()
        if level is PermissionLevel.WRITE
    }
    if permission_set.default_level is PermissionLevel.WRITE or (write_scopes - {"id-token"}):
        missing.append("fork pull-request write-token repository setting")
    if permission_set.default_level is PermissionLevel.WRITE or "id-token" in write_scopes:
        missing.append("fork pull-request OIDC token availability")
    return EffectivePermissionSet(
        levels={scope: adjust(level) for scope, level in permission_set.levels.items()},
        default_level=adjust(permission_set.default_level),
        provenance=[*permission_set.provenance, "fork-pull-request-adjustment"],
        missing_evidence=sorted(set(missing)),
    )


def resolve_effective_permissions(
    workflow: Workflow,
    job: Job,
    *,
    event_name: str,
    profile: AttackerProfile,
    caller_ceiling: EffectivePermissionSet | None = None,
) -> EffectivePermissionSet:
    permissions = _declared_permission_set(workflow.permissions, job.permissions_declared)
    permissions = _apply_event_permission_adjustment(
        permissions,
        event_name=event_name,
        profile=profile,
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
    if condition is None:
        return TriState.TRUE
    expression = condition.strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    if expression.startswith("(") and expression.endswith(")"):
        expression = expression[1:-1].strip()

    or_parts = _split_top_level(expression, "||")
    if len(or_parts) > 1:
        result = TriState.FALSE
        for part in or_parts:
            result = _tri_or(
                result,
                evaluate_job_condition(part, event_name=event_name, profile=profile),
            )
        return result
    and_parts = _split_top_level(expression, "&&")
    if len(and_parts) > 1:
        result = TriState.TRUE
        for part in and_parts:
            result = _tri_and(
                result,
                evaluate_job_condition(part, event_name=event_name, profile=profile),
            )
        return result
    if expression.startswith("!") and not expression.startswith("!="):
        inner = evaluate_job_condition(
            expression[1:].strip(), event_name=event_name, profile=profile
        )
        if inner is TriState.TRUE:
            return TriState.FALSE
        if inner is TriState.FALSE:
            return TriState.TRUE
        return TriState.UNKNOWN

    lowered = expression.casefold()
    if lowered in {"true", "success()", "always()"}:
        return TriState.TRUE
    if lowered == "false":
        return TriState.FALSE
    if lowered in {"failure()", "cancelled()"}:
        return TriState.UNKNOWN

    event_match = re.fullmatch(
        r"github\.event_name\s*(==|!=)\s*(['\"])([^'\"]+)\2",
        expression,
        flags=re.IGNORECASE,
    )
    if event_match:
        equal = event_name == event_match.group(3)
        return TriState.TRUE if (equal == (event_match.group(1) == "==")) else TriState.FALSE

    fork_match = re.fullmatch(
        r"github\.event\.pull_request\.head\.repo\.fork\s*(==|!=)\s*(true|false)",
        expression,
        flags=re.IGNORECASE,
    )
    if fork_match:
        if event_name not in {"pull_request", "pull_request_target"}:
            actual = False
        elif profile is AttackerProfile.FORK_PR:
            actual = True
        else:
            return TriState.UNKNOWN
        expected = fork_match.group(2).casefold() == "true"
        equal = actual == expected
        return TriState.TRUE if (equal == (fork_match.group(1) == "==")) else TriState.FALSE
    return TriState.UNKNOWN


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
    attacker_can_trigger: TriState | None = None,
    payload_control: TriState | None = None,
    workflow_revision_trusted: TriState | None = None,
    caller_job_context_id: str | None = None,
    call_depth: int = 0,
    missing_evidence: list[str] | None = None,
) -> TriggerExecutionContext:
    root = _root_trigger_semantics(event_name, profile)
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
        attacker_can_trigger=attacker_can_trigger or root[0],
        event_payload_attacker_controlled=payload_control or root[1],
        workflow_revision_trusted=workflow_revision_trusted or root[2],
        caller_job_context_id=caller_job_context_id,
        call_depth=call_depth,
        evidence_ids=workflow.evidence_ids,
        missing_evidence=sorted(set((missing_evidence or []) + root[3])),
    )


def _job_context(
    state: AnalysisState,
    workflow: Workflow,
    job: Job,
    trigger: TriggerExecutionContext,
    profile: AttackerProfile,
    *,
    caller_ceiling: EffectivePermissionSet | None = None,
) -> JobExecutionContext:
    condition = evaluate_job_condition(
        job.condition,
        event_name=trigger.event_name,
        profile=profile,
    )
    # Reachability answers whether this configured execution may run. Attacker triggerability is a
    # separate trust property: persistence/cross-workflow paths can reach a privileged job even when
    # the attacker cannot directly dispatch that job.
    reachable = condition
    attacker_code, assessment_evidence, assessment_missing = _assessment_verdict(state, job)
    attacker_code = _tri_and(trigger.attacker_can_trigger, _tri_and(reachable, attacker_code))
    permissions = resolve_effective_permissions(
        workflow,
        job,
        event_name=trigger.event_name,
        profile=profile,
        caller_ceiling=caller_ceiling,
    )
    missing = list(trigger.missing_evidence)
    if condition is TriState.UNKNOWN:
        missing.append("job condition result")
    missing.extend(assessment_missing)
    missing.extend(permissions.missing_evidence)
    return JobExecutionContext(
        context_id=stable_id("job-context", trigger.context_id, job.job_id),
        trigger_context_id=trigger.context_id,
        workflow_id=workflow.workflow_id,
        job_id=job.job_id,
        reachable=reachable,
        effective_permissions=permissions,
        attacker_code_execution=attacker_code,
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
) -> list[Relation]:
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
        ),
        _relation(
            job_context.context_id,
            "executes_job_definition",
            job.job_id,
            job_context.evidence_ids,
            category="context",
            verdict=job_context.reachable,
            missing_evidence=job_context.missing_evidence,
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

    for step in job.steps:
        execution_id = stable_id("step-execution", job_context.context_id, step.step_id)
        step_condition = evaluate_job_condition(
            step.condition,
            event_name=trigger.event_name,
            profile=profile,
        )
        step_reachable = _tri_and(job_context.reachable, step_condition)
        step_missing = list(job_context.missing_evidence)
        if step_condition is TriState.UNKNOWN:
            step_missing.append("step condition result")
        assessment_verdict, assessment_evidence, assessment_missing = (
            _subject_assessment_verdict(state, step.step_id)
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
                    missing_evidence=sorted(
                        set(step_missing + write_token_missing)
                    ),
                )
            )
    return relations


def derive_execution_semantics(
    state: AnalysisState,
    profile: AttackerProfile,
) -> ExecutionSemanticsResult:
    workflows_by_id = {workflow.workflow_id: workflow for workflow in state.workflows}
    workflows_by_path = {workflow.path: workflow for workflow in state.workflows}
    trigger_contexts: dict[str, TriggerExecutionContext] = {}
    job_contexts: dict[str, JobExecutionContext] = {}
    relations: dict[tuple[str, str, str], Relation] = {}

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
        for job in workflow.jobs:
            context = _job_context(
                state,
                workflow,
                job,
                trigger,
                profile,
                caller_ceiling=caller_ceiling,
            )
            job_contexts[context.context_id] = context
            if trigger.caller_job_context_id is not None:
                caller_context = job_contexts.get(trigger.caller_job_context_id)
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
                state, trigger, workflow, job, context, profile
            ):
                add_relation(relation)

            reusable_path = _local_reusable_path(job.uses)
            if reusable_path is None or trigger.call_depth >= MAX_REUSABLE_DEPTH - 1:
                continue
            callee = workflows_by_path.get(reusable_path)
            if callee is None or "workflow_call" not in callee.triggers:
                continue
            if any(
                existing.workflow_id == callee.workflow_id
                and existing.caller_job_context_id == context.context_id
                for existing in trigger_contexts.values()
            ):
                continue
            callee_trigger = _trigger_context(
                callee,
                event_name=trigger.event_name,
                activation_type="workflow_call",
                profile=profile,
                attacker_can_trigger=_tri_and(trigger.attacker_can_trigger, context.reachable),
                payload_control=trigger.event_payload_attacker_controlled,
                workflow_revision_trusted=TriState.TRUE,
                caller_job_context_id=context.context_id,
                call_depth=trigger.call_depth + 1,
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

    return ExecutionSemanticsResult(
        trigger_contexts=list(trigger_contexts.values()),
        job_contexts=list(job_contexts.values()),
        relations=list(relations.values()),
    )
