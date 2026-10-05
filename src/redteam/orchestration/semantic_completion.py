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
from redteam.core.contracts.mission import (
    MissionCriterionResult,
    MissionTask,
    redact_secret_material,
)
from redteam.core.enums import EvidenceSourceType, MissionStatus, TaskStatus
from redteam.core.ids import EvidenceId, TaskId, new_id
from redteam.orchestration.completion import (
    check_record_linkage,
    validate_completion,
    verify_stored_origin,
)
from redteam.orchestration.completion_proposal import (
    CompletionProposal,
    record_unresolved,
    validate_unresolved,
)
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.evidence_access import raw_from_metadata
from redteam.orchestration.result_claims import (
    RESULT_REVIEW_INSTRUCTION,
    ResultClaim,
    ResultReview,
    check_result_review,
    mission_result_purpose,
)
from redteam.orchestration.work.models import WorkStatus

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
    }


def verify_task_receipt(
    task: MissionTask,
    receipt: dict[str, Any],
    *,
    run_id: str | None = None,
    mission_id: str | None = None,
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
    if receipt.get("outcome") != "success":
        return ["task receipt does not prove successful completion"]
    expected = purpose_hash(task.objective or "")
    if str(receipt.get("purpose_hash", "")) != expected:
        return ["task purpose changed since the accepted receipt"]
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
    return []


async def verify_single_origin(
    *,
    evidence_store: Any,
    path_store: Any,
    run_id: str,
    engagement_id: str | None,
    mission_id: str | None,
    evidence_id: str,
    require_proposal: bool = True,
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
    )
    if origin_errors:
        return None, ["task origin '" + str(evidence_id) + "' " + origin_errors[0]]
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
) -> tuple[Any | None, list[str]]:
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
        return None, ["evidence lookup failed: " + type(exc).__name__]
    if record is None:
        return None, ["evidence '" + str(evidence_id) + "' does not exist"]
    if _record_text(getattr(record, "run_id", "")) != str(run_id):
        return None, ["evidence '" + str(evidence_id) + "' belongs to a different run"]
    if engagement_id and _record_text(getattr(record, "engagement_id", "")) != str(engagement_id):
        return None, ["evidence '" + str(evidence_id) + "' belongs to a different engagement"]
    meta = getattr(record, "metadata", None) or {}
    if not isinstance(meta, dict):
        return None, ["evidence '" + str(evidence_id) + "' has unreadable metadata"]
    raw = raw_from_metadata(meta)
    if not isinstance(raw, dict):
        return None, ["evidence '" + str(evidence_id) + "' has no preserved raw result"]
    linkage = check_record_linkage(record, meta, raw, str(evidence_id), require_proposal=True)
    if linkage:
        return None, [linkage[0][0]]
    origin_errors = await verify_stored_origin(
        path_store=path_store,
        run_id=str(run_id),
        m_path_id=_record_text(meta.get("path_id", "")).strip(),
        m_version=meta.get("path_version", None),
        m_step_id=_record_text(meta.get("path_step_id", "")).strip(),
        mission_id=mission_id,
    )
    if origin_errors:
        return None, ["evidence '" + str(evidence_id) + "' " + origin_errors[0]]
    target_ref = _record_text(meta.get("target_node_id", "")).strip()
    if not target_ref:
        return None, ["evidence '" + str(evidence_id) + "' has no target linkage"]
    related_nodes: set[str] = set()
    if task is not None:
        for node in list(getattr(task, "related_attack_graph_node_ids", []) or []):
            cleaned = str(node).strip()
            if cleaned:
                related_nodes.add(cleaned)
    if related_nodes and target_ref not in related_nodes:
        return None, ["evidence '" + str(evidence_id) + "' targets an object outside this task"]
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
        return None, ["evidence '" + str(evidence_id) + "' uses a principal outside this task"]
    return record, []


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
    *, purpose: str, conditions: list[str], excerpts: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "purpose": str(purpose or ""),
        "conditions": list(conditions or []),
        "evidence_excerpts": list(excerpts or [])[:MAX_EXCERPTS],
    }


async def _pending_execution_errors(engine: Any, runtime: Any) -> list[str]:
    try:
        history = await engine.deps.work_store.list_items(run_id=runtime.run_id)
    except Exception:
        return ["execution history lookup failed; treating as unproven"]
    if any(
        work.status in {WorkStatus.LEASED, WorkStatus.RUNNING}
        or (work.status is WorkStatus.PENDING and work.job_id is not None)
        for work in (history or [])
    ):
        return ["unfinished execution records remain"]
    if getattr(runtime, "pending_approval_id", None):
        return ["an approval is still pending"]
    try:
        records = await engine.deps.evidence_store.list_for_run(runtime.run_id)
    except Exception:
        return ["task evidence lookup failed; treating as unproven"]
    settled = {
        _record_text(getattr(record, "action_id", None))
        for record in (records or [])
        if isinstance(getattr(record, "metadata", None), dict)
        and isinstance(getattr(record, "metadata", {}).get("tool_result", {}), dict)
        and getattr(record, "metadata", {})
        .get("tool_result", {})
        .get("metadata", {})
        .get("execution_reconciled")
    }
    for record in records or []:
        meta = getattr(record, "metadata", None)
        raw = meta.get("tool_result") if isinstance(meta, dict) else None
        if not isinstance(raw, dict):
            continue
        if raw.get("metadata", {}).get("orchestration_exception"):
            error = raw.get("error") or {}
            if (
                error.get("code") not in {"tool_binding_invalid", "lease_lost_before_execution"}
                and _record_text(getattr(record, "action_id", None)) not in settled
            ):
                return ["an execution outcome is uncertain; collect its actual result or hold"]
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
    task = await engine.deps.task_graph_store.get_task(runtime.active_task_id)
    if (
        task is None
        or getattr(task, "mission_id", None) != runtime.mission_id
        or getattr(task, "status", None) is not TaskStatus.RUNNING
    ):
        return ["task completion requires the current running task"]
    if _record_text(getattr(path, "mission_task_id", "")) != str(task.task_id):
        return ["task completion path is not bound to this mission task"]
    raw_proposal = getattr(decision, "completion", {})
    if raw_proposal:
        proposal = CompletionProposal.model_validate({
            **raw_proposal, "reason": raw_proposal.get("reason") or decision.reason,
        })
        if proposal.unresolved:
            return await settle_unresolved(
                engine, path, proposal, runtime, token=token, task=task,
            )
    evidence_ids = select_task_evidence_ids(decision, path)
    if not evidence_ids:
        return ["task completion needs related stored evidence"]
    verified: list[Any] = []
    excerpt_notes: list[dict[str, Any]] = []
    for identity in evidence_ids:
        record, errors = await _verify_task_origin(
            evidence_store=engine.deps.evidence_store,
            path_store=getattr(getattr(engine, "deps", None), "path_store", None),
            run_id=str(runtime.run_id),
            engagement_id=str(getattr(runtime, "engagement_id", "") or ""),
            evidence_id=identity,
            task=task,
            mission_id=getattr(runtime, "mission_id", None),
            step_principal_scope=None,
        )
        if errors:
            return errors
        if record is not None:
            verified.append(record)
            excerpt_notes.append(_excerpt_of(record))
    if not verified:
        return ["task completion needs related stored evidence"]
    pending = await _pending_execution_errors(engine, runtime)
    if pending:
        return pending
    await engine._check_review_path(runtime, path)
    review = await engine._ask(
        stage="task_completion_review",
        payload={
            **(context or {}),
            "task": task.model_dump(mode="json"),
            "path": path.model_dump(mode="json"),
            **_review_payload_common(
                purpose=getattr(task, "objective", "") or "", conditions=[], excerpts=excerpt_notes
            ),
        },
        response_schema=ResultReview.model_json_schema(),
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
        return ["semantic completion review rejected the evidence"]
    from redteam.attack_graph.service import AttackGraphService

    claims = list(review.get("claims", []))
    service = AttackGraphService(engine.deps.graph_store)
    errors = await check_result_review(
        claims=claims,
        evidence_ids=evidence_ids,
        evidence_store=engine.deps.evidence_store,
        graph_service=service,
        path_store=engine.deps.path_store,
        run_id=str(runtime.run_id),
        mission_id=str(runtime.mission_id),
        target_node_ids=list(task.related_attack_graph_node_ids),
    )
    if errors:
        return errors
    mission = await engine.deps.task_graph_store.get_mission(runtime.mission_id)
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
    )
    receipt_payload["claims"] = claims
    receipt_record = _receipt_record(
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        payload=receipt_payload,
        summary=str(review.get("reason", "")),
    )

    async def complete(change: Any) -> None:
        staged = await change.tasks.get_task(task.task_id)
        if staged is None:
            raise ValueError("completion_task_missing")
        updated = staged.model_copy(
            update={
                "completion_evidence_id": receipt_record.evidence_id,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        await change.tasks.update_task(updated)
        await change.tasks.transition_task(
            task.task_id, TaskStatus.COMPLETED, now=datetime.now(timezone.utc)
        )

    # The receipt is immutable evidence; authority still comes only from
    # the atomic task transition below, never from the receipt alone.
    await engine.deps.evidence_store.append(receipt_record)
    await engine._commit_review(runtime, token, complete)
    return []


async def settle_unresolved(
    engine: Any, path: Any, proposal: CompletionProposal, runtime: Any, *,
    token: Any, task: Any = None, finish_investigation: bool = False,
) -> list[str]:
    """End an investigation unit without granting a success/refutation receipt."""
    pending = await _pending_execution_errors(engine, runtime)
    if pending:
        return pending
    targets = list(task.related_attack_graph_node_ids) if task is not None else list(
        dict.fromkeys(target for step in path.steps for target in step.target_node_ids)
    )
    errors = await validate_unresolved(
        proposal, evidence_store=engine.deps.evidence_store, runtime=runtime,
        target_node_ids=targets,
    )
    if errors:
        return errors
    await engine._check_review_path(runtime, path)
    identity = await record_unresolved(
        evidence_store=engine.deps.evidence_store, runtime=runtime, proposal=proposal,
        path=path, task_id=str(task.task_id) if task is not None else None,
    )
    now = datetime.now(timezone.utc)

    async def block(change: Any) -> None:
        if task is not None:
            await change.tasks.transition_task(
                task.task_id, TaskStatus.BLOCKED, now=now,
                reason=proposal.reason,
            )
        for step in path.steps:
            if step.status in {"pending", "running"}:
                await change.paths.update_step(
                    str(runtime.run_id), path.path_id, path.version, step.step_id,
                    "blocked", outcome=proposal.outcome,
                    evidence_ids=list(dict.fromkeys([*step.evidence_ids, identity])),
                )
        if finish_investigation:
            mission = await change.tasks.get_mission(runtime.mission_id)
            if mission is None:
                raise ValueError("unresolved_mission_missing")
            for unfinished in await change.tasks.list_tasks(mission_id=runtime.mission_id):
                if unfinished.status in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING}:
                    await change.tasks.transition_task(
                        unfinished.task_id, TaskStatus.BLOCKED, now=now,
                        reason=proposal.reason,
                    )
            await change.tasks.update_mission(mission.model_copy(update={
                "status": MissionStatus.BLOCKED, "updated_at": now,
            }))

    await engine._commit_review(runtime, token, block)
    runtime.unresolved_evidence_ids = list(dict.fromkeys([
        *runtime.unresolved_evidence_ids, identity,
    ]))
    from redteam.orchestration.delegation_decision import mark_finished

    runtime.delegations = {
        key: mark_finished(value, reason="investigation_unresolved")
        if value.path_id == path.path_id and value.path_version == path.version
        else value
        for key, value in runtime.delegations.items()
    }
    if finish_investigation:
        runtime.mission_status = MissionStatus.BLOCKED
    engine.updated_runtime = engine._event_runtime = runtime
    return []


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
    token = await engine._review_token(runtime)
    if getattr(runtime, "mission_id", None) is None:
        return ["goal completion requires an operator mission"]
    mission = await engine.deps.task_graph_store.get_mission(runtime.mission_id)
    if mission is None:
        return ["goal completion requires an operator mission"]
    raw_proposal = getattr(decision, "completion", {})
    if raw_proposal:
        proposal = CompletionProposal.model_validate({
            **raw_proposal, "reason": raw_proposal.get("reason") or decision.reason,
        })
        if proposal.unresolved:
            return await settle_unresolved(
                engine, path, proposal, runtime, token=token, finish_investigation=True,
            )
    criteria = list(getattr(mission, "success_criteria", []) or [])
    if not criteria:
        return ["goal has no original operator criteria"]
    pending = await _pending_execution_errors(engine, runtime)
    if pending:
        return pending
    tasks = await engine.deps.task_graph_store.list_tasks(mission_id=runtime.mission_id)
    errors, task_evidence, excerpts = await _goal_task_coverage(
        engine, runtime=runtime, tasks=tasks
    )
    if errors:
        return errors
    try:
        from redteam.attack_graph.service import AttackGraphService

        cleanup = await AttackGraphService(engine.deps.graph_store).cleanup_report()
        if getattr(cleanup, "outstanding", None):
            return ["required cleanup is still outstanding"]
    except Exception:
        return ["cleanup state lookup failed; treating as unproven"]
    await engine._check_review_path(runtime, path)
    review = await engine._ask(
        stage="goal_completion_review",
        payload={
            **(context or {}),
            "mission": mission.model_dump(mode="json"),
            "path": path.model_dump(mode="json"),
            **_review_payload_common(
                purpose=getattr(mission, "objective", "") or "",
                conditions=criteria,
                excerpts=excerpts,
            ),
        },
        response_schema=_GoalReview.model_json_schema(),
        system=_engine_system(engine) + RESULT_REVIEW_INSTRUCTION,
        budget=budget,
    )
    await engine.record_completion_review(
        "goal_completion_review",
        path,
        runtime,
        review,
        [identity for origins in task_evidence.values() for identity in origins],
    )
    if not review.get("accepted"):
        return ["semantic completion review rejected the evidence"]
    # No new goal prefix cases: the review must cover the original
    # operator criteria exactly.
    items = review.get("criteria", [])
    table = {item.get("criterion"): item for item in items if isinstance(item, dict)}
    if len(items) != len(criteria) or set(table) != set(criteria):
        return ["goal review must cover the original operator criteria exactly"]
    bound_tasks: dict[str, list[str]] = {}
    for criterion_index, item in enumerate(items):
        if not item.get("met") or not item.get("evidence_ids"):
            return ["goal criterion lacks evidence"]
        owners: set[str] = set()
        for identity in item.get("evidence_ids", []):
            owner = next(
                (str(task_id) for task_id, origins in task_evidence.items() if identity in origins),
                "",
            )
            if not owner:
                return ["goal criterion cites unrelated evidence"]
            owners.add(owner)
        if not owners:
            return ["goal criterion evidence lacks validated task ownership"]
        errors = await check_result_review(
            claims=item.get("claims", []),
            claims_location=("criteria", criterion_index, "claims"),
            evidence_ids=item["evidence_ids"],
            evidence_store=engine.deps.evidence_store,
            graph_service=AttackGraphService(engine.deps.graph_store),
            path_store=engine.deps.path_store,
            run_id=str(runtime.run_id),
            mission_id=str(runtime.mission_id),
        )
        if errors:
            return errors
        bound_tasks[item.get("criterion")] = sorted(owners)
    now = datetime.now(timezone.utc)

    async def complete(change: Any) -> None:
        for task in tasks:
            if (
                getattr(task, "required", False)
                and getattr(task, "status", None) is TaskStatus.RUNNING
            ):
                await change.tasks.transition_task(task.task_id, TaskStatus.COMPLETED, now=now)
        await change.tasks.update_mission(
            mission.model_copy(
                update={
                    "status": MissionStatus.COMPLETED,
                    "updated_at": now,
                    "revision": int(getattr(mission, "revision", 1) or 1) + 1,
                    "criterion_results": [
                        MissionCriterionResult(
                            criterion=item.get("criterion"),
                            met=True,
                            evidence_ids=list(item.get("evidence_ids", [])),
                            task_ids=[
                                TaskId(identity)
                                for identity in bound_tasks.get(item.get("criterion"), [])
                            ],
                            reason=str(review.get("reason", "")),
                            result_claims=list(item.get("claims", [])),
                            purpose_hash=mission_result_purpose(mission),
                        )
                        for item in items
                    ],
                }
            )
        )
        change.binding(str(runtime.run_id), str(runtime.mission_id))["closed"] = True

    await engine._commit_review(runtime, token, complete)
    return []


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
