"""Consume structured provider resource effects grounded in preserved output.

Providers can describe created/cleaned accounts, objects or sessions using the
existing ToolResult metadata. Arbitrary output/prose is never searched for
creation, and the model cannot call these writes as a local cleanup tool.
"""

import hashlib
import json
from datetime import datetime

from redteam.attack_graph.predicates import Provenance, cleanup_of
from redteam.core.contracts.tool import ToolResourceEffect, execution_outcome_uncertain
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphNodeId
from redteam.orchestration.coordination import StateConflict

ResourceEffect = ToolResourceEffect


async def unmapped_resource_effects(evidence_store, executions):
    """Absent declarations are unknown, not proof that no resource was created."""
    unmapped = set()
    for execution in executions:
        if execution.result_evidence_id is None:
            continue
        record = await evidence_store.get(execution.result_evidence_id)
        original = record.metadata.get("original_result", {}) if record is not None else {}
        if not isinstance(original, dict):
            unmapped.add(str(execution.tool_id))
            continue
        metadata = original.get("metadata", {})
        if not isinstance(metadata, dict):
            unmapped.add(str(execution.tool_id))
            continue
        if metadata.get("execution_started") is False:
            continue
        effects = metadata.get("resource_effects")
        if not isinstance(effects, list):
            unmapped.add(str(execution.tool_id))
            continue
        try:
            if any(ResourceEffect.model_validate(raw).version != 2 for raw in effects):
                unmapped.add(str(execution.tool_id))
        except ValueError:
            unmapped.add(str(execution.tool_id))
    return sorted(unmapped)


def read_path(output, path):
    value = output
    for part in path:
        if isinstance(value, dict) and isinstance(part, str):
            value = value[part]
        elif isinstance(value, list) and type(part) is int and 0 <= part < len(value):
            value = value[part]
        else:
            raise ValueError("resource_effect_output_path_unproven")
    return value


def resource_node_id(effect, output, run_id):
    if effect.version != 2 or not effect.identity_domain:
        raise ValueError("resource_identity_domain_unproven")
    identity = read_path(output, effect.identity_path)
    confirmed = read_path(output, effect.confirmation_path)
    if type(identity) not in {str, int} or not str(identity).strip() or confirmed is not True:
        raise ValueError("resource_effect_not_confirmed_by_output")
    digest = hashlib.sha256(
        json.dumps([2, str(run_id), effect.identity_domain, effect.resource_type,
                    type(identity).__name__, identity], ensure_ascii=True).encode()
    ).hexdigest()
    return GraphNodeId("resource:" + digest)


def removal_after_creation(result, created_at):
    try:
        creation = (created_at if isinstance(created_at, datetime)
                    else datetime.fromisoformat(str(created_at).replace("Z", "+00:00")))
        return (creation.tzinfo is not None and result.started_at.tzinfo is not None
                and result.started_at >= creation)
    except (ValueError, TypeError):
        return False


async def cleanup_originals(deps, runtime, cleanup):
    """Validate terminal cleanup against retained creation/removal outputs."""
    from redteam.attack_graph.cleanup import CleanupStatus
    from redteam.core.contracts.tool import ToolResult
    from redteam.orchestration.central_state import check_evidence, check_execution_receipt

    errors, checked = [], []
    executions = {str(item.result_evidence_id): item
                  for item in await deps.central_state.list_executions() if item.result_evidence_id}
    for item in cleanup.items:
        if item.is_outstanding:
            continue
        if item.status is not CleanupStatus.CLEANED:
            errors.append("cleanup_exemption_requires_operator_verification")
            continue
        for kind, identity in (("created", item.created_evidence_id),
                               ("cleaned", item.cleanup_evidence_id)):
            execution = executions.get(str(identity))
            if execution is None or item.run_id != str(runtime.run_id):
                errors.append("cleanup_original_execution_missing")
                continue
            originals = await check_evidence(
                deps.evidence_store, run_id=str(runtime.run_id),
                engagement_id=str(runtime.engagement_id), evidence_ids=[str(identity)],
            )
            record = originals[0].record
            task = await deps.central_state.get_task(execution.task_id)
            check_execution_receipt(execution, record, result=True, task=task)
            result = ToolResult.model_validate(record.metadata["original_result"])
            if kind == "created" and item.created_at != result.finished_at:
                errors.append("cleanup_creation_time_unproven")
                continue
            if kind == "cleaned" and not removal_after_creation(result, item.created_at):
                errors.append("cleanup_creation_generation_unproven")
                continue
            if ((kind == "cleaned" and not result.success)
                    or execution_outcome_uncertain(result.metadata)
                    or result.metadata.get("development_double") or result.metadata.get("mock")
                    or result.metadata.get("fixture")):
                errors.append("cleanup_original_success_unproven")
                continue
            found = False
            for raw in result.metadata.get("resource_effects", []):
                try:
                    effect = ResourceEffect.model_validate(raw)
                    node_id = resource_node_id(effect, result.output, runtime.run_id)
                except (ValueError, KeyError, IndexError, TypeError):
                    errors.append("cleanup_resource_identity_unverified")
                    continue
                if (effect.effect == kind and effect.resource_type == item.resource_type
                        and node_id == item.node_id):
                    found = True
            if not found or (kind == "created" and str(result.action_id)
                             != item.created_by_action_id):
                errors.append("cleanup_original_effect_unproven")
            checked.extend(originals)
    return errors, checked


async def apply_resource_effects(service, *, result, runtime, evidence_id):
    """Called on original results during the existing prepared graph write."""
    effects = result.metadata.get("resource_effects", [])
    if not isinstance(effects, list):
        raise StateConflict("resource_effect_contract_invalid")
    if not effects:
        return []
    if not evidence_id or result.metadata.get("development_double") or (
        result.metadata.get("mock") or result.metadata.get("fixture")
    ):
        raise StateConflict("resource_effect_requires_real_original")
    applied = []
    for raw in effects:
        try:
            effect = ResourceEffect.model_validate(raw)
            if effect.version != 2:
                raise StateConflict("resource_identity_domain_unproven")
            if read_path(result.output, effect.confirmation_path) is False:
                continue  # Explicit failure/no creation never closes an obligation.
            node_id = resource_node_id(effect, result.output, runtime.run_id)
        except (ValueError, KeyError, IndexError, TypeError):
            raise StateConflict("resource_effect_original_output_unproven") from None
        if effect.effect == "created":
            existing = await service.get_node(node_id)
            cleanup = cleanup_of(existing.properties) if existing else None
            reopen = bool(cleanup and cleanup.get("created_by_action_id") != str(result.action_id))
            await service.upsert_node(
                GraphNodeType.RESOURCE, node_id=node_id,
                facts={"resource_type": effect.resource_type,
                       "identity_domain": effect.identity_domain, "resource_effect_version": 2},
                provenance=Provenance(run_id=str(runtime.run_id), evidence_id=str(evidence_id)),
                step=runtime.step_count,
            )
            item = await service.register_created_resource(
                node_id, resource_type=effect.resource_type, action_id=str(result.action_id),
                evidence_id=str(evidence_id), run_id=str(runtime.run_id), step=runtime.step_count,
                created_at=result.finished_at,
                reopen=reopen,
            )
        else:
            if not result.success or execution_outcome_uncertain(result.metadata):
                raise StateConflict("cleanup_terminal_success_unproven")
            existing = await service.get_node(node_id)
            cleanup = cleanup_of(existing.properties) if existing else None
            if cleanup and not removal_after_creation(result, cleanup.get("created_at")):
                raise StateConflict("cleanup_creation_generation_unproven")
            item = await service.mark_cleaned(node_id, evidence_id=str(evidence_id))
        if item is None:
            raise StateConflict("cleanup_original_resource_missing")
        applied.append(str(node_id))
    return applied
