from __future__ import annotations

from cicd_redteam_agent.domain.evidence_requirement import (
    EvidenceFreshness,
    EvidenceRequirement,
    evidence_key,
    requirement_id_for,
)
from cicd_redteam_agent.domain.investigation import RedTeamSession, concrete_environment_name
from cicd_redteam_agent.domain.investigation_policy import PortfolioEntry
from cicd_redteam_agent.domain.models import (
    EvidenceProvider,
    EvidenceRequirementKind,
    EvidenceRequirementRef,
    Relation,
)
from cicd_redteam_agent.domain.proposal_draft import indexed_relations

_REPOSITORY_DEFAULT_MISSING = "repository default GITHUB_TOKEN permissions"
_FORK_WRITE_TOKEN_MISSING = "fork pull-request write-token repository setting"
_ENVIRONMENT_POLICY_MISSING = {
    "GitHub environment protection settings",
    "runtime GitHub environment name and protection settings",
    "environment deployment branch policy",
}
_ENVIRONMENT_BRANCH_MISSING = "environment deployment ref eligibility"
_PENDING_DEPLOYMENT_MISSING = "environment deployment approval state"
_EVENT_LINKAGE_MISSING = {
    "workflow run event resource linkage",
    "event content author provenance and authorization",
    "exact label action provenance",
    "comment author authorization",
    "label/comment author authorization",
}
_WORKFLOW_RUN_JOBS_MISSING = {
    "runner assignment and runtime execution",
    "runner identity match",
    "runtime machine identity",
    "workspace cleanup and runner scheduling",
}
_ACTION_POLICY_RELATIONS = {
    "uses_external_action_ref",
    "uses_external_reusable_workflow",
    "external_code_may_execute_in_step",
    "mutable_reusable_workflow_may_execute_in_job",
}


def _environment_for_relation(session: RedTeamSession, relation: Relation) -> str | None:
    jobs = {
        job.job_id: concrete_environment_name(job)
        for workflow in session.analysis_state.workflows
        for job in workflow.jobs
    }
    if relation.source_id in jobs:
        return jobs[relation.source_id]
    if relation.target_id in jobs:
        return jobs[relation.target_id]
    context_jobs = {
        context.context_id: context.job_id
        for context in session.analysis_state.job_execution_contexts
    }
    for node in (relation.source_id, relation.target_id):
        job_id = context_jobs.get(node)
        if job_id in jobs:
            return jobs[job_id]
    return None


def _fallback_refs(
    session: RedTeamSession,
    relation: Relation,
) -> tuple[EvidenceRequirementRef, ...]:
    repository_id = session.analysis_state.repository.repository_id
    refs: list[EvidenceRequirementRef] = []
    missing = set(relation.missing_evidence)
    if _REPOSITORY_DEFAULT_MISSING in missing:
        refs.append(
            EvidenceRequirementRef(
                kind=EvidenceRequirementKind.GITHUB_REPOSITORY_DEFAULT_WORKFLOW_PERMISSION,
                provider=EvidenceProvider.GITHUB,
                subject_id=repository_id,
            )
        )
    if _FORK_WRITE_TOKEN_MISSING in missing:
        refs.append(
            EvidenceRequirementRef(
                kind=EvidenceRequirementKind.GITHUB_FORK_PR_WORKFLOW_POLICY,
                provider=EvidenceProvider.GITHUB,
                subject_id=repository_id,
            )
        )
    if relation.relation_type in _ACTION_POLICY_RELATIONS:
        refs.append(
            EvidenceRequirementRef(
                kind=EvidenceRequirementKind.GITHUB_ACTIONS_POLICY,
                provider=EvidenceProvider.GITHUB,
                subject_id=repository_id,
            )
        )
    environment_name = _environment_for_relation(session, relation)
    if environment_name is not None and missing & _ENVIRONMENT_POLICY_MISSING:
        refs.append(
            EvidenceRequirementRef(
                kind=EvidenceRequirementKind.GITHUB_ENVIRONMENT_POLICY,
                provider=EvidenceProvider.GITHUB,
                subject_id=f"{repository_id}:environment:{environment_name}",
                qualifier=environment_name,
            )
        )
    if environment_name is not None and _ENVIRONMENT_BRANCH_MISSING in missing:
        refs.append(
            EvidenceRequirementRef(
                kind=EvidenceRequirementKind.GITHUB_ENVIRONMENT_BRANCH_PATTERNS,
                provider=EvidenceProvider.GITHUB,
                subject_id=f"{repository_id}:environment:{environment_name}",
                qualifier=environment_name,
            )
        )
    if session.github_run_id is not None and _PENDING_DEPLOYMENT_MISSING in missing:
        refs.append(
            EvidenceRequirementRef(
                kind=EvidenceRequirementKind.GITHUB_PENDING_DEPLOYMENT_STATE,
                provider=EvidenceProvider.GITHUB,
                subject_id=f"{repository_id}:run:{session.github_run_id}",
                qualifier=str(session.github_run_id),
            )
        )
    if session.github_run_id is not None and missing & _EVENT_LINKAGE_MISSING:
        refs.append(
            EvidenceRequirementRef(
                kind=EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
                provider=EvidenceProvider.GITHUB,
                subject_id=f"{repository_id}:run:{session.github_run_id}",
                qualifier=str(session.github_run_id),
            )
        )
    status_jobs_missing = any(
        item.startswith("dependency result:")
        or item.startswith("prior step result:")
        or item.startswith("prior step outcome:")
        or item.startswith("prior step conclusion:")
        or item in {
            "runtime status result for success()",
            "runtime status result for failure()",
            "runtime status result for cancelled()",
            "runtime status result for implicit success()",
        }
        for item in missing
    )
    if session.github_run_id is not None and status_jobs_missing:
        refs.append(
            EvidenceRequirementRef(
                kind=EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY,
                provider=EvidenceProvider.GITHUB,
                subject_id=f"{repository_id}:run:{session.github_run_id}",
                qualifier=str(session.github_run_id),
            )
        )
    if session.github_run_id is not None and (
        missing & _WORKFLOW_RUN_JOBS_MISSING or status_jobs_missing
    ):
        refs.append(
            EvidenceRequirementRef(
                kind=EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_JOBS,
                provider=EvidenceProvider.GITHUB,
                subject_id=f"{repository_id}:run:{session.github_run_id}",
                qualifier=str(session.github_run_id),
            )
        )
    return tuple(refs)


def _requirement_resolved(session: RedTeamSession, ref: EvidenceRequirementRef) -> bool:
    key = evidence_key(ref.kind, ref.subject_id, qualifier=ref.qualifier)
    investigation = session.investigation_state
    requires_resume_revalidation = (
        investigation.resume_revalidation_epoch > 0
        and key not in investigation.revalidated_evidence_keys
    )
    if key in session.knowledge_state.external_evidence:
        return not requires_resume_revalidation
    if ref.kind is EvidenceRequirementKind.GITHUB_REPOSITORY_DEFAULT_WORKFLOW_PERMISSION:
        resolved = session.knowledge_state.repository_default_workflow_permissions is not None
        return resolved and not requires_resume_revalidation
    if ref.kind is EvidenceRequirementKind.GITHUB_ENVIRONMENT_POLICY and ref.qualifier:
        resolved = ref.qualifier in session.knowledge_state.environment_protection_policies
        return resolved and not requires_resume_revalidation
    return False


def compile_evidence_requirements(
    session: RedTeamSession,
    portfolio: tuple[PortfolioEntry, ...],
) -> tuple[EvidenceRequirement, ...]:
    relation_by_ref = dict(indexed_relations(session.analysis_state))
    merged: dict[tuple[EvidenceRequirementKind, str, str | None], EvidenceRequirement] = {}

    for entry in portfolio:
        frontier = entry.frontier
        candidate = entry.candidate
        if frontier is None and candidate is None:
            continue
        if frontier is not None:
            relation_refs = set(frontier.relation_refs)
            relation_refs.update(guard.relation_ref for guard in frontier.guards)
            direct_refs = ()
            item_id = frontier.frontier_id
        else:
            assert candidate is not None
            relation_refs = {
                reference
                for reference, relation in relation_by_ref.items()
                if relation.relation_id
                in set(candidate.supporting_relation_ids + candidate.contradicting_relation_ids)
            }
            direct_refs = tuple(candidate.evidence_requirements)
            item_id = candidate.candidate_id
        for direct_ref in direct_refs:
            if _requirement_resolved(session, direct_ref):
                continue
            key = (direct_ref.kind, direct_ref.subject_id, direct_ref.qualifier)
            merged[key] = EvidenceRequirement(
                requirement_id=requirement_id_for(
                    direct_ref.kind,
                    direct_ref.subject_id,
                    qualifier=direct_ref.qualifier,
                ),
                kind=direct_ref.kind,
                provider=direct_ref.provider,
                subject_id=direct_ref.subject_id,
                qualifier=direct_ref.qualifier,
                basis_relation_ids=list(candidate.supporting_relation_ids)
                if candidate is not None
                else [],
                frontier_ids=[item_id],
                priority=1000 - entry.rank * 100 + entry.score.information_gain,
                reason=f"selected investigation item requires {direct_ref.kind.value}",
                freshness=EvidenceFreshness(
                    scope="resume-revalidate",
                    revalidate_on_resume=True,
                ),
            )
        for reference in sorted(relation_refs):
            relation = relation_by_ref.get(reference)
            if relation is None:
                continue
            refs_by_key = {
                (ref.kind, ref.subject_id, ref.qualifier): ref
                for ref in (
                    *relation.evidence_requirements,
                    *_fallback_refs(session, relation),
                )
            }
            for ref in refs_by_key.values():
                if _requirement_resolved(session, ref):
                    continue
                key = (ref.kind, ref.subject_id, ref.qualifier)
                basis = [relation.relation_id]
                requirement = EvidenceRequirement(
                    requirement_id=requirement_id_for(
                        ref.kind,
                        ref.subject_id,
                        qualifier=ref.qualifier,
                    ),
                    kind=ref.kind,
                    provider=ref.provider,
                    subject_id=ref.subject_id,
                    qualifier=ref.qualifier,
                    basis_relation_ids=basis,
                    frontier_ids=[item_id],
                    priority=1000 - entry.rank * 100 + entry.score.information_gain,
                    reason=f"selected investigation item requires {ref.kind.value}",
                    freshness=EvidenceFreshness(
                        scope="resume-revalidate",
                        revalidate_on_resume=True,
                    ),
                )
                previous = merged.get(key)
                if previous is None:
                    merged[key] = requirement
                else:
                    merged[key] = previous.model_copy(
                        update={
                            "basis_relation_ids": sorted(
                                set(previous.basis_relation_ids + basis)
                            ),
                            "frontier_ids": sorted(
                                set(previous.frontier_ids + [item_id])
                            ),
                            "priority": max(previous.priority, requirement.priority),
                        }
                    )

    # Repository metadata is a prerequisite only for resolvers whose semantics depend on
    # repository visibility/default-branch identity. It is not fetched for every observation.
    needs_metadata = any(
        requirement.kind is EvidenceRequirementKind.GITHUB_FORK_PR_WORKFLOW_POLICY
        for requirement in merged.values()
    )
    repository_id = session.analysis_state.repository.repository_id
    metadata_ref = EvidenceRequirementRef(
        kind=EvidenceRequirementKind.GITHUB_REPOSITORY_METADATA,
        provider=EvidenceProvider.GITHUB,
        subject_id=repository_id,
    )
    if needs_metadata and not _requirement_resolved(session, metadata_ref):
        key = (EvidenceRequirementKind.GITHUB_REPOSITORY_METADATA, repository_id, None)
        merged[key] = EvidenceRequirement(
            requirement_id=requirement_id_for(
                EvidenceRequirementKind.GITHUB_REPOSITORY_METADATA,
                repository_id,
            ),
            kind=EvidenceRequirementKind.GITHUB_REPOSITORY_METADATA,
            provider=EvidenceProvider.GITHUB,
            subject_id=repository_id,
            priority=max((item.priority for item in merged.values()), default=0) + 100,
            reason="repository metadata is required by a selected external evidence resolver",
            freshness=EvidenceFreshness(scope="resume-revalidate", revalidate_on_resume=True),
        )

    return tuple(
        sorted(
            merged.values(),
            key=lambda item: (
                -item.priority,
                item.kind.value,
                item.subject_id,
                item.qualifier or "",
            ),
        )
    )
