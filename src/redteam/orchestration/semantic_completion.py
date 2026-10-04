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
  result, action and invocation linkage) while other steps or other
  workers origins are allowed when the origin exists in this run.
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

from redteam.core.contracts.mission import MissionCriterionResult
from redteam.core.enums import EvidenceSourceType, MissionStatus, TaskStatus
from redteam.core.ids import new_id
from redteam.orchestration.completion import validate_completion
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.evidence_access import raw_from_metadata
from redteam.orchestration.work.models import ACTIVE_STATUSES

#: Evidence metadata marker for an accepted task completion receipt.
TASK_RECEIPT_KIND = "task_completion_receipt"

#: Characters kept per raw excerpt handed to the review call.
EXCERPT_CHARS = 2000

#: Cap on excerpts forwarded to one review call.
MAX_EXCERPTS = 8


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
    }


def verify_task_receipt(task: Any, receipt: dict[str, Any]) -> list[str]:
    """Check a stored receipt against the task current purpose.

    A bare mission revision drift never invalidates; a purpose change
    does. Returns error strings, empty when the receipt still binds.
    """
    if not isinstance(receipt, dict):
        return ["task receipt is missing"]
    if receipt.get("record_kind") != TASK_RECEIPT_KIND:
        return ["task receipt has an unknown kind"]
    if str(receipt.get("task_id", "")) != str(getattr(task, "task_id", "")):
        return ["task receipt belongs to another task"]
    expected = purpose_hash(getattr(task, "objective", "") or "")
    if str(receipt.get("purpose_hash", "")) != expected:
        return ["task purpose changed since the accepted receipt"]
    origins = [item for item in (receipt.get("source_evidence_ids", []) or []) if str(item).strip()]
    if not origins:
        return ["task receipt cites no origin evidence"]
    return []


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


async def _verify_task_origin(*, evidence_store: Any, run_id: str, evidence_id: str) -> tuple[Any | None, list[str]]:
    """Provenance check for one task-level origin record.

    Same run, preserved raw result, and action and invocation linkage stay
    mandatory. Path and step identity is not forced to the completing step:
    other steps or other workers origins pass when they exist in this run.
    """
    try:
        record = await evidence_store.get(evidence_id)
    except Exception as exc:  # noqa: BLE001 - store failure reads as unproven
        return None, ["evidence lookup failed: " + type(exc).__name__]
    if record is None:
        return None, ["evidence '" + str(evidence_id) + "' does not exist"]
    if _record_text(getattr(record, "run_id", "")) != str(run_id):
        return None, ["evidence '" + str(evidence_id) + "' belongs to a different run"]
    meta = getattr(record, "metadata", None) or {}
    if not isinstance(meta, dict):
        return None, ["evidence '" + str(evidence_id) + "' has unreadable metadata"]
    raw = raw_from_metadata(meta)
    if not isinstance(raw, dict):
        return None, ["evidence '" + str(evidence_id) + "' has no preserved raw result"]
    action_ref = _record_text(getattr(record, "action_id", None)).strip()
    invocation_ref = _record_text(getattr(record, "invocation_id", None)).strip()
    if not action_ref or not invocation_ref:
        return None, ["evidence '" + str(evidence_id) + "' needs action and invocation linkage"]
    if _record_text(raw.get("action_id", "")).strip() != action_ref:
        return None, ["evidence '" + str(evidence_id) + "' raw result answers another action"]
    if _record_text(raw.get("invocation_id", "")).strip() != invocation_ref:
        return None, ["evidence '" + str(evidence_id) + "' raw result answers another invocation"]
    stored_proposal = meta.get("proposal")
    if isinstance(stored_proposal, dict):
        stored_action = _record_text(stored_proposal.get("action_id", "")).strip()
        if stored_action and stored_action != action_ref:
            return None, ["evidence '" + str(evidence_id) + "' stored proposal answers another action"]
    return record, []


def _excerpt_of(record: Any, *, limit: int = EXCERPT_CHARS) -> dict[str, Any]:
    meta = getattr(record, "metadata", None) or {}
    raw = raw_from_metadata(meta) if isinstance(meta, dict) else None
    try:
        rendered = json.dumps(raw, ensure_ascii=False, default=str)
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


def _review_payload_common(*, purpose: str, conditions: list[str], excerpts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "purpose": str(purpose or ""),
        "conditions": list(conditions or []),
        "evidence_excerpts": list(excerpts or [])[:MAX_EXCERPTS],
    }


async def _pending_execution_errors(engine: Any, runtime: Any) -> list[str]:
    try:
        history = await engine.deps.work_store.list_items(run_id=runtime.run_id)
    except Exception:
        history = []
    if any(getattr(work, "status", None) in ACTIVE_STATUSES for work in (history or [])):
        return ["unfinished execution records remain"]
    if getattr(runtime, "pending_approval_id", None):
        return ["an approval is still pending"]
    try:
        records = await engine.deps.evidence_store.list_for_run(runtime.run_id)
    except Exception:
        return []
    settled = {
        _record_text(getattr(record, "action_id", None))
        for record in (records or [])
        if isinstance(getattr(record, "metadata", None), dict)
        and isinstance(getattr(record, "metadata", {}).get("tool_result", {}), dict)
        and getattr(record, "metadata", {}).get("tool_result", {}).get("metadata", {}).get("execution_reconciled")
    }
    for record in records or []:
        meta = getattr(record, "metadata", None)
        raw = meta.get("tool_result") if isinstance(meta, dict) else None
        if not isinstance(raw, dict):
            continue
        if raw.get("metadata", {}).get("orchestration_exception"):
            error = raw.get("error") or {}
            if error.get("code") not in {"tool_binding_invalid", "lease_lost_before_execution"} and _record_text(getattr(record, "action_id", None)) not in settled:
                return ["an execution outcome is uncertain; collect its actual result or hold"]
    return []


def _receipt_record(*, run_id: Any, engagement_id: Any, payload: dict[str, Any], summary: str) -> Any:
    from redteam.core.contracts.evidence import EvidenceRecord

    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return EvidenceRecord(
        evidence_id=new_id("evidence"),
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


async def complete_task(engine: Any, path: Any, decision: Any, runtime: Any, context: Any, *, budget: Any = None) -> list[str]:
    """Close one delegated task on its original purpose and stored origins."""
    token = await engine._review_token(runtime)
    if getattr(runtime, "mission_id", None) is None or getattr(runtime, "active_task_id", None) is None:
        return ["task completion requires an active mission task"]
    task = await engine.deps.task_graph_store.get_task(runtime.active_task_id)
    if task is None or getattr(task, "mission_id", None) != runtime.mission_id or getattr(task, "status", None) is not TaskStatus.RUNNING:
        return ["task completion requires the current running task"]
    if _record_text(getattr(path, "mission_task_id", "")) != str(task.task_id):
        return ["task completion path is not bound to this mission task"]
    evidence_ids = select_task_evidence_ids(decision, path)
    if not evidence_ids:
        return ["task completion needs related stored evidence"]
    verified: list[Any] = []
    excerpt_notes: list[dict[str, Any]] = []
    for identity in evidence_ids:
        record, errors = await _verify_task_origin(evidence_store=engine.deps.evidence_store, run_id=str(runtime.run_id), evidence_id=identity)
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
        payload={**(context or {}), "task": task.model_dump(mode="json"), "path": path.model_dump(mode="json"), **_review_payload_common(purpose=getattr(task, "objective", "") or "", conditions=[], excerpts=excerpt_notes)},
        response_schema={"type": "object", "properties": {"accepted": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["accepted", "reason"], "additionalProperties": False},
        system=_engine_system(engine),
        budget=budget,
    )
    await engine.record_completion_review("task_completion_review", path, runtime, review, [str(getattr(record, "evidence_id", "")) for record in verified])
    if not review.get("accepted"):
        return ["semantic completion review rejected the evidence"]
    mission = await engine.deps.task_graph_store.get_mission(runtime.mission_id)
    revision = int(getattr(mission, "revision", 1) or 1)
    receipt_payload = build_task_receipt(task_id=str(task.task_id), mission_id=str(task.mission_id), run_id=str(runtime.run_id), purpose_hash_value=purpose_hash(getattr(task, "objective", "") or ""), mission_revision=revision, source_evidence_ids=[str(getattr(record, "evidence_id", "")) for record in verified], outcome="success", reason=str(review.get("reason", "")), path_id=getattr(path, "path_id", None), path_version=getattr(path, "version", None))
    receipt_record = _receipt_record(run_id=runtime.run_id, engagement_id=runtime.engagement_id, payload=receipt_payload, summary=str(review.get("reason", "")))
    async def complete(change: Any) -> None:
        staged = await change.tasks.get_task(task.task_id)
        if staged is None:
            raise ValueError("completion_task_missing")
        updated = staged.model_copy(update={"completion_evidence_id": receipt_record.evidence_id, "updated_at": datetime.now(timezone.utc)})
        await change.tasks.update_task(updated)
        await change.tasks.transition_task(task.task_id, TaskStatus.COMPLETED, now=datetime.now(timezone.utc))
    # The receipt is immutable evidence; authority still comes only from
    # the atomic task transition below, never from the receipt alone.
    await engine.deps.evidence_store.append(receipt_record)
    await engine._commit_review(runtime, token, complete)
    return []


async def _legacy_task_proof(engine: Any, *, runtime: Any, task: Any, run_id: str) -> list[str]:
    """Read a receipt-less completed task only through the stored path proof."""
    latest = None
    try:
        listed = await engine.deps.path_store.list_paths(str(run_id))
    except Exception:
        return ["completed required task lacks a readable path proof"]
    for saved in listed or []:
        if getattr(saved, "mission_task_id", None) != str(task.task_id) or getattr(saved, "mission_id", None) != str(runtime.mission_id):
            continue
        if latest is None or getattr(saved, "version", 0) > getattr(latest, "version", 0):
            latest = saved
    if latest is None:
        return ["completed required task lacks an accepted evidence-bearing path"]
    from redteam.attack_graph.service import AttackGraphService
    service = AttackGraphService(engine.deps.graph_store)
    for step in list(getattr(latest, "steps", []) or []):
        if not getattr(step, "required", False):
            continue
        if _record_text(getattr(step, "status", "")) != "completed":
            return ["completed task has unfinished path steps"]
        errors = await validate_completion(path=latest, step=step, proposal={"evidence_ids": list(getattr(step, "evidence_ids", []) or []), "outcome": getattr(step, "outcome", None) or "success", "reason": getattr(step, "objective", "") or ""}, evidence_store=engine.deps.evidence_store, graph_service=service, run_id=str(run_id), path_store=engine.deps.path_store)
        if errors:
            return errors
    return []


async def _goal_task_coverage(engine: Any, *, runtime: Any, tasks: list[Any]) -> tuple[list[str], dict[str, list[str]], list[dict[str, Any]]]:
    """Check required tasks and gather their bound origin excerpts.

    Returns (errors, task_evidence, excerpts). Receipt-less legacy tasks
    fall back to the stored path proof without minting new receipts.
    """
    task_evidence: dict[str, list[str]] = {}
    excerpts: list[dict[str, Any]] = []
    for task in tasks or []:
        if not getattr(task, "required", False):
            continue
        if getattr(task, "status", None) is not TaskStatus.COMPLETED:
            return (["required mission task '" + str(task.task_id) + "' is not completed"], {}, [])
        receipt_id = getattr(task, "completion_evidence_id", None)
        if receipt_id is None:
            errors = await _legacy_task_proof(engine, runtime=runtime, task=task, run_id=str(runtime.run_id))
            if errors:
                return (errors, {}, [])
            continue
        try:
            receipt_record = await engine.deps.evidence_store.get(receipt_id)
        except Exception:
            return (["task receipt lookup failed; treating as unproven"], {}, [])
        if receipt_record is None:
            return (["task receipt is missing"], {}, [])
        receipt_payload = getattr(receipt_record, "metadata", None) or {}
        if not isinstance(receipt_payload, dict):
            return (["task receipt is missing"], {}, [])
        errors = verify_task_receipt(task, receipt_payload)
        if errors:
            return (errors, {}, [])
        origins = [str(item).strip() for item in (receipt_payload.get("source_evidence_ids", []) or []) if str(item).strip()]
        if not origins:
            return (["task receipt cites no origin evidence"], {}, [])
        task_evidence[str(task.task_id)] = origins
        for identity in origins:
            try:
                origin = await engine.deps.evidence_store.get(identity)
            except Exception:
                return (["task origin lookup failed; treating as unproven"], {}, [])
            if origin is None or _record_text(getattr(origin, "run_id", "")) != str(runtime.run_id):
                return (["task origin is missing or belongs to another run"], {}, [])
            # Failed tool outcomes stay readable as general purpose
            # evidence here; the review call weighs them with the excerpt.
            excerpts.append(_excerpt_of(origin))
            if len(excerpts) >= MAX_EXCERPTS:
                break
        if len(excerpts) >= MAX_EXCERPTS:
            break
    return ([], task_evidence, excerpts)


async def complete_goal(engine: Any, path: Any, decision: Any, runtime: Any, context: Any, *, budget: Any = None) -> list[str]:
    """Settle the original mission on its own criteria and required tasks."""
    token = await engine._review_token(runtime)
    if getattr(runtime, "mission_id", None) is None:
        return ["goal completion requires an operator mission"]
    mission = await engine.deps.task_graph_store.get_mission(runtime.mission_id)
    if mission is None:
        return ["goal completion requires an operator mission"]
    criteria = list(getattr(mission, "success_criteria", []) or [])
    if not criteria:
        return ["goal has no original operator criteria"]
    pending = await _pending_execution_errors(engine, runtime)
    if pending:
        return pending
    tasks = await engine.deps.task_graph_store.list_tasks(mission_id=runtime.mission_id)
    errors, task_evidence, excerpts = await _goal_task_coverage(engine, runtime=runtime, tasks=tasks)
    if errors:
        return errors
    try:
        from redteam.attack_graph.service import AttackGraphService
        cleanup = await AttackGraphService(engine.deps.graph_store).cleanup_report()
        if getattr(cleanup, "outstanding", None):
            return ["required cleanup is still outstanding"]
    except Exception:
        pass
    await engine._check_review_path(runtime, path)
    review = await engine._ask(
        stage="goal_completion_review",
        payload={**(context or {}), "mission": mission.model_dump(mode="json"), "path": path.model_dump(mode="json"), **_review_payload_common(purpose=getattr(mission, "objective", "") or "", conditions=criteria, excerpts=excerpts)},
        response_schema={"type": "object", "properties": {"accepted": {"type": "boolean"}, "reason": {"type": "string"}, "criteria": {"type": "array", "items": {"type": "object", "properties": {"criterion": {"type": "string"}, "met": {"type": "boolean"}, "evidence_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["criterion", "met", "evidence_ids"], "additionalProperties": False}}}, "required": ["accepted", "reason", "criteria"], "additionalProperties": False},
        system=_engine_system(engine),
        budget=budget,
    )
    await engine.record_completion_review("goal_completion_review", path, runtime, review, [identity for origins in task_evidence.values() for identity in origins])
    if not review.get("accepted"):
        return ["semantic completion review rejected the evidence"]
    # No new goal prefix cases: the review must cover the original
    # operator criteria exactly.
    items = review.get("criteria", [])
    table = {item.get("criterion"): item for item in items if isinstance(item, dict)}
    if len(items) != len(criteria) or set(table) != set(criteria):
        return ["goal review must cover the original operator criteria exactly"]
    bound_tasks: dict[str, list[str]] = {}
    for item in items:
        if not item.get("met") or not item.get("evidence_ids"):
            return ["goal criterion lacks evidence"]
        owners: set[str] = set()
        for identity in item.get("evidence_ids", []):
            owner = next((str(task_id) for task_id, origins in task_evidence.items() if identity in origins), "")
            if not owner:
                return ["goal criterion cites unrelated evidence"]
            owners.add(owner)
        if not owners:
            return ["goal criterion evidence lacks validated task ownership"]
        bound_tasks[item.get("criterion")] = sorted(owners)
    now = datetime.now(timezone.utc)
    async def complete(change: Any) -> None:
        for task in tasks:
            if getattr(task, "required", False) and getattr(task, "status", None) is TaskStatus.RUNNING:
                await change.tasks.transition_task(task.task_id, TaskStatus.COMPLETED, now=now)
        await change.tasks.update_mission(mission.model_copy(update={"status": MissionStatus.COMPLETED, "updated_at": now, "revision": int(getattr(mission, "revision", 1) or 1) + 1, "criterion_results": [MissionCriterionResult(criterion=item.get("criterion"), met=True, evidence_ids=list(item.get("evidence_ids", [])), task_ids=list(bound_tasks.get(item.get("criterion"), [])), reason=str(review.get("reason", ""))) for item in items]}))
        change.binding(str(runtime.run_id), str(runtime.mission_id))["closed"] = True
    await engine._commit_review(runtime, token, complete)
    return []


__all__ = ["TASK_RECEIPT_KIND", "EXCERPT_CHARS", "MAX_EXCERPTS", "purpose_hash", "build_task_receipt", "verify_task_receipt", "select_task_evidence_ids", "complete_task", "complete_goal"]
