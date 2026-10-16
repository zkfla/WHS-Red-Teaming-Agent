"""Structured planner/task/worker view built only from stored originals.

Pure function of what ``graph_read`` loaded: no I/O, no model, no inference. A value the
stores do not hold is ``None`` (shown as unknown), never a guess. Layout is the client's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from redteam.app.cli.display import safe_text
from redteam.core.contracts.assessment import ReviewedFindingDetail
from redteam.core.contracts.execution import ExecutionState, TaskExecution
from redteam.core.contracts.graph import Finding, Hypothesis
from redteam.core.contracts.mission import MissionPlan, MissionTask
from redteam.orchestration.llm_requests import request_stage_usage, request_usage

PLANNER_OWNER = "specialist_planning"
PLANNER_OWNERS = frozenset({PLANNER_OWNER, "hypothesis_planning", "task_atomization"})
PLANNER_STAGES = frozenset({"hypothesis_planning", "task_atomization"})
REVIEW_OWNERS = frozenset({"task_completion_review", "goal_completion"})
_LIVE_REQUEST = frozenset({"reserved", "dispatched"})
_OPEN_EXECUTION = frozenset({ExecutionState.PENDING, ExecutionState.RUNNING})
_EVENT_KEYS = ("action_id", "mission_task_id", "task_id", "tool_id", "operation", "verdict",
               "approval_id", "evidence_id", "finding_id", "success", "error_code")
EVENT_LIMIT = 40
EVIDENCE_REF_LIMIT = 5
LLM_EXCHANGE_LIMIT = 80


@dataclass
class GraphRaw:
    run_id: str
    mission: MissionPlan | None
    tasks: list[MissionTask]
    executions: list[TaskExecution]
    activations: list[dict[str, Any]]
    llm_requests: list[dict[str, Any]]
    planner_judgment: dict[str, Any] | None
    hypotheses: list[Hypothesis]
    evidence_count: int
    event_counts: dict[str, int]
    finding_count: int = 0
    verified_finding_count: int = 0
    candidate_finding_promotions: list[dict[str, Any]] = field(default_factory=list)
    candidate_review_applications: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)  # newest first
    budget_remaining: int | None = None


def _text(value: Any, limit: int) -> str | None:
    return safe_text(value, limit=limit) if value not in (None, "") else None


def _live(requests: list[dict[str, Any]], owner: str) -> bool:
    return any(r.get("owner") == owner and r.get("state") in _LIVE_REQUEST for r in requests)


def _usage(requests: list[dict[str, Any]]) -> dict[str, Any]:
    spent = [r for r in requests if r.get("state") in {"settled", "unknown"}]
    return {
        "requests": len(spent),
        "in_flight": sum(r.get("state") in _LIVE_REQUEST for r in requests),
        "known_tokens": sum(r.get("total_tokens") or 0 for r in spent),
        "unknown_usage": sum(r.get("total_tokens") is None for r in spent),
        "unknown_outcome": sum(r.get("state") == "unknown" for r in spent),
        "usage": request_usage(requests)["usage"],
    }


def _decision_usage(requests: list[dict[str, Any]]) -> dict[str, int]:
    classified = request_usage(requests)
    return {
        "external_tool_decisions": classified["external_tool_decisions"],
        "local_reads": classified["local_reads"],
        "repairs": classified["repairs"],
        "transport_retries": classified["attempts"]["transport_retry"],
        "provider_failures": classified["provider_failures"],
        "settlements": classified["settlements"],
        "text_only_held": classified["text_only_held"],
    }


def _optional_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _optional_status(value: Any) -> int | None:
    return value if type(value) is int and 100 <= value <= 599 else None


def _optional_delay(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _llm_response_state(request: dict[str, Any]) -> str:
    state = request.get("state")
    if state == "reserved":
        return "not_sent"
    if state == "released":
        return "cancelled"
    if state == "dispatched":
        return "waiting"
    if state == "settled":
        return "received" if request.get("provider_outcome") in (None, "success") else "failed"
    if state == "unknown":
        return "failed" if request.get("provider_outcome") not in (None, "unknown") else "unknown"
    return "unknown"


def _llm_actor(
    request: dict[str, Any], raw: GraphRaw, task_ids: set[str],
) -> str | None:
    owner = request.get("owner")
    activation = next((item for item in raw.activations if item.get("owner") == owner), None)
    if activation is not None:
        return f"worker:{activation['instance_id']}"
    task_id = request.get("task_id")
    if isinstance(task_id, str) and task_id in task_ids:
        return f"task:{task_id}"
    stage = request.get("stage")
    if stage in PLANNER_STAGES or (stage is None and owner in PLANNER_OWNERS):
        return "planner"
    return None


def _llm_exchange(request: dict[str, Any], actor: str | None) -> dict[str, Any]:
    stored_usage = request.get("usage")
    usage: dict[str, Any] = stored_usage if isinstance(stored_usage, dict) else {}
    return {
        "request_id": _text(request.get("request_id"), 120),
        "sequence": _optional_count(request.get("request_sequence")),
        "actor": actor,
        "owner": _text(request.get("owner"), 120),
        "task_id": _text(request.get("task_id"), 120),
        "stage": _text(request.get("stage"), 80),
        "kind": _text(request.get("kind"), 80),
        "purpose": _text(request.get("purpose"), 80),
        "attempt_kind": _text(request.get("attempt_kind"), 80),
        "request_state": _text(request.get("state"), 40),
        "response_state": _llm_response_state(request),
        "provider_outcome": _text(request.get("provider_outcome"), 40),
        "provider_status": _optional_status(request.get("provider_status")),
        "provider_code": _text(request.get("provider_code"), 100),
        "provider_retryable": (
            request.get("provider_retryable")
            if isinstance(request.get("provider_retryable"), bool) else None
        ),
        "provider_retry_after": _optional_delay(request.get("provider_retry_after")),
        "decision_outcome": _text(request.get("decision_outcome"), 80),
        "decision_valid": (
            request.get("decision_valid")
            if isinstance(request.get("decision_valid"), bool) else None
        ),
        "made_progress": (
            request.get("made_progress")
            if isinstance(request.get("made_progress"), bool) else None
        ),
        "request_content": _text(request.get("request_content"), 65_536),
        "request_truncated": (
            request.get("request_truncated")
            if isinstance(request.get("request_truncated"), bool) else None
        ),
        "response_content": _text(request.get("response_content"), 32_768),
        "response_truncated": (
            request.get("response_truncated")
            if isinstance(request.get("response_truncated"), bool) else None
        ),
        "tokens": {
            "input": _optional_count(usage.get("input_tokens")),
            "output": _optional_count(usage.get("output_tokens")),
            "cache_read": _optional_count(usage.get("cache_read_tokens")),
            "cache_write": _optional_count(usage.get("cache_write_tokens")),
            "total": _optional_count(request.get("total_tokens")),
        },
    }


def _llm(raw: GraphRaw, task_ids: set[str]) -> tuple[dict[str, Any], list[str]]:
    projected = [
        _llm_exchange(request, _llm_actor(request, raw, task_ids))
        for request in raw.llm_requests
    ]
    projected.sort(key=lambda item: item["sequence"] or 0, reverse=True)
    response_counts = {
        state: sum(item["response_state"] == state for item in projected)
        for state in ("not_sent", "cancelled", "waiting", "received", "failed", "unknown")
    }
    failure_breakdown: dict[str, int] = {}
    for item in projected:
        if item["response_state"] != "failed":
            continue
        outcome = item["provider_outcome"] or "unknown"
        failure_breakdown[outcome] = failure_breakdown.get(outcome, 0) + 1
    actors = sorted({item["actor"] for item in projected if item["actor"] is not None})
    return {
        "provider_requests": sum(
            item["request_state"] in {"dispatched", "settled", "unknown"}
            for item in projected
        ),
        "reservations": len(projected),
        "responses": response_counts["received"],
        "waiting": response_counts["waiting"],
        "failures": response_counts["failed"],
        "failure_breakdown": failure_breakdown,
        "unknown": response_counts["unknown"],
        "cancelled": response_counts["cancelled"],
        "shown": min(len(projected), LLM_EXCHANGE_LIMIT),
        "truncated": len(projected) > LLM_EXCHANGE_LIMIT,
        "exchanges": projected[:LLM_EXCHANGE_LIMIT],
    }, actors


def _planner(raw: GraphRaw) -> dict[str, Any]:
    planner_requests = [r for r in raw.llm_requests if r.get("stage") in PLANNER_STAGES
                        or (r.get("stage") is None and r.get("owner") in PLANNER_OWNERS)]
    live = [r for r in planner_requests if r.get("state") in _LIVE_REQUEST]
    judgment = raw.planner_judgment
    finished = bool(raw.mission and raw.mission.status.value in {
        "completed", "cancelled", "failed",
    })
    stages = {r.get("stage") for r in live}
    if live:
        state = "deciding"
    elif finished:
        state = "finished"
    elif judgment and judgment.get("termination") in {"held", "exhausted"}:
        state = judgment["termination"]
    elif raw.mission and raw.mission.termination_reason:
        state = "finished"
    else:
        state = "waiting"
    by_status: dict[str, int] = {}
    for task in raw.tasks:
        by_status[task.status.value] = by_status.get(task.status.value, 0) + 1
    return {
        "state": state,
        "stage": _text(next(iter(stages)), 80) if len(stages) == 1 else None,
        "trigger": None,
        "inputs": {
            "goal": _text(raw.mission.objective if raw.mission else None, 300),
            "evidence": raw.evidence_count,
            "candidates": sum(item.contract_version == 2 for item in raw.hypotheses),
            "tasks": len(raw.tasks),
            "tasks_by_status": by_status,
            "open_questions": None,
        },
        "last_judgment": None if judgment is None else {
            "termination": _text(judgment.get("termination"), 40),
            "reason": _text(judgment.get("reason"), 300),
        },
        "tasks_registered": raw.event_counts.get("TaskCreated"),
        # New candidate judgments persist candidate_inputs; that stored marker
        # proves the operator-intent-aware contract was used. Before a judgment,
        # or for historical schema-2 records, the read model does not guess.
        "guidance_in_input": (
            True if judgment is not None and "candidate_inputs" in judgment else None
        ),
        "usage": _usage(planner_requests),
        "usage_by_stage": request_stage_usage(planner_requests),
    }


def _wait(execution: TaskExecution) -> dict[str, Any] | None:
    if execution.wait_reason is None:
        return None
    return {"reason": execution.wait_reason, "approval_ref": execution.approval_ref}


def _execution(execution: TaskExecution) -> dict[str, Any]:
    selection = execution.selection
    outcome = execution.outcome
    return {
        "invocation_id": str(execution.invocation_id),
        "task_id": str(execution.task_id),
        "instance_id": str(execution.instance_id),
        "tool_id": str(execution.tool_id),
        "state": execution.state.value,
        "wait": _wait(execution),
        "evidence_id": (
            str(execution.result_evidence_id) if execution.result_evidence_id else None
        ),
        "route": selection.route if selection else None,
        "route_status": selection.route_status if selection else "unknown",
        "route_reason_code": (
            selection.route_reason_code if selection else "historical_projection_unavailable"
        ),
        "selected_program": selection.selected_program if selection else None,
        "program_executable": selection.program_executable if selection else None,
        "result_kind": outcome.result_kind if outcome else None,
        "success": outcome.success if outcome else None,
        "receipt_provider": outcome.receipt_provider if outcome else None,
        "executed_program": outcome.executed_program if outcome else None,
        "executed_program_path": outcome.program_executable if outcome else None,
        "diagnostic_category": outcome.diagnostic_category if outcome else None,
        "diagnostic_code": outcome.diagnostic_code if outcome else None,
        "cause_scope": outcome.cause_scope if outcome else None,
        "resume_condition": outcome.resume_condition if outcome else None,
    }


def _worker(activation: dict[str, Any], raw: GraphRaw) -> dict[str, Any]:
    instance, owner, task_id = (
        activation["instance_id"], activation["owner"], activation["task_id"],
    )
    mine = [e for e in raw.executions if str(e.instance_id) == instance]
    open_runs = [e for e in mine if e.state in _OPEN_EXECUTION]
    unknown = [e for e in mine if e.state == ExecutionState.UNKNOWN]
    waiting = next((e for e in open_runs if e.wait_reason), None)
    stage = activation["launch_stage"]
    task = next((item for item in raw.tasks if str(item.task_id) == task_id), None)
    if waiting is not None:
        phase = f"waiting_{waiting.wait_reason}"
    elif open_runs:
        phase = "tool"
    elif _live(raw.llm_requests, owner):
        phase = "model"
    elif unknown:
        phase = "outcome_unknown"
    elif stage == "returned" and task is not None and (
        task.status.value == "blocked" or task.status.value == "running"
        and task.result is not None and task.result.outcome == "blocked"
    ):
        phase = "waiting_review"
    elif stage == "returned":
        phase = "returned"
    elif stage == "reserved":
        phase = "reserved"
    else:
        phase = "between"  # settle/hand-off: neither a model request nor a tool is open
    # Recovery may transfer the owner epoch without creating another Task/Worker.
    # Preserve those original charges through the recorded Task attribution.
    requests = [r for r in raw.llm_requests
                if r.get("owner") == owner or r.get("task_id") == task_id]
    refs = [str(e.result_evidence_id) for e in mine if e.result_evidence_id]
    return {
        "instance_id": instance,
        "task_id": task_id,
        "launch_stage": stage,
        "phase": phase,
        "model_requests": len(requests),
        "usage": {**_usage(requests), **_decision_usage(requests)},
        "tool_calls": len(mine),
        "tool_states": {s.value: sum(e.state == s for e in mine)
                        for s in ExecutionState if any(e.state == s for e in mine)},
        "current_tool": str(open_runs[0].tool_id) if open_runs else None,
        "wait": _wait(waiting) if waiting else None,
        # Returned while an execution still waits: the slot was given back, the request is kept.
        "slot_released": stage == "returned" and bool(open_runs),
        "evidence_refs": refs[-EVIDENCE_REF_LIMIT:],
        "executions": [_execution(item) for item in mine],
    }


def _task(task: MissionTask, workers: list[dict[str, Any]], raw: GraphRaw) -> dict[str, Any]:
    holder = next((w for w in workers if w["task_id"] == str(task.task_id)
                   and w["launch_stage"] != "returned"), None)
    waits = [e for e in raw.executions if str(e.task_id) == str(task.task_id)
             and e.state in _OPEN_EXECUTION and e.wait_reason]
    criteria = [safe_text(c.description, limit=160) for c in task.completion_criteria[:2]
                if c.description]
    task_requests = [r for r in raw.llm_requests if r.get("task_id") == str(task.task_id)]
    usage = _usage(task_requests)
    external_actions = sum(str(item.task_id) == str(task.task_id) for item in raw.executions)
    return {
        "task_id": str(task.task_id),
        "title": safe_text(task.title, limit=160),
        "status": task.status.value,
        "role": str(task.assigned_agent) if task.assigned_agent else None,
        "worker": holder["instance_id"] if holder else None,
        "completion": criteria or [safe_text(task.objective, limit=160)],
        "blocking_reason": _text(task.blocking_reason, 200),
        "wait": _wait(waits[0]) if waits else None,
        "required": task.required,
        "evidence": len(task.evidence_ids),
        "has_result": task.result is not None,
        "result_outcome": task.result.outcome if task.result else None,
        "candidate_ids": list(task.related_hypothesis_ids),
        "source_task_id": str(task.source_task_id) if task.source_task_id else None,
        "source_result_id": task.source_result_id,
        "usage": {
            **usage,
            **_decision_usage(task_requests),
            "external_actions": external_actions,
            "external_actions_per_model_request": (
                external_actions / usage["requests"] if usage["requests"] else None
            ),
        },
    }


def _candidate(
    candidate: Hypothesis,
    tasks: list[MissionTask],
    promotions: list[dict[str, Any]],
    applications: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    related = [str(task.task_id) for task in tasks
               if candidate.hypothesis_id in task.related_hypothesis_ids]
    promotion = next(({
        "status": _text(item.get("status"), 40),
        "reason": _text(item.get("reason"), 120),
        "finding_id": _text(item.get("finding_id"), 120),
        "review_receipt_id": _text(item.get("review_receipt_id"), 120),
    } for item in reversed(promotions)
        if item.get("candidate_id") == candidate.hypothesis_id), None)
    result = {
        "hypothesis_id": candidate.hypothesis_id,
        "description": safe_text(candidate.description, limit=240),
        "status": candidate.status,
        "confidence": candidate.confidence,
        "revision": candidate.revision,
        "reason": _text(candidate.reason, 240),
        "target_node_ids": list(candidate.target_node_ids),
        "supporting_evidence_ids": list(candidate.supporting_evidence_ids),
        "refuting_evidence_ids": list(candidate.refuting_evidence_ids),
        "source_hypothesis_ids": list(candidate.source_hypothesis_ids),
        "task_ids": related,
    }
    if promotion is not None:
        result["finding_promotion"] = promotion
    projected_applications = [
        _review_application(item) for item in applications
        if item.get("candidate_id") == candidate.hypothesis_id
    ]
    result["review_applications"] = projected_applications
    finding_ids = {
        item.get("finding_id") for item in projected_applications
        if item.get("finding_id")
    }
    result["findings"] = [
        item for item in findings
        if item.get("finding_id") in finding_ids
        or item.get("candidate_id") == candidate.hypothesis_id
    ]
    return result


def _review_application(item: dict[str, Any]) -> dict[str, Any]:
    """Bounded structured status; never expose internal binding/content hashes."""
    return {
        "review_receipt_id": _text(item.get("review_receipt_id"), 120),
        "candidate_pre_verdict_revision": _optional_count(
            item.get("candidate_pre_verdict_revision")
        ),
        "candidate_revision": _optional_count(item.get("candidate_revision")),
        "task_id": _text(item.get("task_id"), 120),
        "finding_id": _text(item.get("finding_id"), 120),
        "status": _text(item.get("status"), 80) or "unknown",
        "reason": _text(item.get("reason"), 500),
    }


def _finding(finding: Finding) -> dict[str, Any]:
    metadata = finding.metadata if isinstance(finding.metadata, dict) else {}
    reviewed = None
    try:
        detail = ReviewedFindingDetail.model_validate(metadata.get("reviewed_finding_detail"))
        reviewed = {
            "title": _text(detail.title, 240),
            "statement": _text(detail.statement, 4000),
            "summary": _text(detail.summary, 1000),
            "vulnclass": _text(detail.vulnclass, 160),
            "severity": _text(detail.severity, 40),
            "severity_reason": _text(detail.severity_reason, 1000),
            "impact": _text(detail.impact, 2000),
            "limitations": [_text(item, 1000) for item in detail.limitations],
        }
    except Exception:
        pass
    evidence_ids = {_text(item, 120) for item in finding.evidence_ids}
    raw_originals = metadata.get("verification_source_refs", [])
    raw_originals = raw_originals if isinstance(raw_originals, (list, tuple)) else []
    originals = [
        identity for identity in (
            _text(item, 120) for item in raw_originals[:20]
        ) if identity is not None and identity in evidence_ids
    ]
    return {
        "finding_id": _text(finding.finding_id, 120),
        "title": _text(finding.title, 240),
        "description": _text(finding.description, 1000),
        "severity": _text(finding.severity, 40),
        "verified": finding.verified,
        "candidate_id": _text(metadata.get("hypothesis_id"), 120),
        "review_receipt_id": _text(metadata.get("review_receipt_id"), 120),
        "original_evidence_ids": originals[:20],
        "reviewed_detail": reviewed,
    }


def _event(row: dict[str, Any]) -> dict[str, Any]:
    from redteam.app.web.events import public_payload

    envelope = row["envelope"]
    public = public_payload(envelope["event_type"], envelope.get("payload") or {})
    kept = {k: public[k] for k in _EVENT_KEYS if k in public}
    return {"seq": row["sequence"], "type": envelope["event_type"],
            "ts": str(envelope["timestamp"]), "data": kept}


def build_graph(raw: GraphRaw) -> dict[str, Any]:
    workers = [_worker(a, raw) for a in raw.activations]
    tasks = [_task(t, workers, raw) for t in raw.tasks]
    task_ids = {item["task_id"] for item in tasks}
    llm, llm_actors = _llm(raw, task_ids)
    findings = [_finding(item) for item in raw.findings]
    candidates = [_candidate(
        item,
        raw.tasks,
        raw.candidate_finding_promotions,
        raw.candidate_review_applications,
        findings,
    )
                  for item in raw.hypotheses
                  if item.contract_version == 2]
    candidate_ids = {item["hypothesis_id"] for item in candidates}
    edges = [{"from": "planner", "to": f"candidate:{c['hypothesis_id']}",
              "kind": "judged"} for c in candidates]
    edges += [{"from": f"candidate:{source}",
               "to": f"candidate:{candidate['hypothesis_id']}", "kind": "derived"}
              for candidate in candidates for source in candidate["source_hypothesis_ids"]
              if source in candidate_ids]
    edges += [{"from": f"candidate:{candidate}", "to": f"task:{t['task_id']}",
               "kind": "probed_by"}
              for t in tasks for candidate in t["candidate_ids"] if candidate in candidate_ids]
    edges += [{"from": f"task:{t['source_task_id']}", "to": f"task:{t['task_id']}",
               "kind": "followup"} for t in tasks if t["source_task_id"]]
    edges += [{"from": "planner", "to": f"task:{t['task_id']}", "kind": "registered"}
              for t in tasks if not candidate_ids.intersection(t["candidate_ids"])]
    edges += [{"from": f"task:{w['task_id']}", "to": f"worker:{w['instance_id']}",
               "kind": "held_by" if w["launch_stage"] != "returned" else "returned"}
              for w in workers]
    edges += [{"from": f"worker:{w['instance_id']}", "to": "origin", "kind": "stored"}
              for w in workers if w["evidence_refs"]]
    edges += [{"from": actor, "to": "llm", "kind": "model_exchange"}
              for actor in llm_actors]
    edges.append({"from": "origin", "to": "planner", "kind": "wakes"})
    return {
        "available": True,
        "run_id": raw.run_id,
        "mission": None if raw.mission is None else {
            "status": raw.mission.status.value,
            "termination_reason": _text(raw.mission.termination_reason, 80),
        },
        "planner": _planner(raw),
        "candidates": candidates,
        "candidate_finding_promotions": raw.candidate_finding_promotions,
        "candidate_review_applications": [
            _review_application(item) for item in raw.candidate_review_applications
        ],
        "findings": findings,
        "tasks": tasks,
        "workers": workers,
        "executions": [_execution(item) for item in raw.executions],
        "llm": llm,
        "origin": {
            "evidence": raw.evidence_count,
            "graph_updates": raw.event_counts.get("GraphUpdated", 0),
            "findings": raw.finding_count,
            "verified_findings": raw.verified_finding_count,
            "hypotheses": len(candidates),
        },
        "usage": {**_usage(raw.llm_requests), "by_stage": request_stage_usage(raw.llm_requests),
                  "budget_remaining": raw.budget_remaining},
        "edges": edges,
        "events": [_event(r) for r in raw.events[:EVENT_LIMIT]],
    }
