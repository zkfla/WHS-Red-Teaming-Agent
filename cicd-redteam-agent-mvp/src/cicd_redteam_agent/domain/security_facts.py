from __future__ import annotations

import re
from collections import defaultdict
from fnmatch import fnmatchcase

from cicd_redteam_agent.domain.execution_semantics import (
    cache_write_access_for_event,
    exact_push_branches,
    fork_pr_checkout_execution_verdict,
)
from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.investigation import (
    ActionAdvisoryEvidence,
    ActionProvenanceEvidence,
    ActionsPolicyEvidence,
    AuditEventsEvidence,
    DeploymentBranchPolicyMode,
    EffectiveBranchRulesEvidence,
    EnvironmentBranchPatternsEvidence,
    EnvironmentProtectionPolicy,
    ForkPullRequestPolicyEvidence,
    PendingDeploymentStateEvidence,
    RuntimeFileEvent,
    RuntimeNetworkEvent,
    RuntimeProcessEvent,
    RuntimeTelemetryEvidence,
    SecretMetadataEvidence,
    SecretRotationPolicyEvidence,
    WorkflowRunJobEvidence,
    WorkflowRunJobsEvidence,
    concrete_environment_name,
)
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    AttackerProfile,
    ConditionEvaluation,
    ConditionSecurityEffect,
    EvidenceRequirementKind,
    EvidenceRequirementRef,
    Job,
    PermissionLevel,
    Relation,
    Step,
    TriState,
    Workflow,
)
from cicd_redteam_agent.domain.revision_semantics import derive_workspace_revision_relations

INPUT_EXPRESSION = re.compile(
    r"\$\{\{\s*("
    r"github\.head_ref|"
    r"github\.event\.pull_request\.(?:title|body|head\.(?:ref|sha))|"
    r"github\.event\.(?:issue\.(?:title|body)|comment\.body|release\."
    r"(?:name|body|tag_name)|inputs\.[A-Za-z_][\w-]*|"
    r"pull_request\.labels\.\*\.name)|"
    r"inputs\.[A-Za-z_][\w-]*"
    r")\s*}}",
    re.IGNORECASE,
)
BARE_INPUT_REFERENCE = re.compile(
    r"(?<![\w.])((?:"
    r"github\.head_ref|"
    r"github\.event\.pull_request\.(?:title|body|head\.(?:ref|sha))|"
    r"github\.event\.(?:issue\.(?:title|body)|comment\.body|release\."
    r"(?:name|body|tag_name)|inputs\.[A-Za-z_][\w-]*|"
    r"pull_request\.labels\.\*\.name)|"
    r"inputs\.[A-Za-z_][\w-]*"
    r"))",
    re.IGNORECASE,
)
SECRET_EXPRESSION = re.compile(
    r"\$\{\{\s*(?:secrets\.[A-Za-z_][A-Za-z0-9_]*|github\.token)\s*}}",
    re.IGNORECASE,
)
STEP_OUTPUT_EXPRESSION = re.compile(
    r"\$\{\{\s*steps\.([A-Za-z_][\w-]*)\.outputs\.([A-Za-z_][\w-]*)\s*}}",
    re.IGNORECASE,
)
GITHUB_OUTPUT_ASSIGNMENT = re.compile(
    r"(?:echo|printf)[^\r\n]*?([A-Za-z_][\w-]*)\s*=([^\r\n]+?)"
    r"(?:>>|\|\s*Out-File)[^\r\n]*(?:GITHUB_OUTPUT|github_output)",
    re.IGNORECASE,
)
COMMAND_INTERPRETER = re.compile(
    r"(?<![\w-])(?:eval|bash\s+-c|sh\s+-c|pwsh(?:\.exe)?\s+-Command|"
    r"Invoke-Expression)\b",
    re.IGNORECASE,
)
EXTERNAL_SEND = re.compile(
    r"(?<![\w-])(?:curl|wget|Invoke-WebRequest|Invoke-RestMethod)\b",
    re.IGNORECASE,
)
CHECKSUM_GUARD = re.compile(
    r"(?<![\w-])(?:sha256sum\s+(?:--check|-c)|shasum\s+-a\s+256|"
    r"Get-FileHash|cosign\s+verify)\b",
    re.IGNORECASE,
)
ALLOWLIST_GUARD = re.compile(
    r"(?<![\w-])(?:case\s+.+\s+in|grep\s+-[A-Za-z]*x|"
    r"contains\s*\(|startsWith\s*\()",
    re.IGNORECASE,
)
RUNTIME_EXECUTION = re.compile(
    r"(?<![\w])(?:\./|bash\s+|sh\s+|python(?:3)?\s+|node\s+|"
    r"pwsh\s+(?:-File\s+)?|source\s+|\.\s+)[\w./-]+",
    re.IGNORECASE,
)
GIT_HEAD_CHECKOUT = re.compile(
    r"git\s+(?:checkout|switch|fetch)[^\r\n]*(?:pull_request|head\.sha|head_ref)",
    re.IGNORECASE,
)
MUTABLE_INSTALL = re.compile(
    r"(?:curl|wget)[^\r\n|]*\|\s*(?:sudo\s+)?(?:bash|sh)|"
    r"(?:npm|pnpm|yarn)\s+(?:install|add)\s+[^\r\n@\s]+(?:@(?:latest|next))?|"
    r"pip(?:3)?\s+install\s+[^\r\n=<>~!\s]+(?:\s|$)",
    re.IGNORECASE,
)
CHECKSUM_MANIFEST = re.compile(
    r"(?:sha256sum\s+(?:--check|-c)|shasum\s+-a\s+256\s+-c)\s+"
    r"[\"']?([A-Za-z0-9_./-]+)",
    re.IGNORECASE,
)
FILE_REDIRECTION = re.compile(
    r"(?:>|>>)\s*[\"']?([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?)",
    re.IGNORECASE,
)
IMPORT_SINK = re.compile(
    r"(?<![\w-])(?:python(?:3)?\s+(?:-m\s+)?|node\s+|"
    r"(?:from|import)\s+|require\s*\()",
    re.IGNORECASE,
)
DEPENDENCY_OR_EXECUTABLE_PATH = re.compile(
    r"(?:^|/)(?:node_modules|\.venv|venv|bin|scripts?|site-packages|vendor)(?:/|$)",
    re.IGNORECASE,
)


def _relation(
    source_id: str,
    relation_type: str,
    target_id: str,
    evidence_ids: list[str],
    *,
    category: str,
    verdict: TriState = TriState.TRUE,
    missing_evidence: list[str] | None = None,
    evidence_requirements: list[EvidenceRequirementRef] | None = None,
) -> Relation:
    return Relation(
        source_id=source_id,
        relation_type=relation_type,
        target_id=target_id,
        category=category,
        verdict=verdict,
        evidence_ids=sorted(set(evidence_ids)),
        missing_evidence=sorted(set(missing_evidence or [])),
        evidence_requirements=evidence_requirements or [],
    )


def _deduplicate(relations: list[Relation]) -> list[Relation]:
    merged: dict[tuple[str, str, str], Relation] = {}
    for relation in relations:
        key = (relation.source_id, relation.relation_type, relation.target_id)
        existing = merged.get(key)
        if existing is None:
            merged[key] = relation
            continue
        existing.evidence_ids = sorted(set(existing.evidence_ids + relation.evidence_ids))
        existing.missing_evidence = sorted(
            set(existing.missing_evidence + relation.missing_evidence)
        )
        requirement_refs = {
            (ref.kind, ref.provider, ref.subject_id, ref.qualifier): ref
            for ref in [*existing.evidence_requirements, *relation.evidence_requirements]
        }
        existing.evidence_requirements = sorted(
            requirement_refs.values(),
            key=lambda ref: (
                ref.kind.value,
                ref.provider.value,
                ref.subject_id,
                ref.qualifier or "",
            ),
        )
        if relation.verdict is TriState.UNKNOWN:
            existing.verdict = TriState.UNKNOWN
        elif relation.verdict is TriState.FALSE and existing.verdict is not TriState.UNKNOWN:
            existing.verdict = TriState.FALSE
    return sorted(
        merged.values(),
        key=lambda item: (item.category or "", item.source_id, item.relation_type, item.target_id),
    )


def _input_node(expression: str) -> str:
    return stable_id("input", expression.lower())


def _output_node(step: Step, name: str) -> str:
    return f"output:{step.step_id}:{name.lower()}"


def _env_node(step: Step, name: str) -> str:
    return f"env:{step.step_id}:{name}"


def _job_env_node(job: Job, name: str) -> str:
    return f"env:{job.job_id}:{name}"


def _env_referenced(run: str, name: str) -> bool:
    patterns = (
        rf"\${re.escape(name)}\b",
        rf"\$\{{{re.escape(name)}\}}",
        rf"\$env:{re.escape(name)}\b",
        rf"%{re.escape(name)}%",
    )
    return any(re.search(pattern, run, re.IGNORECASE) for pattern in patterns)


def _job_has_sensitive_authority(state: AnalysisState, job: Job) -> bool:
    contexts = [item for item in state.job_execution_contexts if item.job_id == job.job_id]
    sensitive_scopes = {
        "actions",
        "contents",
        "deployments",
        "id-token",
        "packages",
        "pull-requests",
    }
    if any(
        context.effective_permissions.get(scope) in {PermissionLevel.WRITE, PermissionLevel.UNKNOWN}
        for context in contexts
        for scope in sensitive_scopes
    ):
        return True
    return any(reference.job_id == job.job_id for reference in state.secret_references)


def _strong_inline_guard(run: str) -> bool:
    """Return whether a local guard is concrete enough to negate the same-step flow.

    This deliberately recognizes only allowlist/integrity constructs. Merely having an
    ``if`` expression or an environment name never proves that execution is blocked.
    """

    case_rejects_default = bool(
        re.search(
            r"case\s+.+?\s+in[\s\S]*?\*\)\s*(?:exit|return)\s+[1-9]",
            run,
            re.IGNORECASE,
        )
    )
    exact_match_fails_closed = bool(
        re.search(
            r"grep\s+-[A-Za-z]*x[^\r\n]*(?:\|\||if\s+!)[^\r\n]*(?:exit|return)",
            run,
            re.IGNORECASE,
        )
    )
    return case_rejects_default or exact_match_fails_closed


def _secret_node(reference_id: str) -> str:
    return f"credential:secret:{reference_id}"


def _file_node(path: str) -> str:
    return f"file:{path.strip().strip(chr(34)).strip(chr(39)).lstrip('./')}"


def _source_verdict(workflow: Workflow, expression: str) -> tuple[TriState, list[str]]:
    triggers = set(workflow.triggers)
    lowered = expression.lower()
    if "pull_request" in lowered and triggers & {"pull_request", "pull_request_target"}:
        return TriState.TRUE, []
    if lowered == "github.head_ref" and triggers & {"pull_request", "pull_request_target"}:
        return TriState.TRUE, []
    if "comment.body" in lowered and "issue_comment" in triggers:
        return TriState.UNKNOWN, ["comment author authorization", "workflow condition result"]
    if "issue." in lowered and triggers & {"issues", "issue_comment"}:
        return TriState.UNKNOWN, ["issue author authorization", "workflow condition result"]
    if "release." in lowered and "release" in triggers:
        return TriState.UNKNOWN, ["release actor authorization"]
    if "inputs." in lowered and triggers & {"workflow_dispatch", "workflow_call"}:
        return TriState.UNKNOWN, ["workflow caller authorization"]
    return TriState.UNKNOWN, ["runtime event provenance"]


def _expressions(value: object) -> list[str]:
    serialized = str(value)
    return sorted(
        {match.group(1) for match in INPUT_EXPRESSION.finditer(serialized)}
        | {match.group(1) for match in BARE_INPUT_REFERENCE.finditer(serialized)}
    )


def _output_references(value: object) -> list[tuple[str, str]]:
    return sorted(
        {(match.group(1), match.group(2)) for match in STEP_OUTPUT_EXPRESSION.finditer(str(value))}
    )


def _source_facts(
    state: AnalysisState, workflow: Workflow, profile: AttackerProfile
) -> list[Relation]:
    relations: list[Relation] = []
    attacker_id = f"attacker:{profile.value}"
    seen: set[str] = set()
    for job in workflow.jobs:
        values: list[object] = [
            job.condition or "",
            job.with_,
            job.env,
            job.outputs,
        ]
        for step in job.steps:
            values.extend([step.condition or "", step.run or "", step.env, step.with_])
        for expression in {item for value in values for item in _expressions(value)}:
            if expression in seen:
                continue
            seen.add(expression)
            verdict, missing = _source_verdict(workflow, expression)
            relations.append(
                _relation(
                    attacker_id,
                    "controls_workflow_input",
                    _input_node(expression),
                    workflow.evidence_ids,
                    category="source",
                    verdict=verdict,
                    missing_evidence=missing,
                )
            )
    assessments = {item.subject_id: item for item in state.assessments}
    job_ids = {job.job_id for job in workflow.jobs}
    member_ids = job_ids | {step.step_id for job in workflow.jobs for step in job.steps}
    for relation in state.relations:
        if relation.relation_type != "executes_repository_file":
            continue
        if relation.source_id not in member_ids:
            continue
        assessment = assessments.get(relation.source_id)
        if assessment is None and relation.source_id in job_ids:
            triggers = set(workflow.triggers)
            if "pull_request" in triggers:
                verdict = TriState.TRUE
                evidence_ids = workflow.evidence_ids + relation.evidence_ids
                missing: list[str] = []
            elif "pull_request_target" in triggers:
                verdict = TriState.UNKNOWN
                evidence_ids = workflow.evidence_ids + relation.evidence_ids
                missing = ["runtime reusable workflow revision"]
            else:
                continue
        elif assessment is None or assessment.verdict is TriState.FALSE:
            continue
        else:
            verdict = assessment.verdict
            evidence_ids = relation.evidence_ids + assessment.evidence_ids
            missing = assessment.missing_evidence
        relations.append(
            _relation(
                attacker_id,
                "controls_repository_file",
                relation.target_id,
                evidence_ids,
                category="source",
                verdict=verdict,
                missing_evidence=missing,
            )
        )
        relations.append(
            _relation(
                relation.target_id,
                "repository_file_executes_in",
                relation.source_id,
                relation.evidence_ids,
                category="execution",
            )
        )
    return relations


def _containment_facts(workflow: Workflow) -> list[Relation]:
    relations: list[Relation] = [
        _relation(
            f"file:{workflow.path}",
            "defines_workflow",
            workflow.workflow_id,
            workflow.evidence_ids,
            category="context",
        )
    ]
    for job in workflow.jobs:
        relations.append(
            _relation(
                workflow.workflow_id,
                "contains_job",
                job.job_id,
                workflow.evidence_ids + job.evidence_ids,
                category="context",
            )
        )
        for step in job.steps:
            relations.append(
                _relation(
                    job.job_id,
                    "contains_step",
                    step.step_id,
                    job.evidence_ids + step.evidence_ids,
                    category="context",
                )
            )
    return relations


def _step_flow_facts(
    state: AnalysisState,
    workflow: Workflow,
    job: Job,
    profile: AttackerProfile,
) -> list[Relation]:
    relations: list[Relation] = []
    keyed_steps = {step.step_key.lower(): step for step in job.steps if step.step_key}
    outputs: dict[tuple[str, str], str] = {}

    if job.uses:
        for expression in _expressions(job.with_):
            verdict, missing = _source_verdict(workflow, expression)
            relations.append(
                _relation(
                    _input_node(expression),
                    "untrusted_input_reaches_reusable_workflow",
                    job.job_id,
                    job.evidence_ids,
                    category="transform",
                    verdict=verdict,
                    missing_evidence=missing,
                )
            )

    for name, value in job.env.items():
        for expression in _expressions(value):
            relations.append(
                _relation(
                    _input_node(expression),
                    "value_flows_to_job_env",
                    _job_env_node(job, name),
                    job.evidence_ids,
                    category="transform",
                )
            )

    for step in job.steps:
        run = step.run or ""
        for assignment in GITHUB_OUTPUT_ASSIGNMENT.finditer(run):
            name = assignment.group(1)
            output_node = _output_node(step, name)
            if step.step_key:
                outputs[(step.step_key.lower(), name.lower())] = output_node
            for expression in _expressions(assignment.group(2)):
                relations.append(
                    _relation(
                        _input_node(expression),
                        "value_flows_to_step_output",
                        output_node,
                        step.evidence_ids,
                        category="transform",
                    )
                )
            for env_name in step.env:
                if _env_referenced(assignment.group(2), env_name):
                    relations.append(
                        _relation(
                            _env_node(step, env_name),
                            "env_value_flows_to_step_output",
                            output_node,
                            step.evidence_ids,
                            category="transform",
                        )
                    )
            for env_name in job.env:
                if _env_referenced(assignment.group(2), env_name):
                    relations.append(
                        _relation(
                            _job_env_node(job, env_name),
                            "env_value_flows_to_step_output",
                            output_node,
                            step.evidence_ids + job.evidence_ids,
                            category="transform",
                        )
                    )

    for step in job.steps:
        run = step.run or ""
        interpreter = bool(COMMAND_INTERPRETER.search(run))
        external_send = bool(EXTERNAL_SEND.search(run))
        output_producer = "github_output" in run.lower()
        guarded = _strong_inline_guard(run)
        for name in job.env:
            if not _env_referenced(run, name):
                continue
            if interpreter:
                relation_type = "env_value_interpreted_as_command"
                category = "execution"
            elif external_send:
                relation_type = "env_value_reaches_external_send"
                category = "execution"
            elif output_producer:
                relation_type = "env_value_reaches_output_producer"
                category = "transform"
            else:
                relation_type = "env_value_reaches_execution"
                category = "execution"
            relations.append(
                _relation(
                    _job_env_node(job, name),
                    relation_type,
                    step.step_id,
                    step.evidence_ids + job.evidence_ids,
                    category=category,
                    verdict=TriState.FALSE if guarded else TriState.TRUE,
                )
            )
        for expression in _expressions(run):
            # GitHub expressions embedded directly in `run` are substituted before the shell
            # parses the script.  They therefore become shell source even without an explicit
            # eval/bash -c construct.  Output-producing commands remain transform-only.
            if output_producer:
                relation_type = "untrusted_input_reaches_output_producer"
                category = "transform"
            else:
                relation_type = "untrusted_value_interpreted_as_command"
                category = "execution"
            relations.append(
                _relation(
                    _input_node(expression),
                    relation_type,
                    step.step_id,
                    step.evidence_ids,
                    category=category,
                    verdict=TriState.FALSE if guarded else TriState.TRUE,
                )
            )
            if guarded:
                guard_id = f"guard:inline:{step.step_id}"
                relations.extend(
                    [
                        _relation(
                            _input_node(expression),
                            "value_validated_by_guard",
                            guard_id,
                            step.evidence_ids,
                            category="guard",
                        ),
                        _relation(
                            guard_id,
                            "guard_blocks_untrusted_execution",
                            step.step_id,
                            step.evidence_ids,
                            category="guard",
                        ),
                    ]
                )
        for expression in _expressions(step.with_):
            if step.uses and step.uses.startswith("actions/checkout@"):
                relation_type = "untrusted_input_selects_checkout_revision"
                category = "execution"
            elif step.uses and step.uses.startswith("actions/github-script@"):
                relation_type = "untrusted_input_interpreted_by_github_script"
                category = "execution"
            else:
                relation_type = "untrusted_input_reaches_action"
                category = "transform"
            relation_verdict = TriState.TRUE
            relation_missing: list[str] = []
            if relation_type == "untrusted_input_selects_checkout_revision":
                relevant_events = [
                    context.event_name
                    for context in state.trigger_contexts
                    if context.workflow_id == workflow.workflow_id
                    and context.event_name in {"pull_request_target", "workflow_run"}
                ]
                checkout_results = [
                    fork_pr_checkout_execution_verdict(
                        job,
                        event_name=event_name,
                        profile=profile,
                    )
                    for event_name in relevant_events
                ]
                if checkout_results:
                    verdicts = [item[0] for item in checkout_results]
                    if any(item is TriState.TRUE for item in verdicts):
                        relation_verdict = TriState.TRUE
                    elif any(item is TriState.UNKNOWN for item in verdicts):
                        relation_verdict = TriState.UNKNOWN
                    else:
                        relation_verdict = TriState.FALSE
                    relation_missing = sorted(
                        {missing for _, items in checkout_results for missing in items}
                    )
            relations.append(
                _relation(
                    _input_node(expression),
                    relation_type,
                    step.step_id,
                    step.evidence_ids,
                    category=category,
                    verdict=relation_verdict,
                    missing_evidence=relation_missing,
                )
            )
        for key, value in step.env.items():
            env_node = _env_node(step, key)
            for expression in _expressions(value):
                relations.append(
                    _relation(
                        _input_node(expression),
                        "value_flows_to_env",
                        env_node,
                        step.evidence_ids,
                        category="transform",
                    )
                )
            for output_key, output_name in _output_references(value):
                source = outputs.get((output_key.lower(), output_name.lower()))
                if source is None and output_key.lower() in keyed_steps:
                    source = _output_node(keyed_steps[output_key.lower()], output_name)
                if source:
                    relations.append(
                        _relation(
                            source,
                            "value_flows_to_env",
                            env_node,
                            step.evidence_ids,
                            category="transform",
                        )
                    )
            if run and _env_referenced(run, key):
                if interpreter:
                    relation_type = "env_value_interpreted_as_command"
                    category = "execution"
                elif external_send:
                    relation_type = "env_value_reaches_external_send"
                    category = "execution"
                elif output_producer:
                    relation_type = "env_value_reaches_output_producer"
                    category = "transform"
                else:
                    relation_type = "env_value_reaches_execution"
                    category = "execution"
                relations.append(
                    _relation(
                        env_node,
                        relation_type,
                        step.step_id,
                        step.evidence_ids,
                        category=category,
                        verdict=TriState.FALSE if guarded else TriState.TRUE,
                    )
                )
        for output_key, output_name in _output_references(run):
            producer = keyed_steps.get(output_key.lower())
            source = outputs.get((output_key.lower(), output_name.lower()))
            if source is None and producer is not None:
                source = _output_node(producer, output_name)
            if source:
                relations.append(
                    _relation(
                        source,
                        (
                            "output_value_interpreted_as_command"
                            if interpreter
                            else "output_value_reaches_execution"
                        ),
                        step.step_id,
                        step.evidence_ids,
                        category="execution",
                        verdict=TriState.FALSE if guarded else TriState.TRUE,
                    )
                )
                # Direct `${{ steps.x.outputs.y }}` substitution happens before shell parsing.
                # Model that syntax boundary independently from whether the script also calls eval.
                relations.append(
                    _relation(
                        source,
                        "output_value_interpolated_into_shell_source",
                        step.step_id,
                        step.evidence_ids,
                        category="execution",
                        verdict=TriState.FALSE if guarded else TriState.TRUE,
                    )
                )
                # For a PR-triggered workflow, an external action output may depend on attacker
                # influenced repository/event data, but that provenance and the action's output
                # escaping are outside the local repository.  Preserve that uncertainty rather
                # than assuming either attacker control or safety.
                if (
                    producer is not None
                    and producer.uses
                    and not producer.uses.startswith("./")
                    and not producer.uses.startswith("docker://")
                    and set(workflow.triggers) & {"pull_request", "pull_request_target"}
                ):
                    relations.append(
                        _relation(
                            f"attacker:{profile.value}",
                            "attacker_may_control_external_action_output",
                            source,
                            workflow.evidence_ids + producer.evidence_ids + step.evidence_ids,
                            category="source",
                            verdict=TriState.UNKNOWN,
                            missing_evidence=[
                                "external action output attacker influence",
                                "external action output encoding and escaping",
                            ],
                        )
                    )
        controlled_nodes = [_input_node(expression) for expression in _expressions(run)] + [
            _env_node(step, name) for name in step.env if _env_referenced(run, name)
        ]
        for match in FILE_REDIRECTION.finditer(run):
            target = _file_node(match.group(1))
            for source in controlled_nodes:
                relations.append(
                    _relation(
                        source,
                        "value_written_to_repository_file",
                        target,
                        step.evidence_ids,
                        category="transform",
                        verdict=TriState.FALSE if guarded else TriState.TRUE,
                    )
                )
        if IMPORT_SINK.search(run):
            for raw_path in re.findall(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)+", run):
                relations.append(
                    _relation(
                        _file_node(raw_path),
                        "repository_file_imported_or_loaded_by",
                        step.step_id,
                        step.evidence_ids,
                        category="execution",
                    )
                )
        if GIT_HEAD_CHECKOUT.search(run):
            relations.append(
                _relation(
                    workflow.workflow_id,
                    "selects_pull_request_revision",
                    step.step_id,
                    workflow.evidence_ids + step.evidence_ids,
                    category="execution",
                )
            )
    return relations


def _permission_summary(
    state: AnalysisState,
    job: Job,
    scope: str,
) -> tuple[TriState, list[str]]:
    contexts = [item for item in state.job_execution_contexts if item.job_id == job.job_id]
    if not contexts:
        return TriState.UNKNOWN, ["execution-context permission semantics"]
    levels = [item.effective_permissions.get(scope) for item in contexts]
    missing = sorted(
        {
            evidence
            for item in contexts
            if item.effective_permissions.get(scope) is PermissionLevel.UNKNOWN
            for evidence in item.effective_permissions.missing_evidence
            if not (
                scope == "id-token"
                and evidence == "fork pull-request write-token repository setting"
            )
            and not (
                scope != "id-token" and evidence == "fork pull-request OIDC token availability"
            )
        }
    )
    if all(level is PermissionLevel.WRITE for level in levels):
        return TriState.TRUE, []
    if any(level in {PermissionLevel.WRITE, PermissionLevel.UNKNOWN} for level in levels):
        return TriState.UNKNOWN, missing or ["event-scoped GITHUB_TOKEN permission"]
    return TriState.FALSE, []


def _credential_facts(state: AnalysisState, workflow: Workflow, job: Job) -> list[Relation]:
    relations: list[Relation] = []
    contexts = [item for item in state.job_execution_contexts if item.job_id == job.job_id]
    explicit_scopes = sorted(
        {
            scope
            for context in contexts
            for scope in context.effective_permissions.levels
            if scope != "__default__"
        }
    )
    write_all = bool(contexts) and all(
        context.effective_permissions.default_level is PermissionLevel.WRITE for context in contexts
    )
    default_unknown = any(
        context.effective_permissions.default_level is PermissionLevel.UNKNOWN
        for context in contexts
    )
    subjects: list[Step | Job] = list(job.steps) if job.steps else [job]
    for subject in subjects:
        subject_id = subject.step_id if isinstance(subject, Step) else subject.job_id
        permission_names = ["write-all"] if write_all else explicit_scopes
        for permission in permission_names:
            verdict, missing = (
                (TriState.TRUE, [])
                if permission == "write-all"
                else _permission_summary(state, job, permission)
            )
            if verdict is TriState.FALSE:
                continue
            relations.append(
                _relation(
                    subject_id,
                    f"can_use_github_token_{permission}_write",
                    f"credential:github-token:{job.job_id}:{permission}",
                    job.evidence_ids + subject.evidence_ids,
                    category="credential",
                    verdict=verdict,
                    missing_evidence=missing,
                )
            )
        if default_unknown:
            relations.append(
                _relation(
                    subject_id,
                    "github_token_permission_unknown",
                    f"credential:github-token:{job.job_id}:unknown",
                    job.evidence_ids + subject.evidence_ids,
                    category="credential",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["repository default GITHUB_TOKEN permissions"],
                )
            )

        oidc_verdict, oidc_missing = _permission_summary(state, job, "id-token")
        if oidc_verdict is not TriState.FALSE:
            relations.append(
                _relation(
                    subject_id,
                    "can_request_oidc_token",
                    f"credential:oidc:{job.job_id}",
                    job.evidence_ids + subject.evidence_ids,
                    category="credential",
                    verdict=oidc_verdict,
                    missing_evidence=oidc_missing,
                )
            )
            uses = subject.uses or ""
            cloud_target: str | None = None
            provider = ""
            if uses.startswith("aws-actions/configure-aws-credentials@"):
                provider = "aws"
                cloud_target = str(subject.with_.get("role-to-assume", "unknown-role"))
            elif uses.startswith("google-github-actions/auth@"):
                provider = "gcp"
                cloud_target = str(
                    subject.with_.get("service_account")
                    or subject.with_.get("workload_identity_provider")
                    or "unknown-identity"
                )
            elif uses.startswith("azure/login@"):
                provider = "azure"
                cloud_target = str(
                    subject.with_.get("client-id")
                    or subject.with_.get("subscription-id")
                    or "unknown-identity"
                )
            if cloud_target is not None:
                assessment = next(
                    (
                        item
                        for item in state.aws_oidc_trust_assessments
                        if isinstance(subject, Step) and item.step_id == subject.step_id
                    ),
                    None,
                )
                if provider == "aws" and assessment is not None:
                    if assessment.authentication_mode.value not in {"github-oidc", "unknown"}:
                        continue
                    cloud_verdict = assessment.verdict
                    cloud_missing = sorted(set(assessment.missing_evidence + oidc_missing))
                    cloud_requirements = assessment.evidence_requirements
                else:
                    cloud_verdict = TriState.UNKNOWN
                    cloud_missing = sorted(
                        set(oidc_missing + [f"{provider} federated identity trust policy"])
                    )
                    cloud_requirements = []
                relations.append(
                    _relation(
                        f"credential:oidc:{job.job_id}",
                        "oidc_may_assume_cloud_identity",
                        stable_id("cloud-identity", provider, cloud_target),
                        job.evidence_ids + subject.evidence_ids,
                        category="credential",
                        verdict=cloud_verdict,
                        missing_evidence=cloud_missing,
                        evidence_requirements=cloud_requirements,
                    )
                )
    for reference in state.secret_references:
        if reference.workflow_id != workflow.workflow_id or reference.job_id != job.job_id:
            continue
        source_id = reference.step_id or job.job_id
        relations.append(
            _relation(
                source_id,
                "can_access_secret_reference",
                f"credential:secret:{reference.reference_id}",
                reference.evidence_ids,
                category="credential",
            )
        )
    return relations


def _secret_flow_facts(state: AnalysisState, workflow: Workflow, job: Job) -> list[Relation]:
    """Connect secret credentials to their concrete local and external sinks."""

    relations: list[Relation] = []
    steps = {step.step_id: step for step in job.steps}
    for reference in state.secret_references:
        if reference.workflow_id != workflow.workflow_id or reference.job_id != job.job_id:
            continue
        secret = _secret_node(reference.reference_id)
        step = steps.get(reference.step_id or "")
        if step is None:
            matched_env = [
                name
                for name, value in job.env.items()
                if reference.expression.lower() in str(value).lower()
            ]
            if not matched_env:
                relations.append(
                    _relation(
                        secret,
                        "secret_available_in_job_environment",
                        job.job_id,
                        reference.evidence_ids,
                        category="credential",
                    )
                )
            for name in matched_env:
                env_node = _job_env_node(job, name)
                relations.append(
                    _relation(
                        secret,
                        "secret_flows_to_env",
                        env_node,
                        reference.evidence_ids + job.evidence_ids,
                        category="transform",
                    )
                )
                for consumer in job.steps:
                    run = consumer.run or ""
                    if not _env_referenced(run, name):
                        continue
                    sink = (
                        "secret_reaches_external_send"
                        if EXTERNAL_SEND.search(run)
                        else "secret_reaches_shell_argument"
                    )
                    relations.append(
                        _relation(
                            env_node,
                            sink,
                            consumer.step_id,
                            reference.evidence_ids + consumer.evidence_ids,
                            category="execution",
                        )
                    )
            continue

        expression = reference.expression.lower()
        run = step.run or ""
        secret_env_nodes = [
            _env_node(step, name)
            for name, value in step.env.items()
            if expression in str(value).lower()
        ]
        for env_node in secret_env_nodes:
            relations.append(
                _relation(
                    secret,
                    "secret_flows_to_env",
                    env_node,
                    reference.evidence_ids + step.evidence_ids,
                    category="transform",
                )
            )
            env_name = env_node.rsplit(":", 1)[-1]
            if not _env_referenced(run, env_name):
                continue
            sinks = {"secret_reaches_shell_argument"}
            if EXTERNAL_SEND.search(run):
                sinks.add("secret_reaches_external_send")
            if "github_output" in run.lower():
                sinks.add("secret_reaches_step_output")
            if re.search(r"(?<![\w-])(?:echo|printf|write-output)\b", run, re.IGNORECASE):
                sinks.add("secret_may_reach_log")
            for sink in sinks:
                relations.append(
                    _relation(
                        env_node,
                        sink,
                        step.step_id,
                        reference.evidence_ids + step.evidence_ids,
                        category="execution",
                    )
                )

        if reference.scope == "with":
            external = bool(step.uses and not step.uses.startswith("./"))
            relations.append(
                _relation(
                    secret,
                    ("secret_passed_to_external_action" if external else "secret_passed_to_action"),
                    step.step_id,
                    reference.evidence_ids + step.evidence_ids,
                    category="credential",
                )
            )
            uses = (step.uses or "").lower()
            provider = next(
                (
                    name
                    for marker, name in (
                        ("aws", "aws"),
                        ("google", "gcp"),
                        ("azure", "azure"),
                    )
                    if marker in uses
                ),
                None,
            )
            if provider:
                relations.append(
                    _relation(
                        secret,
                        "static_cloud_credential_may_authenticate_identity",
                        stable_id("cloud-identity", provider, step.step_id),
                        reference.evidence_ids + step.evidence_ids,
                        category="credential",
                        verdict=TriState.UNKNOWN,
                        missing_evidence=[f"{provider} credential scope and runtime identity"],
                    )
                )
        elif reference.scope == "run":
            relations.append(
                _relation(
                    secret,
                    "secret_interpolated_in_shell",
                    step.step_id,
                    reference.evidence_ids + step.evidence_ids,
                    category="execution",
                )
            )

        secret_sources = [secret, *secret_env_nodes]
        for match in FILE_REDIRECTION.finditer(run):
            for source in secret_sources:
                relations.append(
                    _relation(
                        source,
                        "secret_written_to_file",
                        _file_node(match.group(1)),
                        reference.evidence_ids + step.evidence_ids,
                        category="transform",
                    )
                )
    return relations


def _condition_security_relations(
    evaluation: ConditionEvaluation,
    target_id: str,
    evidence_ids: list[str],
) -> list[Relation]:
    guard_id = f"guard:condition:{evaluation.evaluation_id}"
    if evaluation.security_effect is ConditionSecurityEffect.ATTACKER_SATISFIABLE:
        return [
            _relation(
                f"attacker:condition:{evaluation.evaluation_id}",
                "attacker_can_satisfy_condition",
                target_id,
                evidence_ids,
                category="source",
                verdict=TriState.TRUE,
            )
        ]
    if evaluation.security_effect is ConditionSecurityEffect.ALWAYS_TRUE:
        return []
    if evaluation.security_effect is ConditionSecurityEffect.BLOCKS:
        verdict = TriState.TRUE
        missing: list[str] = []
    elif (
        evaluation.security_effect is ConditionSecurityEffect.TRUST_REQUIRED
        and evaluation.revision_bound is TriState.TRUE
    ):
        verdict = TriState.TRUE
        missing = []
    else:
        verdict = TriState.UNKNOWN
        missing = sorted(
            set(evaluation.security_missing_evidence + evaluation.missing_evidence)
        ) or ["condition satisfier or trust boundary"]
    return [
        _relation(
            guard_id,
            "guard_blocks_untrusted_execution",
            target_id,
            evidence_ids,
            category="guard",
            verdict=verdict,
            missing_evidence=missing,
            evidence_requirements=evaluation.evidence_requirements,
        )
    ]


def _guard_facts(
    state: AnalysisState,
    repository_id: str,
    workflow: Workflow,
    job: Job,
    environment_policies: dict[str, EnvironmentProtectionPolicy],
    environment_branch_patterns: dict[str, EnvironmentBranchPatternsEvidence],
    pending_deployment_state: PendingDeploymentStateEvidence | None = None,
    fork_policy: ForkPullRequestPolicyEvidence | None = None,
) -> list[Relation]:
    relations: list[Relation] = []
    job_evaluations = [
        item
        for item in state.condition_evaluations
        if item.target_kind == "job" and item.target_id == job.job_id
    ]
    if (
        fork_policy is not None
        and fork_policy.require_approval_for_fork_pr_workflows is True
        and "pull_request" in workflow.triggers
    ):
        relations.append(
            _relation(
                job.job_id,
                "fork_pr_workflow_requires_approval",
                f"guard:fork-approval:{job.job_id}",
                workflow.evidence_ids + job.evidence_ids,
                category="guard",
                verdict=TriState.UNKNOWN,
                missing_evidence=["fork pull-request workflow approval state"],
            )
        )
    if job.condition:
        for evaluation in job_evaluations:
            relations.extend(
                _condition_security_relations(
                    evaluation,
                    job.job_id,
                    workflow.evidence_ids + job.evidence_ids,
                )
            )
    environment_name = concrete_environment_name(job)
    if job.environment is not None and environment_name is None:
        relations.append(
            _relation(
                job.job_id,
                "environment_protection_unknown",
                f"guard:environment:{job.job_id}",
                job.evidence_ids,
                category="guard",
                verdict=TriState.UNKNOWN,
                missing_evidence=["runtime GitHub environment name and protection settings"],
            )
        )
    elif environment_name is not None and environment_name not in environment_policies:
        relations.append(
            _relation(
                job.job_id,
                "environment_protection_unknown",
                f"guard:environment:{job.job_id}",
                job.evidence_ids,
                category="guard",
                verdict=TriState.UNKNOWN,
                missing_evidence=["GitHub environment protection settings"],
            )
        )
    elif environment_name is not None:
        policy = environment_policies[environment_name]
        guard = f"guard:environment:{job.job_id}"
        if policy.required_reviewer_count:
            relations.append(
                _relation(
                    job.job_id,
                    "environment_requires_deployment_review",
                    guard,
                    job.evidence_ids,
                    category="guard",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["environment deployment approval state"],
                )
            )
        if pending_deployment_state is not None and any(
            pending.environment_name.casefold() == environment_name.casefold()
            for pending in pending_deployment_state.pending_environments
        ):
            relations.append(
                _relation(
                    f"guard:pending-deployment:{job.job_id}",
                    "guard_blocks_untrusted_execution",
                    job.job_id,
                    job.evidence_ids,
                    category="guard",
                    verdict=TriState.TRUE,
                )
            )
        if (
            policy.deployment_branch_policy is DeploymentBranchPolicyMode.CUSTOM
            and environment_name not in environment_branch_patterns
        ):
            relations.append(
                _relation(
                    job.job_id,
                    "environment_restricts_deployment_branches",
                    guard,
                    job.evidence_ids,
                    category="guard",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["environment deployment ref eligibility"],
                    evidence_requirements=[
                        EvidenceRequirementRef(
                            kind=EvidenceRequirementKind.GITHUB_ENVIRONMENT_BRANCH_PATTERNS,
                            subject_id=f"{repository_id}:environment:{environment_name}",
                            qualifier=environment_name,
                        )
                    ],
                )
            )
        elif policy.deployment_branch_policy is DeploymentBranchPolicyMode.PROTECTED:
            relations.append(
                _relation(
                    job.job_id,
                    "environment_restricts_deployment_branches",
                    guard,
                    job.evidence_ids,
                    category="guard",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["environment deployment protected-branch eligibility"],
                )
            )
        elif policy.deployment_branch_policy is DeploymentBranchPolicyMode.UNKNOWN:
            relations.append(
                _relation(
                    job.job_id,
                    "environment_deployment_branch_policy_unknown",
                    guard,
                    job.evidence_ids,
                    category="guard",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["environment deployment branch policy"],
                )
            )
        if policy.custom_protection_rule_count:
            relations.append(
                _relation(
                    job.job_id,
                    "environment_custom_protection_rules_enabled",
                    guard,
                    job.evidence_ids,
                    category="guard",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["custom deployment protection rule result"],
                )
            )
        if policy.wait_timer_minutes:
            relations.append(
                _relation(
                    job.job_id,
                    "environment_wait_timer_configured",
                    guard,
                    job.evidence_ids,
                    category="guard",
                )
            )
        if policy.deployment_branch_policy is DeploymentBranchPolicyMode.NONE and not any(
            (
                policy.required_reviewer_count,
                policy.wait_timer_minutes,
                policy.custom_protection_rule_count,
            )
        ):
            relations.append(
                _relation(
                    job.job_id,
                    "environment_has_no_deployment_protection",
                    guard,
                    job.evidence_ids,
                    category="guard",
                )
            )
    for step in job.steps:
        run = step.run or ""
        if step.condition:
            step_evaluations = [
                item
                for item in state.condition_evaluations
                if item.target_kind == "step" and item.target_id == step.step_id
            ]
            for evaluation in step_evaluations:
                relations.extend(
                    _condition_security_relations(
                        evaluation,
                        step.step_id,
                        step.evidence_ids,
                    )
                )
        if CHECKSUM_GUARD.search(run):
            relations.append(
                _relation(
                    step.step_id,
                    "verifies_content_integrity",
                    f"guard:integrity:{step.step_id}",
                    step.evidence_ids,
                    category="guard",
                )
            )
        if ALLOWLIST_GUARD.search(run):
            effective = _strong_inline_guard(run)
            relations.append(
                _relation(
                    step.step_id,
                    "validates_value_with_allowlist",
                    f"guard:allowlist:{step.step_id}",
                    step.evidence_ids,
                    category="guard",
                    verdict=TriState.TRUE if effective else TriState.UNKNOWN,
                    missing_evidence=[] if effective else ["allowlist fail-closed behavior"],
                )
            )
            if effective:
                relations.append(
                    _relation(
                        f"guard:allowlist:{step.step_id}",
                        "guard_blocks_untrusted_execution",
                        step.step_id,
                        step.evidence_ids,
                        category="guard",
                    )
                )
    return relations


def _artifact_and_cache_facts(state: AnalysisState) -> list[Relation]:
    relations: list[Relation] = []
    assessments = {item.subject_id: item for item in state.assessments}
    artifacts: dict[str, list[tuple[Workflow, Job, Step, str]]] = defaultdict(list)
    downloads: list[tuple[Workflow, Job, Step, str, str, bool]] = []
    caches: dict[str, list[tuple[Workflow, Job, Step, str]]] = defaultdict(list)
    for workflow in state.workflows:
        for job in workflow.jobs:
            for step in job.steps:
                uses = step.uses or ""
                if uses.startswith("actions/upload-artifact@"):
                    name = str(step.with_.get("name", ""))
                    path = str(step.with_.get("path", ""))
                    if name:
                        artifacts[name].append((workflow, job, step, path))
                        relations.append(
                            _relation(
                                step.step_id,
                                "uploads_artifact",
                                f"artifact:{name}",
                                step.evidence_ids,
                                category="transform",
                            )
                        )
                        normalized_upload = path.lstrip("./")
                        for prior in job.steps:
                            if prior.step_id == step.step_id or not prior.run:
                                continue
                            for match in FILE_REDIRECTION.finditer(prior.run):
                                written = match.group(1).lstrip("./")
                                if normalized_upload in {"", "."} or (
                                    written == normalized_upload
                                    or written.startswith(normalized_upload.rstrip("/") + "/")
                                ):
                                    relations.append(
                                        _relation(
                                            _file_node(written),
                                            "file_uploaded_as_artifact",
                                            f"artifact:{name}",
                                            prior.evidence_ids + step.evidence_ids,
                                            category="transform",
                                        )
                                    )
                        if "pull_request" in workflow.triggers:
                            relations.append(
                                _relation(
                                    "attacker:fork-pr",
                                    "attacker_may_control_artifact_content",
                                    f"artifact:{name}",
                                    workflow.evidence_ids + job.evidence_ids + step.evidence_ids,
                                    category="source",
                                )
                            )
                        for prior in job.steps:
                            assessment = assessments.get(prior.step_id)
                            if prior.step_id == step.step_id or assessment is None:
                                continue
                            if assessment.verdict is TriState.FALSE:
                                continue
                            relations.append(
                                _relation(
                                    prior.step_id,
                                    "repository_execution_may_produce_artifact",
                                    f"artifact:{name}",
                                    prior.evidence_ids + step.evidence_ids,
                                    category="transform",
                                    verdict=assessment.verdict,
                                    missing_evidence=assessment.missing_evidence,
                                )
                            )
                elif uses.startswith("actions/download-artifact@"):
                    name = str(step.with_.get("name", ""))
                    path = str(step.with_.get("path", "."))
                    run_id = str(step.with_.get("run-id", ""))
                    if name:
                        downloads.append(
                            (
                                workflow,
                                job,
                                step,
                                name,
                                path,
                                "github.event.workflow_run.id" in run_id,
                            )
                        )
                if uses.startswith("actions/cache@"):
                    key = str(step.with_.get("key", "UNKNOWN"))
                    cache_path = str(step.with_.get("path", ""))
                    caches[key].append((workflow, job, step, cache_path))
                    cache_id = stable_id("cache", key)
                    verdict = TriState.UNKNOWN if "${{" in key else TriState.TRUE
                    missing = ["runtime cache key value"] if verdict is TriState.UNKNOWN else []
                    relations.extend(
                        [
                            _relation(
                                step.step_id,
                                "writes_cache",
                                cache_id,
                                step.evidence_ids,
                                category="transform",
                                verdict=verdict,
                                missing_evidence=missing,
                            ),
                            _relation(
                                cache_id,
                                "restores_cache_into_step",
                                step.step_id,
                                step.evidence_ids,
                                category="transform",
                                verdict=verdict,
                                missing_evidence=missing,
                            ),
                        ]
                    )
                    trigger_by_id = {
                        context.context_id: context for context in state.trigger_contexts
                    }
                    job_contexts = [
                        context
                        for context in state.job_execution_contexts
                        if context.job_id == job.job_id
                        and context.workflow_id == workflow.workflow_id
                    ]
                    for job_context in job_contexts:
                        trigger = trigger_by_id.get(job_context.trigger_context_id)
                        if trigger is None:
                            continue
                        cache_write, cache_missing = cache_write_access_for_event(
                            trigger.event_name
                        )
                        context_missing = sorted(set(missing + cache_missing))
                        relations.append(
                            _relation(
                                job_context.context_id,
                                "cache_write_access_in_context",
                                cache_id,
                                job_context.evidence_ids + step.evidence_ids,
                                category="context",
                                verdict=cache_write,
                                missing_evidence=context_missing,
                            )
                        )
                        if trigger.attacker_can_trigger is TriState.FALSE:
                            attacker_cache_write = TriState.FALSE
                        elif cache_write is TriState.FALSE:
                            attacker_cache_write = TriState.FALSE
                        elif (
                            trigger.attacker_can_trigger is TriState.TRUE
                            and cache_write is TriState.TRUE
                        ):
                            attacker_cache_write = verdict
                        else:
                            attacker_cache_write = TriState.UNKNOWN
                        if attacker_cache_write is TriState.FALSE:
                            continue
                        attacker_missing = sorted(
                            set(
                                context_missing
                                + trigger.missing_evidence
                                + (
                                    []
                                    if attacker_cache_write is TriState.TRUE
                                    else ["attacker cache-write execution context"]
                                )
                            )
                        )
                        relations.append(
                            _relation(
                                "attacker:fork-pr",
                                "attacker_may_populate_cache",
                                cache_id,
                                workflow.evidence_ids
                                + job.evidence_ids
                                + step.evidence_ids
                                + job_context.evidence_ids,
                                category="source",
                                verdict=attacker_cache_write,
                                missing_evidence=attacker_missing,
                            )
                        )
                    for prior in job.steps:
                        assessment = assessments.get(prior.step_id)
                        if prior.step_id == step.step_id or assessment is None:
                            continue
                        if assessment.verdict is TriState.FALSE:
                            continue
                        relations.append(
                            _relation(
                                prior.step_id,
                                "repository_execution_may_populate_cache",
                                cache_id,
                                prior.evidence_ids + step.evidence_ids,
                                category="transform",
                                verdict=assessment.verdict,
                                missing_evidence=assessment.missing_evidence,
                            )
                        )
                    for expression in _expressions(key):
                        source_verdict, source_missing = _source_verdict(workflow, expression)
                        relations.append(
                            _relation(
                                _input_node(expression),
                                "untrusted_input_selects_cache_key",
                                cache_id,
                                step.evidence_ids,
                                category="transform",
                                verdict=source_verdict,
                                missing_evidence=source_missing,
                            )
                        )
                    restore_keys = step.with_.get("restore-keys")
                    if restore_keys:
                        relations.append(
                            _relation(
                                stable_id("cache-prefix", str(restore_keys)),
                                "broad_restore_key_may_select_cache",
                                step.step_id,
                                step.evidence_ids,
                                category="transform",
                                verdict=TriState.UNKNOWN,
                                missing_evidence=["runtime cache match and producer provenance"],
                            )
                        )
                    if DEPENDENCY_OR_EXECUTABLE_PATH.search(cache_path):
                        relations.append(
                            _relation(
                                cache_id,
                                "cache_restores_executable_or_dependency_path",
                                step.step_id,
                                step.evidence_ids,
                                category="execution",
                                verdict=verdict,
                                missing_evidence=missing,
                            )
                        )
    for _workflow, job, download, name, path, run_linked in downloads:
        verdict = TriState.TRUE if run_linked and name in artifacts else TriState.UNKNOWN
        missing = [] if verdict is TriState.TRUE else ["artifact producer and run provenance"]
        relations.append(
            _relation(
                f"artifact:{name}",
                "downloads_artifact_into_step",
                download.step_id,
                download.evidence_ids,
                category="transform",
                verdict=verdict,
                missing_evidence=missing,
            )
        )
        normalized_path = path.lstrip("./")
        trusted_checksum_seen = False
        for step in job.steps:
            if step.step_id == download.step_id or not step.run:
                continue
            if CHECKSUM_GUARD.search(step.run):
                manifest_match = CHECKSUM_MANIFEST.search(step.run)
                manifest = manifest_match.group(1).lstrip("./") if manifest_match else ""
                manifest_from_artifact = normalized_path in {"", "."} or (
                    bool(manifest)
                    and (
                        manifest == normalized_path
                        or manifest.startswith(normalized_path.rstrip("/") + "/")
                    )
                )
                trusted_checksum = bool(manifest) and not manifest_from_artifact
                trusted_checksum_seen = trusted_checksum_seen or trusted_checksum
                relations.append(
                    _relation(
                        f"artifact:{name}",
                        "artifact_verified_by_integrity_guard",
                        f"guard:integrity:{step.step_id}",
                        download.evidence_ids + step.evidence_ids,
                        category="guard",
                        verdict=verdict if trusted_checksum else TriState.UNKNOWN,
                        missing_evidence=(
                            missing
                            if trusted_checksum
                            else ["trusted checksum manifest provenance"]
                        ),
                    )
                )
            path_used = normalized_path in {"", "."} or normalized_path in step.run
            if path_used and RUNTIME_EXECUTION.search(step.run):
                relation_type = (
                    "artifact_content_reaches_import"
                    if IMPORT_SINK.search(step.run)
                    else "artifact_content_reaches_execution"
                )
                relations.append(
                    _relation(
                        f"artifact:{name}",
                        relation_type,
                        step.step_id,
                        download.evidence_ids + step.evidence_ids,
                        category="execution",
                        verdict=(
                            TriState.FALSE
                            if trusted_checksum_seen and verdict is TriState.TRUE
                            else verdict
                        ),
                        missing_evidence=missing,
                    )
                )
            elif path_used and re.search(
                r"(?<![\w-])(?:make|npm|pnpm|yarn|pytest|cargo)\b", step.run
            ):
                relations.append(
                    _relation(
                        f"artifact:{name}",
                        "artifact_content_reaches_command_argument",
                        step.step_id,
                        download.evidence_ids + step.evidence_ids,
                        category="execution",
                        verdict=verdict,
                        missing_evidence=missing,
                    )
                )
        if normalized_path in {"", "."}:
            relations.append(
                _relation(
                    f"artifact:{name}",
                    "artifact_may_overwrite_workspace_files",
                    job.job_id,
                    download.evidence_ids + job.evidence_ids,
                    category="transform",
                    verdict=verdict,
                    missing_evidence=missing,
                )
            )

    for key, entries in caches.items():
        cache_id = stable_id("cache", key)
        producers = [item for item in entries if "pull_request" in item[0].triggers]
        consumers = [item for item in entries if _job_has_sensitive_authority(state, item[1])]
        for producer_workflow, producer_job, producer_step, _ in producers:
            for consumer_workflow, consumer_job, consumer_step, _ in consumers:
                if producer_step.step_id == consumer_step.step_id:
                    continue
                relations.append(
                    _relation(
                        producer_step.step_id,
                        "cache_producer_reaches_privileged_consumer",
                        consumer_step.step_id,
                        producer_workflow.evidence_ids
                        + producer_job.evidence_ids
                        + producer_step.evidence_ids
                        + consumer_workflow.evidence_ids
                        + consumer_job.evidence_ids
                        + consumer_step.evidence_ids,
                        category="transform",
                        verdict=TriState.UNKNOWN if "${{" in key else TriState.TRUE,
                        missing_evidence=(["runtime cache key value"] if "${{" in key else []),
                    )
                )
                relations.append(
                    _relation(
                        cache_id,
                        "cache_restored_by_privileged_job",
                        consumer_job.job_id,
                        consumer_job.evidence_ids + consumer_step.evidence_ids,
                        category="transform",
                        verdict=TriState.UNKNOWN if "${{" in key else TriState.TRUE,
                        missing_evidence=(["runtime cache key value"] if "${{" in key else []),
                    )
                )
    return relations


def _actions_policy_allows_external_reference(
    reference: str,
    policy: ActionsPolicyEvidence | None,
) -> bool | None:
    if policy is None:
        return None
    if not policy.enabled:
        return False
    target = reference.removeprefix("external:")
    if policy.sha_pinning_required:
        ref = target.rsplit("@", 1)[-1] if "@" in target else ""
        if not re.fullmatch(r"[0-9a-fA-F]{40}", ref):
            return False
    if policy.allowed_actions == "all":
        return True
    if policy.allowed_actions == "local_only":
        return False
    if policy.allowed_actions != "selected":
        return None
    owner_repo = target.split("@", 1)[0]
    if owner_repo.startswith("actions/") and policy.github_owned_allowed is True:
        return True
    if any(
        fnmatchcase(target, pattern) or fnmatchcase(owner_repo, pattern)
        for pattern in policy.patterns_allowed
    ):
        return True
    if policy.verified_allowed is True:
        return None
    return False


def _action_provenance_subject(payload: ActionProvenanceEvidence) -> str:
    action = f"{payload.action_owner}/{payload.action_repository}"
    if payload.subpath:
        action += f"/{payload.subpath}"
    return f"external:{action}@{payload.requested_ref}"


def _external_action_package_ref(target_id: str) -> tuple[str, str] | None:
    target = target_id.removeprefix("external:")
    if "@" not in target:
        return None
    path, ref = target.rsplit("@", 1)
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}", ref


def _supply_chain_facts(
    state: AnalysisState,
    actions_policy: ActionsPolicyEvidence | None = None,
    *,
    action_provenance: list[ActionProvenanceEvidence] | None = None,
    action_advisories: list[ActionAdvisoryEvidence] | None = None,
) -> list[Relation]:
    relations: list[Relation] = []
    provenance_by_subject = {
        _action_provenance_subject(payload).casefold(): payload
        for payload in action_provenance or []
    }
    advisory_by_package_ref = {
        (payload.package_name.casefold(), payload.requested_ref.casefold()): payload
        for payload in action_advisories or []
    }
    for relation in state.relations:
        if relation.relation_type not in {
            "uses_external_action_ref",
            "uses_external_reusable_workflow",
        }:
            continue
        target = relation.target_id.removeprefix("external:")
        ref = target.rsplit("@", 1)[-1] if "@" in target else ""
        provenance = provenance_by_subject.get(relation.target_id.casefold())
        immutable_reference = bool(
            re.fullmatch(r"[0-9a-fA-F]{40}", ref)
            or (
                provenance is not None
                and provenance.immutable_release_tag is TriState.TRUE
            )
        )
        if provenance is not None:
            immutable_verdict = (
                TriState.TRUE
                if immutable_reference
                else TriState.FALSE
                if provenance.ref_kind in {"tag", "branch"}
                and provenance.immutable_release_tag is not TriState.UNKNOWN
                else TriState.UNKNOWN
            )
            relations.append(
                _relation(
                    relation.target_id,
                    "action_reference_is_immutable",
                    relation.source_id,
                    relation.evidence_ids,
                    category="guard",
                    verdict=immutable_verdict,
                    missing_evidence=(
                        []
                        if immutable_verdict is not TriState.UNKNOWN
                        else ["verified action reference immutability"]
                    ),
                )
            )
            if provenance.resolved_sha:
                relations.append(
                    _relation(
                        relation.target_id,
                        "action_ref_resolves_to_commit",
                        f"git:commit:{provenance.resolved_sha.lower()}",
                        relation.evidence_ids,
                        category="context",
                    )
                )
        package_ref = _external_action_package_ref(relation.target_id)
        advisory = (
            advisory_by_package_ref.get((package_ref[0].casefold(), package_ref[1].casefold()))
            if package_ref is not None
            else None
        )
        if advisory is not None:
            relations.append(
                _relation(
                    relation.target_id,
                    "known_action_advisory_affects_reference",
                    relation.source_id,
                    relation.evidence_ids,
                    category="execution",
                    verdict=advisory.affected,
                    missing_evidence=(
                        ["action advisory applicability"]
                        if advisory.affected is TriState.UNKNOWN
                        else []
                    ),
                )
            )
        if immutable_reference:
            continue
        allowed = _actions_policy_allows_external_reference(relation.target_id, actions_policy)
        relations.append(
            _relation(
                relation.target_id,
                (
                    "mutable_reusable_workflow_may_execute_in_job"
                    if relation.relation_type == "uses_external_reusable_workflow"
                    else "external_code_may_execute_in_step"
                ),
                relation.source_id,
                relation.evidence_ids,
                category="execution",
                verdict=TriState.FALSE if allowed is False else TriState.UNKNOWN,
                missing_evidence=(
                    []
                    if allowed is False
                    else ["external dependency integrity", "upstream compromise"]
                ),
            )
        )
    return relations


def _reusable_workflow_facts(state: AnalysisState) -> list[Relation]:
    relations: list[Relation] = []
    by_path = {workflow.path: workflow for workflow in state.workflows}
    for workflow in state.workflows:
        for job in workflow.jobs:
            if not job.uses or not job.uses.startswith("./") or "${{" in job.uses:
                continue
            path = job.uses[2:]
            called = by_path.get(path)
            target = called.workflow_id if called else f"file:{path}"
            relations.append(
                _relation(
                    job.job_id,
                    "calls_reusable_workflow",
                    target,
                    job.evidence_ids + (called.evidence_ids if called else []),
                    category="execution",
                    verdict=TriState.TRUE if called else TriState.UNKNOWN,
                    missing_evidence=[] if called else ["local reusable workflow parse result"],
                )
            )
    return relations


def _runtime_job_matches(
    state: AnalysisState,
    workflow_run_jobs: WorkflowRunJobsEvidence | None,
) -> dict[str, WorkflowRunJobEvidence]:
    if workflow_run_jobs is None:
        return {}
    static_by_name: dict[str, list[Job]] = defaultdict(list)
    for workflow in state.workflows:
        for job in workflow.jobs:
            raw_name = job.name if job.name is not None else job.job_key
            if not isinstance(raw_name, str):
                continue
            name = raw_name.strip()
            if not name or "${{" in name:
                continue
            static_by_name[name].append(job)
    runtime_by_name: dict[str, list[WorkflowRunJobEvidence]] = defaultdict(list)
    for runtime_job in workflow_run_jobs.jobs:
        runtime_by_name[runtime_job.name].append(runtime_job)
    return {
        jobs[0].job_id: runtime_by_name[name][0]
        for name, jobs in static_by_name.items()
        if len(jobs) == 1 and len(runtime_by_name.get(name, ())) == 1
    }


def _runtime_job_executed(job: WorkflowRunJobEvidence | None) -> bool:
    return bool(
        job is not None
        and job.runner_id is not None
        and job.status.casefold() in {"in_progress", "completed"}
    )


def _runtime_telemetry_facts(
    state: AnalysisState,
    telemetry: list[RuntimeTelemetryEvidence],
) -> list[Relation]:
    """Bind normalized runtime events only to unique exact static identities."""

    workflow_by_path = {workflow.path: workflow for workflow in state.workflows}
    relations: list[Relation] = []
    for payload in telemetry:
        for index, event in enumerate(payload.events):
            workflow = workflow_by_path.get(event.identity.workflow_path)
            if workflow is None:
                continue
            matching_jobs = [
                job
                for job in workflow.jobs
                if event.identity.job_name
                in {
                    value
                    for value in (job.name, job.job_key)
                    if isinstance(value, str) and value
                }
            ]
            if len(matching_jobs) != 1:
                continue
            job = matching_jobs[0]
            step_candidates = list(job.steps)
            if event.identity.step_number is not None:
                number = event.identity.step_number
                step_candidates = (
                    [step_candidates[number - 1]] if number <= len(step_candidates) else []
                )
            if event.identity.step_name is not None:
                step_candidates = [
                    step for step in step_candidates if step.name == event.identity.step_name
                ]
            if len(step_candidates) != 1:
                continue
            step = step_candidates[0]
            runtime_node = stable_id(
                "runtime-event",
                payload.run_id,
                str(payload.run_attempt),
                str(index),
                event.kind,
                step.step_id,
            )
            relations.append(
                _relation(
                    step.step_id,
                    "runtime_event_exactly_bound_to_step",
                    runtime_node,
                    step.evidence_ids,
                    category="context",
                )
            )
            if isinstance(event, RuntimeProcessEvent):
                relations.append(
                    _relation(
                        runtime_node,
                        "runtime_process_observed_in_step",
                        f"runtime:process:{event.process_name}",
                        step.evidence_ids,
                        category="execution",
                    )
                )
            elif isinstance(event, RuntimeNetworkEvent):
                relations.append(
                    _relation(
                        runtime_node,
                        "runtime_process_made_external_network_call",
                        f"runtime:network:{event.destination}:{event.port or 0}",
                        step.evidence_ids,
                        category="execution",
                    )
                )
            elif isinstance(event, RuntimeFileEvent) and event.workspace_relative:
                relations.append(
                    _relation(
                        runtime_node,
                        "runtime_process_modified_workspace_file",
                        _file_node(event.path),
                        step.evidence_ids,
                        category="transform",
                    )
                )
    return relations


def _runtime_and_dependency_facts(
    state: AnalysisState,
    workflow_run_jobs: WorkflowRunJobsEvidence | None = None,
) -> list[Relation]:
    relations: list[Relation] = []
    runtime_jobs = _runtime_job_matches(state, workflow_run_jobs)
    runner_jobs: dict[str, list[tuple[Workflow, Job]]] = defaultdict(list)
    for workflow in state.workflows:
        for job in workflow.jobs:
            runs_on = str(job.runs_on or "")
            if "self-hosted" in runs_on.lower():
                runner_id = stable_id("runner", runs_on.lower())
                runner_jobs[runner_id].append((workflow, job))
                runtime_job = runtime_jobs.get(job.job_id)
                runtime_self_hosted = bool(
                    _runtime_job_executed(runtime_job)
                    and any(label.casefold() == "self-hosted" for label in runtime_job.labels)
                )
                missing = ["runner isolation", "runner ephemerality"]
                if not runtime_self_hosted:
                    missing.append("runtime machine identity")
                relations.extend(
                    [
                        _relation(
                            job.job_id,
                            "uses_self_hosted_runner",
                            runner_id,
                            job.evidence_ids,
                            category="context",
                            verdict=TriState.TRUE if runtime_self_hosted else TriState.UNKNOWN,
                            missing_evidence=[] if runtime_self_hosted else missing,
                        ),
                        _relation(
                            runner_id,
                            "runner_state_may_reach_job",
                            job.job_id,
                            job.evidence_ids,
                            category="transform",
                            verdict=TriState.UNKNOWN,
                            missing_evidence=missing,
                        ),
                    ]
                )
            container = job.container
            if isinstance(container, dict):
                image = str(container.get("image", ""))
            else:
                image = str(container or "")
            if image and "@sha256:" not in image:
                relations.append(
                    _relation(
                        f"external:container:{image}",
                        "mutable_container_image_executes_in_job",
                        job.job_id,
                        job.evidence_ids,
                        category="execution",
                        verdict=TriState.UNKNOWN,
                        missing_evidence=["container image digest", "registry integrity"],
                    )
                )
            for step in job.steps:
                run = step.run or ""
                match = MUTABLE_INSTALL.search(run)
                if match:
                    dependency_id = stable_id("external-install", match.group(0).lower())
                    relations.append(
                        _relation(
                            dependency_id,
                            "mutable_install_source_executes_in_step",
                            step.step_id,
                            step.evidence_ids,
                            category="execution",
                            verdict=TriState.UNKNOWN,
                            missing_evidence=["downloaded dependency integrity"],
                        )
                    )
    for runner_id, entries in runner_jobs.items():
        attacker_jobs = [
            (workflow, job) for workflow, job in entries if "pull_request" in workflow.triggers
        ]
        privileged_jobs = [
            (workflow, job) for workflow, job in entries if _job_has_sensitive_authority(state, job)
        ]
        for source_workflow, source_job in attacker_jobs:
            source_runtime_job = runtime_jobs.get(source_job.job_id)
            source_executed = bool(
                _runtime_job_executed(source_runtime_job)
                and any(label.casefold() == "self-hosted" for label in source_runtime_job.labels)
            )
            relations.append(
                _relation(
                    "attacker:fork-pr",
                    "attacker_code_executes_on_self_hosted_runner",
                    runner_id,
                    source_workflow.evidence_ids + source_job.evidence_ids,
                    category="execution",
                    verdict=TriState.TRUE if source_executed else TriState.UNKNOWN,
                    missing_evidence=(
                        [] if source_executed else ["runner assignment and runtime execution"]
                    ),
                )
            )
            for target_workflow, target_job in privileged_jobs:
                if source_job.job_id == target_job.job_id:
                    continue
                target_runtime_job = runtime_jobs.get(target_job.job_id)
                identities_known = _runtime_job_executed(
                    source_runtime_job
                ) and _runtime_job_executed(target_runtime_job)
                same_runner = bool(
                    identities_known
                    and source_runtime_job is not None
                    and target_runtime_job is not None
                    and source_runtime_job.runner_id == target_runtime_job.runner_id
                )
                different_runner = bool(identities_known and not same_runner)
                relations.append(
                    _relation(
                        source_job.job_id,
                        "runner_process_or_file_state_may_reach_privileged_job",
                        target_job.job_id,
                        source_workflow.evidence_ids
                        + source_job.evidence_ids
                        + target_workflow.evidence_ids
                        + target_job.evidence_ids,
                        category="transform",
                        verdict=TriState.FALSE if different_runner else TriState.UNKNOWN,
                        missing_evidence=(
                            []
                            if different_runner
                            else ["runner cleanup", "runner ephemerality"]
                            if same_runner
                            else [
                                "runner identity match",
                                "runner cleanup",
                                "runner ephemerality",
                            ]
                        ),
                    )
                )
                if source_job.job_id.split(":job:")[0] == target_job.job_id.split(":job:")[0]:
                    relations.append(
                        _relation(
                            source_job.job_id,
                            "workspace_may_be_reused_by_privileged_job",
                            target_job.job_id,
                            source_job.evidence_ids + target_job.evidence_ids,
                            category="transform",
                            verdict=TriState.FALSE if different_runner else TriState.UNKNOWN,
                            missing_evidence=(
                                []
                                if different_runner
                                else ["workspace cleanup"]
                                if same_runner
                                else ["workspace cleanup and runner scheduling"]
                            ),
                        )
                    )
    return relations


def _deployment_branch_pattern_matches(pattern: str, branch: str) -> TriState:
    """Match GitHub deployment branch patterns without letting `*` cross `/`.

    GitHub documents Ruby File.fnmatch-style path matching for environment deployment policies.
    Character classes and escapes are left UNKNOWN here instead of approximated with Python's
    fnmatch semantics, whose `*` handling across `/` is too permissive for this use case.
    """

    if any(character in pattern for character in "[]\\"):
        return TriState.UNKNOWN
    translated: list[str] = []
    for character in pattern:
        if character == "*":
            translated.append("[^/]*")
        elif character == "?":
            translated.append("[^/]")
        else:
            translated.append(re.escape(character))
    return (
        TriState.TRUE if re.fullmatch("".join(translated), branch) is not None else TriState.FALSE
    )


def _environment_branch_pattern_facts(
    state: AnalysisState,
    environment_policies: dict[str, EnvironmentProtectionPolicy],
    environment_branch_patterns: dict[str, EnvironmentBranchPatternsEvidence],
) -> list[Relation]:
    workflows = {workflow.workflow_id: workflow for workflow in state.workflows}
    jobs = {job.job_id: job for workflow in state.workflows for job in workflow.jobs}
    triggers = {trigger.context_id: trigger for trigger in state.trigger_contexts}
    relations: list[Relation] = []
    for context in state.job_execution_contexts:
        job = jobs.get(context.job_id)
        trigger = triggers.get(context.trigger_context_id)
        workflow = workflows.get(context.workflow_id)
        if job is None or trigger is None or workflow is None:
            continue
        environment_name = concrete_environment_name(job)
        if environment_name is None:
            continue
        policy = environment_policies.get(environment_name)
        evidence = environment_branch_patterns.get(environment_name)
        if (
            policy is None
            or policy.deployment_branch_policy is not DeploymentBranchPolicyMode.CUSTOM
            or evidence is None
        ):
            continue
        guard_id = f"guard:environment-ref:{context.context_id}"
        if trigger.event_name != "push":
            relations.append(
                _relation(
                    context.context_id,
                    "environment_restricts_deployment_branches",
                    guard_id,
                    context.evidence_ids,
                    category="guard",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["runtime GITHUB_REF for environment deployment"],
                )
            )
            continue
        branches = exact_push_branches(workflow)
        if not branches:
            relations.append(
                _relation(
                    context.context_id,
                    "environment_restricts_deployment_branches",
                    guard_id,
                    context.evidence_ids,
                    category="guard",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["runtime GITHUB_REF for environment deployment"],
                )
            )
            continue

        branch_patterns = list(evidence.branch_patterns)
        unknown_target_patterns = list(evidence.unknown_target_patterns)
        if (
            evidence.patterns
            and not branch_patterns
            and not evidence.tag_patterns
            and not unknown_target_patterns
        ):
            # Old checkpoints did not preserve branch-vs-tag target type.
            unknown_target_patterns = list(evidence.patterns)

        unresolved_match = False
        allowed = False
        for branch in branches:
            for pattern in branch_patterns:
                match = _deployment_branch_pattern_matches(pattern, branch)
                if match is TriState.TRUE:
                    allowed = True
                    break
                if match is TriState.UNKNOWN:
                    unresolved_match = True
            if allowed:
                break
        if allowed:
            continue
        if unknown_target_patterns or unresolved_match:
            relations.append(
                _relation(
                    context.context_id,
                    "environment_restricts_deployment_branches",
                    guard_id,
                    context.evidence_ids,
                    category="guard",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["deployment branch pattern matching semantics"],
                )
            )
            continue
        relations.append(
            _relation(
                guard_id,
                "guard_blocks_untrusted_execution",
                context.context_id,
                context.evidence_ids,
                category="guard",
                verdict=TriState.TRUE,
            )
        )
    return relations


_BRANCH_RULES_DIRECT_PUSH_GATES = {
    "update",
    "required_status_checks",
    "required_deployments",
    "required_code_scanning",
}
_BRANCH_RULES_DIRECT_PUSH_NONBLOCKING = {
    "creation",
    "deletion",
    "non_fast_forward",
    "required_linear_history",
    "required_signatures",
    "commit_message_pattern",
    "commit_author_email_pattern",
    "committer_email_pattern",
    "branch_name_pattern",
}


def _branch_rule_blocking_reasons(evidence: EffectiveBranchRulesEvidence) -> list[str]:
    rule_types = set(evidence.active_rule_types)
    reasons: list[str] = []
    if evidence.update_restricted_to_bypass_actors or "update" in rule_types:
        reasons.append("repository-writer branch update bypass eligibility")
    if evidence.requires_pull_request or "pull_request" in rule_types:
        approvals = evidence.required_approving_review_count
        if approvals is None:
            reasons.append("pull-request review requirement details")
        elif approvals > 0 or evidence.require_last_push_approval is True:
            reasons.append("required pull-request approval outcome")
    if evidence.required_status_check_count is None and "required_status_checks" in rule_types:
        reasons.append("required status check configuration")
    elif (evidence.required_status_check_count or 0) > 0:
        reasons.append("required status check outcome")
    if rule_types & (_BRANCH_RULES_DIRECT_PUSH_GATES - {"update", "required_status_checks"}):
        reasons.append("active branch rule outcome")
    unknown_rule_types = (
        rule_types
        - _BRANCH_RULES_DIRECT_PUSH_GATES
        - {
            "pull_request",
            *_BRANCH_RULES_DIRECT_PUSH_NONBLOCKING,
        }
    )
    if unknown_rule_types:
        reasons.append("unmodeled active branch rule effect")
    if evidence.legacy_protection_present is TriState.TRUE:
        reasons.append("legacy branch protection push/merge restrictions")
    elif (
        evidence.protected is TriState.TRUE
        and not rule_types
        and evidence.legacy_protection_present is not TriState.FALSE
    ):
        reasons.append("protected branch enforcement details")
    elif evidence.protected is TriState.UNKNOWN and not rule_types:
        reasons.append("branch protection state")
    return sorted(set(reasons))


def _branch_rule_facts(
    state: AnalysisState,
    profile: AttackerProfile,
    branch_rules: dict[str, EffectiveBranchRulesEvidence],
) -> list[Relation]:
    if profile is not AttackerProfile.REPO_WRITER:
        return []
    workflows = {workflow.workflow_id: workflow for workflow in state.workflows}
    relations: list[Relation] = []
    for trigger in state.trigger_contexts:
        if trigger.event_name != "push" or trigger.attacker_can_trigger is TriState.FALSE:
            continue
        workflow = workflows.get(trigger.workflow_id)
        if workflow is None:
            continue
        branches = exact_push_branches(workflow)
        if not branches:
            continue
        missing_branches = [branch for branch in branches if branch not in branch_rules]
        if missing_branches:
            reasons = ["branch protection and effective branch rules for configured push branches"]
        else:
            reasons = []
            for branch in branches:
                evidence = branch_rules[branch]
                branch_reasons = _branch_rule_blocking_reasons(evidence)
                # One allowed branch is enough for this push-triggered workflow to remain reachable.
                if not branch_reasons:
                    reasons = []
                    break
                reasons.extend(branch_reasons)
        if not reasons:
            continue
        relations.append(
            _relation(
                trigger.context_id,
                "branch_rules_may_block_direct_push",
                f"guard:branch-rules:{trigger.context_id}",
                trigger.evidence_ids,
                category="guard",
                verdict=TriState.UNKNOWN,
                missing_evidence=sorted(set(reasons)),
                evidence_requirements=[
                    EvidenceRequirementRef(
                        kind=EvidenceRequirementKind.GITHUB_EFFECTIVE_BRANCH_RULES,
                        subject_id=state.repository.repository_id,
                        qualifier=branch,
                    )
                    for branch in branches
                ],
            )
        )
    return relations


def _secret_metadata_and_audit_facts(
    state: AnalysisState,
    *,
    secret_metadata: list[SecretMetadataEvidence],
    rotation_policy: SecretRotationPolicyEvidence | None,
    audit_events: list[AuditEventsEvidence],
) -> list[Relation]:
    relations: list[Relation] = []
    metadata_by_name: dict[str, list[tuple[SecretMetadataEvidence, object]]] = defaultdict(list)
    for payload in secret_metadata:
        for item in payload.secrets:
            metadata_by_name[item.name.casefold()].append((payload, item))

    for reference in state.secret_references:
        match = re.search(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", reference.expression, re.IGNORECASE)
        if match is None:
            continue
        name = match.group(1)
        secret_node = f"secret:{reference.reference_id}"
        matches = metadata_by_name.get(name.casefold(), [])
        if matches:
            relations.append(
                _relation(
                    secret_node,
                    "secret_metadata_observed",
                    f"secret-metadata:{name.casefold()}",
                    reference.evidence_ids,
                    category="credential",
                )
            )
        elif secret_metadata:
            relations.append(
                _relation(
                    secret_node,
                    "secret_metadata_scope_unresolved",
                    f"secret-metadata:{name.casefold()}",
                    reference.evidence_ids,
                    category="credential",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["environment secret metadata or inaccessible secret scope"],
                )
            )

        if rotation_policy is None:
            if matches:
                relations.append(
                    _relation(
                        secret_node,
                        "secret_rotation_policy_unknown",
                        f"secret-metadata:{name.casefold()}",
                        reference.evidence_ids,
                        category="guard",
                        verdict=TriState.UNKNOWN,
                        missing_evidence=["operator secret rotation policy"],
                    )
                )
            continue
        if len(matches) != 1:
            if matches:
                relations.append(
                    _relation(
                        secret_node,
                        "secret_rotation_age_exceeds_policy",
                        f"secret-metadata:{name.casefold()}",
                        reference.evidence_ids,
                        category="guard",
                        verdict=TriState.UNKNOWN,
                        missing_evidence=["exact secret scope binding"],
                    )
                )
            continue
        payload, item = matches[0]
        changed_at = item.updated_at or item.created_at
        if changed_at is None:
            verdict = TriState.UNKNOWN
            missing = ["secret creation or update timestamp"]
        else:
            age_days = max((payload.observed_at - changed_at).days, 0)
            verdict = (
                TriState.TRUE if age_days > rotation_policy.max_age_days else TriState.FALSE
            )
            missing = []
        relations.append(
            _relation(
                secret_node,
                "secret_rotation_age_exceeds_policy",
                f"secret-metadata:{name.casefold()}",
                reference.evidence_ids,
                category="guard",
                verdict=verdict,
                missing_evidence=missing,
            )
        )

    for payload in audit_events:
        audit_node = stable_id(
            "audit-window",
            payload.organization,
            payload.query,
            payload.coverage_start.isoformat(),
            payload.coverage_end.isoformat(),
        )
        relations.append(
            _relation(
                state.repository.repository_id,
                "audit_window_observed_for_repository",
                audit_node,
                [],
                category="context",
            )
        )
        for index, event in enumerate(payload.events):
            event_node = stable_id(
                "audit-event",
                audit_node,
                str(index),
                event.action,
                event.actor or "",
                event.created_at.isoformat() if event.created_at else "",
            )
            relations.append(
                _relation(
                    audit_node,
                    "audit_event_observed",
                    event_node,
                    [],
                    category="context",
                )
            )
    return relations


def derive_security_facts(
    state: AnalysisState,
    profile: AttackerProfile,
    *,
    environment_policies: dict[str, EnvironmentProtectionPolicy] | None = None,
    environment_branch_patterns: dict[str, EnvironmentBranchPatternsEvidence] | None = None,
    fork_policy: ForkPullRequestPolicyEvidence | None = None,
    actions_policy: ActionsPolicyEvidence | None = None,
    branch_rules: dict[str, EffectiveBranchRulesEvidence] | None = None,
    pending_deployment_state: PendingDeploymentStateEvidence | None = None,
    workflow_run_jobs: WorkflowRunJobsEvidence | None = None,
    runtime_telemetry: list[RuntimeTelemetryEvidence] | None = None,
    action_provenance: list[ActionProvenanceEvidence] | None = None,
    action_advisories: list[ActionAdvisoryEvidence] | None = None,
    secret_metadata: list[SecretMetadataEvidence] | None = None,
    rotation_policy: SecretRotationPolicyEvidence | None = None,
    audit_events: list[AuditEventsEvidence] | None = None,
) -> list[Relation]:
    """Derive reusable facts without emitting vulnerability Findings."""
    relations: list[Relation] = []
    for workflow in state.workflows:
        relations.extend(_containment_facts(workflow))
        relations.extend(_source_facts(state, workflow, profile))
        for job in workflow.jobs:
            relations.extend(_step_flow_facts(state, workflow, job, profile))
            relations.extend(derive_workspace_revision_relations(workflow, job, profile))
            relations.extend(_credential_facts(state, workflow, job))
            relations.extend(_secret_flow_facts(state, workflow, job))
            relations.extend(
                _guard_facts(
                    state,
                    state.repository.repository_id,
                    workflow,
                    job,
                    environment_policies or {},
                    environment_branch_patterns or {},
                    pending_deployment_state=pending_deployment_state,
                    fork_policy=fork_policy,
                )
            )
    relations.extend(
        _environment_branch_pattern_facts(
            state,
            environment_policies or {},
            environment_branch_patterns or {},
        )
    )
    relations.extend(_branch_rule_facts(state, profile, branch_rules or {}))
    relations.extend(_artifact_and_cache_facts(state))
    relations.extend(
        _supply_chain_facts(
            state,
            actions_policy=actions_policy,
            action_provenance=action_provenance,
            action_advisories=action_advisories,
        )
    )
    relations.extend(_reusable_workflow_facts(state))
    relations.extend(_runtime_and_dependency_facts(state, workflow_run_jobs))
    relations.extend(_runtime_telemetry_facts(state, runtime_telemetry or []))
    relations.extend(
        _secret_metadata_and_audit_facts(
            state,
            secret_metadata=secret_metadata or [],
            rotation_policy=rotation_policy,
            audit_events=audit_events or [],
        )
    )
    return _deduplicate(relations)
