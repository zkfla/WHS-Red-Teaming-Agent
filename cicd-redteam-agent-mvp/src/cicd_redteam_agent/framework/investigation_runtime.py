from __future__ import annotations

from cicd_redteam_agent.application.semantic_snapshot import rebuild_semantic_snapshot
from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.graph_frontier import build_attack_frontiers
from cicd_redteam_agent.domain.investigation import (
    EnvironmentBranchPatternsEvidence,
    EnvironmentPolicyEvidence,
    EventPrincipalEvidence,
    GitHubEventLocator,
    InvestigationTask,
    InvestigationTaskKind,
    InvestigationTaskStatus,
    KnowledgeState,
    Observation,
    ObservationKind,
    PendingDeploymentStateEvidence,
    RedTeamSession,
    RepositoryActorPermissionEvidence,
    RepositoryWorkflowPermissionEvidence,
    TaskLease,
    WorkflowRunEventLinkageEvidence,
    WorkflowRunIdentityEvidence,
    WorkflowRunStatusEvidence,
    concrete_environment_name,
)
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    AttackerProfile,
    EvidenceRequirementKind,
    RepositoryWorkflowPermissionDefault,
)
from cicd_redteam_agent.domain.proposal_draft import indexed_relations
from cicd_redteam_agent.framework.environment_observer import EnvironmentObserver
from cicd_redteam_agent.framework.evidence_planning import compile_evidence_requirements
from cicd_redteam_agent.framework.evidence_resolver import ResolverRegistry
from cicd_redteam_agent.framework.perception import apply_observation
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

_REPOSITORY_DEFAULT_PERMISSION_MISSING = "repository default GITHUB_TOKEN permissions"


def _observed_repository_default(
    state: AnalysisState,
) -> RepositoryWorkflowPermissionDefault | None:
    observed = {
        provenance.removeprefix("repository-default-observed:")
        for context in state.job_execution_contexts
        for provenance in context.effective_permissions.provenance
        if provenance.startswith("repository-default-observed:")
    }
    if len(observed) != 1:
        return None
    value = next(iter(observed))
    try:
        return RepositoryWorkflowPermissionDefault(value)
    except ValueError:
        return None


def create_red_team_session(
    analysis_state: AnalysisState,
    *,
    github_run_id: int | None = None,
    github_event_locator: GitHubEventLocator | None = None,
) -> RedTeamSession:
    baseline = analysis_state.model_copy(deep=True)
    session = RedTeamSession(
        baseline_analysis=baseline,
        analysis_state=analysis_state.model_copy(deep=True),
        github_run_id=github_run_id,
        github_event_locator=github_event_locator,
    )
    observed_default = _observed_repository_default(analysis_state)
    if observed_default is not None:
        session.knowledge_state.repository_default_workflow_permissions = observed_default
        session.knowledge_state.revision = 1
    return session


def _active_knowledge_state(session: RedTeamSession) -> KnowledgeState:
    knowledge = session.knowledge_state
    if session.investigation_state.resume_revalidation_epoch == 0:
        return knowledge

    active = knowledge.model_copy(deep=True)
    allowed = session.investigation_state.revalidated_evidence_keys
    active.external_evidence = {
        key: payload for key, payload in active.external_evidence.items() if key in allowed
    }
    active.repository_default_workflow_permissions = None
    active.environment_protection_policies = {}
    for payload in active.external_evidence.values():
        if isinstance(payload, RepositoryWorkflowPermissionEvidence):
            active.repository_default_workflow_permissions = payload.value
        elif isinstance(payload, EnvironmentPolicyEvidence):
            active.environment_protection_policies[payload.policy.environment_name] = payload.policy
    return active


def prepare_resume_revalidation(
    session: RedTeamSession,
    *,
    boundary: RepositoryBoundary,
    profile: AttackerProfile,
) -> None:
    """Start a lazy fail-closed revalidation epoch for persisted external evidence."""

    investigation = session.investigation_state
    investigation.resume_revalidation_epoch += 1
    investigation.revalidated_evidence_keys.clear()
    session.analysis_state = rebuild_semantic_snapshot(
        session.baseline_analysis,
        boundary,
        profile,
        knowledge_state=_active_knowledge_state(session),
        runtime_state=session.analysis_state,
        github_run_id=session.github_run_id,
    )


def synchronize_analysis_state(target: AnalysisState, source: AnalysisState) -> None:
    """Atomically replace the fields of a shared live AnalysisState object."""

    restored = AnalysisState.model_validate(source.model_dump(mode="json", by_alias=True))
    for field_name in AnalysisState.model_fields:
        setattr(target, field_name, getattr(restored, field_name))


def _repository_permission_basis(session: RedTeamSession) -> list[str]:
    relation_by_ref = dict(indexed_relations(session.analysis_state))
    for frontier in build_attack_frontiers(session.analysis_state).frontiers:
        basis = [
            relation_by_ref[reference].relation_id
            for reference in frontier.relation_refs
            if reference in relation_by_ref
            and _REPOSITORY_DEFAULT_PERMISSION_MISSING
            in relation_by_ref[reference].missing_evidence
        ]
        if basis:
            return list(dict.fromkeys(basis))
    return []


def _environment_policy_bases(session: RedTeamSession) -> dict[str, list[str]]:
    relation_by_ref = dict(indexed_relations(session.analysis_state))
    environment_by_job = {
        job.job_id: environment_name
        for workflow in session.analysis_state.workflows
        for job in workflow.jobs
        if (environment_name := concrete_environment_name(job)) is not None
    }
    bases: dict[str, list[str]] = {}
    frontiers = build_attack_frontiers(session.analysis_state).frontiers
    job_by_context = {
        context.context_id: context.job_id
        for context in session.analysis_state.job_execution_contexts
    }
    frontier_job_ids = {
        job_id
        for frontier in frontiers
        if frontier.job_context_id is not None
        if (job_id := job_by_context.get(frontier.job_context_id)) is not None
    }
    candidate_relations = {
        relation.relation_id: relation
        for relation in session.analysis_state.relations
        if relation.relation_type == "environment_protection_unknown"
        and relation.source_id in frontier_job_ids
    }
    for frontier in frontiers:
        for reference in (guard.relation_ref for guard in frontier.guards):
            relation = relation_by_ref.get(reference)
            if relation is None or relation.relation_type != "environment_protection_unknown":
                continue
            candidate_relations[relation.relation_id] = relation
    for relation in candidate_relations.values():
        environment_name = environment_by_job.get(relation.source_id)
        if environment_name is not None:
            bases.setdefault(environment_name, []).append(relation.relation_id)
    return {name: list(dict.fromkeys(relation_ids)) for name, relation_ids in sorted(bases.items())}


def _environment_policy_task(
    session: RedTeamSession,
    environment_name: str,
    basis_relation_ids: list[str],
) -> InvestigationTask | None:
    repository_id = session.analysis_state.repository.repository_id
    subject_id = f"{repository_id}:environment:{environment_name}"
    task_id = stable_id(
        "task",
        InvestigationTaskKind.OBSERVE_ENVIRONMENT_PROTECTION_POLICY.value,
        repository_id,
        environment_name,
        *basis_relation_ids,
    )
    existing = session.investigation_state.tasks.get(task_id)
    if existing is not None:
        if existing.status in {InvestigationTaskStatus.READY, InvestigationTaskStatus.LEASED}:
            return existing
        return None
    task = InvestigationTask(
        task_id=task_id,
        kind=InvestigationTaskKind.OBSERVE_ENVIRONMENT_PROTECTION_POLICY,
        subject_id=subject_id,
        environment_name=environment_name,
        basis_relation_ids=basis_relation_ids,
    )
    session.investigation_state.tasks[task_id] = task
    return task


def plan_next_task(
    session: RedTeamSession,
    *,
    portfolio=(),
    resolver_registry: ResolverRegistry | None = None,
) -> InvestigationTask | None:
    if resolver_registry is not None:
        if not portfolio:
            return None
        requirements = compile_evidence_requirements(session, tuple(portfolio))
        for requirement in requirements:
            session.investigation_state.requirements.setdefault(
                requirement.requirement_id,
                requirement,
            )
        for requirement in requirements:
            descriptor = resolver_registry.descriptor_for(requirement.kind)
            if descriptor is None:
                continue
            task_identity = [
                InvestigationTaskKind.RESOLVE_EXTERNAL_EVIDENCE.value,
                requirement.requirement_id,
                descriptor.resolver_id,
            ]
            if session.investigation_state.resume_revalidation_epoch:
                task_identity.append(
                    f"revalidation:{session.investigation_state.resume_revalidation_epoch}"
                )
            task_id = stable_id("task", *task_identity)
            existing = session.investigation_state.tasks.get(task_id)
            if existing is not None:
                if existing.status in {
                    InvestigationTaskStatus.READY,
                    InvestigationTaskStatus.LEASED,
                }:
                    return existing
                continue
            task = InvestigationTask(
                task_id=task_id,
                kind=InvestigationTaskKind.RESOLVE_EXTERNAL_EVIDENCE,
                subject_id=requirement.subject_id,
                requirement_id=requirement.requirement_id,
                resolver_id=descriptor.resolver_id,
                provider=descriptor.provider,
                environment_name=(
                    requirement.qualifier
                    if requirement.kind
                    in {
                        EvidenceRequirementKind.GITHUB_ENVIRONMENT_POLICY,
                        EvidenceRequirementKind.GITHUB_ENVIRONMENT_BRANCH_PATTERNS,
                    }
                    else None
                ),
                basis_relation_ids=requirement.basis_relation_ids,
            )
            session.investigation_state.tasks[task_id] = task
            return task
        return None

    # Legacy planner remains as a checkpoint/test compatibility fallback.
    if session.knowledge_state.repository_default_workflow_permissions is None:
        basis_relation_ids = _repository_permission_basis(session)
        if basis_relation_ids:
            subject_id = session.analysis_state.repository.repository_id
            task_id = stable_id(
                "task",
                InvestigationTaskKind.OBSERVE_REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS.value,
                subject_id,
                *basis_relation_ids,
            )
            existing = session.investigation_state.tasks.get(task_id)
            if existing is not None:
                if existing.status in {
                    InvestigationTaskStatus.READY,
                    InvestigationTaskStatus.LEASED,
                }:
                    return existing
            else:
                task = InvestigationTask(
                    task_id=task_id,
                    kind=(InvestigationTaskKind.OBSERVE_REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS),
                    subject_id=subject_id,
                    basis_relation_ids=basis_relation_ids,
                )
                session.investigation_state.tasks[task_id] = task
                return task

    observed = session.knowledge_state.environment_protection_policies
    for environment_name, relation_ids in _environment_policy_bases(session).items():
        if environment_name in observed:
            continue
        task = _environment_policy_task(session, environment_name, relation_ids)
        if task is not None:
            return task
    return None


def lease_task(session: RedTeamSession, task: InvestigationTask) -> TaskLease:
    investigation = session.investigation_state
    if investigation.active_lease is not None:
        raise ValueError("an investigation task is already leased")
    if task.status is not InvestigationTaskStatus.READY:
        raise ValueError(f"task is not ready for lease: {task.task_id}")

    next_attempt = task.attempts + 1
    lease = TaskLease(
        lease_id=stable_id(
            "lease",
            task.task_id,
            session.knowledge_state.revision,
            next_attempt,
        ),
        task_id=task.task_id,
        knowledge_revision=session.knowledge_state.revision,
    )
    task.attempts = next_attempt
    task.status = InvestigationTaskStatus.LEASED
    investigation.active_lease = lease
    return lease


def commit_observation(
    session: RedTeamSession,
    observation: Observation,
    *,
    boundary: RepositoryBoundary,
    profile: AttackerProfile,
) -> bool:
    investigation = session.investigation_state
    lease = investigation.active_lease
    if lease is None:
        raise ValueError("observation cannot be committed without an active lease")
    if lease.knowledge_revision != session.knowledge_state.revision:
        raise ValueError("stale investigation lease")

    task = investigation.tasks.get(lease.task_id)
    if task is None or task.status is not InvestigationTaskStatus.LEASED:
        raise ValueError("active lease does not reference a leased task")
    if observation.subject_id != task.subject_id:
        raise ValueError("observation subject does not match the leased task")
    if observation.task_id != task.task_id:
        raise ValueError("observation does not belong to the leased task")
    if task.kind is InvestigationTaskKind.RESOLVE_EXTERNAL_EVIDENCE:
        if observation.requirement_id != task.requirement_id or observation.payload is None:
            raise ValueError("generic observation does not match the leased evidence task")
        requirement = investigation.requirements.get(task.requirement_id or "")
        if requirement is None or observation.payload.type != requirement.kind.value:
            raise ValueError("generic observation payload does not match its evidence requirement")
        if requirement.kind is EvidenceRequirementKind.GITHUB_ENVIRONMENT_POLICY:
            if (
                not isinstance(observation.payload, EnvironmentPolicyEvidence)
                or observation.payload.policy.environment_name != requirement.qualifier
            ):
                raise ValueError("environment policy payload does not match its requirement")
        elif requirement.kind is EvidenceRequirementKind.GITHUB_ENVIRONMENT_BRANCH_PATTERNS:
            if (
                not isinstance(observation.payload, EnvironmentBranchPatternsEvidence)
                or observation.payload.environment_name != requirement.qualifier
            ):
                raise ValueError(
                    "environment branch-pattern payload does not match its requirement"
                )
        elif requirement.kind is EvidenceRequirementKind.GITHUB_PENDING_DEPLOYMENT_STATE:
            if (
                not isinstance(observation.payload, PendingDeploymentStateEvidence)
                or str(observation.payload.run_id) != requirement.qualifier
            ):
                raise ValueError("pending deployment payload does not match its requirement")
        elif requirement.kind is EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY:
            if (
                not isinstance(observation.payload, WorkflowRunIdentityEvidence)
                or str(observation.payload.run_id) != requirement.qualifier
                or session.github_run_id != observation.payload.run_id
            ):
                raise ValueError("workflow run identity payload does not match its requirement")
        elif requirement.kind is EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_STATUS:
            if (
                not isinstance(observation.payload, WorkflowRunStatusEvidence)
                or str(observation.payload.run_id) != requirement.qualifier
                or session.github_run_id != observation.payload.run_id
            ):
                raise ValueError("workflow run status payload does not match its requirement")
        elif requirement.kind is EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE:
            if (
                not isinstance(observation.payload, WorkflowRunEventLinkageEvidence)
                or str(observation.payload.run_id) != requirement.qualifier
                or session.github_run_id != observation.payload.run_id
            ):
                raise ValueError(
                    "workflow run event linkage payload does not match its requirement"
                )
        elif requirement.kind is EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL:
            if (
                not isinstance(observation.payload, EventPrincipalEvidence)
                or session.github_run_id != observation.payload.run_id
            ):
                raise ValueError("event principal payload does not match its requirement")
        elif requirement.kind is EvidenceRequirementKind.GITHUB_REPOSITORY_ACTOR_PERMISSION:
            if (
                not isinstance(observation.payload, RepositoryActorPermissionEvidence)
                or observation.payload.actor_login.casefold()
                != (requirement.qualifier or "").casefold()
            ):
                raise ValueError("actor permission payload does not match its requirement")
    if (
        task.kind is InvestigationTaskKind.OBSERVE_REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS
        and observation.kind is not ObservationKind.REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS
    ):
        raise ValueError("observation kind does not match the leased task")
    if (
        task.kind is InvestigationTaskKind.OBSERVE_ENVIRONMENT_PROTECTION_POLICY
        and observation.kind is not ObservationKind.ENVIRONMENT_PROTECTION_POLICY
    ):
        raise ValueError("observation kind does not match the leased task")
    if (
        task.kind is InvestigationTaskKind.OBSERVE_ENVIRONMENT_PROTECTION_POLICY
        and task.environment_name is not None
    ):
        policy = observation.environment_protection_policy
        if policy is None or policy.environment_name != task.environment_name:
            raise ValueError("environment observation does not match the leased task")

    candidate = session.model_copy(deep=True)
    changed = apply_observation(candidate, observation)
    if changed or candidate.investigation_state.resume_revalidation_epoch > 0:
        candidate.analysis_state = rebuild_semantic_snapshot(
            candidate.baseline_analysis,
            boundary,
            profile,
            knowledge_state=_active_knowledge_state(candidate),
            runtime_state=candidate.analysis_state,
            github_run_id=candidate.github_run_id,
        )

    candidate_task = candidate.investigation_state.tasks[lease.task_id]
    candidate_task.status = InvestigationTaskStatus.COMPLETED
    candidate.investigation_state.active_lease = None
    session.analysis_state = candidate.analysis_state
    session.knowledge_state = candidate.knowledge_state
    session.investigation_state = candidate.investigation_state
    return changed


async def run_observation_once(
    session: RedTeamSession,
    observer: EnvironmentObserver,
    *,
    boundary: RepositoryBoundary,
    profile: AttackerProfile,
    task: InvestigationTask | None = None,
) -> Observation | None:
    task = task or plan_next_task(session)
    if task is None:
        return None
    lease_task(session, task)
    try:
        observation = await observer.observe(session, task)
    except Exception:
        task.status = InvestigationTaskStatus.READY
        session.investigation_state.active_lease = None
        raise
    commit_observation(
        session,
        observation,
        boundary=boundary,
        profile=profile,
    )
    return observation
