"""Task and goal completion grounded in original objectives and stored evidence.

Root engine completion entries call the shared helpers here as thin
wrappers: complete_task(engine, path, decision, runtime, context,
budget=None) and complete_goal(...). The helpers reuse the engine review
call, review token and atomic commit path, so the engine body needs no
change to adopt them.

Rules followed:

- The authority is the original delegated purpose (the task own
  objective) and the original operator result conditions (the mission
  success criteria). A provisional plan required-step count never
  decides completion.
- Explicit evidence ids from the decision win; step evidence is the
  fallback. Provenance is checked per record (same run, preserved raw
  result, action/invocation/tool/proposal linkage) while other steps or
  other workers origins are allowed when the origin exists in this run.
  Cross-step path identity is never rewritten onto the origin.
- Accepted task completions leave an immutable receipt in the original
  evidence area and bind the task receipt reference together with the
  status change in one atomic commit. A receipt alone never counts.
- Purpose-hash drift invalidates a receipt; a bare mission revision bump
  does not. Legacy completed tasks without a receipt are only read
  through the existing path proof, never backfilled silently.
- Goal settlement checks the original criteria and the required mission
  tasks (with cleanup). New natural-language conditions use the existing
  review call; no new goal prefix cases are added.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.completion import (
    completion_conditions,
    completion_contract_hash,
    criterion_id,
)
from redteam.core.contracts.mission import (
    MissionTask,
    redact_secret_material,
)
from redteam.core.enums import EvidenceSourceType, TaskStatus
from redteam.core.ids import EvidenceId, new_id
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.completion import (
    CompletionIssues,
    check_record_linkage,
    preserved_target_is_absent,
    validate_completion,
    verify_stored_origin,
)
from redteam.orchestration.completion_proposal import (
    CompletionProposal,
)
from redteam.orchestration.context_projection import (
    review_path_reference,
)
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.evidence_access import raw_from_metadata
from redteam.orchestration.result_claims import (
    RESULT_REVIEW_INSTRUCTION,
    CriterionReview,
    ResultClaim,
    ResultReview,
    TaskContractReview,
    capture_result_graph,
    check_result_review,
)
from redteam.orchestration.task_completion_contract import check_task_conditions

#: Evidence metadata marker for an accepted task completion receipt.
TASK_RECEIPT_KIND = "task_completion_receipt"

#: Characters kept per raw excerpt handed to the review call.
EXCERPT_CHARS = 2000

#: Cap on excerpts forwarded to one review call.
MAX_EXCERPTS = 8


class _GoalCriterionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion: str
    met: bool
    evidence_ids: list[str]
    claims: list[ResultClaim]


class _GoalReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: bool
    reason: str
    criteria: list[_GoalCriterionReview]


def _engine_system(engine: Any) -> str:
    try:
        from redteam.orchestration.engine import SYSTEM as engine_system
    except Exception:
        return ""
    return engine_system


async def _publish_settlement(
    engine: Any, runtime: Any, event_type: str, payload: dict[str, Any]
) -> Any | None:
    """Announce one accepted settlement on the engine event chain.

    The semantic owner publishes exactly once per accepted settlement,
    citing the engine last event so legacy and native callers share one
    chain. Callers thread engine.last_event_id back into their own state.
    """
    bus = getattr(getattr(engine, "deps", None), "event_bus", None)
    if bus is None:
        return None
    publisher = EventPublisher(
        bus,
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        source="orchestration.semantic_completion",
    )
    event = await publisher.publish(
        event_type,
        dict(payload),
        causation_id=getattr(engine, "last_event_id", None),
    )
    engine.last_event_id = event.event_id
    return event


def purpose_hash(objective: str) -> str:
    """Stable hash of the delegated purpose text at review time."""
    return content_hash({"objective": (objective or "").strip()})


def build_task_receipt(
    *,
    task_id: str,
    mission_id: str,
    run_id: str,
    purpose_hash_value: str,
    mission_revision: int,
    source_evidence_ids: list[str],
    outcome: str,
    reason: str,
    path_id: str | None = None,
    path_version: int | None = None,
    related_attack_graph_node_ids: list[str] | None = None,
    related_hypothesis_ids: list[str] | None = None,
    claims: list[dict[str, Any]] | None = None,
    completion_contract_hash_value: str | None = None,
    criterion_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Public receipt payload stored as immutable evidence metadata."""
    return {
        "record_kind": TASK_RECEIPT_KIND,
        "task_id": str(task_id),
        "mission_id": str(mission_id),
        "run_id": str(run_id),
        "purpose_hash": str(purpose_hash_value),
        "mission_revision": int(mission_revision),
        "source_evidence_ids": list(dict.fromkeys(map(str, source_evidence_ids or []))),
        "outcome": str(outcome or "success"),
        "reason": str(reason or "")[:500],
        "path_id": str(path_id) if path_id else None,
        "path_version": path_version,
        "related_attack_graph_node_ids": sorted(
            {str(item) for item in (related_attack_graph_node_ids or []) if str(item).strip()}
        ),
        "related_hypothesis_ids": sorted(
            {str(item) for item in (related_hypothesis_ids or []) if str(item).strip()}
        ),
        "claims": list(claims or []),
        **({
            "completion_contract_version": 2,
            "completion_contract_hash": completion_contract_hash_value,
            "criterion_results": list(criterion_results or []),
        } if completion_contract_hash_value is not None else {}),
    }


def verify_task_receipt(
    task: MissionTask,
    receipt: dict[str, Any],
    *,
    run_id: str | None = None,
    mission_id: str | None = None,
    accepted_outcomes: tuple[str, ...] = ("success",),
) -> list[str]:
    """Check a stored receipt against the task current purpose.

    A bare mission revision drift never invalidates; a purpose change
    does. Optional run and mission binding is enforced when supplied.
    Returns error strings, empty when the receipt still binds.
    """
    if not isinstance(receipt, dict):
        return ["task receipt is missing"]
    if receipt.get("record_kind") != TASK_RECEIPT_KIND:
        return ["task receipt has an unknown kind"]
    if str(receipt.get("task_id", "")) != str(task.task_id):
        return ["task receipt belongs to another task"]
    if run_id is not None and str(receipt.get("run_id", "")) != str(run_id):
        return ["task receipt belongs to another run"]
    if mission_id is not None and str(receipt.get("mission_id", "")) != str(mission_id):
        return ["task receipt belongs to another mission"]
    if receipt.get("outcome") not in accepted_outcomes:
        return ["task receipt does not prove successful completion"]
    expected = purpose_hash(task.objective or "")
    if str(receipt.get("purpose_hash", "")) != expected:
        return ["task purpose changed since the accepted receipt"]
    if task.completion_contract_version == 2 and (
        receipt.get("completion_contract_version") != 2
        or receipt.get("completion_contract_hash") != completion_contract_hash(task)
    ):
        return ["task completion contract changed or v2 receipt is missing"]
    for key, current in (
        ("related_attack_graph_node_ids", list(task.related_attack_graph_node_ids or [])),
        ("related_hypothesis_ids", list(task.related_hypothesis_ids or [])),
    ):
        if key in receipt:
            stored = receipt.get(key)
            if not isinstance(stored, list) or sorted(map(str, stored)) != sorted(
                map(str, current)
            ):
                return ["task subjects changed since the accepted receipt"]
    origins = [item for item in (receipt.get("source_evidence_ids", []) or []) if str(item).strip()]
    if not origins:
        return ["task receipt cites no origin evidence"]
    if task.completion_contract_version == 2:
        try:
            reviews = [CriterionReview.model_validate(row)
                       for row in receipt.get("criterion_results", [])]
        except (ValueError, TypeError):
            return ["task receipt criterion results are invalid"]
        expected_ids = {criterion_id(c) for c in task.completion_criteria}
        if (len(reviews) != len(expected_ids)
                or {row.criterion_id for row in reviews} != expected_ids
                or any(not row.met or set(row.evidence_ids) - set(origins) for row in reviews)):
            return ["task receipt does not prove each fixed criterion"]
    return []


def _cites_declared_source(task: Any, meta: dict[str, Any]) -> bool:
    """A follow-up task may cite its own already-verified source task's evidence.

    ``task.source_task_id``/``source_result_id`` is validated once, at Task
    registration (hypothesis_planner.bind_decision / probe_atomization.bind_plan):
    the cited result must already exist on the named source task's stored
    ``MissionTask.result``. Citing that same source task's evidence again here
    -- to review or carry forward its observation -- reads the original, it does
    not change its recorded purpose. This never widens to "any task": only the
    exact task_id this Task declared (and had verified) as its source, matched
    against the evidence's own recorded originating task, is accepted. A task
    with no declared source_task_id gets no exception; the purpose hash must
    still match exactly.
    """
    source_task_id = _record_text(getattr(task, "source_task_id", "") or "").strip()
    if not source_task_id:
        return False
    origin_task_id = _record_text(meta.get("mission_task_id", "") or "").strip()
    return bool(origin_task_id) and origin_task_id == source_task_id


async def verify_single_origin(
    *,
    evidence_store: Any,
    path_store: Any,
    run_id: str,
    engagement_id: str | None,
    mission_id: str | None,
    evidence_id: str,
    require_proposal: bool = True,
    task: Any | None = None,
    task_graph_store: Any | None = None,
    work_store: Any | None = None,
 ) -> tuple[Any | None, list[str]]:
    """Prove one stored origin record against the actual execution.

    Fetch, same run and engagement, preserved raw result, shared
    action/invocation/tool/proposal linkage, and stored path origin under
    the same mission. Returns (record, errors); lookups never succeed on
    failure. Goal and terminal settlement share this single proof.
    """
    try:
        record = await evidence_store.get(evidence_id)
    except Exception:
        return None, ["task origin lookup failed; treating as unproven"]
    if record is None or _record_text(getattr(record, "run_id", "")) != str(run_id):
        return None, ["task origin is missing or belongs to another run"]
    if engagement_id is not None and _record_text(getattr(record, "engagement_id", "")) != str(
        engagement_id
    ):
        return None, ["task origin belongs to another engagement"]
    meta = getattr(record, "metadata", None) or {}
    if not isinstance(meta, dict):
        return None, ["task origin '" + str(evidence_id) + "' has unreadable metadata"]
    raw = raw_from_metadata(meta)
    if not isinstance(raw, dict):
        return None, ["task origin '" + str(evidence_id) + "' has no preserved raw result"]
    linkage = check_record_linkage(
        record, meta, raw, str(evidence_id), require_proposal=require_proposal
    )
    if linkage:
        return None, [linkage[0][0]]
    origin_errors = await verify_stored_origin(
        path_store=path_store,
        run_id=str(run_id),
        m_path_id=_record_text(meta.get("path_id", "")).strip(),
        m_version=meta.get("path_version", None),
        m_step_id=_record_text(meta.get("path_step_id", "")).strip(),
        mission_id=mission_id,
        metadata=meta,
        record=record,
        task_graph_store=task_graph_store,
        work_store=work_store,
    )
    if origin_errors:
        return None, ["task origin '" + str(evidence_id) + "' " + origin_errors[0]]
    if task is not None:
        expected_hash = _record_text(meta.get("task_purpose_hash", "") or "").strip()
        current_hash = (completion_contract_hash(task)
                        if meta.get("central_execution_version") == 1
                        else purpose_hash(_record_text(getattr(task, "objective", ""))))
        if expected_hash and expected_hash != current_hash and not _cites_declared_source(
            task, meta,
        ):
            message = "task origin '" + str(evidence_id) + "' purpose changed since the evidence"
            return None, [message]
    return record, []


async def verify_receipt_bound_origins(
    *,
    evidence_store: Any,
    path_store: Any,
    run_id: str,
    engagement_id: str | None,
    mission_id: str | None,
    task: MissionTask,
    graph_service: Any | None = None,
    task_graph_store: Any | None = None,
    work_store: Any | None = None,
 ) -> tuple[list[str], list[str]]:
    """Shared receipt-to-origin proof for one completed required task.

    Checks the receipt record itself (same run and engagement, system
    source, payload shape) and the current purpose binding, then re-proves
    every cited origin with the shared single-origin proof. Goal
    settlement and terminal settlement consume this same helper, so their
    receipt authority cannot drift apart. Returns (errors, proven ids).
    """
    receipt_id = task.completion_evidence_id
    try:
        receipt_record = await evidence_store.get(receipt_id)
    except Exception:
        return (["task receipt lookup failed; treating as unproven"], [])
    if receipt_record is None:
        return (["task receipt is missing"], [])
    if _record_text(getattr(receipt_record, "run_id", "")) != str(run_id):
        return (["task receipt belongs to another run"], [])
    if engagement_id is not None and _record_text(
        getattr(receipt_record, "engagement_id", "")
    ) != str(engagement_id):
        return (["task receipt belongs to another engagement"], [])
    if getattr(receipt_record, "source_type", None) is not EvidenceSourceType.SYSTEM:
        return (["task receipt is not system evidence"], [])
    receipt_payload = getattr(receipt_record, "metadata", None) or {}
    if not isinstance(receipt_payload, dict):
        return (["task receipt is missing"], [])
    errors = verify_task_receipt(
        task,
        receipt_payload,
        run_id=str(run_id),
        mission_id=None if mission_id is None else str(mission_id),
    )
    if errors:
        return (errors, [])
    origins = [
        str(item).strip()
        for item in (receipt_payload.get("source_evidence_ids", []) or [])
        if str(item).strip()
    ]
    if not origins:
        return (["task receipt cites no origin evidence"], [])
    proven: list[str] = []
    for identity in origins:
        _, origin_errors = await verify_single_origin(
            evidence_store=evidence_store,
            path_store=path_store,
            run_id=str(run_id),
            engagement_id=engagement_id,
            mission_id=mission_id,
            evidence_id=identity,
            require_proposal=True,
            task=task,
            task_graph_store=task_graph_store,
            work_store=work_store,
        )
        if origin_errors:
            return (origin_errors, [])
        proven.append(identity)
    errors = await check_result_review(
        claims=receipt_payload.get("claims", []),
        evidence_ids=proven,
        evidence_store=evidence_store,
        graph_service=graph_service,
        path_store=path_store,
        run_id=str(run_id),
        mission_id=mission_id,
        target_node_ids=list(task.related_attack_graph_node_ids),
        task=task,
        task_graph_store=task_graph_store,
        work_store=work_store,
    )
    if errors:
        return (errors, [])
    return ([], proven)


def select_task_evidence_ids(decision: Any, path: Any) -> list[str]:
    """Explicit decision evidence first, step evidence as fallback.

    The provisional required-step count is never consulted here.
    """
    completion = getattr(decision, "completion", None)
    if isinstance(decision, dict):
        completion = decision.get("completion", completion)
    explicit: list[str] = []
    if isinstance(completion, dict):
        raw = completion.get("evidence_ids", [])
        if isinstance(raw, str):
            raw = [raw]
        try:
            explicit = [str(item).strip() for item in list(raw or []) if str(item).strip()]
        except TypeError:
            explicit = []
    if explicit:
        return list(dict.fromkeys(explicit))
    collected: list[str] = []
    for step in list(getattr(path, "steps", []) or []):
        for identity in list(getattr(step, "evidence_ids", []) or []):
            text = str(identity).strip()
            if text and text not in collected:
                collected.append(text)
    return collected


def _record_text(value: Any) -> str:
    return "" if value is None else str(value)


async def _verify_task_origin(
    *,
    evidence_store: Any,
    path_store: Any,
    run_id: str,
    engagement_id: str,
    evidence_id: str,
    task: Any,
    mission_id: str | None = None,
    step_principal_scope: set[str] | None = None,
    task_graph_store: Any | None = None,
    work_store: Any | None = None,
    graph_service: Any | None = None,
 ) -> tuple[Any | None, list[str], dict[str, str]]:
    """Task-level origin check: actual linkage, stored origin, relevance.

    Readonly and purpose-free: no purpose hash is needed here and nothing
    is written. Other steps or other workers origins pass when stored in
    this run under the same mission. Declared task targets and subjects
    are enforced; the review call weighs the rest with the excerpts. The
    stored proposal is required: the execution path always stores it, so
    cross-step admission loses nothing by demanding it.
    """
    try:
        record = await evidence_store.get(evidence_id)
    except Exception as exc:  # noqa: BLE001 - store failure reads as unproven
        return None, ["evidence lookup failed: " + type(exc).__name__], {}
    if record is None:
        return None, ["evidence '" + str(evidence_id) + "' does not exist"], {}
    if _record_text(getattr(record, "run_id", "")) != str(run_id):
        return None, ["evidence '" + str(evidence_id) + "' belongs to a different run"], {}
    if engagement_id and _record_text(getattr(record, "engagement_id", "")) != str(engagement_id):
        return None, ["evidence '" + str(evidence_id) + "' belongs to a different engagement"], {}
    meta = getattr(record, "metadata", None) or {}
    if not isinstance(meta, dict):
        return None, ["evidence '" + str(evidence_id) + "' has unreadable metadata"], {}
    raw = raw_from_metadata(meta)
    if not isinstance(raw, dict):
        return None, ["evidence '" + str(evidence_id) + "' has no preserved raw result"], {}
    linkage = check_record_linkage(record, meta, raw, str(evidence_id), require_proposal=True)
    if linkage:
        return None, [linkage[0][0]], {}
    origin_errors = await verify_stored_origin(
        path_store=path_store,
        run_id=str(run_id),
        m_path_id=_record_text(meta.get("path_id", "")).strip(),
        m_version=meta.get("path_version", None),
        m_step_id=_record_text(meta.get("path_step_id", "")).strip(),
        mission_id=mission_id,
        metadata=meta,
        record=record,
        task_graph_store=task_graph_store,
        work_store=work_store,
    )
    if origin_errors:
        return None, ["evidence '" + str(evidence_id) + "' " + origin_errors[0]], {}
    task_hash = _record_text(meta.get("task_purpose_hash", "") or "").strip()
    if task_hash and task is not None:
        expected_hash = (completion_contract_hash(task)
                         if meta.get("central_execution_version") == 1
                         else purpose_hash(_record_text(getattr(task, "objective", ""))))
        if task_hash != expected_hash and not _cites_declared_source(task, meta):
            message = "evidence '" + str(evidence_id) + "' purpose changed since the evidence"
            return None, [message], {}
    target_ref = _record_text(meta.get("target_node_id", "")).strip()
    if not target_ref and not preserved_target_is_absent(meta):
        return None, ["evidence '" + str(evidence_id) + "' has no target linkage"], {}
    related_nodes: set[str] = set()
    if task is not None:
        for node in list(getattr(task, "related_attack_graph_node_ids", []) or []):
            cleaned = str(node).strip()
            if cleaned:
                related_nodes.add(cleaned)
    edge_proof: dict[str, str] = {}
    if target_ref and related_nodes and target_ref not in related_nodes:
        from redteam.orchestration.analysis import grounding_edge_for

        for node in sorted(related_nodes):
            edge = await grounding_edge_for(graph_service, record, node)
            if edge is not None:
                edge_proof[str(edge.id)] = content_hash(edge)
                break
        if not edge_proof:
            message = "evidence '" + str(evidence_id) + "' targets an object outside this task"
            return None, [message], {}
    principals: set[str] = set()
    single = meta.get("principal_ref")
    if isinstance(single, str) and single.strip():
        principals.add(single.strip())
    multi = meta.get("principal_refs")
    if isinstance(multi, (list, tuple, set)):
        for item in multi:
            cleaned = str(item).strip()
            if cleaned:
                principals.add(cleaned)
    lowered = {item.lower() for item in principals}
    anonymous = meta.get("anonymous") is True or "anonymous" in lowered
    scope = {str(item).strip() for item in (step_principal_scope or set()) if str(item).strip()}
    if scope and principals and not anonymous and principals.isdisjoint(scope):
        return None, ["evidence '" + str(evidence_id) + "' uses a principal outside this task"], {}
    return record, [], edge_proof


def _excerpt_of(record: Any, *, limit: int = EXCERPT_CHARS) -> dict[str, Any]:
    meta = getattr(record, "metadata", None) or {}
    raw = raw_from_metadata(meta) if isinstance(meta, dict) else None
    try:
        rendered = json.dumps(
            redact_secret_material(raw, drop_secret_keys=True),
            ensure_ascii=False,
            default=str,
        )
    except Exception:
        rendered = _record_text(raw)
    kept = max(1, int(limit))
    return {
        "evidence_id": _record_text(getattr(record, "evidence_id", "")),
        "source_type": _record_text(getattr(record, "source_type", "")),
        "success": (meta.get("success") if isinstance(meta, dict) else None),
        "target_node_id": (meta.get("target_node_id") if isinstance(meta, dict) else None),
        "excerpt": rendered[:kept],
        "truncated": len(rendered) > kept,
    }


def _review_payload_common(
    *, purpose: str, conditions: list[Any], excerpts: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "purpose": str(purpose or ""),
        "conditions": list(conditions or []),
        "evidence_excerpts": list(excerpts or [])[:MAX_EXCERPTS],
    }


#: Review guidance attached to every completion review payload. The review
#: call receives every proven origin id, but shown contents cover only some
#: of them. The reviewer must never describe an unshown id as read or quote
#: content for it; such ids count only as proven references through the
#: ownership map and the receipt binding.
REVIEW_EXCERPT_NOTE = (
    "evidence_excerpts and requested_evidence hold the stored contents actually "
    "shown to this review. verified_source_ids can name more proven origins than "
    "the shown contents cover. Never describe an evidence id without shown content "
    "as read, and never quote or paraphrase content for it; unshown ids count only "
    "as proven references through the task_evidence ownership map and the receipt "
    "binding, never as observed results."
)


def _task_review_subject(task: MissionTask) -> dict[str, Any]:
    """Minimal task identity for the review call: purpose and origin subjects.

    The full task record stays in the store; the reviewer gets the delegated
    purpose plus the subject bindings the origin checks already enforced.
    """
    return {
        "task_id": str(task.task_id),
        "mission_id": str(task.mission_id),
        "objective": str(task.objective or ""),
        "required": bool(task.required),
        "completion_contract_version": task.completion_contract_version,
        "completion_contract_hash": completion_contract_hash(task),
        "completion_criteria": completion_conditions(task),
        "principal_refs": list(task.principal_refs),
        "related_attack_graph_node_ids": [
            str(item) for item in (task.related_attack_graph_node_ids or [])
        ],
        "related_hypothesis_ids": [
            str(item) for item in (task.related_hypothesis_ids or [])
        ],
    }


def _mission_review_subject(mission: Any, criteria: list[str]) -> dict[str, Any]:
    """Original operator contract for the goal review: criteria and constraints.

    The full mission record stays in the store; the reviewer judges the
    original success criteria under the original operator constraints.
    """
    constraints = getattr(mission, "operator_constraints", {}) or {}
    return {
        "mission_id": str(getattr(mission, "mission_id", "")),
        "success_criteria": list(criteria),
        "operator_constraints": dict(constraints) if isinstance(constraints, dict) else {},
    }


def _task_review_payload(
    *,
    task: MissionTask,
    path: Any,
    context: Any,
    evidence_ids: list[str],
    excerpts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the task review payload: subjects, ownership and shown contents.

    The full task record and the full path stay in their stores. Every proven
    origin id is named even when the excerpt cap trims the shown contents.
    """
    return {
        **context,
        "task": _task_review_subject(task),
        "active_path": review_path_reference(path),
        **_review_payload_common(
            purpose=getattr(task, "objective", "") or "",
            conditions=completion_conditions(task),
            excerpts=excerpts,
        ),
        "verified_source_ids": list(evidence_ids),
        "task_evidence": {str(task.task_id): list(evidence_ids)},
        "review_note": REVIEW_EXCERPT_NOTE,
    }


def _goal_review_payload(
    *,
    mission: Any,
    criteria: list[str],
    path: Any,
    context: Any,
    task_evidence: dict[str, list[str]],
    excerpts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the goal review payload: original contract plus ownership map.

    The full mission record and the full path stay in their stores. The
    task_evidence map names every proven origin per owning task even when the
    excerpt cap trims the shown contents.
    """
    verified = [identity for origins in task_evidence.values() for identity in origins]
    return {
        **context,
        "mission": _mission_review_subject(mission, criteria),
        "active_path": review_path_reference(path),
        **_review_payload_common(
            purpose=getattr(mission, "objective", "") or "",
            conditions=criteria,
            excerpts=excerpts,
        ),
        "verified_source_ids": verified,
        "task_evidence": {
            str(task_id): list(origins) for task_id, origins in task_evidence.items()
        },
        "review_note": REVIEW_EXCERPT_NOTE,
    }


async def _pending_execution_errors(
    engine: Any,
    runtime: Any,
    *,
    scope: str = "task",
    task: Any = None,
    mission_task_id: Any = None,
    all_instances: bool = False,
) -> list[str]:
    """Scoped execution gate: task scope checks own instance/task only."""
    goal_scope = bool(all_instances or scope == "goal")
    resolved_task_id: str | None = None
    if mission_task_id is not None and str(mission_task_id).strip():
        resolved_task_id = str(mission_task_id)
    elif task is not None:
        tid = getattr(task, "task_id", None)
        if tid is not None and str(tid).strip():
            resolved_task_id = str(tid)
    else:
        active = getattr(runtime, "active_task_id", None)
        if active is not None and str(active).strip():
            resolved_task_id = str(active)
    from redteam.orchestration.central_completion import unsafe

    history = await engine.deps.central_state.list_executions(
        task_id=None if goal_scope else resolved_task_id,
    )
    if any(unsafe(item) for item in history):
        return CompletionIssues.failure(
            ["unfinished execution records remain"], code="completion_execution_wait",
            summary="Original executions still require approval, job collection or recovery.",
            cause_scope="policy_wait", owner="orchestration.central_runtime",
            repair_condition="Resume only when authoritative ledger recovery permits it; "
                             "preserve original invocation identities and UNKNOWN protection.",
        )
    return []


def _receipt_record(
    *, run_id: Any, engagement_id: Any, payload: dict[str, Any], summary: str
) -> Any:
    from redteam.core.contracts.evidence import EvidenceRecord

    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return EvidenceRecord(
        evidence_id=EvidenceId(new_id("evidence")),
        run_id=run_id,
        engagement_id=engagement_id,
        source_type=EvidenceSourceType.SYSTEM,
        source_id="orchestration.task_completion_receipt",
        content_type="application/json",
        digest=hashlib.sha256(body.encode()).hexdigest(),
        summary=str(summary or "")[:280],
        created_at=datetime.now(timezone.utc),
        metadata=dict(payload),
    )


async def complete_task(
    engine: Any, path: Any, decision: Any, runtime: Any, context: Any, *, budget: Any = None
) -> list[str]:
    """Close one delegated task on its original purpose and stored origins."""
    token = await engine._review_token(runtime)
    if (
        getattr(runtime, "mission_id", None) is None
        or getattr(runtime, "active_task_id", None) is None
    ):
        return ["task completion requires an active mission task"]
    investigations = getattr(engine.deps, "investigations", engine.deps.task_graph_store)
    task = await investigations.get_task(runtime.active_task_id)
    if (
        task is None
        or getattr(task, "mission_id", None) != runtime.mission_id
        or getattr(task, "status", None) is not TaskStatus.RUNNING
    ):
        return ["task completion requires the current running task"]
    if path is not None:
        if _record_text(getattr(path, "mission_task_id", "")) != str(task.task_id):
            return ["task completion path is not bound to this mission task"]
    # The public API must bind the same reasoning guard/audit usage as native settlement.
    engine._event_runtime = runtime
    raw_proposal = getattr(decision, "completion", {})
    if raw_proposal:
        proposal = CompletionProposal.model_validate({
            **raw_proposal, "reason": raw_proposal.get("reason") or decision.reason,
        })
        if proposal.unresolved:
            return await settle_unresolved(
                engine, path, proposal, runtime, token=token, task=task,
            )
    pending = await _pending_execution_errors(engine, runtime, scope="task", task=task)
    if pending:
        return pending
    evidence_ids = select_task_evidence_ids(decision, path)
    if not evidence_ids:
        return CompletionIssues.failure(
            ["task completion needs related stored evidence"], code="completion_evidence_required",
            summary="Cite related stored originals for the completion proposal.",
            cause_scope="model_input", retryable=True,
            repair_condition="Correct the proposal references or return an unresolved gap.",
        )
    from redteam.orchestration.evidence_validity import capture_validity

    validity_read, errors = await capture_validity(engine.deps, evidence_ids)
    if errors:
        return CompletionIssues.failure(
            errors, code="completion_validity_unproven",
            summary="Current evidence reuse conditions do not support completion.",
            cause_scope="evidence_validity", owner="orchestration.evidence_validity",
            repair_condition="Reassess the binding prerequisites; unbound is absence of a "
                             "binding, not a false observation. Rereading changes no binding.",
        )
    verified: list[Any] = []
    excerpt_notes: list[dict[str, Any]] = []
    task_edge_proof: dict[str, str] = {}
    for identity in evidence_ids:
        record, errors, proof = await _verify_task_origin(
            evidence_store=engine.deps.evidence_store,
            path_store=getattr(getattr(engine, "deps", None), "path_store", None),
            run_id=str(runtime.run_id),
            engagement_id=str(getattr(runtime, "engagement_id", "") or ""),
            evidence_id=identity,
            task=task,
            mission_id=getattr(runtime, "mission_id", None),
            step_principal_scope=None,
            task_graph_store=getattr(engine.deps, "investigations", engine.deps.task_graph_store),
            work_store=getattr(getattr(engine, "deps", None), "work_store", None),
            graph_service=AttackGraphService(engine.deps.graph_store),
        )
        if errors:
            return errors
        if record is not None:
            verified.append(record)
            excerpt_notes.append(_excerpt_of(record))
            task_edge_proof.update(proof)
    if not verified:
        return CompletionIssues.failure(
            ["task completion needs related stored evidence"], code="completion_evidence_required",
            summary="Cite related stored originals for the completion proposal.",
            cause_scope="model_input", retryable=True,
            repair_condition="Correct the proposal references or return an unresolved gap.",
        )
    contract_errors = await check_task_conditions(
        task, evidence_ids, deps=engine.deps, run_id=str(runtime.run_id),
    )
    if contract_errors:
        return contract_errors
    service = AttackGraphService(engine.deps.graph_store)
    graph_proof = await capture_result_graph(service, conditions=task.completion_criteria)
    pending = await _pending_execution_errors(engine, runtime, scope="task", task=task)
    if pending:
        return pending
    await engine._review_token(runtime)
    from redteam.orchestration.metered_llm import llm_request_scope

    with llm_request_scope(str(runtime.run_id), "task_completion_review"):
        review = await engine._ask(
            stage="task_completion_review",
            payload=_task_review_payload(
                task=task,
                path=path,
                context=await engine.review_context(runtime, context, evidence_ids),
                evidence_ids=evidence_ids,
                excerpts=excerpt_notes,
            ),
            response_schema=(
                TaskContractReview if task.completion_contract_version == 2 else ResultReview
            ).model_json_schema(),
            system=_engine_system(engine) + RESULT_REVIEW_INSTRUCTION,
            budget=budget,
        )
    await engine.record_completion_review(
        "task_completion_review",
        path,
        runtime,
        review,
        [str(getattr(record, "evidence_id", "")) for record in verified],
    )
    if not review.get("accepted"):
        return CompletionIssues.failure(
            ["semantic completion review rejected the evidence"], code="completion_review_rejected",
            summary="Independent review did not accept the proposed completion support.",
            cause_scope="target_prerequisite", owner="orchestration.semantic_completion",
            repair_condition="Preserve successful originals; return the unmet purpose as an "
                             "unresolved gap unless independent relevant support is available.",
        )
    contract_errors = await check_task_conditions(
        task, evidence_ids, deps=engine.deps, run_id=str(runtime.run_id), review=review,
    )
    if contract_errors:
        return contract_errors

    claims = list(review.get("claims", []))
    service = AttackGraphService(engine.deps.graph_store)
    for identity, expected in (await capture_result_graph(service, claims)).items():
        if identity in graph_proof and graph_proof[identity] != expected:
            return CompletionIssues.failure(
                ["completion_graph_proof_changed_reassessment_required"],
                code="completion_graph_changed", summary="Graph support changed during review.",
                cause_scope="evidence_validity", owner="orchestration.result_claims",
                repair_condition="Reassess changed support before proposing completion.",
            )
        graph_proof[identity] = expected
    errors = await check_result_review(
        claims=claims,
        evidence_ids=evidence_ids,
        evidence_store=engine.deps.evidence_store,
        graph_service=service,
        path_store=engine.deps.path_store,
        run_id=str(runtime.run_id),
        mission_id=str(runtime.mission_id),
        target_node_ids=list(task.related_attack_graph_node_ids),
        task=task,
        task_graph_store=getattr(engine.deps, "investigations", engine.deps.task_graph_store),
        work_store=getattr(getattr(engine, "deps", None), "work_store", None),
    )
    if errors:
        return errors
    mission = await investigations.get_mission(runtime.mission_id)
    validity_read, errors = await validity_read.refresh(engine.deps)
    if errors:
        return errors
    revision = int(getattr(mission, "revision", 1) or 1)
    receipt_payload = build_task_receipt(
        task_id=str(task.task_id),
        mission_id=str(task.mission_id),
        run_id=str(runtime.run_id),
        purpose_hash_value=purpose_hash(task.objective or ""),
        mission_revision=revision,
        source_evidence_ids=[str(getattr(record, "evidence_id", "")) for record in verified],
        outcome="success",
        reason=str(review.get("reason", "")),
        path_id=getattr(path, "path_id", None),
        path_version=getattr(path, "version", None),
        related_attack_graph_node_ids=list(task.related_attack_graph_node_ids or []),
        related_hypothesis_ids=list(task.related_hypothesis_ids or []),
        completion_contract_hash_value=(
            completion_contract_hash(task) if task.completion_contract_version == 2 else None
        ),
        criterion_results=review.get("criterion_results"),
    )
    receipt_payload["claims"] = claims
    receipt_record = _receipt_record(
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        payload=receipt_payload,
        summary=str(review.get("reason", "")),
    )

    # The receipt is immutable evidence; authority still comes only from
    # the atomic task transition below, never from the receipt alone.
    await engine.deps.evidence_store.append(receipt_record)
    from redteam.orchestration.central_results import complete_reviewed_result

    await complete_reviewed_result(
        engine.deps, runtime, task, token, receipt_record,
        CompletionProposal.model_validate(raw_proposal), validity_read=validity_read,
        graph_proof=graph_proof, edge_proof=task_edge_proof,
    )
    return []


async def settle_unresolved(
    engine: Any, path: Any, proposal: CompletionProposal, runtime: Any, *,
    token: Any, task: Any = None, finish_investigation: bool = False,
) -> list[str]:
    """Reuse the central unresolved result without a legacy global mutation."""
    if path is not None or task is None or finish_investigation:
        return ["unresolved settlement requires the current central investigation task"]
    from redteam.orchestration.central_results import complete_unresolved_result

    errors = await complete_unresolved_result(engine.deps, runtime, task, token, proposal)
    if not errors:
        engine.updated_runtime = engine._event_runtime = runtime
    return errors


async def verify_legacy_task_origins(
    *,
    evidence_store: Any,
    path_store: Any,
    graph_service: Any,
    mission_id: str,
    engagement_id: str | None,
    task: MissionTask,
    run_id: str,
) -> tuple[list[str], list[str]]:
    """Read a receipt-less completed task only through the stored path proof.

    Returns (errors, proven_evidence_ids). Active paths only; a path with
    no required steps proves nothing. A task that already has an accepted
    receipt in this run but carries no receipt reference changed purpose,
    so the legacy fallback must fail for it instead of minting coverage.
    Record lookups never succeed on failure.
    """
    try:
        listed = await path_store.list_paths(str(run_id))
    except Exception:
        return (["completed required task path proof lookup failed; treating as unproven"], [])
    from redteam.orchestration.paths import is_active_status as _is_active

    candidates = [
        saved
        for saved in (listed or [])
        if getattr(saved, "mission_task_id", None) == str(task.task_id)
        and getattr(saved, "mission_id", None) == str(mission_id)
        and _is_active(str(getattr(saved, "status", "")))
    ]
    if not candidates:
        return (["completed required task lacks an accepted evidence-bearing path"], [])
    latest = max(candidates, key=lambda saved: getattr(saved, "version", 0))
    if str(getattr(latest, "goal", "") or "").strip() != str(task.objective or "").strip():
        return (["completed task path proves an older purpose; its stored proof is obsolete"], [])
    try:
        run_records = await evidence_store.list_for_run(str(run_id))
    except Exception:
        return (["completed required task receipt history lookup failed; treating as unproven"], [])
    for rec in run_records or []:
        meta = getattr(rec, "metadata", None) or {}
        if (
            isinstance(meta, dict)
            and meta.get("record_kind") == TASK_RECEIPT_KIND
            and str(meta.get("task_id", "")) == str(task.task_id)
        ):
            return (
                [
                    "completed task purpose changed since its accepted receipt; "
                    "legacy path proof cannot substitute"
                ],
                [],
            )
    required_steps = [
        step
        for step in list(getattr(latest, "steps", []) or [])
        if getattr(step, "required", False)
    ]
    if not required_steps:
        return (["completed task path has no required steps to prove it"], [])
    proven: list[str] = []
    for step in required_steps:
        if _record_text(getattr(step, "status", "")) != "completed":
            return (["completed task has unfinished path steps"], [])
        step_ids = [
            str(item).strip()
            for item in list(getattr(step, "evidence_ids", []) or [])
            if str(item).strip()
        ]
        if not step_ids:
            return (["completed required task step cites no evidence"], [])
        errors = await validate_completion(
            path=latest,
            step=step,
            proposal={
                "evidence_ids": step_ids,
                "outcome": getattr(step, "outcome", None) or "success",
                "reason": getattr(step, "objective", "") or "",
            },
            evidence_store=evidence_store,
            graph_service=graph_service,
            run_id=str(run_id),
            path_store=path_store,
        )
        if errors:
            return (errors, [])
        for identity in step_ids:
            try:
                record = await evidence_store.get(identity)
            except Exception:
                return (["task origin lookup failed; treating as unproven"], [])
            if record is None or _record_text(getattr(record, "run_id", "")) != str(run_id):
                return (["task origin is missing or belongs to another run"], [])
            if engagement_id is not None and _record_text(
                getattr(record, "engagement_id", "")
            ) != str(engagement_id):
                return (["task origin belongs to another engagement"], [])
            if identity not in proven:
                proven.append(identity)
    if not proven:
        return (["completed required task has no proven origin evidence"], [])
    return ([], proven)


async def _goal_task_coverage(
    engine: Any, *, runtime: Any, tasks: list[MissionTask]
) -> tuple[list[str], dict[str, list[str]], list[dict[str, Any]]]:
    """Check every required task and gather their bound origin excerpts.

    Returns (errors, task_evidence, excerpts). Receipt-less legacy tasks
    fall back to the stored path proof without minting new receipts, and
    their proven ids join the same task map. Every required task is
    verified even after the excerpt cap is reached; the cap only trims
    the review payload, never the coverage itself. Record lookups never
    succeed on failure.
    """
    task_evidence: dict[str, list[str]] = {}
    excerpts: list[dict[str, Any]] = []
    engagement_id = getattr(runtime, "engagement_id", None)
    path_store = getattr(getattr(engine, "deps", None), "path_store", None)
    task_graph_store = getattr(getattr(engine, "deps", None), "task_graph_store", None)
    work_store = getattr(getattr(engine, "deps", None), "work_store", None)
    for task in tasks or []:
        if not task.required:
            continue
        if task.status is not TaskStatus.COMPLETED:
            return (["required mission task '" + str(task.task_id) + "' is not completed"], {}, [])
        receipt_id = task.completion_evidence_id
        if receipt_id is None:
            errors, proven = await verify_legacy_task_origins(
                evidence_store=engine.deps.evidence_store,
                path_store=path_store,
                graph_service=AttackGraphService(engine.deps.graph_store),
                mission_id=str(runtime.mission_id),
                engagement_id=engagement_id,
                task=task,
                run_id=str(runtime.run_id),
            )
            if errors:
                return (errors, {}, [])
            if not proven:
                return (["completed required task has no proven origin evidence"], {}, [])
            task_evidence[str(task.task_id)] = proven
            origins = proven
        else:
            errors, origins = await verify_receipt_bound_origins(
                evidence_store=engine.deps.evidence_store,
                path_store=path_store,
                run_id=str(runtime.run_id),
                engagement_id=engagement_id,
                mission_id=str(runtime.mission_id),
                task=task,
                graph_service=AttackGraphService(engine.deps.graph_store),
                task_graph_store=task_graph_store,
                work_store=work_store,
            )
            if errors:
                return (errors, {}, [])
            if not origins:
                return (["task receipt cites no origin evidence"], {}, [])
            task_evidence[str(task.task_id)] = origins
        for identity in origins:
            origin, origin_errors = await verify_single_origin(
                evidence_store=engine.deps.evidence_store,
                path_store=path_store,
                run_id=str(runtime.run_id),
                engagement_id=engagement_id,
                mission_id=str(runtime.mission_id),
                evidence_id=identity,
                require_proposal=True,
                task=task,
                task_graph_store=task_graph_store,
                work_store=work_store,
            )
            if origin_errors:
                return (origin_errors, {}, [])
            # Failed tool outcomes stay readable as general purpose
            # evidence here; the review call weighs them with the excerpt.
            # The cap only trims this payload; coverage above already ran.
            if len(excerpts) < MAX_EXCERPTS:
                excerpts.append(_excerpt_of(origin))
    return ([], task_evidence, excerpts)


async def complete_goal(
    engine: Any, path: Any, decision: Any, runtime: Any, context: Any, *, budget: Any = None
) -> list[str]:
    """Settle the original mission on its own criteria and required tasks."""
    from redteam.orchestration.central_completion import complete_goal as central_goal

    return await central_goal(engine.deps, runtime, budget=budget)


__all__ = [
    "TASK_RECEIPT_KIND",
    "EXCERPT_CHARS",
    "MAX_EXCERPTS",
    "purpose_hash",
    "build_task_receipt",
    "verify_task_receipt",
    "verify_single_origin",
    "verify_receipt_bound_origins",
    "select_task_evidence_ids",
    "complete_task",
    "complete_goal",
]

# Shared goal review contracts/payload, with the existing origin and claim
# verifiers. SQL settlement is owned by the selected state implementation.
GoalReview = _GoalReview
goal_review_payload = _goal_review_payload
origin_excerpt = _excerpt_of
