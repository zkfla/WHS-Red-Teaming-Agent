from __future__ import annotations

from dataclasses import dataclass

RELATION_SEMANTICS_VERSION = "1.1"


@dataclass(frozen=True)
class RelationSemantics:
    traversable: bool = False
    source_tag: str | None = None
    mechanism_tags: tuple[str, ...] = ()
    sink_tag: str | None = None
    context_bridge: bool = False


_SOURCE_RELATIONS = {
    "can_trigger_event_context": "pull_request",
    "controls_workflow_input": "pull_request",
    "controls_repository_file": "repository_writer",
    "controls_step_execution_context": "repository_writer",
    "attacker_may_control_artifact_content": "artifact",
    "attacker_may_populate_cache": "cache",
    "label_or_comment_controls_job_condition": "pull_request",
}

_MECHANISM_RELATIONS = {
    "value_flows_to_env": ("shell_execution",),
    "value_flows_to_job_env": ("shell_execution",),
    "env_value_flows_to_step_output": ("shell_execution",),
    "env_value_reaches_output_producer": ("shell_execution",),
    "value_written_to_repository_file": ("local_action",),
    "untrusted_input_reaches_action": ("local_action",),
    "untrusted_input_reaches_reusable_workflow": ("reusable_workflow",),
    "calls_reusable_workflow": ("reusable_workflow",),
    "calls_reusable_workflow_context": ("reusable_workflow",),
    "attacker_may_control_artifact_content": ("artifact",),
    "artifact_may_overwrite_workspace_files": ("artifact",),
    "downloads_artifact_into_step": ("artifact",),
    "file_uploaded_as_artifact": ("artifact",),
    "repository_execution_may_produce_artifact": ("artifact",),
    "uploads_artifact": ("artifact",),
    "attacker_may_populate_cache": ("cache",),
    "broad_restore_key_may_select_cache": ("cache",),
    "cache_producer_reaches_privileged_consumer": ("cache",),
    "cache_restored_by_privileged_job": ("cache",),
    "restores_cache_into_step": ("cache",),
    "repository_execution_may_populate_cache": ("cache",),
    "writes_cache": ("cache",),
    "runner_process_or_file_state_may_reach_privileged_job": ("self_hosted_runner",),
    "runner_state_may_reach_job": ("self_hosted_runner",),
    "workspace_may_be_reused_by_privileged_job": ("self_hosted_runner",),
    "secret_flows_to_env": ("secret",),
    "secret_written_to_file": ("secret",),
    "external_code_may_execute_in_step": ("mutable_dependency",),
    "mutable_reusable_workflow_may_execute_in_job": (
        "mutable_dependency",
        "reusable_workflow",
    ),
    "mutable_container_image_executes_in_job": ("mutable_dependency",),
    "mutable_install_source_executes_in_step": ("mutable_dependency",),
}

_SINK_RELATIONS = {
    "env_value_interpreted_as_command": "privileged_execution",
    "env_value_reaches_execution": "privileged_execution",
    "untrusted_input_reaches_execution": "privileged_execution",
    "untrusted_value_interpreted_as_command": "privileged_execution",
    "untrusted_input_interpreted_by_github_script": "privileged_execution",
    "untrusted_input_selects_checkout_revision": "privileged_execution",
    "artifact_content_reaches_command_argument": "privileged_execution",
    "artifact_content_reaches_execution": "privileged_execution",
    "artifact_content_reaches_import": "privileged_execution",
    "cache_restores_executable_or_dependency_path": "privileged_execution",
    "repository_file_executes_in": "privileged_execution",
    "repository_file_imported_or_loaded_by": "privileged_execution",
    "attacker_code_executes_on_self_hosted_runner": "privileged_execution",
    "secret_may_reach_log": "secret_exposure",
    "secret_passed_to_external_action": "secret_exposure",
    "secret_reaches_external_send": "secret_exposure",
    "secret_reaches_shell_argument": "secret_exposure",
    "secret_reaches_step_output": "secret_exposure",
    "can_access_secret_reference": "secret_exposure",
    "can_request_oidc_token": "oidc_token",
    "oidc_may_assume_cloud_identity": "cloud_identity",
    "static_cloud_credential_may_authenticate_identity": "cloud_identity",
    "executes_with_effective_write_github_token": "github_token_write",
}

_PLUMBING_RELATIONS = {
    "event_reaches_job_context",
    "event_starts_workflow",
    "executes_job_definition",
    "job_reaches_step_execution",
    "executes_step_definition",
    "step_definition_may_execute_as_context",
    "job_receives_github_token",
    "executes_with_github_token",
    "grants_effective_permission",
    "selects_pull_request_revision",
    "workflow_run_may_trigger",
    "reusable_workflow_permission_ceiling_applied",
    "artifact_verified_by_integrity_guard",
}

_CONTEXT_BRIDGES = {
    "calls_reusable_workflow",
    "calls_reusable_workflow_context",
    "workflow_run_may_trigger",
    "downloads_artifact_into_step",
    "cache_producer_reaches_privileged_consumer",
    "cache_restored_by_privileged_job",
    "runner_process_or_file_state_may_reach_privileged_job",
    "runner_state_may_reach_job",
}


def relation_semantics(relation_type: str) -> RelationSemantics:
    """Return fail-closed graph semantics for one deterministic Relation type."""
    # ``id-token: write`` is the permission gate for GitHub OIDC, not a
    # repository-writing GITHUB_TOKEN capability.  The separately extracted
    # ``can_request_oidc_token`` relation is the canonical OIDC path edge.
    # Keeping this permission fact non-traversable prevents the frontier layer
    # from presenting it as a second, misleading GitHub-token write sink.
    if relation_type == "can_use_github_token_id-token_write":
        return RelationSemantics()

    source_tag = _SOURCE_RELATIONS.get(relation_type)
    mechanism_tags = _MECHANISM_RELATIONS.get(relation_type, ())
    sink_tag = _SINK_RELATIONS.get(relation_type)
    if relation_type.startswith("can_use_github_token_") and relation_type.endswith("_write"):
        sink_tag = "github_token_write"
        mechanism_tags = (*mechanism_tags, "permissions")
    if relation_type == "github_token_permission_unknown":
        sink_tag = "github_token_write"
        mechanism_tags = (*mechanism_tags, "permissions")
    if relation_type == "can_request_oidc_token":
        mechanism_tags = (*mechanism_tags, "oidc")
    if relation_type == "oidc_may_assume_cloud_identity":
        mechanism_tags = (*mechanism_tags, "oidc")

    traversable = bool(
        source_tag or mechanism_tags or sink_tag or relation_type in _PLUMBING_RELATIONS
    )
    return RelationSemantics(
        traversable=traversable,
        source_tag=source_tag,
        mechanism_tags=tuple(dict.fromkeys(mechanism_tags)),
        sink_tag=sink_tag,
        context_bridge=relation_type in _CONTEXT_BRIDGES,
    )
