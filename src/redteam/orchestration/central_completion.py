"""Goal settlement against the central originals; no Path/Work completion flags."""

import json
from datetime import datetime, timezone

from redteam.attack_graph.identity import hypothesis_node_id
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.execution import ExecutionState
from redteam.core.contracts.mission import MissionCriterionResult
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.orchestration.central_runtime import event, process_controls
from redteam.orchestration.central_state import check_evidence
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.result_claims import (
    RESULT_REVIEW_INSTRUCTION,
    capture_result_graph,
    check_result_graph,
    check_result_review,
    mission_result_purpose,
)
from redteam.orchestration.semantic_completion import (
    EXCERPT_CHARS,
    MAX_EXCERPTS,
    GoalReview,
    goal_review_payload,
    origin_excerpt,
    verify_single_origin,
    verify_task_receipt,
)
from redteam.orchestration.state import TerminationReason


def unsafe(execution):
    return (
        execution.state in {ExecutionState.PENDING, ExecutionState.RUNNING, ExecutionState.UNKNOWN}
        or execution.wait_reason or execution.resource_execution_pending or execution.job_ref
    )


def goal_required_task_ids(tasks):
    needed = {str(task.task_id) for task in tasks if task.required}
    for source in tasks:
        if source.result is None:
            continue
        for question in source.result.questions:
            if question.required_for_goal:
                needed.add(str(source.task_id))
                needed.update(str(task.task_id) for task in tasks if (
                    task.source_task_id == source.task_id
                    and task.source_result_id == source.result.result_id
                    and task.source_question_id == question.question_id
                ))
    return needed


def goal_snapshot(tx, *, include_assessment=True):
    """Only relevant domain inputs, never the whole graph fingerprint."""
    mission_id, _ = tx.binding()
    mission = tx.get_mission(mission_id)
    tasks = tx.list_tasks()
    executions = [row[0] for row in tx.db.execute(
        "SELECT payload FROM state_executions WHERE run_id=? ORDER BY invocation_id", (tx.run_id,),
    )]
    # Cleanup obligations and hypotheses actually required by the delegated
    # contracts. Unrelated observations/hypotheses may change during review.
    required = {str(hypothesis_node_id(str(identity))) for task in tasks if task.required
                for identity in task.goal_hypothesis_ids()}
    graph = []
    prefix = tx.engagement_id + "\x1f"
    for identity, payload in tx.db.execute(
        "SELECT node_id,payload FROM state_graph_nodes WHERE namespace=? ORDER BY node_id",
        (tx.engagement_id,),
    ):
        properties = json.loads(payload).get("properties", {})
        cleanup = properties.get("cleanup")
        if cleanup is not None or identity.removeprefix(prefix) in required:
            graph.append((identity, cleanup, properties.get("facts")))
    mission_input = mission.model_dump(mode="json")
    if not include_assessment:
        # A criterion assessment does not change the exploration inputs. The
        # completion CAS below still checks the complete original revision.
        for key in ("revision", "updated_at", "criterion_results", "termination_reason"):
            mission_input.pop(key, None)
    needed = goal_required_task_ids(tasks)
    return mission, tasks, content_hash([
        mission_input, [task.model_dump(mode="json") for task in tasks
                        if not include_assessment or str(task.task_id) in needed],
        executions, graph,
    ])


def required_questions(tasks):
    """Registration is not resolution: only a reviewed, completed follow-up can prove it."""
    unresolved = []
    for source in tasks:
        if source.result is None:
            continue
        for question in source.result.questions:
            if not question.required_for_goal:
                continue
            followups = [task for task in tasks if (
                task.source_task_id == source.task_id
                and task.source_result_id == source.result.result_id
                and task.source_question_id == question.question_id
            )]
            if not any(task.status is TaskStatus.COMPLETED and task.result is not None
                       and task.result.outcome in {"confirmed", "refuted"}
                       and task.result.completion_receipt_id for task in followups):
                unresolved.append((str(source.task_id), question))
    return unresolved


async def goal_inputs(deps, runtime, *, graph_service=None, validity_reads=None):
    mission, tasks, stamp = await deps.central_state.transaction(goal_snapshot, write=False)
    errors = []
    if not mission.success_criteria:
        errors.append("goal_has_no_original_criteria")
    if any(task.required and task.status is not TaskStatus.COMPLETED for task in tasks):
        errors.append("required_investigation_unfinished")
    if any(task.required and task.result is not None and task.result.outcome == "blocked"
           for task in tasks):
        errors.append("required_investigation_blocked")
    if required_questions(tasks):
        errors.append("goal_required_questions_unresolved")
    executions = await deps.central_state.list_executions()
    from redteam.orchestration.resource_cleanup import unmapped_resource_effects
    if await unmapped_resource_effects(deps.evidence_store, executions):
        errors.append("resource_effects_unmapped_cleanup_unverified")
    if any(unsafe(item) for item in executions):
        errors.append("original_execution_unsettled")
    if await deps.central_state.transaction(lambda tx: tx.active_activation_count(), write=False):
        errors.append("actors_not_drained")
    graph = graph_service or AttackGraphService(deps.graph_store)
    cleanup = await graph.cleanup_report()
    if cleanup.outstanding:
        errors.append("required_cleanup_outstanding")
    from redteam.orchestration.code_artifacts import fixture_cleanup_blockers
    errors.extend(await fixture_cleanup_blockers(deps, runtime))
    required_hypotheses = {str(identity) for task in tasks if task.required
                           for identity in task.goal_hypothesis_ids()}
    integrity = await graph.integrity_report()
    if required_hypotheses.intersection(integrity.unresolved_hypotheses):
        errors.append("goal_required_hypotheses_unresolved")
    if integrity.blocking:
        errors.extend(integrity.blocking)
    mapping, excerpts, inspected = {}, [], []
    if errors:
        return mission, tasks, stamp, errors, mapping, excerpts, inspected
    from redteam.orchestration.resource_cleanup import cleanup_originals
    cleanup_errors, cleanup_refs = await cleanup_originals(deps, runtime, cleanup)
    errors.extend(cleanup_errors)
    inspected.extend(cleanup_refs)
    # Every required follow-up's receipt/original is checked, not just the
    # first result or the excerpts that fit into one review prompt.
    needed = goal_required_task_ids(tasks)
    for task in tasks:
        if task.status is not TaskStatus.COMPLETED or task.result is None:
            continue
        result = task.result
        task_checked, task_excerpts, task_errors = [], [], []
        refs = list(map(str, result.evidence_ids))
        receipt_id = result.completion_receipt_id
        if result.outcome in {"confirmed", "refuted"}:
            if receipt_id is None:
                if str(task.task_id) in needed:
                    errors.append("reviewed_result_receipt_missing")
                continue
            try:
                checked = await check_evidence(
                    deps.evidence_store, run_id=str(runtime.run_id),
                    engagement_id=str(runtime.engagement_id), evidence_ids=[str(receipt_id)],
                )
            except StateConflict as exc:
                if str(task.task_id) in needed:
                    errors.append(str(exc))
                continue
            receipt = checked[0].record.metadata
            receipt_errors = verify_task_receipt(
                task, receipt, run_id=str(runtime.run_id), mission_id=str(runtime.mission_id),
                accepted_outcomes=("success", "refuted"),
            )
            if receipt_errors:
                if str(task.task_id) in needed:
                    errors.extend(receipt_errors)
                continue
            task_checked.extend(checked)
            refs = receipt["source_evidence_ids"]
        proven = []
        for identity in refs:
            record = await deps.evidence_store.get(identity)
            if record is None or not isinstance(record.metadata.get("tool_result"), dict):
                continue  # System gap notes report uncertainty, never prove success.
            record, origin_errors = await verify_single_origin(
                evidence_store=deps.evidence_store, path_store=deps.path_store,
                run_id=str(runtime.run_id), engagement_id=str(runtime.engagement_id),
                mission_id=str(runtime.mission_id), evidence_id=identity, task=task,
                task_graph_store=deps.investigations,
            )
            if origin_errors:
                task_errors.extend(origin_errors)
                continue
            task_checked.extend(await check_evidence(
                deps.evidence_store, run_id=str(runtime.run_id),
                engagement_id=str(runtime.engagement_id), evidence_ids=[identity],
            ))
            proven.append(identity)
            task_excerpts.append(origin_excerpt(record))
        if proven:
            from redteam.orchestration.evidence_validity import capture_validity

            task_reads = {}
            for identity in [*proven, *([str(receipt_id)] if receipt_id else [])]:
                try:
                    read, reuse_errors = await capture_validity(
                        deps, [identity], graph_service=graph,
                    )
                except StateConflict as exc:
                    read, reuse_errors = None, [str(exc)]
                task_errors.extend(reuse_errors)
                task_reads[identity] = read
            if task_errors:
                if str(task.task_id) in needed:
                    errors.extend(task_errors)
                continue
            mapping[str(task.task_id)] = proven
            if validity_reads is not None:
                validity_reads[str(task.task_id)] = task_reads
            if str(task.task_id) in needed:
                inspected.extend(task_checked)
            excerpts.extend(task_excerpts[:MAX_EXCERPTS - len(excerpts)])
        elif str(task.task_id) in needed:
            errors.append("required_result_has_no_proven_originals")
    if not mapping:
        errors.append("goal_has_no_proven_originals")
    return mission, tasks, stamp, errors, mapping, excerpts, inspected


async def complete_goal(deps, runtime, *, budget=None):
    """Owned caller has closed new admission and drained before evaluation."""
    from redteam.orchestration.engine import InvestigationEngine
    from redteam.orchestration.metered_llm import llm_request_scope

    if runtime.instance_id is not None or runtime.active_task_id is not None:
        return ["only_the_owned_planner_can_settle_the_goal"]
    if not await deps.central_state.transaction(lambda tx: tx.settlement_closed(), write=False):
        return ["goal_admission_must_close_before_settlement"]
    await process_controls(deps)
    validity_reads = {}
    mission, tasks, stamp, errors, mapping, excerpts, checked = await goal_inputs(
        deps, runtime, validity_reads=validity_reads,
    )
    if mission.status is not MissionStatus.ACTIVE:
        return ["mission_not_active"]
    if errors:
        return errors
    engine = InvestigationEngine(deps)
    engine._event_runtime = runtime
    with llm_request_scope(str(runtime.run_id), "goal_completion"):
        review = GoalReview.model_validate(await engine._ask(
            stage="goal_completion_review",
            payload=goal_review_payload(
                mission=mission, criteria=mission.success_criteria, path=None,
                context={"investigations": [task.model_dump(mode="json") for task in tasks],
                         "excerpt_limit": EXCERPT_CHARS},
                task_evidence=mapping, excerpts=excerpts,
            ),
            response_schema=GoalReview.model_json_schema(),
            system=RESULT_REVIEW_INSTRUCTION +
                   " Judge only the original goal criteria. Unrelated open hypotheses/questions "
                   "may be reported without blocking success. A registered follow-up is not proof.",
            budget=budget,
        ))
    criteria = [item.criterion for item in review.criteria]
    if (len(criteria) != len(mission.success_criteria)
            or set(criteria) != set(mission.success_criteria)):
        return ["goal_review_must_cover_original_criteria_exactly"]
    results = []
    service = AttackGraphService(deps.graph_store)
    proof_nodes = await capture_result_graph(
        service, [claim for item in review.criteria for claim in item.claims],
    )
    for item in review.criteria:
        owners = sorted(task for task, refs in mapping.items()
                        if set(item.evidence_ids).intersection(refs))
        available = {ref for refs in mapping.values() for ref in refs}
        if set(item.evidence_ids) - available or (item.met and not item.evidence_ids):
            return ["goal_criterion_originals_unproven"]
        if item.met:
            errors = await check_result_review(
                claims=[claim.model_dump(mode="json") for claim in item.claims],
                evidence_ids=item.evidence_ids, evidence_store=deps.evidence_store,
                graph_service=service, path_store=deps.path_store,
                run_id=str(runtime.run_id), mission_id=str(runtime.mission_id),
                task_graph_store=deps.investigations,
            )
            if errors:
                return errors
        results.append(MissionCriterionResult(
            criterion=item.criterion, met=item.met, task_ids=owners, evidence_ids=item.evidence_ids,
            reason=review.reason,
            result_claims=[claim.model_dump(mode="json") for claim in item.claims],
            purpose_hash=mission_result_purpose(mission), goal_revision=mission.revision,
        ))
    success = review.accepted and all(item.met for item in results)
    needed = goal_required_task_ids(tasks)
    used = needed | {owner for item in results for owner in item.task_ids}
    cited = {identity for item in results for identity in item.evidence_ids}
    receipts = {str(task.result.completion_receipt_id) for task in tasks if task.result
                and task.result.completion_receipt_id}
    current_reads = []
    for owner in used:
        for identity, original_read in validity_reads.get(owner, {}).items():
            if owner not in needed and identity not in cited | receipts:
                continue
            read, errors = await original_read.refresh(deps)
            if errors:
                return errors
            current_reads.append(read)
    refreshed = await check_evidence(
        deps.evidence_store, run_id=str(runtime.run_id), engagement_id=str(runtime.engagement_id),
        evidence_ids=[str(item.record.evidence_id) for item in checked],
    )
    before_hashes = {str(item.record.evidence_id): item.content_hash for item in checked}
    if any(before_hashes[str(item.record.evidence_id)] != item.content_hash for item in refreshed):
        return ["goal_original_evidence_changed_reassessment_required"]
    from redteam.orchestration.code_artifacts import (
        check_fixture_admissions,
        fixture_cleanup_blockers,
        read_fixture_admissions,
    )
    fixture_snapshot = await read_fixture_admissions(deps, str(runtime.run_id))
    fixture_errors = await fixture_cleanup_blockers(deps, runtime)
    if fixture_errors:
        return fixture_errors
    now = datetime.now(timezone.utc)

    def commit(tx):
        current, current_tasks, current_stamp = goal_snapshot(tx)
        tx.admit(settlement=True)
        if current_stamp != stamp:
            raise StateConflict("goal_related_inputs_changed_reassessment_required")
        check_fixture_admissions(tx, fixture_snapshot)
        check_result_graph(tx, proof_nodes)
        if tx.active_activation_count() or any(tx._unsafe_execution(str(t.task_id))
                                              for t in current_tasks):
            raise StateConflict("goal_original_execution_unsettled")
        for task in current_tasks:
            tx._check_external_activity(str(task.task_id))
        original_tasks = {str(task.task_id): task for task in tasks}
        if any(content_hash(tx.get_task(identity)) != content_hash(original_tasks[identity])
               for identity in used):
            raise StateConflict("goal_used_task_changed_reassessment_required")
        for read in current_reads:
            read.check(tx)
        tx.add_evidence_refs(checked)
        tx.update_mission(current.model_copy(update={
            "criterion_results": results, "revision": current.revision + 1, "updated_at": now,
            "status": MissionStatus.COMPLETED if success else current.status,
            "termination_reason": TerminationReason.GOAL_ACHIEVED if success else None,
        }), expected_revision=current.revision)
        if success:
            tx.set_settlement_gate(True)
        tx.queue(event(deps.central_state, "MissionUpdated", {
            "mission_id": str(runtime.mission_id), "status": "completed" if success else "active",
            "goal_revision": mission.revision, "criteria_met": success,
        }))

    await deps.central_state.transaction(commit)
    await deps.event_bus.flush()
    return [] if success else ["goal_criteria_unmet"]
