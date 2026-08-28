from __future__ import annotations

import re
from collections.abc import Iterable

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    AttackEdge,
    AttackPath,
    Confidence,
    Finding,
    FindingStatus,
    Job,
    PermissionLevel,
    Step,
    TriState,
    Workflow,
)

UNTRUSTED_PR_EXPRESSION = re.compile(
    r"\$\{\{\s*(github\.(?:head_ref|event\.pull_request\."
    r"(?:title|body|head\.ref)))\s*}}",
    re.IGNORECASE,
)
FULL_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40}")
EXECUTED_ARTIFACT = re.compile(
    r"(?<![\w])(?:\./|(?:bash|sh|python|python3|pwsh|node)\s+)"
    r"([\w./-]+\.(?:sh|py|ps1|js|ts))"
)


def _privilege_verdict(job: Job, state: AnalysisState) -> TriState:
    if any(reference.job_id == job.job_id for reference in state.secret_references):
        return TriState.TRUE
    contexts = [item for item in state.job_execution_contexts if item.job_id == job.job_id]
    sensitive = {"actions", "contents", "deployments", "id-token", "packages", "pull-requests"}
    levels = [
        context.effective_permissions.get(scope)
        for context in contexts
        for scope in sensitive
    ]
    if any(level is PermissionLevel.WRITE for level in levels):
        return TriState.TRUE
    if any(level is PermissionLevel.UNKNOWN for level in levels):
        return TriState.UNKNOWN
    return TriState.FALSE


def _edges_for_step(workflow: Workflow, job: Job, step: Step) -> list[AttackEdge]:
    return [
        AttackEdge(
            source_id=workflow.workflow_id,
            relation_type="contains_job",
            target_id=job.job_id,
            evidence_ids=sorted(set(workflow.evidence_ids + job.evidence_ids)),
        ),
        AttackEdge(
            source_id=job.job_id,
            relation_type="contains_step",
            target_id=step.step_id,
            evidence_ids=sorted(set(job.evidence_ids + step.evidence_ids)),
        ),
    ]


def _finding_and_path(
    *,
    workflow: Workflow,
    job: Job,
    step: Step,
    rule_id: str,
    title: str,
    summary: str,
    status: FindingStatus,
    confidence: Confidence,
    missing_evidence: list[str] | None = None,
) -> tuple[Finding, AttackPath]:
    evidence_ids = sorted(
        set(workflow.evidence_ids + job.evidence_ids + step.evidence_ids)
    )
    missing = missing_evidence or []
    finding_id = stable_id("finding", rule_id, workflow.workflow_id, job.job_id, step.step_id)
    finding = Finding(
        finding_id=finding_id,
        rule_id=rule_id,
        title=title,
        status=status,
        evidence_ids=evidence_ids,
        missing_evidence=missing,
        confidence=confidence,
        summary=summary,
    )
    path = AttackPath(
        path_id=stable_id("path", finding_id),
        nodes=[workflow.workflow_id, job.job_id, step.step_id],
        edges=_edges_for_step(workflow, job, step),
        status=status,
        evidence_ids=evidence_ids,
        missing_evidence=missing,
    )
    return finding, path


def _derive_shell_injection(state: AnalysisState) -> tuple[list[Finding], list[AttackPath]]:
    findings: list[Finding] = []
    paths: list[AttackPath] = []
    for workflow in state.workflows:
        if not set(workflow.triggers) & {"pull_request", "pull_request_target"}:
            continue
        for job in workflow.jobs:
            for step in job.steps:
                if not step.run:
                    continue
                matches = sorted(
                    {
                        match.group(1)
                        for match in UNTRUSTED_PR_EXPRESSION.finditer(step.run)
                    }
                )
                if not matches:
                    continue
                finding, path = _finding_and_path(
                    workflow=workflow,
                    job=job,
                    step=step,
                    rule_id="GHA-01-PR-SHELL-INJECTION",
                    title="Untrusted pull-request input is interpolated into a shell command",
                    summary=(
                        "The run script directly interpolates attacker-controlled pull-request "
                        f"context: {', '.join(matches)}. Passing the value through an environment "
                        "variable prevents GitHub expression text from becoming shell syntax."
                    ),
                    status=FindingStatus.SUPPORTED,
                    confidence=Confidence.HIGH,
                )
                findings.append(finding)
                paths.append(path)
    return findings, paths


def _external_action_ref(uses: str | None) -> str | None:
    if not uses or uses.startswith("./") or uses.startswith("docker://"):
        return None
    if "@" not in uses:
        return None
    action, ref = uses.rsplit("@", 1)
    if "/" not in action or FULL_COMMIT_SHA.fullmatch(ref):
        return None
    return ref


def _derive_mutable_actions(state: AnalysisState) -> tuple[list[Finding], list[AttackPath]]:
    findings: list[Finding] = []
    paths: list[AttackPath] = []
    for workflow in state.workflows:
        for job in workflow.jobs:
            for step in job.steps:
                mutable_ref = _external_action_ref(step.uses)
                if mutable_ref is None:
                    continue
                privilege = _privilege_verdict(job, state)
                if privilege is TriState.FALSE:
                    continue
                finding, path = _finding_and_path(
                    workflow=workflow,
                    job=job,
                    step=step,
                    rule_id="GHA-04-MUTABLE-EXTERNAL-ACTION",
                    title="External action is not pinned to a full commit SHA",
                    summary=(
                        f"{step.uses} uses mutable ref {mutable_ref!r}. This is a supply-chain "
                        "risk, not proof that the action is compromised. Pin a reviewed full "
                        "commit SHA."
                    ),
                    status=FindingStatus.CANDIDATE,
                    confidence=Confidence.MEDIUM,
                    missing_evidence=[
                        "external action ref integrity",
                        "upstream compromise",
                        *(
                            ["effective credential availability"]
                            if privilege is TriState.UNKNOWN
                            else []
                        ),
                    ],
                )
                findings.append(finding)
                paths.append(path)
    return findings, paths


def _derive_broad_token_permissions(
    state: AnalysisState,
) -> tuple[list[Finding], list[AttackPath]]:
    assessments = {assessment.subject_id: assessment for assessment in state.assessments}
    findings: list[Finding] = []
    paths: list[AttackPath] = []
    for workflow in state.workflows:
        for job in workflow.jobs:
            declared = (
                job.permissions_declared
                if job.permissions_declared is not None
                else workflow.permissions
            )
            if declared != "write-all":
                continue
            controlled_steps = [
                step
                for step in job.steps
                if assessments.get(step.step_id)
                and assessments[step.step_id].verdict is TriState.TRUE
            ]
            step = (
                controlled_steps[0]
                if controlled_steps
                else (job.steps[0] if job.steps else None)
            )
            if step is None:
                continue
            privilege = _privilege_verdict(job, state)
            supported = bool(controlled_steps) and privilege is TriState.TRUE
            finding, path = _finding_and_path(
                workflow=workflow,
                job=job,
                step=step,
                rule_id="GHA-05-BROAD-GITHUB-TOKEN",
                title="Job grants write-all GITHUB_TOKEN permissions",
                summary=(
                    "The job grants every available GITHUB_TOKEN write permission. "
                    + (
                        "Repository-controlled execution is also linked to this job."
                        if supported
                        else (
                            "No attacker-controlled execution path was proven, so this "
                            "remains a candidate."
                        )
                    )
                ),
                status=FindingStatus.SUPPORTED if supported else FindingStatus.CANDIDATE,
                confidence=Confidence.HIGH if supported else Confidence.MEDIUM,
                missing_evidence=(
                    []
                    if supported
                    else [
                        "effective event-scoped GITHUB_TOKEN permissions"
                        if privilege is TriState.UNKNOWN
                        else "workflow permission requirements"
                    ]
                ),
            )
            findings.append(finding)
            paths.append(path)
    return findings, paths


def _steps_using(job: Job, prefix: str) -> Iterable[Step]:
    return (step for step in job.steps if step.uses and step.uses.startswith(prefix))


def _workflow_run_names(workflow: Workflow) -> set[str]:
    config = workflow.triggers.get("workflow_run")
    if not isinstance(config, dict):
        return set()
    names = config.get("workflows") or []
    if isinstance(names, str):
        return {names}
    return {str(name) for name in names}


def _derive_artifact_execution(state: AnalysisState) -> tuple[list[Finding], list[AttackPath]]:
    producers: dict[str, list[tuple[Workflow, Job, Step]]] = {}
    for workflow in state.workflows:
        if "pull_request" not in workflow.triggers or not workflow.name:
            continue
        uploads = [
            (workflow, job, step)
            for job in workflow.jobs
            for step in _steps_using(job, "actions/upload-artifact@")
        ]
        if uploads:
            producers[workflow.name] = uploads

    findings: list[Finding] = []
    paths: list[AttackPath] = []
    for consumer in state.workflows:
        producer_names = _workflow_run_names(consumer)
        if not producer_names:
            continue
        for job in consumer.jobs:
            privilege = _privilege_verdict(job, state)
            if privilege is TriState.FALSE:
                continue
            downloads = list(_steps_using(job, "actions/download-artifact@"))
            for download in downloads:
                run_id = str(download.with_.get("run-id", ""))
                if "github.event.workflow_run.id" not in run_id:
                    continue
                download_name = str(download.with_.get("name", ""))
                download_path = str(download.with_.get("path", ".")).lstrip("./")
                for execute in job.steps:
                    if execute.step_id == download.step_id or not execute.run:
                        continue
                    executed = [
                        match.group(1).lstrip("./")
                        for match in EXECUTED_ARTIFACT.finditer(execute.run)
                    ]
                    if not executed or (
                        download_path not in {"", "."}
                        and not any(
                            path.startswith(download_path.rstrip("/") + "/")
                            for path in executed
                        )
                    ):
                        continue
                    for producer_name in sorted(producer_names & producers.keys()):
                        for producer, producer_job, upload in producers[producer_name]:
                            upload_name = str(upload.with_.get("name", ""))
                            if not download_name or not upload_name or download_name != upload_name:
                                continue
                            evidence_ids = sorted(
                                set(
                                    producer.evidence_ids
                                    + producer_job.evidence_ids
                                    + upload.evidence_ids
                                    + consumer.evidence_ids
                                    + job.evidence_ids
                                    + download.evidence_ids
                                    + execute.evidence_ids
                                )
                            )
                            finding_id = stable_id(
                                "finding",
                                "GHA-03-WORKFLOW-RUN-ARTIFACT-EXECUTION",
                                producer.workflow_id,
                                consumer.workflow_id,
                                execute.step_id,
                            )
                            finding_status = (
                                FindingStatus.SUPPORTED
                                if privilege is TriState.TRUE
                                else FindingStatus.CANDIDATE
                            )
                            privilege_missing = (
                                []
                                if privilege is TriState.TRUE
                                else ["effective workflow_run consumer credential permissions"]
                            )
                            findings.append(
                                Finding(
                                    finding_id=finding_id,
                                    rule_id="GHA-03-WORKFLOW-RUN-ARTIFACT-EXECUTION",
                                    title=(
                                        "Privileged workflow executes an artifact produced by "
                                        "a PR workflow"
                                    ),
                                    status=finding_status,
                                    evidence_ids=evidence_ids,
                                    missing_evidence=privilege_missing,
                                    confidence=(
                                        Confidence.HIGH
                                        if privilege is TriState.TRUE
                                        else Confidence.MEDIUM
                                    ),
                                    summary=(
                                        f"PR workflow {producer_name!r} uploads artifact "
                                        f"{upload_name!r}; a privileged workflow_run job downloads "
                                        "that run's artifact and executes a file from its "
                                        "download path."
                                    ),
                                )
                            )
                            nodes = [
                                producer.workflow_id,
                                upload.step_id,
                                consumer.workflow_id,
                                download.step_id,
                                execute.step_id,
                            ]
                            paths.append(
                                AttackPath(
                                    path_id=stable_id("path", finding_id),
                                    nodes=nodes,
                                    edges=[
                                        AttackEdge(
                                            source_id=producer.workflow_id,
                                            relation_type="uploads_pr_artifact",
                                            target_id=upload.step_id,
                                            evidence_ids=upload.evidence_ids,
                                        ),
                                        AttackEdge(
                                            source_id=upload.step_id,
                                            relation_type="triggers_privileged_workflow_run",
                                            target_id=consumer.workflow_id,
                                            evidence_ids=sorted(
                                                set(upload.evidence_ids + consumer.evidence_ids)
                                            ),
                                        ),
                                        AttackEdge(
                                            source_id=consumer.workflow_id,
                                            relation_type="downloads_artifact",
                                            target_id=download.step_id,
                                            evidence_ids=download.evidence_ids,
                                        ),
                                        AttackEdge(
                                            source_id=download.step_id,
                                            relation_type="artifact_file_executed_by",
                                            target_id=execute.step_id,
                                            evidence_ids=sorted(
                                                set(download.evidence_ids + execute.evidence_ids)
                                            ),
                                        ),
                                    ],
                                    status=finding_status,
                                    evidence_ids=evidence_ids,
                                    missing_evidence=privilege_missing,
                                )
                            )
    return findings, paths


def derive_additional_scenario_results(
    state: AnalysisState,
) -> tuple[list[Finding], list[AttackPath]]:
    findings: list[Finding] = []
    paths: list[AttackPath] = []
    for derive in (
        _derive_shell_injection,
        _derive_artifact_execution,
        _derive_mutable_actions,
        _derive_broad_token_permissions,
    ):
        new_findings, new_paths = derive(state)
        findings.extend(new_findings)
        paths.extend(new_paths)
    return findings, paths
