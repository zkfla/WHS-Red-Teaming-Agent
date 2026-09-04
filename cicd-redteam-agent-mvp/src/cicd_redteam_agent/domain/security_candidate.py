from __future__ import annotations

import json
from collections.abc import Iterable

from cicd_redteam_agent.domain.attacker_control import derive_attack_paths
from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.models import (
    AnalysisProposal,
    AnalysisState,
    AttackPath,
    CandidateDecisionDraft,
    CandidateDisposition,
    CandidateKind,
    ClaimContract,
    CoverageLedgerEntry,
    Finding,
    FindingStatus,
    FrontierDecisionDraft,
    SecurityCandidate,
    TriState,
)
from cicd_redteam_agent.domain.relation_semantics import relation_semantics
from cicd_redteam_agent.domain.scenario_rules import derive_additional_scenario_results

_RULE_TO_KIND: dict[str, CandidateKind] = {
    "CICD-UNTRUSTED-PRIVILEGED-EXECUTION": CandidateKind.UNTRUSTED_PRIVILEGED_EXECUTION,
    "GHA-01-PR-SHELL-INJECTION": CandidateKind.SHELL_INJECTION,
    "GHA-03-WORKFLOW-RUN-ARTIFACT-EXECUTION": CandidateKind.ARTIFACT_EXECUTION,
    "GHA-04-MUTABLE-EXTERNAL-ACTION": CandidateKind.MUTABLE_ACTION,
    "GHA-05-BROAD-GITHUB-TOKEN": CandidateKind.BROAD_TOKEN_PERMISSION,
    "GHA-07-SECRET-MAY-REACH-LOG": CandidateKind.SECRET_LOG,
    "GHA-09-SELF-HOSTED-RUNNER-PERSISTENCE": CandidateKind.SELF_HOSTED_PERSISTENCE,
    "GHA-10-PR-REVISION-TOCTOU": CandidateKind.REVISION_TOCTOU,
    "GHA-11-KNOWN-ACTION-ADVISORY": CandidateKind.KNOWN_ACTION_ADVISORY,
    "GHA-12-SECRET-ROTATION-AGE": CandidateKind.SECRET_ROTATION,
    "CICD-BUILD-NETWORK-SIDE-EFFECT": CandidateKind.BUILD_NETWORK_SIDE_EFFECT,
    "CICD-BUILD-WORKSPACE-MUTATION": CandidateKind.BUILD_WORKSPACE_MUTATION,
    "CICD-RUNTIME-NETWORK-SIDE-EFFECT": CandidateKind.RUNTIME_NETWORK_SIDE_EFFECT,
    "CICD-RUNTIME-WORKSPACE-MUTATION": CandidateKind.RUNTIME_WORKSPACE_MUTATION,
}

_RULE_TO_ROOT_CAUSE_FAMILY: dict[str, str] = {
    "CICD-UNTRUSTED-PRIVILEGED-EXECUTION": "untrusted-privileged-execution",
    "GHA-01-PR-SHELL-INJECTION": "shell-injection",
    "GHA-03-WORKFLOW-RUN-ARTIFACT-EXECUTION": "artifact-trust",
    "GHA-04-MUTABLE-EXTERNAL-ACTION": "mutable-action-reference",
    "GHA-05-BROAD-GITHUB-TOKEN": "broad-token-permission",
    "GHA-07-SECRET-MAY-REACH-LOG": "secret-log-flow",
    "GHA-09-SELF-HOSTED-RUNNER-PERSISTENCE": "self-hosted-state-persistence",
    "GHA-10-PR-REVISION-TOCTOU": "revision-approval-binding",
    "GHA-11-KNOWN-ACTION-ADVISORY": "known-action-advisory",
    "GHA-12-SECRET-ROTATION-AGE": "secret-rotation-age",
    "CICD-BUILD-NETWORK-SIDE-EFFECT": "build-network-side-effect",
    "CICD-BUILD-WORKSPACE-MUTATION": "build-workspace-mutation",
    "CICD-RUNTIME-NETWORK-SIDE-EFFECT": "runtime-network-side-effect",
    "CICD-RUNTIME-WORKSPACE-MUTATION": "runtime-workspace-mutation",
}

_STRUCTURAL_RELATION_TYPES = frozenset({"contains_job", "contains_step"})
_REFERENCE_RELATION_TYPES = frozenset(
    {
        "executes_repository_file",
        "uses_external_action_ref",
        "uses_external_action_sha",
        "uses_external_reusable_workflow",
    }
)
_MUTABLE_ACTION_RELATION_TYPES = frozenset(
    {
        "uses_external_action_ref",
        "external_code_may_execute_in_step",
        "action_reference_is_immutable",
        "action_ref_resolves_to_commit",
        "known_action_advisory_affects_reference",
    }
)

# A claim contract describes the semantic shape of the claim. It is deliberately narrower than
# the supporting Relation set: provenance, policy, permission, and guard Relations can support or
# qualify a candidate without all becoming mandatory parts of the claim itself.
_CLAIM_REQUIRED_RELATION_TYPES: dict[str, tuple[str, ...]] = {
    "CICD-UNTRUSTED-PRIVILEGED-EXECUTION": (
        "controls_repository_file",
        "executes_repository_file",
        "repository_file_executes_in",
    ),
    "GHA-01-PR-SHELL-INJECTION": (
        "untrusted_value_interpreted_as_command",
        "attacker_may_control_external_action_output",
        "output_value_interpolated_into_shell_source",
    ),
    "GHA-03-WORKFLOW-RUN-ARTIFACT-EXECUTION": (
        "uploads_artifact",
        "workflow_run_may_trigger",
        "downloads_artifact_into_step",
        "artifact_content_reaches_import",
        "repository_file_imported_or_loaded_by",
    ),
    "GHA-04-MUTABLE-EXTERNAL-ACTION": (
        "uses_external_action_ref",
        "external_code_may_execute_in_step",
    ),
    "GHA-05-BROAD-GITHUB-TOKEN": ("grants_effective_permission",),
    "GHA-07-SECRET-MAY-REACH-LOG": (
        "secret_flows_to_env",
        "secret_may_reach_log",
    ),
    "GHA-09-SELF-HOSTED-RUNNER-PERSISTENCE": (
        "attacker_code_executes_on_self_hosted_runner",
        "runner_state_may_reach_job",
        "runner_process_or_file_state_may_reach_privileged_job",
    ),
    "GHA-10-PR-REVISION-TOCTOU": (
        "attacker_selects_current_pull_request_head",
        "step_selects_workspace_revision",
        "workspace_revision_reaches_execution",
    ),
    "GHA-11-KNOWN-ACTION-ADVISORY": ("known_action_advisory_affects_reference",),
    "GHA-12-SECRET-ROTATION-AGE": ("secret_rotation_age_exceeds_policy",),
    "CICD-BUILD-NETWORK-SIDE-EFFECT": (
        "step_runs_package_install",
        "package_manifest_declares_local_dependency",
        "dependency_install_executes_lifecycle_script",
        "script_performs_external_network_request",
    ),
    "CICD-BUILD-WORKSPACE-MUTATION": (
        "step_runs_package_install",
        "package_manifest_declares_local_dependency",
        "dependency_install_executes_lifecycle_script",
        "script_modifies_workspace_file",
    ),
    "CICD-RUNTIME-NETWORK-SIDE-EFFECT": (
        "runtime_event_exactly_bound_to_step",
        "runtime_process_made_external_network_call",
    ),
    "CICD-RUNTIME-WORKSPACE-MUTATION": (
        "runtime_event_exactly_bound_to_step",
        "runtime_process_modified_workspace_file",
    ),
}

_FORBIDDEN_BY_KIND: dict[CandidateKind, tuple[str, ...]] = {
    CandidateKind.SECRET_LOG: (
        "plaintext secret leaked",
        "secret leaked",
        "masking failed",
        "masking failure",
    ),
    CandidateKind.REVISION_TOCTOU: (
        "repository compromise",
        "remote code execution",
        "rce",
    ),
    CandidateKind.MUTABLE_ACTION: (
        "action compromised",
        "compromise confirmed",
        "code execution confirmed",
    ),
    CandidateKind.SELF_HOSTED_PERSISTENCE: (
        "persistence confirmed",
        "runner compromised",
    ),
    CandidateKind.UNTRUSTED_PRIVILEGED_EXECUTION: (
        "repository compromise",
        "remote code execution",
        "rce",
    ),
    CandidateKind.BUILD_NETWORK_SIDE_EFFECT: (
        "credential exfiltration confirmed",
        "data exfiltration confirmed",
        "network request observed",
    ),
    CandidateKind.BUILD_WORKSPACE_MUTATION: (
        "artifact tampering confirmed",
        "source tampering observed",
        "runtime modification observed",
    ),
    CandidateKind.RUNTIME_NETWORK_SIDE_EFFECT: (
        "credential exfiltration confirmed",
        "data exfiltration confirmed",
        "malicious intent confirmed",
    ),
    CandidateKind.RUNTIME_WORKSPACE_MUTATION: (
        "artifact tampering confirmed",
        "malicious modification confirmed",
        "production compromise confirmed",
    ),
}

_PRIVILEGED_RELATION_PREFIX = "can_use_github_token_"


def _path_matches_finding(finding: Finding, paths: list[AttackPath]) -> list[AttackPath]:
    exact = [
        path
        for path in paths
        if path.path_id == stable_id("path", finding.finding_id)
    ]
    if exact:
        return exact

    # Scenario composers use ``stable_id("path", finding_id, occurrence_id)`` when one
    # Finding has multiple occurrence paths (notably mutable actions). Recover that exact
    # relationship before falling back to evidence-only matching, which can merge distinct
    # action/ref instances that happen to share the same evidence.
    occurrence_paths = [
        path
        for path in paths
        if any(
            path.path_id == stable_id("path", finding.finding_id, node)
            for node in path.nodes
        )
    ]
    if occurrence_paths:
        return occurrence_paths[:16]

    finding_evidence = set(finding.evidence_ids)
    matched = [
        path
        for path in paths
        if path.status is finding.status
        and set(path.missing_evidence) == set(finding.missing_evidence)
        and set(path.evidence_ids) <= finding_evidence
    ]
    if matched:
        return matched[:16]

    matched = [
        path
        for path in paths
        if path.status is finding.status
        and set(path.missing_evidence) == set(finding.missing_evidence)
    ]
    return matched[:16]


def _common_node(paths: list[AttackPath], index: int) -> str | None:
    values = {path.nodes[index] for path in paths if len(path.nodes) > index}
    return next(iter(values)) if len(values) == 1 else None


def _real_relation_ids(state: AnalysisState, paths: Iterable[AttackPath]) -> list[str]:
    known_relation_ids = {relation.relation_id for relation in state.relations}
    return list(
        dict.fromkeys(
            edge.relation_id
            for path in paths
            for edge in path.edges
            if edge.relation_type not in _STRUCTURAL_RELATION_TYPES
            and edge.relation_id in known_relation_ids
        )
    )


def _scoped_relation_ids(state: AnalysisState, paths: Iterable[AttackPath]) -> list[str]:
    nodes = {node for path in paths for node in path.nodes}
    allowed_categories = {"source", "transform", "execution", "credential", "guard"}
    return [
        relation.relation_id
        for relation in state.relations
        if relation.category in allowed_categories
        and relation.relation_type not in _STRUCTURAL_RELATION_TYPES
        and (
            (relation.source_id in nodes and relation.target_id in nodes)
            or (
                relation.category == "guard"
                and (relation.source_id in nodes or relation.target_id in nodes)
            )
        )
    ]


def _claim_anchor_relation_ids(
    state: AnalysisState,
    paths: Iterable[AttackPath],
    rule_id: str,
) -> list[str]:
    """Add claim-specific semantic Relations whose intermediate node is outside the path."""

    nodes = {node for path in paths for node in path.nodes}
    required_types = set(_CLAIM_REQUIRED_RELATION_TYPES.get(rule_id, ()))
    if rule_id in {
        "CICD-UNTRUSTED-PRIVILEGED-EXECUTION",
        "GHA-05-BROAD-GITHUB-TOKEN",
    }:
        required_types.update(
            relation.relation_type
            for relation in state.relations
            if _is_explicit_privileged_relation_type(relation.relation_type)
            and (relation.source_id in nodes or relation.target_id in nodes)
        )
    return [
        relation.relation_id
        for relation in state.relations
        if relation.relation_type in required_types
        and (relation.source_id in nodes or relation.target_id in nodes)
    ]


def _path_step_ids(state: AnalysisState, paths: Iterable[AttackPath]) -> set[str]:
    path_nodes = {node for path in paths for node in path.nodes}
    return {
        step.step_id
        for workflow in state.workflows
        for job in workflow.jobs
        for step in job.steps
        if step.step_id in path_nodes
    }


def _mutable_action_relation_ids(
    state: AnalysisState,
    paths: Iterable[AttackPath],
) -> list[str]:
    """Return action/ref Relations for the exact candidate occurrence paths."""

    step_ids = _path_step_ids(state, paths)
    references = [
        relation
        for relation in state.relations
        if relation.relation_type == "uses_external_action_ref"
        and relation.source_id in step_ids
    ]
    action_targets = {relation.target_id for relation in references}
    return [
        relation.relation_id
        for relation in state.relations
        if (
            (
                relation.relation_type == "uses_external_action_ref"
                and relation.source_id in step_ids
            )
            or (
                relation.relation_type
                in {
                    "external_code_may_execute_in_step",
                    "action_reference_is_immutable",
                    "known_action_advisory_affects_reference",
                }
                and relation.target_id in step_ids
                and relation.source_id in action_targets
            )
            or (
                relation.relation_type == "action_ref_resolves_to_commit"
                and relation.source_id in action_targets
            )
        )
    ]


def _is_explicit_privileged_relation_type(relation_type: str) -> bool:
    return (
        relation_type.startswith(_PRIVILEGED_RELATION_PREFIX)
        and relation_type.endswith("_write")
        and relation_type != "can_use_github_token_id-token_write"
    ) or relation_type in {"can_request_oidc_token", "can_access_secret_reference"}


def _required_relation_types(
    rule_id: str,
    relation_types: Iterable[str],
) -> list[str]:
    observed = list(dict.fromkeys(relation_types))
    observed_set = set(observed)
    required = [
        relation_type
        for relation_type in _CLAIM_REQUIRED_RELATION_TYPES.get(rule_id, ())
        if relation_type in observed_set
    ]
    if rule_id in {
        "CICD-UNTRUSTED-PRIVILEGED-EXECUTION",
        "GHA-05-BROAD-GITHUB-TOKEN",
    }:
        required.extend(
            relation_type
            for relation_type in observed
            if _is_explicit_privileged_relation_type(relation_type)
        )
    if required:
        return list(dict.fromkeys(required))

    # Keep an unknown/future rule grounded in typed semantic Relations without turning every
    # supporting Relation (especially context plumbing) into a mandatory claim component.
    return [
        relation_type
        for relation_type in observed
        if relation_type in _REFERENCE_RELATION_TYPES
        or (
            relation_semantics(relation_type).source_tag is not None
            or relation_semantics(relation_type).mechanism_tags
            or relation_semantics(relation_type).sink_tag is not None
        )
    ]


def _canonical_action_reference(target_id: str) -> str:
    target = target_id.removeprefix("external:")
    if "@" not in target:
        return target.casefold()
    action, ref = target.rsplit("@", 1)
    return f"{action.casefold()}@{ref}"


def _mutable_action_root_cause_key(
    family: str,
    finding: Finding,
    state: AnalysisState,
    relation_ids: Iterable[str],
) -> str:
    relation_id_set = set(relation_ids)
    references = sorted(
        {
            _canonical_action_reference(relation.target_id)
            for relation in state.relations
            if relation.relation_id in relation_id_set
            and relation.relation_type == "uses_external_action_ref"
        }
    )
    if len(references) == 1:
        return f"{family}:{references[0]}"
    if references:
        # Do not collapse an ambiguous/malformed occurrence into the family-level key.
        return f"{family}:ambiguous:{stable_id('root-cause', *references)}"
    return f"{family}:occurrence:{finding.finding_id}"


def _requirements(state: AnalysisState, relation_ids: Iterable[str]):
    relation_by_id = {relation.relation_id: relation for relation in state.relations}
    seen: set[str] = set()
    requirements = []
    for relation_id in relation_ids:
        relation = relation_by_id.get(relation_id)
        if relation is None:
            continue
        for requirement in relation.evidence_requirements:
            key = json.dumps(requirement.model_dump(mode="json"), sort_keys=True)
            if key not in seen:
                seen.add(key)
                requirements.append(requirement)
    return requirements


def _candidate_scope_context(state: AnalysisState, workflow_id: str | None, job_id: str | None):
    contexts = [
        context
        for context in state.job_execution_contexts
        if context.workflow_id == workflow_id and context.job_id == job_id
    ]
    return contexts[0].context_id if len(contexts) == 1 else None


def _candidate_from_pair(
    state: AnalysisState,
    finding: Finding,
    paths: list[AttackPath],
) -> SecurityCandidate:
    variants = _path_matches_finding(finding, paths)
    kind = _RULE_TO_KIND.get(
        finding.rule_id,
        CandidateKind.UNTRUSTED_PRIVILEGED_EXECUTION,
    )
    candidate_specific_relation_ids = (
        [
            *_claim_anchor_relation_ids(state, variants, finding.rule_id),
            *(
                _mutable_action_relation_ids(state, variants)
                if kind is CandidateKind.MUTABLE_ACTION
                else []
            ),
        ]
    )
    relation_ids = list(
        dict.fromkeys(
            [
                *_real_relation_ids(state, variants),
                *_scoped_relation_ids(state, variants),
                *candidate_specific_relation_ids,
            ]
        )
    )
    relation_by_id = {relation.relation_id: relation for relation in state.relations}
    supporting = [
        relation_id
        for relation_id in relation_ids
        if relation_id not in relation_by_id
        or relation_by_id[relation_id].verdict is not TriState.FALSE
    ]
    contradicting = [
        relation_id
        for relation_id in relation_ids
        if relation_id in relation_by_id
        and relation_by_id[relation_id].verdict is TriState.FALSE
    ]
    guards = [
        relation_id
        for relation_id in relation_ids
        if relation_id in relation_by_id
        and (
            relation_by_id[relation_id].category == "guard"
        or "guard" in relation_by_id[relation_id].relation_type
        )
    ]
    root_cause_family = _RULE_TO_ROOT_CAUSE_FAMILY.get(
        finding.rule_id,
        finding.rule_id.casefold(),
    )
    root_cause_key = (
        _mutable_action_root_cause_key(
            root_cause_family,
            finding,
            state,
            relation_ids,
        )
        if kind is CandidateKind.MUTABLE_ACTION
        else f"{root_cause_family}:{finding.finding_id}"
    )
    status = finding.status
    if status is FindingStatus.CONFIRMED:
        status = FindingStatus.SUPPORTED
    ceiling = (
        FindingStatus.SUPPORTED
        if status is FindingStatus.SUPPORTED
        else status
    )
    observed_relation_types = [
        relation_by_id[relation_id].relation_type
        for relation_id in relation_ids
        if relation_id in relation_by_id
    ]
    required_relation_types = _required_relation_types(
        finding.rule_id,
        observed_relation_types,
    )
    required_privileged = [
        relation_type
        for relation_type in required_relation_types
        if _is_explicit_privileged_relation_type(relation_type)
    ]
    contract = ClaimContract(
        contract_id=stable_id("claim", finding.rule_id),
        rule_id=finding.rule_id,
        title=finding.title,
        bounded_summary=finding.summary,
        proposal_status_ceiling=ceiling,
        required_relation_types=required_relation_types,
        required_privileged_relation_types=required_privileged,
        forbidden_claims=list(_FORBIDDEN_BY_KIND.get(kind, ())),
    )
    candidate_relations = [
        relation for relation_id in relation_ids if (relation := relation_by_id.get(relation_id))
    ]
    evidence_ids = list(
        dict.fromkeys(
            [
                *finding.evidence_ids,
                *[
                    evidence_id
                    for path in variants
                    for evidence_id in path.evidence_ids
                ],
                *[
                    evidence_id
                    for relation in candidate_relations
                    for evidence_id in relation.evidence_ids
                ],
            ]
        )
    )
    missing = list(
        dict.fromkeys(
            [
                *finding.missing_evidence,
                *[
                    missing_evidence
                    for path in variants
                    for missing_evidence in path.missing_evidence
                ],
                *[
                    missing_evidence
                    for relation in candidate_relations
                    for missing_evidence in relation.missing_evidence
                ],
            ]
        )
    )
    workflow_ids = {
        workflow.workflow_id
        for workflow in state.workflows
        if any(workflow.workflow_id in path.nodes for path in variants)
    }
    job_ids = {
        job.job_id
        for workflow in state.workflows
        for job in workflow.jobs
        if any(job.job_id in path.nodes for path in variants)
    }
    step_ids = {
        step.step_id
        for workflow in state.workflows
        for job in workflow.jobs
        for step in job.steps
        if any(step.step_id in path.nodes for path in variants)
    }
    workflow_id = next(iter(workflow_ids)) if len(workflow_ids) == 1 else None
    job_id = next(iter(job_ids)) if len(job_ids) == 1 else None
    step_id = next(iter(step_ids)) if len(step_ids) == 1 else None
    return SecurityCandidate(
        candidate_id=stable_id("candidate", finding.rule_id, finding.finding_id),
        source_finding_id=finding.finding_id,
        candidate_kind=kind,
        root_cause_family=root_cause_family,
        root_cause_key=root_cause_key,
        workflow_id=workflow_id,
        job_id=job_id,
        step_id=step_id,
        context_id=_candidate_scope_context(state, workflow_id, job_id),
        claim_contract=contract,
        proposal_status_ceiling=ceiling,
        status=status,
        confidence=finding.confidence,
        disposition=(
            CandidateDisposition.REJECTED
            if status is FindingStatus.REJECTED
            else CandidateDisposition.UNREVIEWED
        ),
        disposition_reason=(
            "deterministic semantic relation is FALSE"
            if status is FindingStatus.REJECTED
            else None
        ),
        supporting_relation_ids=supporting,
        contradicting_relation_ids=contradicting,
        guards=guards,
        evidence_ids=evidence_ids,
        missing_evidence=missing,
        evidence_requirements=_requirements(state, relation_ids),
        path_variants=[path.model_copy(deep=True) for path in variants],
    )


def derive_security_candidates(state: AnalysisState) -> list[SecurityCandidate]:
    """Derive the one canonical semantic candidate set used by both modes.

    Existing deterministic scenario composers remain the semantic source of truth in this
    migration slice. Their Findings/AttackPaths are immediately normalized into candidates;
    callers choose whether to materialize those candidates for presentation.
    """

    base_findings, base_paths = derive_attack_paths(state)
    additional_findings, additional_paths = derive_additional_scenario_results(state)
    findings = [*base_findings, *additional_findings]
    paths = [*base_paths, *additional_paths]
    candidates = [
        _candidate_from_pair(state, finding, paths)
        for finding in findings
    ]
    return sorted(candidates, key=lambda candidate: candidate.candidate_id)


def materialize_candidate_results(
    candidates: Iterable[SecurityCandidate],
) -> tuple[list[Finding], list[AttackPath]]:
    findings: list[Finding] = []
    paths: list[AttackPath] = []
    for candidate in candidates:
        if candidate.source_finding_id is not None:
            findings.append(
                Finding(
                    finding_id=candidate.source_finding_id,
                    rule_id=candidate.claim_contract.rule_id,
                    title=candidate.claim_contract.title,
                    status=candidate.status,
                    candidate_id=candidate.candidate_id,
                    evidence_ids=list(candidate.evidence_ids),
                    missing_evidence=list(candidate.missing_evidence),
                    confidence=candidate.confidence,
                    summary=candidate.claim_contract.bounded_summary,
                )
            )
        for variant in candidate.path_variants:
            paths.append(variant.model_copy(update={"candidate_id": candidate.candidate_id}))
    return findings, paths


def reconcile_candidate_state(
    previous: Iterable[SecurityCandidate],
    current: Iterable[SecurityCandidate],
    ledger: Iterable[CoverageLedgerEntry] = (),
) -> tuple[list[SecurityCandidate], list[CoverageLedgerEntry]]:
    previous_list = list(previous)
    ledger_list = list(ledger)
    previous_by_id = {candidate.candidate_id: candidate for candidate in previous_list}
    current_list: list[SecurityCandidate] = []
    for candidate in current:
        old = previous_by_id.get(candidate.candidate_id)
        if old is not None and old.disposition is not CandidateDisposition.UNREVIEWED:
            if candidate.status is FindingStatus.REJECTED:
                candidate = candidate.model_copy(
                    update={
                        "disposition": CandidateDisposition.REJECTED,
                        "disposition_reason": "deterministic semantic recompute rejected candidate",
                    }
                )
            else:
                candidate = candidate.model_copy(
                    update={
                        "disposition": old.disposition,
                        "disposition_reason": old.disposition_reason,
                    }
                )
        current_list.append(candidate)

    next_ledger = {entry.item_id: entry.model_copy(deep=True) for entry in ledger_list}
    for candidate in current_list:
        previous_entry = next_ledger.get(candidate.candidate_id)
        next_ledger[candidate.candidate_id] = CoverageLedgerEntry(
            item_id=candidate.candidate_id,
            candidate_id=candidate.candidate_id,
            disposition=candidate.disposition,
            selected=previous_entry.selected if previous_entry is not None else False,
            reason=candidate.disposition_reason,
        )
    current_ids = {candidate.candidate_id for candidate in current_list}
    for old in previous_list:
        if old.candidate_id not in current_ids:
            old_entry = next_ledger.get(old.candidate_id)
            next_ledger[old.candidate_id] = CoverageLedgerEntry(
                item_id=old.candidate_id,
                candidate_id=old.candidate_id,
                disposition=CandidateDisposition.BLOCKED_EXTERNAL,
                selected=old_entry.selected if old_entry is not None else False,
                reason="candidate disappeared or was hard-blocked after semantic recompute",
            )
    return current_list, list(next_ledger.values())


def candidate_for_id(state: AnalysisState, candidate_id: str) -> SecurityCandidate | None:
    return next(
        (candidate for candidate in state.candidates if candidate.candidate_id == candidate_id),
        None,
    )


_TERMINAL_DISPOSITIONS = {
    CandidateDisposition.ACCEPTED,
    CandidateDisposition.REJECTED,
    CandidateDisposition.BLOCKED_EXTERNAL,
    CandidateDisposition.MERGED,
}


def materialize_candidate_decisions(
    state: AnalysisState,
    decisions: Iterable[CandidateDecisionDraft],
) -> tuple[AnalysisProposal, tuple[CandidateDecisionDraft, ...]]:
    """Turn candidate decisions into canonical Findings/AttackPaths without trusting prose."""

    candidates = {candidate.candidate_id: candidate for candidate in state.candidates}
    seen: set[str] = set()
    normalized: list[CandidateDecisionDraft] = []
    findings = []
    paths = []
    for decision in decisions:
        if decision.candidate_id in seen:
            raise ValueError(f"duplicate candidate decision: {decision.candidate_id}")
        seen.add(decision.candidate_id)
        candidate = candidates.get(decision.candidate_id)
        if candidate is None:
            raise ValueError(f"unknown candidate_id: {decision.candidate_id}")
        if decision.decision not in _TERMINAL_DISPOSITIONS:
            raise ValueError(
                f"candidate {decision.candidate_id} requires a terminal disposition"
            )
        normalized.append(decision)
        if decision.decision is not CandidateDisposition.ACCEPTED:
            continue
        if candidate.status is FindingStatus.REJECTED:
            raise ValueError(f"rejected candidate cannot be accepted: {candidate.candidate_id}")
        if not candidate.path_variants:
            raise ValueError(f"candidate has no grounded path variant: {candidate.candidate_id}")
        if decision.selected_path_variant is None:
            variant = candidate.path_variants[0]
        else:
            variant = next(
                (
                    item
                    for item in candidate.path_variants
                    if item.path_id == decision.selected_path_variant
                ),
                None,
            )
            if variant is None:
                raise ValueError(
                    f"unknown path variant {decision.selected_path_variant} for "
                    f"candidate {candidate.candidate_id}"
                )
        path = variant.model_copy(
            update={
                "candidate_id": candidate.candidate_id,
                "status": candidate.status,
                "evidence_ids": list(candidate.evidence_ids),
                "missing_evidence": list(candidate.missing_evidence),
            },
            deep=True,
        )
        paths.append(path)
        if candidate.source_finding_id is not None:
            findings.append(
                {
                    "finding_id": candidate.source_finding_id,
                    "rule_id": candidate.claim_contract.rule_id,
                    "title": candidate.claim_contract.title,
                    "status": candidate.status,
                    "candidate_id": candidate.candidate_id,
                    "evidence_ids": list(candidate.evidence_ids),
                    "missing_evidence": list(candidate.missing_evidence),
                    "confidence": decision.confidence,
                    "summary": candidate.claim_contract.bounded_summary,
                }
            )
    return AnalysisProposal(findings=findings, paths=paths), tuple(normalized)


def commit_candidate_decisions(
    state: AnalysisState,
    decisions: Iterable[CandidateDecisionDraft],
) -> None:
    """Commit terminal dispositions only after the resulting proposal passed validation."""

    candidate_by_id = {candidate.candidate_id: candidate for candidate in state.candidates}
    decision_by_root: dict[str, CandidateDecisionDraft] = {}
    for decision in decisions:
        selected = candidate_by_id.get(decision.candidate_id)
        if selected is None:
            continue
        if selected.root_cause_key in decision_by_root:
            raise ValueError(
                f"multiple decisions for root cause {selected.root_cause_key} in one batch"
            )
        decision_by_root[selected.root_cause_key] = decision
    state.candidates = [
        candidate.model_copy(
            update={
                "disposition": decision_by_root[candidate.root_cause_key].decision,
                "disposition_reason": decision_by_root[candidate.root_cause_key].analyst_note,
            }
        )
        if candidate.root_cause_key in decision_by_root
        else candidate
        for candidate in state.candidates
    ]
    existing = {entry.item_id: entry for entry in state.coverage_ledger}
    for candidate in state.candidates:
        decision = decision_by_root.get(candidate.root_cause_key)
        old = existing.get(candidate.candidate_id)
        if decision is not None or old is not None:
            existing[candidate.candidate_id] = CoverageLedgerEntry(
                item_id=candidate.candidate_id,
                candidate_id=candidate.candidate_id,
                disposition=candidate.disposition,
                selected=old.selected if old is not None else True,
                reason=candidate.disposition_reason,
            )
    state.coverage_ledger = list(existing.values())


def commit_frontier_decisions(
    state: AnalysisState,
    decisions: Iterable[FrontierDecisionDraft],
) -> None:
    existing = {entry.item_id: entry for entry in state.coverage_ledger}
    for decision in decisions:
        old = existing.get(decision.frontier_id)
        existing[decision.frontier_id] = CoverageLedgerEntry(
            item_id=decision.frontier_id,
            frontier_id=decision.frontier_id,
            disposition=decision.decision,
            selected=old.selected if old is not None else True,
            reason=decision.analyst_note,
        )
    state.coverage_ledger = list(existing.values())
