from __future__ import annotations

from cicd_redteam_agent.domain.evidence_requirement import (
    evidence_key,
    requirement_kind_for_payload_type,
)
from cicd_redteam_agent.domain.investigation import (
    EnvironmentPolicyEvidence,
    Observation,
    ObservationKind,
    RedTeamSession,
    RepositoryWorkflowPermissionEvidence,
)


def apply_observation(session: RedTeamSession, observation: Observation) -> bool:
    """Normalize a trusted observation into canonical knowledge and return semantic progress."""

    investigation = session.investigation_state
    if observation.observation_id in investigation.observations:
        return False

    investigation.observations[observation.observation_id] = observation
    knowledge = session.knowledge_state
    changed = False

    if observation.payload is not None:
        requirement = (
            investigation.requirements.get(observation.requirement_id)
            if observation.requirement_id is not None
            else None
        )
        kind = requirement_kind_for_payload_type(observation.payload.type)
        qualifier = requirement.qualifier if requirement is not None else None
        key = evidence_key(kind, observation.subject_id, qualifier=qualifier)
        if knowledge.external_evidence.get(key) != observation.payload:
            knowledge.external_evidence[key] = observation.payload
            changed = True
        if session.investigation_state.resume_revalidation_epoch > 0:
            session.investigation_state.revalidated_evidence_keys.add(key)
        if isinstance(observation.payload, RepositoryWorkflowPermissionEvidence):
            if knowledge.repository_default_workflow_permissions != observation.payload.value:
                knowledge.repository_default_workflow_permissions = observation.payload.value
                changed = True
        elif isinstance(observation.payload, EnvironmentPolicyEvidence):
            policy = observation.payload.policy
            existing = knowledge.environment_protection_policies.get(policy.environment_name)
            if existing != policy:
                knowledge.environment_protection_policies[policy.environment_name] = policy
                changed = True
    elif observation.kind is ObservationKind.REPOSITORY_DEFAULT_WORKFLOW_PERMISSIONS:
        value = observation.repository_default_workflow_permissions
        if value is None:
            raise ValueError("repository default workflow permission observation has no value")
        if knowledge.repository_default_workflow_permissions != value:
            knowledge.repository_default_workflow_permissions = value
            changed = True
    elif observation.kind is ObservationKind.ENVIRONMENT_PROTECTION_POLICY:
        policy = observation.environment_protection_policy
        if policy is None:
            raise ValueError("environment protection observation has no policy")
        existing = knowledge.environment_protection_policies.get(policy.environment_name)
        if existing != policy:
            knowledge.environment_protection_policies[policy.environment_name] = policy
            changed = True

    if changed:
        knowledge.revision += 1
    return changed
