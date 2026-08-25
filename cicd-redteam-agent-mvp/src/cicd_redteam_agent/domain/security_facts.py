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
    r"(?:name|body|tag_name)|inputs\.[A-Za-z_][\w-]*)|"
    r"inputs\.[A-Za-z_][\w-]*"
    r")\s*}}",
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


def _env_referenced(run: str, name: str) -> bool:
    patterns = (
        rf"\${re.escape(name)}\b",
        rf"\$\{{{re.escape(name)}\}}",
        rf"\$env:{re.escape(name)}\b",
        rf"%{re.escape(name)}%",
    )
    return any(re.search(pattern, run, re.IGNORECASE) for pattern in patterns)


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
    return sorted({match.group(1) for match in INPUT_EXPRESSION.finditer(str(value))})


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
        values: list[object] = [job.condition or "", job.with_]
        for step in job.steps:
            values.extend([step.run or "", step.env, step.with_])
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
    relations: list[Relation] = []
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

    for step in job.steps:
        run = step.run or ""
        interpreter = bool(COMMAND_INTERPRETER.search(run))
        external_send = bool(EXTERNAL_SEND.search(run))
        for expression in _expressions(run):
            relation_type = (
                "untrusted_value_interpreted_as_command"
                if interpreter
                else "untrusted_input_reaches_execution"
            )
            relations.append(
                _relation(
                    _input_node(expression),
                    relation_type,
                    step.step_id,
                    step.evidence_ids,
                    category="execution",
                )
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
                elif external_send:
                    relation_type = "env_value_reaches_external_send"
                else:
                    relation_type = "env_value_reaches_execution"
                relations.append(
                    _relation(
                        env_node,
                        relation_type,
                        step.step_id,
                        step.evidence_ids,
                        category="execution",
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


def _guard_facts(workflow: Workflow, job: Job) -> list[Relation]:
    relations: list[Relation] = []
    if job.condition:
        relations.append(
            _relation(
                job.job_id,
                "execution_condition_present",
                f"guard:condition:{job.job_id}",
                job.evidence_ids,
                category="guard",
                verdict=TriState.UNKNOWN,
                missing_evidence=["condition effectiveness"],
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
            relations.append(
                _relation(
                    step.step_id,
                    "validates_value_with_allowlist",
                    f"guard:allowlist:{step.step_id}",
                    step.evidence_ids,
                    category="guard",
                )
            )
    return relations


def _artifact_and_cache_facts(state: AnalysisState) -> list[Relation]:
    relations: list[Relation] = []
    artifacts: dict[str, list[tuple[Workflow, Job, Step, str]]] = defaultdict(list)
    downloads: list[tuple[Workflow, Job, Step, str, str, bool]] = []
    caches: dict[str, list[tuple[Workflow, Job, Step]]] = defaultdict(list)
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
                    caches[key].append((workflow, job, step))
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
        for step in job.steps:
            if step.step_id == download.step_id or not step.run:
                continue
            path_used = normalized_path in {"", "."} or normalized_path in step.run
            if path_used and RUNTIME_EXECUTION.search(step.run):
                relations.append(
                    _relation(
                        f"artifact:{name}",
                        "artifact_content_reaches_execution",
                        step.step_id,
                        download.evidence_ids + step.evidence_ids,
                        category="execution",
                        verdict=verdict,
                        missing_evidence=missing,
                    )
                )
    return relations


def _supply_chain_facts(state: AnalysisState) -> list[Relation]:
    relations: list[Relation] = []
    for relation in state.relations:
        if relation.relation_type != "uses_external_action_ref":
            continue
        relations.append(
            _relation(
                relation.target_id,
                "external_code_may_execute_in_step",
                relation.source_id,
                relation.evidence_ids,
                category="execution",
                verdict=TriState.UNKNOWN,
                missing_evidence=["external dependency integrity", "upstream compromise"],
            )
        )
    return relations


def _runtime_and_dependency_facts(state: AnalysisState) -> list[Relation]:
    relations: list[Relation] = []
    for workflow in state.workflows:
        for job in workflow.jobs:
            runs_on = str(job.runs_on or "")
            if "self-hosted" in runs_on.lower():
                runner_id = stable_id("runner", runs_on.lower())
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
            relations.extend(_guard_facts(workflow, job))
    relations.extend(_artifact_and_cache_facts(state))
    relations.extend(_supply_chain_facts(state))
    relations.extend(_runtime_and_dependency_facts(state))
    return _deduplicate(relations)
