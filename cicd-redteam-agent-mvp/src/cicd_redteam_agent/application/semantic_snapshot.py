from __future__ import annotations

from cicd_redteam_agent.domain.attacker_control import assess_attacker_control
from cicd_redteam_agent.domain.aws_oidc import derive_aws_oidc_trust_assessments
from cicd_redteam_agent.domain.build_semantics import derive_build_semantics
from cicd_redteam_agent.domain.evidence_requirement import (
    annotate_external_evidence_requirements,
    evidence_key,
)
from cicd_redteam_agent.domain.execution_semantics import derive_execution_semantics
from cicd_redteam_agent.domain.identity_analysis import derive_identity_candidates
from cicd_redteam_agent.domain.investigation import (
    ActionAdvisoryEvidence,
    ActionProvenanceEvidence,
    ActionsPolicyEvidence,
    AuditEventsEvidence,
    EffectiveBranchRulesEvidence,
    EnvironmentBranchPatternsEvidence,
    EnvironmentProtectionPolicy,
    EventPrincipalEvidence,
    ForkPullRequestPolicyEvidence,
    KnowledgeState,
    PendingDeploymentStateEvidence,
    RepositoryActorPermissionEvidence,
    RepositoryMetadataEvidence,
    RuntimeTelemetryEvidence,
    SecretMetadataEvidence,
    SecretRotationPolicyEvidence,
    WorkflowRunEventLinkageEvidence,
    WorkflowRunIdentityEvidence,
    WorkflowRunJobsEvidence,
    WorkflowRunStatusEvidence,
)
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    AttackerProfile,
    EvidenceRequirementKind,
    RepositoryWorkflowPermissionDefault,
)
from cicd_redteam_agent.domain.reference_tracer import trace_local_references
from cicd_redteam_agent.domain.security_candidate import (
    derive_security_candidates,
    reconcile_candidate_state,
)
from cicd_redteam_agent.domain.security_facts import derive_security_facts
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary


def rebuild_semantic_snapshot(
    baseline: AnalysisState,
    boundary: RepositoryBoundary,
    profile: AttackerProfile,
    *,
    repository_default: RepositoryWorkflowPermissionDefault | None = None,
    environment_policies: dict[str, EnvironmentProtectionPolicy] | None = None,
    knowledge_state: KnowledgeState | None = None,
    runtime_state: AnalysisState | None = None,
    github_run_id: int | None = None,
) -> AnalysisState:
    """Rebuild derived semantics while preserving committed runtime investigation state."""

    runtime = runtime_state or baseline
    repository_id = baseline.repository.repository_id
    if knowledge_state is not None:
        repository_default = (
            knowledge_state.repository_default_workflow_permissions
            if repository_default is None
            else repository_default
        )
        environment_policies = (
            knowledge_state.environment_protection_policies
            if environment_policies is None
            else environment_policies
        )
        fork_policy = knowledge_state.external_evidence.get(
            evidence_key(EvidenceRequirementKind.GITHUB_FORK_PR_WORKFLOW_POLICY, repository_id)
        )
        actions_policy = knowledge_state.external_evidence.get(
            evidence_key(EvidenceRequirementKind.GITHUB_ACTIONS_POLICY, repository_id)
        )
        branch_rules = {
            payload.branch: payload
            for payload in knowledge_state.external_evidence.values()
            if isinstance(payload, EffectiveBranchRulesEvidence)
        }
        environment_branch_patterns = {
            payload.environment_name: payload
            for payload in knowledge_state.external_evidence.values()
            if isinstance(payload, EnvironmentBranchPatternsEvidence)
        }
        pending_deployment_state = next(
            (
                payload
                for payload in knowledge_state.external_evidence.values()
                if isinstance(payload, PendingDeploymentStateEvidence)
            ),
            None,
        )
        workflow_run_jobs = next(
            (
                payload
                for payload in knowledge_state.external_evidence.values()
                if isinstance(payload, WorkflowRunJobsEvidence)
                and (github_run_id is None or payload.run_id == github_run_id)
            ),
            None,
        )
        workflow_run_status = next(
            (
                payload
                for payload in knowledge_state.external_evidence.values()
                if isinstance(payload, WorkflowRunStatusEvidence)
                and (github_run_id is None or payload.run_id == github_run_id)
            ),
            None,
        )
        workflow_run_identity = next(
            (
                payload
                for payload in knowledge_state.external_evidence.values()
                if isinstance(payload, WorkflowRunIdentityEvidence)
                and (github_run_id is None or payload.run_id == github_run_id)
            ),
            None,
        )
        actor_permissions = {
            payload.actor_login: payload
            for payload in knowledge_state.external_evidence.values()
            if isinstance(payload, RepositoryActorPermissionEvidence)
        }
        event_linkage = next(
            (
                payload
                for payload in knowledge_state.external_evidence.values()
                if isinstance(payload, WorkflowRunEventLinkageEvidence)
                and (github_run_id is None or payload.run_id == github_run_id)
            ),
            None,
        )
        event_principals = {
            payload.principal_role: payload
            for payload in knowledge_state.external_evidence.values()
            if isinstance(payload, EventPrincipalEvidence)
            and (github_run_id is None or payload.run_id == github_run_id)
        }
        repository_metadata = next(
            (
                payload
                for payload in knowledge_state.external_evidence.values()
                if isinstance(payload, RepositoryMetadataEvidence)
            ),
            None,
        )
        runtime_telemetry = [
            payload
            for payload in knowledge_state.external_evidence.values()
            if isinstance(payload, RuntimeTelemetryEvidence)
        ]
        action_provenance = [
            payload
            for payload in knowledge_state.external_evidence.values()
            if isinstance(payload, ActionProvenanceEvidence)
        ]
        action_advisories = [
            payload
            for payload in knowledge_state.external_evidence.values()
            if isinstance(payload, ActionAdvisoryEvidence)
        ]
        secret_metadata = [
            payload
            for payload in knowledge_state.external_evidence.values()
            if isinstance(payload, SecretMetadataEvidence)
        ]
        rotation_policy = next(
            (
                payload
                for payload in knowledge_state.external_evidence.values()
                if isinstance(payload, SecretRotationPolicyEvidence)
            ),
            None,
        )
        audit_events = [
            payload
            for payload in knowledge_state.external_evidence.values()
            if isinstance(payload, AuditEventsEvidence)
        ]
    else:
        fork_policy = None
        actions_policy = None
        branch_rules = {}
        environment_branch_patterns = {}
        pending_deployment_state = None
        workflow_run_jobs = None
        workflow_run_status = None
        workflow_run_identity = None
        actor_permissions = {}
        event_linkage = None
        event_principals = {}
        repository_metadata = None
        runtime_telemetry = []
        action_provenance = []
        action_advisories = []
        secret_metadata = []
        rotation_policy = None
        audit_events = []
    typed_fork_policy = (
        fork_policy if isinstance(fork_policy, ForkPullRequestPolicyEvidence) else None
    )
    typed_actions_policy = (
        actions_policy if isinstance(actions_policy, ActionsPolicyEvidence) else None
    )
    evidence = {
        evidence_id: item.model_copy(deep=True) for evidence_id, item in baseline.evidence.items()
    }
    evidence.update(
        {evidence_id: item.model_copy(deep=True) for evidence_id, item in runtime.evidence.items()}
    )
    state = AnalysisState(
        repository=baseline.repository.model_copy(deep=True),
        workflows=[item.model_copy(deep=True) for item in baseline.workflows],
        evidence=evidence,
        secret_references=[item.model_copy(deep=True) for item in baseline.secret_references],
        inspected_resources=list(runtime.inspected_resources),
        repeated_calls=dict(runtime.repeated_calls),
        working_memory=runtime.working_memory.model_copy(deep=True),
    )

    relations, reference_evidence, _ = trace_local_references(state.workflows, boundary)
    state.relations.extend(relations)
    state.evidence.update(reference_evidence)
    build_relations, build_evidence, _ = derive_build_semantics(state, boundary)
    state.relations.extend(build_relations)
    state.evidence.update(build_evidence)
    state.assessments = assess_attacker_control(state, profile)

    execution = derive_execution_semantics(
        state,
        profile,
        repository_default=repository_default,
        repository_metadata=repository_metadata,
        fork_policy=typed_fork_policy,
        workflow_run_identity=workflow_run_identity,
        actor_permissions=actor_permissions,
        event_linkage=event_linkage,
        event_principals=event_principals,
        workflow_run_jobs=workflow_run_jobs,
        workflow_run_status=workflow_run_status,
        github_run_id=github_run_id,
    )
    state.trigger_contexts = execution.trigger_contexts
    state.job_execution_contexts = execution.job_contexts
    state.condition_evaluations = execution.condition_evaluations
    state.relations.extend(execution.relations)
    state.aws_oidc_trust_assessments = derive_aws_oidc_trust_assessments(
        state,
        external_evidence=(
            knowledge_state.external_evidence.values() if knowledge_state is not None else ()
        ),
    )

    security_facts = derive_security_facts(
        state,
        profile,
        environment_policies=environment_policies,
        fork_policy=typed_fork_policy,
        actions_policy=typed_actions_policy,
        branch_rules=branch_rules,
        environment_branch_patterns=environment_branch_patterns,
        pending_deployment_state=pending_deployment_state,
        workflow_run_jobs=workflow_run_jobs,
        runtime_telemetry=runtime_telemetry,
        action_provenance=action_provenance,
        action_advisories=action_advisories,
        secret_metadata=secret_metadata,
        rotation_policy=rotation_policy,
        audit_events=audit_events,
    )
    state.relations.extend(security_facts)
    annotate_external_evidence_requirements(state)
    state.pending_questions = sorted(
        {
            missing
            for relation in [*execution.relations, *security_facts]
            for missing in relation.missing_evidence
        }
    )
    state.identities = derive_identity_candidates(state)
    derived_candidates = derive_security_candidates(state)
    state.candidates, state.coverage_ledger = reconcile_candidate_state(
        runtime.candidates,
        derived_candidates,
        runtime.coverage_ledger,
    )
    return state
