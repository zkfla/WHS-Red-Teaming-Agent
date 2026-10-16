"""Validate stored assessment references without judging their security meaning."""

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.assessment import AssessmentDraft
from redteam.core.contracts.mission import redact_secret_material
from redteam.core.enums import EvidenceSourceType
from redteam.core.evidence_paths import resolve_output_field
from redteam.orchestration.central_state import check_evidence
from redteam.orchestration.completion import check_record_linkage
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.evidence_access import raw_from_metadata, record_view


async def validate_assessments(deps, runtime, task, result, *, checked=None):
    """Inspect the same-run originals, permitted subjects and redacted field coordinates.

    The owning Task/result supplies Run, Task and author lineage. No model-owned
    attribution or statement here grants a verdict, permission or measured fact.
    Other same-run Tasks may supply evidence; existing source lineage is not rewritten.
    """
    if not result.assessment_drafts:
        return
    if task.investigation_contract_version != 2:
        raise StateConflict("assessment_requires_result_v2")
    if checked is None:
        checked = await check_evidence(
            deps.evidence_store, run_id=str(runtime.run_id),
            engagement_id=str(runtime.engagement_id),
            evidence_ids=result.assessment_evidence_ids(),
        )
    records = {str(item.record.evidence_id): item.record for item in checked}
    graph = AttackGraphService(deps.graph_store)
    from redteam.orchestration.analysis import claim_target_is_grounded

    for draft in result.assessment_drafts:
        if draft.target_node_id not in set(map(str, task.related_attack_graph_node_ids)):
            raise StateConflict("assessment_target_outside_task")
        if set(draft.principal_refs) - set(task.principal_refs):
            raise StateConflict("assessment_principal_outside_task")
        if draft.hypothesis_id and draft.hypothesis_id not in set(
            map(str, task.related_hypothesis_ids)
        ):
            raise StateConflict("assessment_hypothesis_outside_task")
        for evidence_id in draft.original_evidence_ids():
            record = records.get(evidence_id)
            raw = raw_from_metadata(record.metadata) if record else None
            if record is None or record.source_type is not EvidenceSourceType.TOOL:
                raise StateConflict("assessment_observation_requires_tool_original")
            if not isinstance(raw, dict) or check_record_linkage(
                record, record.metadata, raw, "reference",
            ):
                raise StateConflict("assessment_original_linkage_invalid")
            if not await claim_target_is_grounded(
                graph, record, draft.target_node_id, evidence_store=deps.evidence_store,
            ):
                raise StateConflict("assessment_source_target_unproven")
        for evidence_id in draft.limitation_refs:
            record = records.get(evidence_id)
            if record is None:
                raise StateConflict("assessment_limitation_source_missing")
            if record.source_type is EvidenceSourceType.TOOL:
                raw = raw_from_metadata(record.metadata)
                if not isinstance(raw, dict) or check_record_linkage(
                    record, record.metadata, raw, "reference",
                ):
                    raise StateConflict("assessment_original_linkage_invalid")
            elif record_view(record)[0] == "unsupported":
                raise StateConflict("assessment_limitation_source_unsupported")
            meta = record.metadata
            if meta.get("mission_id") and str(meta["mission_id"]) != str(task.mission_id):
                raise StateConflict("assessment_source_mission_mismatch")
            if meta.get("target_node_id") and str(meta["target_node_id"]) not in set(
                map(str, task.related_attack_graph_node_ids)
            ):
                raise StateConflict("assessment_source_target_outside_task")
        pointers = [ref for item in draft.observations for ref in item.sources]
        pointers.extend(draft.expectation.evidence_refs)
        for ref in pointers:
            raw = raw_from_metadata(records[ref.evidence_id].metadata)
            # Coordinates refer only to safe stored text, never a raw secret or a
            # model-supplied quotation. No extracted body is copied into the draft.
            output = redact_secret_material(raw.get("output"))
            span = ref.text_span
            field = resolve_output_field(
                output, ref.raw_path,
                text_offset=span.offset if span else None,
                text_length=span.length if span else None,
            )
            if field.status == "field_unavailable":
                raise StateConflict("assessment_source_path_missing")
            if field.status != "ok":
                raise StateConflict("assessment_" + field.status)


def merge_assessments(previous, incoming):
    """An immutable draft ID may replay; an edited draft needs a different ID."""
    retained = {draft.draft_id: draft for draft in previous}
    for draft in incoming:
        normalized = AssessmentDraft.model_validate(redact_secret_material(
            draft.model_dump(mode="json"),
        ))
        if normalized.draft_id in retained and retained[normalized.draft_id] != normalized:
            raise StateConflict("original_result_assessment_changed")
        retained[normalized.draft_id] = normalized
    return list(retained.values())
