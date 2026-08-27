from __future__ import annotations

import re
from collections import defaultdict

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    AttackerProfile,
    Job,
    Relation,
    Step,
    TriState,
    Workflow,
)

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
WEAK_ACTOR_GUARD = re.compile(
    r"github\.(?:actor|triggering_actor)|github\.event\.(?:comment|issue|pull_request)",
    re.IGNORECASE,
)
LABEL_OR_COMMENT_CONTROL = re.compile(
    r"(?:label|comment\.body|issue\.body|issue\.title)", re.IGNORECASE
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
) -> Relation:
    return Relation(
        source_id=source_id,
        relation_type=relation_type,
        target_id=target_id,
        category=category,
        verdict=verdict,
        evidence_ids=sorted(set(evidence_ids)),
        missing_evidence=sorted(set(missing_evidence or [])),
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
    permissions = job.permissions_effective
    if permissions == "write-all":
        return True
    if isinstance(permissions, dict) and any(
        name != "__default__" and str(value).lower() == "write"
        for name, value in permissions.items()
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
    member_ids = job_ids | {
        step.step_id for job in workflow.jobs for step in job.steps
    }
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


def _step_flow_facts(workflow: Workflow, job: Job) -> list[Relation]:
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
            if interpreter:
                relation_type = "untrusted_value_interpreted_as_command"
                category = "execution"
            elif output_producer:
                relation_type = "untrusted_input_reaches_output_producer"
                category = "transform"
            else:
                relation_type = "untrusted_input_reaches_execution"
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
            relations.append(
                _relation(
                    _input_node(expression),
                    relation_type,
                    step.step_id,
                    step.evidence_ids,
                    category=category,
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
            source = outputs.get((output_key.lower(), output_name.lower()))
            if source is None and output_key.lower() in keyed_steps:
                source = _output_node(keyed_steps[output_key.lower()], output_name)
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
        controlled_nodes = [
            _input_node(expression) for expression in _expressions(run)
        ] + [
            _env_node(step, name)
            for name in step.env
            if _env_referenced(run, name)
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


def _credential_facts(state: AnalysisState, workflow: Workflow, job: Job) -> list[Relation]:
    relations: list[Relation] = []
    permissions = job.permissions_effective
    write_permissions: list[str] = []
    permission_unknown = False
    if permissions == "write-all":
        write_permissions = ["write-all"]
    elif isinstance(permissions, dict):
        write_permissions = [
            name for name, value in permissions.items() if str(value).lower() == "write"
        ]
        permission_unknown = permissions.get("__default__") == "UNKNOWN"
    subjects: list[Step | Job] = list(job.steps) if job.steps else [job]
    for subject in subjects:
        for permission in write_permissions:
            relations.append(
                _relation(
                    subject.step_id if isinstance(subject, Step) else subject.job_id,
                    f"can_use_github_token_{permission}_write",
                    f"credential:github-token:{job.job_id}:{permission}",
                    job.evidence_ids + subject.evidence_ids,
                    category="credential",
                )
            )
        if permission_unknown:
            relations.append(
                _relation(
                    subject.step_id if isinstance(subject, Step) else subject.job_id,
                    "github_token_permission_unknown",
                    f"credential:github-token:{job.job_id}:unknown",
                    job.evidence_ids + subject.evidence_ids,
                    category="credential",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["repository default GITHUB_TOKEN permissions"],
                )
            )
        if isinstance(permissions, dict) and permissions.get("id-token") == "write":
            subject_id = subject.step_id if isinstance(subject, Step) else subject.job_id
            relations.append(
                _relation(
                    subject_id,
                    "can_request_oidc_token",
                    f"credential:oidc:{job.job_id}",
                    job.evidence_ids + subject.evidence_ids,
                    category="credential",
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
                relations.append(
                    _relation(
                        f"credential:oidc:{job.job_id}",
                        "oidc_may_assume_cloud_identity",
                        stable_id("cloud-identity", provider, cloud_target),
                        job.evidence_ids + subject.evidence_ids,
                        category="credential",
                        verdict=TriState.UNKNOWN,
                        missing_evidence=[f"{provider} federated identity trust policy"],
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


def _secret_flow_facts(
    state: AnalysisState, workflow: Workflow, job: Job
) -> list[Relation]:
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
            if re.search(
                r"(?<![\w-])(?:echo|printf|write-output)\b", run, re.IGNORECASE
            ):
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
                    (
                        "secret_passed_to_external_action"
                        if external
                        else "secret_passed_to_action"
                    ),
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


def _guard_facts(workflow: Workflow, job: Job) -> list[Relation]:
    relations: list[Relation] = []
    if job.condition:
        condition_guard = f"guard:condition:{job.job_id}"
        for expression in _expressions(job.condition):
            verdict, missing = _source_verdict(workflow, expression)
            relations.append(
                _relation(
                    _input_node(expression),
                    "untrusted_input_reaches_condition",
                    condition_guard,
                    job.evidence_ids,
                    category="transform",
                    verdict=verdict,
                    missing_evidence=missing,
                )
            )
        relations.append(
            _relation(
                job.job_id,
                "execution_condition_present",
                condition_guard,
                job.evidence_ids,
                category="guard",
                verdict=TriState.UNKNOWN,
                missing_evidence=["condition effectiveness"],
            )
        )
        if WEAK_ACTOR_GUARD.search(job.condition):
            relations.append(
                _relation(
                    f"guard:condition:{job.job_id}",
                    "actor_or_event_guard_effectiveness_unknown",
                    job.job_id,
                    job.evidence_ids,
                    category="guard",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["actor authorization and condition bypass analysis"],
                )
            )
        if LABEL_OR_COMMENT_CONTROL.search(job.condition):
            relations.append(
                _relation(
                    workflow.workflow_id,
                    "label_or_comment_controls_job_condition",
                    job.job_id,
                    workflow.evidence_ids + job.evidence_ids,
                    category="source",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["label/comment author authorization"],
                )
            )
    if job.environment is not None:
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
    for step in job.steps:
        run = step.run or ""
        if step.condition:
            guard_id = f"guard:condition:{step.step_id}"
            relations.append(
                _relation(
                    step.step_id,
                    "execution_condition_present",
                    guard_id,
                    step.evidence_ids,
                    category="guard",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["condition effectiveness"],
                )
            )
            if WEAK_ACTOR_GUARD.search(step.condition):
                relations.append(
                    _relation(
                        guard_id,
                        "actor_or_event_guard_effectiveness_unknown",
                        step.step_id,
                        step.evidence_ids,
                        category="guard",
                        verdict=TriState.UNKNOWN,
                        missing_evidence=[
                            "actor authorization and condition bypass analysis"
                        ],
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
                    if "pull_request" in workflow.triggers:
                        relations.append(
                            _relation(
                                "attacker:fork-pr",
                                "attacker_may_populate_cache",
                                cache_id,
                                workflow.evidence_ids + job.evidence_ids + step.evidence_ids,
                                category="source",
                                verdict=verdict,
                                missing_evidence=missing,
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
                        missing_evidence=(
                            ["runtime cache key value"] if "${{" in key else []
                        ),
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
                        missing_evidence=(
                            ["runtime cache key value"] if "${{" in key else []
                        ),
                    )
                )
    return relations


def _supply_chain_facts(state: AnalysisState) -> list[Relation]:
    relations: list[Relation] = []
    for relation in state.relations:
        if relation.relation_type not in {
            "uses_external_action_ref",
            "uses_external_reusable_workflow",
        }:
            continue
        target = relation.target_id.removeprefix("external:")
        ref = target.rsplit("@", 1)[-1] if "@" in target else ""
        if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
            continue
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
                verdict=TriState.UNKNOWN,
                missing_evidence=["external dependency integrity", "upstream compromise"],
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
            if called:
                caller_permissions = job.permissions_effective
                for called_job in called.jobs:
                    called_permissions = called_job.permissions_effective
                    caller_write = {
                        name
                        for name, value in (
                            caller_permissions.items()
                            if isinstance(caller_permissions, dict)
                            else []
                        )
                        if str(value).lower() == "write"
                    }
                    called_write = {
                        name
                        for name, value in (
                            called_permissions.items()
                            if isinstance(called_permissions, dict)
                            else []
                        )
                        if str(value).lower() == "write"
                    }
                    if called_permissions == "write-all" or not called_write.issubset(caller_write):
                        relations.append(
                            _relation(
                                job.job_id,
                                "reusable_workflow_permission_boundary_unknown",
                                called_job.job_id,
                                job.evidence_ids + called_job.evidence_ids,
                                category="credential",
                                verdict=TriState.UNKNOWN,
                                missing_evidence=[
                                    "effective reusable workflow permission intersection"
                                ],
                            )
                        )
    return relations


def _runtime_and_dependency_facts(state: AnalysisState) -> list[Relation]:
    relations: list[Relation] = []
    runner_jobs: dict[str, list[tuple[Workflow, Job]]] = defaultdict(list)
    for workflow in state.workflows:
        for job in workflow.jobs:
            runs_on = str(job.runs_on or "")
            if "self-hosted" in runs_on.lower():
                runner_id = stable_id("runner", runs_on.lower())
                runner_jobs[runner_id].append((workflow, job))
                missing = ["runner isolation", "runner ephemerality", "runtime machine identity"]
                relations.extend(
                    [
                        _relation(
                            job.job_id,
                            "uses_self_hosted_runner",
                            runner_id,
                            job.evidence_ids,
                            category="context",
                            verdict=TriState.UNKNOWN,
                            missing_evidence=missing,
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
            (workflow, job)
            for workflow, job in entries
            if "pull_request" in workflow.triggers
        ]
        privileged_jobs = [
            (workflow, job)
            for workflow, job in entries
            if _job_has_sensitive_authority(state, job)
        ]
        for source_workflow, source_job in attacker_jobs:
            relations.append(
                _relation(
                    "attacker:fork-pr",
                    "attacker_code_executes_on_self_hosted_runner",
                    runner_id,
                    source_workflow.evidence_ids + source_job.evidence_ids,
                    category="execution",
                    verdict=TriState.UNKNOWN,
                    missing_evidence=["runner assignment and runtime execution"],
                )
            )
            for target_workflow, target_job in privileged_jobs:
                if source_job.job_id == target_job.job_id:
                    continue
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
                        verdict=TriState.UNKNOWN,
                        missing_evidence=[
                            "runner identity match",
                            "runner cleanup",
                            "runner ephemerality",
                        ],
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
                            verdict=TriState.UNKNOWN,
                            missing_evidence=["workspace cleanup and runner scheduling"],
                        )
                    )
    return relations


def derive_security_facts(
    state: AnalysisState, profile: AttackerProfile
) -> list[Relation]:
    """Derive reusable facts without emitting vulnerability Findings."""
    relations: list[Relation] = []
    for workflow in state.workflows:
        relations.extend(_containment_facts(workflow))
        relations.extend(_source_facts(state, workflow, profile))
        for job in workflow.jobs:
            relations.extend(_step_flow_facts(workflow, job))
            relations.extend(_credential_facts(state, workflow, job))
            relations.extend(_secret_flow_facts(state, workflow, job))
            relations.extend(_guard_facts(workflow, job))
    relations.extend(_artifact_and_cache_facts(state))
    relations.extend(_supply_chain_facts(state))
    relations.extend(_reusable_workflow_facts(state))
    relations.extend(_runtime_and_dependency_facts(state))
    return _deduplicate(relations)
