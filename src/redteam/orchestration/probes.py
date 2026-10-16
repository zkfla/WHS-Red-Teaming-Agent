"""Bounded worker input and code-only yield over existing Task/execution rows."""

from typing import Literal

from redteam.core.contracts.graph import Hypothesis
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.orchestration.central_completion import unsafe
from redteam.orchestration.central_results import (
    commit_result,
    probe_result_wire_schema,
    result_for,
)
from redteam.orchestration.central_runtime import runtime_token
from redteam.orchestration.completion_proposal import CompletionProposal, record_unresolved
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.engine import DecisionRuleError
from redteam.orchestration.state import TerminationReason

PROBE_SYSTEM = """
이 호출은 active Task의 probe result contract v2를 사용한다. `probe.remaining`과
`probe.physical_cap`은 현재 ledger에서 계산된 한도이며 권한이나 성공 상태가 아니다.
physical_cap.budget_available=false이면 새 대상 호출을 제안하지 말고, 남은 local 조회·기록·검토·
settlement만 수행한다. pending/running/UNKNOWN invocation은 원래 ID를 보존한다.

`probe.candidates`는 변경할 수 없는 admitted snapshot이다. `return_conditions`는 현재 Task의 완료
조건이다. 완료 또는 미해결 결과는 task.settle, 유효한 중간 결과는 task.publish_result를 사용한다.
runtime 진단은 cause_scope, owner, repair_condition으로 반환한다.
"""


async def probe_task(deps, runtime):
    if runtime.active_task_id is None:
        return None
    task = await deps.central_state.get_task(str(runtime.active_task_id))
    return task if task is not None and task.probe_limits is not None else None


BoundaryMode = Literal["all", "actions", "model"]


async def boundary_reason(deps, runtime, *, mode: BoundaryMode = "all"):
    """Return the first exhausted probe limit, or None.

    Modes: "all" checks both caps; "actions" checks only the physical cap (the per-dispatch
    guard before a physical call); "model" is the local-turn check for model and settlement
    turns. Under "model" the physical cap stops only while an original is still in flight
    (pending/running/UNKNOWN, a wait, a resource hold, a job, or a BLOCKED task). Otherwise
    the local turn continues within the remaining model budget. Central admission still
    rejects any physical call beyond the cap.
    """
    if mode not in ("all", "actions", "model"):
        raise ValueError("unknown probe boundary mode: " + str(mode))
    task = await probe_task(deps, runtime)
    if task is None or task.status is TaskStatus.COMPLETED:
        return None
    if mode == "model":
        # A local turn is new admission too: an operator pause or cancel keeps it closed.
        mission = await deps.central_state.get_mission(task.mission_id)
        if mission is not None and mission.status in {
            MissionStatus.PAUSED, MissionStatus.CANCELLED,
        }:
            return "operator_stopped"
    used = await deps.central_state.transaction(
        lambda tx: tx.probe_usage(str(task.task_id)), write=False,
    )
    if used["actions"] >= task.probe_limits.max_actions and (
        mode != "model" or await _physical_wait(deps, task)
    ):
        return "probe_action_limit_exhausted"
    if mode != "actions" and used["model_requests"] >= task.probe_limits.max_model_requests:
        return "probe_model_request_limit_exhausted"
    return None


async def _physical_wait(deps, task) -> bool:
    """Existing execution rows decide whether an original still holds the task open."""
    if task.status is TaskStatus.BLOCKED:
        return True
    executions = await deps.central_state.list_executions(task_id=str(task.task_id))
    return any(unsafe(item) for item in executions)


async def original_records(deps, task):
    """Use existing review-facing ToolResult records, not journal envelopes as proof."""
    records = await deps.evidence_store.list_for_run(deps.central_state.run_id)
    return [record for record in records
            if record.metadata.get("mission_task_id") == str(task.task_id)
            and isinstance(record.metadata.get("tool_result"), dict)]


def project_worker_executions(executions, *, count: int = 6) -> list[dict]:
    """Expose current blockers and recent outcomes without replaying the audit ledger.

    Admission fingerprints, reservations, resource keys and ownership bookkeeping remain
    code-owned. The worker needs only the invocation pointer, current state, selected route,
    result pointer and a bounded diagnostic that can change its next decision.
    """
    unresolved = [item for item in executions if unsafe(item)]
    settled = [item for item in executions if not unsafe(item)]
    selected = [*unresolved[-4:], *settled[-2:]]
    selected = sorted(
        {str(item.invocation_id): item for item in selected}.values(),
        key=lambda item: executions.index(item),
    )[-count:]
    result = []
    for item in selected:
        row = {
            "invocation_id": str(item.invocation_id),
            "tool_id": str(item.tool_id),
            "state": item.state.value,
            "target_node_id": item.target_node_id,
            "wait_reason": item.wait_reason,
            "result_evidence_id": (
                str(item.result_evidence_id) if item.result_evidence_id else None
            ),
        }
        if item.selection is not None:
            row["selection"] = item.selection.model_dump(mode="json")
        if item.outcome is not None:
            row["outcome"] = item.outcome.model_dump(mode="json")
        result.append(row)
    return result


async def context_for_probe(deps, runtime, context):
    """Only the admitted subjects and this worker's originals enter its payload."""
    task = await probe_task(deps, runtime)
    if task is None:
        return context
    if runtime.probe_hypotheses is None:
        from redteam.attack_graph.service import AttackGraphService

        service = AttackGraphService(deps.graph_store)
        snapshots = {}
        for identity, revision in task.hypothesis_revisions.items():
            candidate = await service.get_hypothesis(identity)
            if candidate is None or candidate.revision != revision:
                raise DecisionRuleError(
                    "The admitted candidate snapshot is unavailable.",
                    code="probe_candidate_snapshot_changed", location="hypothesis_revisions",
                )
            snapshots[identity] = candidate.model_dump(mode="json")
        runtime.probe_hypotheses = snapshots
    snapshots = {identity: Hypothesis.model_validate(value)
                 for identity, value in runtime.probe_hypotheses.items()}
    if {identity: candidate.revision for identity, candidate in snapshots.items()} != (
        task.hypothesis_revisions
    ):
        raise DecisionRuleError(
            "The checkpoint candidate snapshot does not match the task's admitted revisions.",
            code="probe_candidate_snapshot_binding_changed", location="hypothesis_revisions",
        )
    allowed_evidence = set(map(str, task.evidence_ids))
    for candidate in snapshots.values():
        allowed_evidence.update(candidate.supporting_evidence_ids)
        allowed_evidence.update(candidate.refuting_evidence_ids)
    originals = await original_records(deps, task)
    allowed_evidence.update(str(record.evidence_id) for record in originals)
    if task.result:
        allowed_evidence.update(map(str, task.result.evidence_ids))
    relevant_nodes = set(map(str, task.related_attack_graph_node_ids))
    relevant_nodes.update(task.principal_refs)
    relevant_nodes.update("hypothesis:" + identity for identity in snapshots)
    filtered = dict(context)
    # These represent other work/frontiers, not the assigned probe's input.
    for key in ("investigation_branches", "unresolved_results", "coverage", "result_processing"):
        filtered.pop(key, None)
    # The full Task body already lives in active_task below; repeating it here
    # would be the exact same bytes twice on every worker turn. Keep the
    # list-of-one shape other consumers key off (task_id), not the body.
    filtered["investigation_tasks"] = [{"task_id": str(task.task_id), "see": "active_task"}]
    knowledge = {}
    for key, value in context.get("knowledge", {}).items():
        if key in {"hypotheses", "historical", "evidence_diagnostics"}:
            continue  # The admitted snapshot below owns this worker's hypothesis input.
        if isinstance(value, list):
            knowledge[key] = [item for item in value if isinstance(item, dict) and (
                str(item.get("node_id", item.get("target_node_id", ""))) in relevant_nodes
                or str(item.get("hypothesis_id", "")) in snapshots
            )]
        elif key == "provided":
            knowledge[key] = {"claims": [item for item in value.get("claims", [])
                                         if item.get("node_id") in relevant_nodes]}
        elif key == "findings":
            knowledge[key] = {"items": [item for item in value.get("items", [])
                                        if set(item.get("target_node_ids", [])) & relevant_nodes
                                        or set(item.get("hypothesis_ids", [])) & snapshots.keys()]}
        else:
            knowledge[key] = value
    filtered["knowledge"] = knowledge
    knowledge["fact_refs"] = sorted({
        ref for fact in knowledge.get("facts", []) for ref in fact.get("fact_refs", [])
    })
    executions = await deps.central_state.list_executions(task_id=str(task.task_id))
    filtered["execution_history"] = project_worker_executions(executions)
    filtered["execution_history_count"] = len(executions)
    filtered["execution_history_omitted_count"] = max(
        len(executions) - len(filtered["execution_history"]), 0,
    )
    filtered["evidence_index"] = [ref for ref in context.get("evidence_index", [])
                                  if str(ref) in allowed_evidence]
    filtered["evidence_count"] = len(filtered["evidence_index"])
    for key in ("evidence", "evidence_validity", "requested_evidence"):
        values = context.get(key, [])
        if isinstance(values, list):
            filtered[key] = [item for item in values if isinstance(item, dict)
                             and str(item.get("evidence_id", "")) in allowed_evidence]
    filtered["evidence_omitted_count"] = max(
        filtered["evidence_count"] - len(filtered.get("evidence", [])), 0,
    )
    usage = await deps.central_state.transaction(
        lambda tx: tx.probe_usage(str(task.task_id)), write=False,
    )
    limit = task.probe_limits.max_actions
    profile_limits = getattr(
        getattr(deps, "specialist_policy", None), "probe_limits", None,
    )
    repair_limits = [
        value for value in (
            task.probe_limits.max_repair_requests,
            getattr(profile_limits, "max_repair_requests", None),
        )
        if value is not None
    ]
    repair_limit = min(repair_limits) if repair_limits else None
    if repair_limit is not None:
        repair_limit = min(repair_limit, task.probe_limits.max_model_requests)
    filtered["probe"] = {
        "candidates": runtime.probe_hypotheses,
        "original_evidence_ids": [str(record.evidence_id) for record in originals],
        "usage": usage,
        # Computed from the same ledger counts; it is not a second stored flag.
        "physical_cap": {
            "limit": limit,
            "used": usage["actions"],
            "remaining": max(limit - usage["actions"], 0),
            "budget_available": usage["actions"] < limit,
        },
        "remaining": {
            "actions": max(task.probe_limits.max_actions - usage["actions"], 0),
            "model_requests": max(
                task.probe_limits.max_model_requests - usage["model_requests"], 0,
            ),
            "repairs": (
                max(repair_limit - usage["repairs"], 0)
                if repair_limit is not None else None
            ),
        },
        "purpose": task.objective,
        "return_conditions": [item.model_dump(mode="json") for item in task.completion_criteria],
        "result_schema": probe_result_wire_schema(),
    }
    return filtered


async def yield_probe(deps, state, reason):
    """Preserve a gap and originals even when no further model request is allowed."""
    runtime = state["runtime"]
    task = await probe_task(deps, runtime)
    if task is None or task.status is TaskStatus.COMPLETED:
        return state
    mission = await deps.central_state.get_mission(task.mission_id)
    if mission.status in {MissionStatus.PAUSED, MissionStatus.CANCELLED}:
        # Operator stop never causes new mutation/admission; the run ends on the stop reason.
        stop = (TerminationReason.OPERATOR_PAUSED if mission.status is MissionStatus.PAUSED
                else TerminationReason.OPERATOR_STOPPED)
        runtime.termination_reason = runtime.termination_reason or stop
        return state
    token = await runtime_token(deps, runtime)
    originals = await deps.central_state.list_executions(task_id=str(task.task_id))
    records = await original_records(deps, task)
    refs = [str(record.evidence_id) for record in records]
    pending = (any(unsafe(item) for item in originals) or task.status is TaskStatus.BLOCKED
               or any(not call.completed for call in state.get("native_calls", [])))
    waiting = any(item.wait_reason for item in originals)
    summary = "Probe returned preserved observations: " + reason
    proposal = CompletionProposal(
        outcome="blocked" if pending else "inconclusive", reason=summary, evidence_ids=refs,
    )
    gap = await record_unresolved(
        evidence_store=deps.evidence_store, runtime=runtime, proposal=proposal,
        path=None, task_id=str(task.task_id),
        diagnostic=DecisionDiagnostic(code="probe_yield", stage="probe_return", kind="validation",
                                      retryable=False, summary=reason,
                                      cause_scope="policy_wait" if waiting else "unknown",
                                      owner="orchestration.probes",
                                      repair_condition=("Resume after ledger recovery releases "
                                                        "the original execution; do not poll the "
                                                        "model or replay successful calls."
                                                        if pending else
                                                        "Reassess preserved observations within "
                                                        "the remaining investigation budget."),
                                      trace=[{"invocation_id": str(item.invocation_id),
                                              "state": item.state.value,
                                              "wait_reason": item.wait_reason,
                                              "result_evidence_id": item.result_evidence_id}
                                             for item in originals[-20:]]),
    )
    result = result_for(task, outcome=proposal.outcome, summary=summary,
                        evidence_ids=[*refs, gap], limitations=[] if pending else [reason])
    await commit_result(deps, runtime, task, result, token, final=not pending)
    if pending:
        runtime.termination_reason = runtime.termination_reason or TerminationReason.RESOURCE_WAIT
        state["native_outcome"] = None  # Keep the original automatic resume classification.
    else:
        runtime.termination_reason = (
            runtime.termination_reason or TerminationReason.INVESTIGATION_HELD
        )
        state["native_outcome"] = "investigation_held"
    return state
