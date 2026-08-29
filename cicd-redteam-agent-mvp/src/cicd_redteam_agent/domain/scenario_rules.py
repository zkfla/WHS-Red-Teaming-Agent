from __future__ import annotations

import re
from collections.abc import Iterable

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    AttackEdge,
    AttackPath,
    ConditionSecurityEffect,
    Confidence,
    Finding,
    FindingStatus,
    Job,
    PermissionLevel,
    Relation,
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


def _external_action_reference(uses: str | None) -> tuple[str, str] | None:
    if not uses or uses.startswith("./") or uses.startswith("docker://"):
        return None
    if "@" not in uses:
        return None
    action, ref = uses.rsplit("@", 1)
    if "/" not in action or FULL_COMMIT_SHA.fullmatch(ref):
        return None
    return action, ref


def _derive_mutable_actions(state: AnalysisState) -> tuple[list[Finding], list[AttackPath]]:
    """Aggregate mutable-action risk by workflow/root cause instead of per occurrence."""

    immutable_steps = {
        relation.target_id
        for relation in state.relations
        if relation.relation_type == "action_reference_is_immutable"
        and relation.verdict is TriState.TRUE
    }
    findings: list[Finding] = []
    paths: list[AttackPath] = []
    for workflow in state.workflows:
        grouped: dict[tuple[str, str], list[tuple[Job, Step, TriState]]] = {}
        for job in workflow.jobs:
            privilege = _privilege_verdict(job, state)
            if privilege is TriState.FALSE:
                continue
            for step in job.steps:
                if step.step_id in immutable_steps:
                    continue
                reference = _external_action_reference(step.uses)
                if reference is None:
                    continue
                action, mutable_ref = reference
                grouped.setdefault((action.casefold(), mutable_ref), []).append(
                    (job, step, privilege)
                )

        for (action_key, mutable_ref), occurrences in sorted(grouped.items()):
            action = (occurrences[0][1].uses or "").rsplit("@", 1)[0]
            evidence_ids = sorted(
                {
                    evidence_id
                    for job, step, _ in occurrences
                    for evidence_id in workflow.evidence_ids
                    + job.evidence_ids
                    + step.evidence_ids
                }
            )
            privilege_unknown = all(
                privilege is TriState.UNKNOWN for _, _, privilege in occurrences
            )
            finding_id = stable_id(
                "finding",
                "GHA-04-MUTABLE-EXTERNAL-ACTION",
                workflow.workflow_id,
                action_key,
                mutable_ref,
            )
            findings.append(
                Finding(
                    finding_id=finding_id,
                    rule_id="GHA-04-MUTABLE-EXTERNAL-ACTION",
                    title="External action is not pinned to a full commit SHA",
                    status=FindingStatus.CANDIDATE,
                    evidence_ids=evidence_ids,
                    missing_evidence=[
                        "external action ref integrity",
                        "upstream compromise",
                        *(
                            ["effective credential availability"]
                            if privilege_unknown
                            else []
                        ),
                    ],
                    confidence=Confidence.MEDIUM,
                    summary=(
                        f"{action}@{mutable_ref} is used {len(occurrences)} time(s) in this "
                        "workflow through the same mutable-reference root cause. This is a "
                        "supply-chain risk, not proof that the action is compromised. Review "
                        "the upstream provenance and prefer a reviewed full commit SHA."
                    ),
                )
            )
            for job, step, _ in occurrences:
                occurrence_evidence = sorted(
                    set(workflow.evidence_ids + job.evidence_ids + step.evidence_ids)
                )
                paths.append(
                    AttackPath(
                        path_id=stable_id("path", finding_id, step.step_id),
                        nodes=[workflow.workflow_id, job.job_id, step.step_id],
                        edges=_edges_for_step(workflow, job, step),
                        status=FindingStatus.CANDIDATE,
                        evidence_ids=occurrence_evidence,
                        missing_evidence=findings[-1].missing_evidence,
                    )
                )
    return findings, paths


def _edge_from_relation(relation: Relation) -> AttackEdge:
    return AttackEdge(
        relation_id=relation.relation_id,
        source_id=relation.source_id,
        relation_type=relation.relation_type,
        target_id=relation.target_id,
        evidence_ids=relation.evidence_ids,
    )


def _object_indexes(
    state: AnalysisState,
) -> tuple[dict[str, Workflow], dict[str, Job], dict[str, Step]]:
    workflow_by_id = {workflow.workflow_id: workflow for workflow in state.workflows}
    job_by_id: dict[str, Job] = {}
    step_by_id: dict[str, Step] = {}
    for workflow in state.workflows:
        for job in workflow.jobs:
            job_by_id[job.job_id] = job
            for step in job.steps:
                step_by_id[step.step_id] = step
    return workflow_by_id, job_by_id, step_by_id


def _workflow_for_job(state: AnalysisState, job_id: str) -> Workflow | None:
    return next(
        (
            workflow
            for workflow in state.workflows
            if any(job.job_id == job_id for job in workflow.jobs)
        ),
        None,
    )


def _job_for_step(state: AnalysisState, step_id: str) -> tuple[Workflow, Job] | None:
    for workflow in state.workflows:
        for job in workflow.jobs:
            if any(step.step_id == step_id for step in job.steps):
                return workflow, job
    return None


def _derive_secret_log_findings(state: AnalysisState) -> tuple[list[Finding], list[AttackPath]]:
    """Promote only complete secret->environment->log relations into Findings."""

    upstream_by_env: dict[str, list[Relation]] = {}
    for relation in state.relations:
        if (
            relation.relation_type == "secret_flows_to_env"
            and relation.verdict is not TriState.FALSE
        ):
            upstream_by_env.setdefault(relation.target_id, []).append(relation)

    findings: list[Finding] = []
    paths: list[AttackPath] = []
    for sink in state.relations:
        if sink.relation_type != "secret_may_reach_log" or sink.verdict is TriState.FALSE:
            continue
        owner = _job_for_step(state, sink.target_id)
        if owner is None:
            continue
        workflow, job = owner
        for upstream in upstream_by_env.get(sink.source_id, []):
            status = (
                FindingStatus.SUPPORTED
                if upstream.verdict is TriState.TRUE and sink.verdict is TriState.TRUE
                else FindingStatus.CANDIDATE
            )
            missing = sorted(set(upstream.missing_evidence + sink.missing_evidence))
            evidence_ids = sorted(set(upstream.evidence_ids + sink.evidence_ids))
            finding_id = stable_id(
                "finding",
                "GHA-07-SECRET-MAY-REACH-LOG",
                workflow.workflow_id,
                job.job_id,
                sink.target_id,
                upstream.source_id,
            )
            findings.append(
                Finding(
                    finding_id=finding_id,
                    rule_id="GHA-07-SECRET-MAY-REACH-LOG",
                    title="Secret-derived value may be emitted to a workflow log",
                    status=status,
                    evidence_ids=evidence_ids,
                    missing_evidence=missing,
                    confidence=(
                        Confidence.HIGH if status is FindingStatus.SUPPORTED else Confidence.MEDIUM
                    ),
                    summary=(
                        "A secret reference deterministically flows into an environment value "
                        "that is consumed by a log-producing shell command. This finding proves "
                        "the secret-derived value reaches the log sink; it does not claim that "
                        "GitHub masking failed or that plaintext disclosure was observed."
                    ),
                )
            )
            paths.append(
                AttackPath(
                    path_id=stable_id("path", finding_id),
                    nodes=[upstream.source_id, upstream.target_id, sink.target_id],
                    edges=[_edge_from_relation(upstream), _edge_from_relation(sink)],
                    status=status,
                    evidence_ids=evidence_ids,
                    missing_evidence=missing,
                )
            )
    return findings, paths


def _derive_self_hosted_persistence_findings(
    state: AnalysisState,
) -> tuple[list[Finding], list[AttackPath]]:
    """Promote a bounded untrusted-runner-state->privileged-job chain without inventing cleanup."""

    uses_runner = {
        relation.source_id: relation
        for relation in state.relations
        if relation.relation_type == "uses_self_hosted_runner"
        and relation.verdict is not TriState.FALSE
    }
    attacker_by_runner = {
        relation.target_id: relation
        for relation in state.relations
        if relation.relation_type == "attacker_code_executes_on_self_hosted_runner"
        and relation.verdict is not TriState.FALSE
    }
    runner_state_to_job = {
        relation.target_id: relation
        for relation in state.relations
        if relation.relation_type == "runner_state_may_reach_job"
        and relation.verdict is not TriState.FALSE
    }

    findings: list[Finding] = []
    paths: list[AttackPath] = []
    for persistence in state.relations:
        if (
            persistence.relation_type
            != "runner_process_or_file_state_may_reach_privileged_job"
            or persistence.verdict is TriState.FALSE
        ):
            continue
        source_runner = uses_runner.get(persistence.source_id)
        source_bridge = runner_state_to_job.get(persistence.source_id)
        if source_runner is None or source_bridge is None:
            continue
        attacker = attacker_by_runner.get(source_runner.target_id)
        if attacker is None or attacker.target_id != source_bridge.source_id:
            continue
        source_workflow = _workflow_for_job(state, persistence.source_id)
        target_workflow = _workflow_for_job(state, persistence.target_id)
        if source_workflow is None or target_workflow is None:
            continue
        relations = [attacker, source_bridge, persistence]
        supported = all(relation.verdict is TriState.TRUE for relation in relations)
        status = FindingStatus.SUPPORTED if supported else FindingStatus.CANDIDATE
        missing = sorted(
            {
                item
                for relation in relations
                for item in relation.missing_evidence
            }
        )
        evidence_ids = sorted(
            {
                item
                for relation in relations
                for item in relation.evidence_ids
            }
        )
        finding_id = stable_id(
            "finding",
            "GHA-09-SELF-HOSTED-RUNNER-PERSISTENCE",
            persistence.source_id,
            persistence.target_id,
        )
        findings.append(
            Finding(
                finding_id=finding_id,
                rule_id="GHA-09-SELF-HOSTED-RUNNER-PERSISTENCE",
                title="Untrusted runner state may persist into a privileged self-hosted job",
                status=status,
                evidence_ids=evidence_ids,
                missing_evidence=missing,
                confidence=Confidence.HIGH if supported else Confidence.MEDIUM,
                summary=(
                    "Fork-PR-controlled execution is linked to a self-hosted runner and the "
                    "same analysis chain may carry process or file state into a later privileged "
                    "job. A matching runner identity alone is not treated as persistence proof; "
                    "cleanup and ephemerality uncertainty remains explicit."
                ),
            )
        )
        paths.append(
            AttackPath(
                path_id=stable_id("path", finding_id),
                nodes=[
                    attacker.source_id,
                    attacker.target_id,
                    persistence.source_id,
                    persistence.target_id,
                ],
                edges=[
                    _edge_from_relation(attacker),
                    _edge_from_relation(source_bridge),
                    _edge_from_relation(persistence),
                ],
                status=status,
                evidence_ids=evidence_ids,
                missing_evidence=missing,
            )
        )
    return findings, paths


def _derive_revision_toctou_findings(
    state: AnalysisState,
) -> tuple[list[Finding], list[AttackPath]]:
    """Find approval/revision TOCTOU without requiring an unrelated privilege sink.

    The claim here is deliberately narrow: a trust-requiring approval is not proven to bind the
    PR revision that is subsequently checked out. Separate execution/credential relations describe
    the eventual impact when repository content from that revision is actually executed.
    """

    selector_by_revision = {
        relation.target_id: relation
        for relation in state.relations
        if relation.relation_type == "step_selects_workspace_revision"
        and relation.verdict is not TriState.FALSE
    }
    executions_by_revision: dict[str, list[Relation]] = {}
    for relation in state.relations:
        if (
            relation.relation_type == "workspace_revision_reaches_execution"
            and relation.verdict is not TriState.FALSE
        ):
            executions_by_revision.setdefault(relation.source_id, []).append(relation)

    findings: list[Finding] = []
    paths: list[AttackPath] = []
    for selection in state.relations:
        if (
            selection.relation_type != "attacker_selects_current_pull_request_head"
            or selection.verdict is TriState.FALSE
        ):
            continue
        selector = selector_by_revision.get(selection.target_id)
        if selector is None:
            continue
        owner = _job_for_step(state, selector.source_id)
        if owner is None:
            continue
        workflow, job = owner
        selection_step = next(
            (step for step in job.steps if step.step_id == selector.source_id),
            None,
        )
        if selection_step is None:
            continue
        executions = [
            relation
            for relation in executions_by_revision.get(selection.target_id, [])
            if _job_for_step(state, relation.target_id) == (workflow, job)
        ]
        target_ids = {job.job_id, selection_step.step_id}
        target_ids.update(relation.target_id for relation in executions)
        evaluations = [
            item
            for item in state.condition_evaluations
            if item.target_id in target_ids
            and item.security_effect is ConditionSecurityEffect.TRUST_REQUIRED
            and item.revision_bound is not TriState.TRUE
        ]
        if not evaluations:
            continue

        revision_explicitly_unbound = any(
            item.revision_bound is TriState.FALSE for item in evaluations
        )
        supported = selection.verdict is TriState.TRUE and revision_explicitly_unbound
        status = FindingStatus.SUPPORTED if supported else FindingStatus.CANDIDATE
        missing = {
            missing
            for evaluation in evaluations
            for missing in evaluation.security_missing_evidence + evaluation.missing_evidence
        }
        if not revision_explicitly_unbound:
            missing.add("exact approval-to-execution revision binding")
        for execution in executions:
            missing.update(execution.missing_evidence)
        evidence_ids = sorted(
            {
                evidence_id
                for evidence_id in selection.evidence_ids
                + selector.evidence_ids
                + workflow.evidence_ids
                + job.evidence_ids
                + selection_step.evidence_ids
                + [
                    evidence_id
                    for execution in executions
                    for evidence_id in execution.evidence_ids
                ]
            }
        )
        finding_id = stable_id(
            "finding",
            "GHA-10-PR-REVISION-TOCTOU",
            workflow.workflow_id,
            job.job_id,
            selection.target_id,
        )
        findings.append(
            Finding(
                finding_id=finding_id,
                rule_id="GHA-10-PR-REVISION-TOCTOU",
                title="Trusted approval is not proven to bind the PR revision that is checked out",
                status=status,
                evidence_ids=evidence_ids,
                missing_evidence=sorted(missing),
                confidence=Confidence.HIGH if supported else Confidence.MEDIUM,
                summary=(
                    "A trust-requiring condition gates the job, but a later checkout resolves "
                    "the current mutable pull-request head instead of an approval-bound revision. "
                    "This proves a revision-binding gap, not by itself code execution or "
                    "credential impact; those remain separate relations."
                ),
            )
        )
        if executions:
            for execution in executions:
                path_evidence = sorted(
                    set(selection.evidence_ids + execution.evidence_ids)
                )
                paths.append(
                    AttackPath(
                        path_id=stable_id("path", finding_id, execution.target_id),
                        nodes=[selection.source_id, selection.target_id, execution.target_id],
                        edges=[_edge_from_relation(selection), _edge_from_relation(execution)],
                        status=status,
                        evidence_ids=path_evidence,
                        missing_evidence=sorted(missing),
                    )
                )
        else:
            paths.append(
                AttackPath(
                    path_id=stable_id("path", finding_id, selector.source_id),
                    nodes=[selection.source_id, selection.target_id],
                    edges=[_edge_from_relation(selection)],
                    status=status,
                    evidence_ids=sorted(set(selection.evidence_ids + selector.evidence_ids)),
                    missing_evidence=sorted(missing),
                )
            )
    return findings, paths


def _derive_known_action_advisory_findings(
    state: AnalysisState,
) -> tuple[list[Finding], list[AttackPath]]:
    findings: list[Finding] = []
    paths: list[AttackPath] = []
    for relation in state.relations:
        if (
            relation.relation_type != "known_action_advisory_affects_reference"
            or relation.verdict is not TriState.TRUE
        ):
            continue
        owner = _job_for_step(state, relation.target_id)
        if owner is None:
            continue
        workflow, job = owner
        step = next(
            (candidate for candidate in job.steps if candidate.step_id == relation.target_id),
            None,
        )
        if step is None:
            continue
        evidence_ids = sorted(set(workflow.evidence_ids + job.evidence_ids + step.evidence_ids))
        finding_id = stable_id(
            "finding",
            "GHA-11-KNOWN-ACTION-ADVISORY",
            workflow.workflow_id,
            relation.source_id,
            step.step_id,
        )
        findings.append(
            Finding(
                finding_id=finding_id,
                rule_id="GHA-11-KNOWN-ACTION-ADVISORY",
                title="Referenced GitHub Action version is affected by a known advisory",
                status=FindingStatus.SUPPORTED,
                evidence_ids=evidence_ids,
                missing_evidence=[],
                confidence=Confidence.HIGH,
                summary=(
                    "A read-only GitHub Advisory observation matched the referenced Action "
                    "package and requested version/ref. This is distinct from the generic risk "
                    "of using a mutable tag."
                ),
            )
        )
        paths.append(
            AttackPath(
                path_id=stable_id("path", finding_id),
                nodes=[relation.source_id, step.step_id],
                edges=[_edge_from_relation(relation)],
                status=FindingStatus.SUPPORTED,
                evidence_ids=evidence_ids,
                missing_evidence=[],
            )
        )
    return findings, paths


def _derive_stale_secret_metadata_findings(
    state: AnalysisState,
) -> tuple[list[Finding], list[AttackPath]]:
    references = {f"secret:{item.reference_id}": item for item in state.secret_references}
    findings: list[Finding] = []
    paths: list[AttackPath] = []
    for relation in state.relations:
        if (
            relation.relation_type != "secret_rotation_age_exceeds_policy"
            or relation.verdict is not TriState.TRUE
        ):
            continue
        reference = references.get(relation.source_id)
        if reference is None:
            continue
        workflow = next(
            (item for item in state.workflows if item.workflow_id == reference.workflow_id),
            None,
        )
        if workflow is None:
            continue
        job = next((item for item in workflow.jobs if item.job_id == reference.job_id), None)
        if job is None:
            continue
        step = next(
            (item for item in job.steps if item.step_id == reference.step_id),
            None,
        ) if reference.step_id else None
        evidence_ids = sorted(
            set(reference.evidence_ids + job.evidence_ids + (step.evidence_ids if step else []))
        )
        finding_id = stable_id(
            "finding",
            "GHA-12-SECRET-ROTATION-AGE",
            reference.reference_id,
        )
        findings.append(
            Finding(
                finding_id=finding_id,
                rule_id="GHA-12-SECRET-ROTATION-AGE",
                title="Referenced GitHub Actions secret exceeds the configured rotation age",
                status=FindingStatus.SUPPORTED,
                evidence_ids=evidence_ids,
                missing_evidence=[],
                confidence=Confidence.HIGH,
                summary=(
                    "GitHub secret metadata shows that the uniquely bound referenced secret has "
                    "not been created or updated within the operator-configured maximum age. "
                    "No secret value was read or stored."
                ),
            )
        )
        nodes = [relation.source_id, relation.target_id]
        paths.append(
            AttackPath(
                path_id=stable_id("path", finding_id),
                nodes=nodes,
                edges=[_edge_from_relation(relation)],
                status=FindingStatus.SUPPORTED,
                evidence_ids=evidence_ids,
                missing_evidence=[],
            )
        )
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
        _derive_secret_log_findings,
        _derive_self_hosted_persistence_findings,
        _derive_revision_toctou_findings,
        _derive_known_action_advisory_findings,
        _derive_stale_secret_metadata_findings,
        _derive_broad_token_permissions,
    ):
        new_findings, new_paths = derive(state)
        findings.extend(new_findings)
        paths.extend(new_paths)
    return findings, paths
