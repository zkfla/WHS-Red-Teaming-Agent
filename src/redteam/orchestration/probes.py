"""Bounded worker input and code-only yield over existing Task/execution rows."""

from typing import Literal

from redteam.core.contracts.graph import Hypothesis
from redteam.core.contracts.mission import InvestigationResult
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.orchestration.central_completion import unsafe
from redteam.orchestration.central_results import commit_result, result_for
from redteam.orchestration.central_runtime import runtime_token
from redteam.orchestration.completion_proposal import CompletionProposal, record_unresolved
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.engine import DecisionRuleError
from redteam.orchestration.state import TerminationReason

PROBE_SYSTEM = """
선택된 역할의 등록 도구를 사용해 이 제한된 probe 목적만 수행합니다.
완료 진단에는 cause_scope, owner, repair_condition이 설명되어 있습니다.
가능하면 model_input을 바로잡습니다. 고칠 수 없는 runtime_contract 또는
알 수 없는 지원 문제는 원본과 진단 참조를 보존해 미해결 결과로 반환합니다.
대상 호출이나 반복 읽기로 runtime contract를 고칠 수는 없습니다. 현재 변경은
새 tool 응답과 worker_updates에서 확인합니다. 최초 execution_history와 과거
진단 메시지는 이력이며 현재 권한을 부여하지 않습니다.
방법은 직접 선택합니다. 필수 도구 순서나 도메인 간 인계는 없습니다.
관련 후보는 변경할 수 없는 승인된 스냅샷이며, 권한이나 검증된 finding이 아닙니다.
관측과 제한 사항은 task.settle (inconclusive/blocked)로 일찍 반환하거나,
result contract_version=2를 사용해 task.publish_result로 중간 피드백을
게시합니다.
관측 probe를 끝내기 위해 완전한 취약점 증명을 요구하지 않습니다.
확인/기각된 probe 결과에는 기존 정산 검토가 필요합니다. 검토된 probe 결과가
자동으로 후보의 판정이나 Mission 완료가 되는 것은 아닙니다.
목적을 이미 충분히 시험했다면 남은 공유 한도 내에서 기존 검토 helper가 이를
검증할 수 있습니다. 한도가 끝났다는 이유로 검토를 건너뛰지 않습니다.
probe.physical_cap.budget_available이 false이면 대상에 새 요청을 보내지
않습니다. 이후 물리 호출은 실행되지 않고 거부됩니다. budget_available은
한도 대비 물리 작업 횟수만 나타내며, 권한, 승인, 범위 또는 준비 상태를
뜻하지 않습니다. 로컬 도구는 보존된 원본을 읽고, 근거 있는 관측을 기록하고,
남은 모델 요청 예산 안에서 정산할 수 있습니다. 따라서 검토된 관측은 대상에
추가 요청을 보내지 않고도 목적을 마칠 수 있습니다. 모델 요청 예산이 소진되면
코드가 중단하고 추가 모델 요청 없이 보존된 원본을 반환하며, 대기 중인
승인/작업/UNKNOWN과 원래 실행 식별자를 유지합니다.
다른 유용하고 승인된 probe에 증거를 재사용할 수 있습니다. 결과가 불명확한
물리 호출은 반복하지 않습니다. 예상하지 못한 방향은 새 조사를 시작하는 대신
planner에 보고합니다.
물리 한도 이후 로컬로 계속 진행하는 것은 대기 중이거나 실행을 기다리거나,
UNKNOWN이거나, 작업 또는 자원에 묶인 원본이 없고 작업이 BLOCKED 상태도
아닐 때만 허용됩니다.
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
        "result_schema": InvestigationResult.model_json_schema(),
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
