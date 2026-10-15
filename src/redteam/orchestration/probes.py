"""Bounded worker input and code-only yield over existing Task/execution rows."""

from redteam.core.contracts.graph import Hypothesis
from redteam.core.contracts.mission import InvestigationResult
from redteam.core.enums import TaskStatus
from redteam.orchestration.central_completion import unsafe
from redteam.orchestration.central_results import commit_result, result_for
from redteam.orchestration.central_runtime import runtime_token
from redteam.orchestration.completion_proposal import CompletionProposal, record_unresolved
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.engine import DecisionRuleError
from redteam.orchestration.state import TerminationReason

PROBE_SYSTEM = """
Execute only this bounded probe purpose using the selected role's registered tools.
Completion diagnostics describe their cause_scope, owner and repair_condition.
Correct model_input when possible. Preserve originals and diagnostic references in
an unresolved return for runtime_contract or unknown support that you cannot repair;
target calls and repeated reads do not fix runtime contracts. Current waits and
changes come from new tool replies and worker_updates. Initial execution_history
and earlier diagnostic messages are historical views, not current permission.
Choose methods yourself; there is no mandatory tool sequence or domain handoff.
Related candidates are immutable admitted snapshots, not permission or verified findings.
Return observations and limitations early with task.settle (inconclusive/blocked)
or publish intermediate feedback with task.publish_result using result contract_version=2.
Do not require a complete vulnerability proof to finish an observation probe.
Confirmed/refuted probe results require the existing settlement review. A reviewed
probe result is not automatically the candidate's verdict or Mission completion.
When the purpose is already sufficiently tested, the existing review helpers may
verify it within the remaining shared limits. Never bypass review because a limit ended.
Reusing evidence for another useful, authorized probe is allowed; unknown physical
calls must not be repeated. Report unexpected directions to the planner instead
of starting another investigation. Code stops new admission at the probe boundary
and returns preserved originals without another model request, retaining pending
approvals/jobs/UNKNOWN and their original execution identifiers.
"""


async def probe_task(deps, runtime):
    if runtime.active_task_id is None:
        return None
    task = await deps.central_state.get_task(str(runtime.active_task_id))
    return task if task is not None and task.probe_limits is not None else None


async def boundary_reason(deps, runtime, *, actions_only=False):
    task = await probe_task(deps, runtime)
    if task is None or task.status is TaskStatus.COMPLETED:
        return None
    used = await deps.central_state.transaction(
        lambda tx: tx.probe_usage(str(task.task_id)), write=False,
    )
    if used["actions"] >= task.probe_limits.max_actions:
        return "probe_action_limit_exhausted"
    if not actions_only and used["model_requests"] >= task.probe_limits.max_model_requests:
        return "probe_model_request_limit_exhausted"
    return None


async def original_records(deps, task):
    """Use existing review-facing ToolResult records, not journal envelopes as proof."""
    records = await deps.evidence_store.list_for_run(deps.central_state.run_id)
    return [record for record in records
            if record.metadata.get("mission_task_id") == str(task.task_id)
            and isinstance(record.metadata.get("tool_result"), dict)]


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
    filtered["execution_history"] = [item.model_dump(mode="json") for item in executions[-20:]]
    filtered["execution_history_count"] = len(executions)
    filtered["execution_history_omitted_count"] = max(len(executions) - 20, 0)
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
    filtered["probe"] = {
        "candidates": runtime.probe_hypotheses,
        "original_evidence_ids": [str(record.evidence_id) for record in originals],
        "usage": await deps.central_state.transaction(
            lambda tx: tx.probe_usage(str(task.task_id)), write=False,
        ),
        "purpose": task.objective,
        "return_conditions": [item.model_dump(mode="json") for item in task.completion_criteria],
        "result_schema": InvestigationResult.model_json_schema(),
    }
    return filtered


async def yield_probe(deps, state, reason):
    """Preserve a gap and originals even when no further model request is allowed."""
    runtime = state["runtime"]
    task = await probe_task(deps, runtime)
    if task is None or task.status is TaskStatus.COMPLETED:
        return state
    from redteam.core.enums import MissionStatus

    mission = await deps.central_state.get_mission(task.mission_id)
    if mission.status in {MissionStatus.PAUSED, MissionStatus.CANCELLED}:
        return state  # Operator stop never causes new mutation/admission.
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
