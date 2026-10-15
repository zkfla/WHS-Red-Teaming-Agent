"""Structured planner/task/worker view built only from stored originals.

Pure function of what ``graph_read`` loaded: no I/O, no model, no inference. A value the
stores do not hold is ``None`` (shown as unknown), never a guess. Layout is the client's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from redteam.app.cli.display import safe_text
from redteam.core.contracts.execution import ExecutionState, TaskExecution
from redteam.core.contracts.graph import Hypothesis
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
        "usage": _usage(requests),
        "tool_calls": len(mine),
        "tool_states": {s.value: sum(e.state == s for e in mine)
                        for s in ExecutionState if any(e.state == s for e in mine)},
        "current_tool": str(open_runs[0].tool_id) if open_runs else None,
        "wait": _wait(waiting) if waiting else None,
        # Returned while an execution still waits: the slot was given back, the request is kept.
        "slot_released": stage == "returned" and bool(open_runs),
        "evidence_refs": refs[-EVIDENCE_REF_LIMIT:],
    }


def _task(task: MissionTask, workers: list[dict[str, Any]], raw: GraphRaw) -> dict[str, Any]:
    holder = next((w for w in workers if w["task_id"] == str(task.task_id)
                   and w["launch_stage"] != "returned"), None)
    waits = [e for e in raw.executions if str(e.task_id) == str(task.task_id)
             and e.state in _OPEN_EXECUTION and e.wait_reason]
    criteria = [safe_text(c.description, limit=160) for c in task.completion_criteria[:2]
                if c.description]
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
        "usage": _usage([r for r in raw.llm_requests if r.get("task_id") == str(task.task_id)]),
    }


def _candidate(candidate: Hypothesis, tasks: list[MissionTask]) -> dict[str, Any]:
    related = [str(task.task_id) for task in tasks
               if candidate.hypothesis_id in task.related_hypothesis_ids]
    return {
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
    candidates = [_candidate(item, raw.tasks) for item in raw.hypotheses
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
        "tasks": tasks,
        "workers": workers,
        "origin": {
            "evidence": raw.evidence_count,
            "graph_updates": raw.event_counts.get("GraphUpdated", 0),
            "findings": raw.event_counts.get("FindingCreated", 0),
            "verified_findings": raw.event_counts.get("FindingVerified", 0),
            "hypotheses": len(candidates),
        },
        "usage": {**_usage(raw.llm_requests), "by_stage": request_stage_usage(raw.llm_requests),
                  "budget_remaining": raw.budget_remaining},
        "edges": edges,
        "events": [_event(r) for r in raw.events[:EVENT_LIMIT]],
    }
