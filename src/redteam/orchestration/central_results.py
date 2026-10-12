"""Central Task result ownership, reusing original evidence and completion review.

Questions remain result items. An unresolved investigation can finish without
claiming positive criteria or a Finding. The owned planner settles the goal.
"""

from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict

from redteam.core.contracts.events import EventType
from redteam.core.contracts.mission import InvestigationResult, ResultQuestion
from redteam.core.ids import EvidenceId
from redteam.orchestration.central_runtime import event, runtime_token
from redteam.orchestration.central_state import check_evidence
from redteam.orchestration.completion_proposal import (
    CompletionProposal,
    record_unresolved,
    validate_unresolved,
)
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.local_tools import LocalToolDescriptor, LocalToolResult

# An open EventEnvelope type; no schema/table or independent question state.
RESULT_UPDATED = "InvestigationResultUpdated"


class ResultPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: InvestigationResult


def result_for(
    task, *, outcome, summary, evidence_ids, questions=(), receipt_id=None, hypothesis_ids=()
):
    prior = task.result
    retained = {question.question_id: question for question in prior.questions} if prior else {}
    for question in questions:
        if question.question_id in retained and retained[question.question_id] != question:
            raise StateConflict("original_result_question_changed")
        retained[question.question_id] = question
    return InvestigationResult(
        result_id=prior.result_id if prior else "result_" + content_hash(str(task.task_id)),
        outcome=outcome,
        summary=summary,
        evidence_ids=list(dict.fromkeys([*(prior.evidence_ids if prior else []), *evidence_ids])),
        questions=list(retained.values()),
        hypothesis_ids=list(
            dict.fromkeys([*(prior.hypothesis_ids if prior else []), *hypothesis_ids])
        ),
        completion_receipt_id=receipt_id,
    )


async def commit_result(
    deps, runtime, task, result, token, *, final, validity_read=None, graph_proof=None,
):
    checked = await check_evidence(
        deps.evidence_store,
        run_id=str(runtime.run_id),
        engagement_id=str(runtime.engagement_id),
        evidence_ids=[
            *map(str, result.evidence_ids),
            *(str(ref) for q in result.questions for ref in q.evidence_ids),
            *([str(result.completion_receipt_id)] if result.completion_receipt_id else []),
        ],
    )
    notification = event(
        deps.central_state,
        EventType.TASK_COMPLETED if final else RESULT_UPDATED,
        {"task_id": str(task.task_id), "result_id": result.result_id, "outcome": result.outcome},
    )

    def commit(tx):
        mission = tx.admit()
        current = tx.get_task(str(task.task_id))
        if (
            mission.revision != token[0]
            or content_hash(mission) != token[1]
            or content_hash(current) != content_hash(task)
        ):
            raise StateConflict("result_related_inputs_changed")
        if validity_read is not None:
            validity_read.check(tx)
        if graph_proof is not None:
            from redteam.orchestration.result_claims import check_result_graph

            check_result_graph(tx, graph_proof)
        if final:
            saved = tx.finish_task(
                str(task.task_id),
                result,
                owner=runtime.work_owner,
                version=runtime.task_ownership_version,
                checked=checked,
            )
        else:
            saved = tx.publish_result(
                str(task.task_id),
                result,
                owner=runtime.work_owner,
                version=runtime.task_ownership_version,
                expected_hash=content_hash(task),
                checked=checked,
            )
        tx.queue(notification)
        return saved

    saved = await deps.central_state.transaction(commit)
    await deps.event_bus.flush()
    return saved


async def publish_result_handler(tool_input, context):
    deps, runtime = context.deps, context.runtime
    if deps.central_state is None:
        return LocalToolResult(ok=False, error="central development selection required")
    token = await runtime_token(deps, runtime)
    publication = ResultPublication.model_validate(tool_input)
    result = publication.result
    if result.outcome == "confirmed" or result.finding_node_ids or result.completion_receipt_id:
        return LocalToolResult(
            ok=False, error="positive results require the existing settlement review"
        )
    task = await deps.central_state.get_task(str(runtime.active_task_id))
    from redteam.attack_graph.service import AttackGraphService

    service = AttackGraphService(deps.graph_store)
    for identity in result.hypothesis_ids:
        if await service.get_hypothesis(str(identity)) is None:
            return LocalToolResult(ok=False, error="result refers to an unknown hypothesis")
    merged = result_for(
        task,
        outcome=result.outcome,
        summary=result.summary,
        evidence_ids=result.evidence_ids,
        questions=result.questions,
        hypothesis_ids=result.hypothesis_ids,
    )
    if result.result_id != merged.result_id:
        # The producer chooses the first result identity; thereafter it is stable.
        if task.result is not None:
            return LocalToolResult(ok=False, error="original result identity changed")
        merged = merged.model_copy(update={"result_id": result.result_id})
    saved = await commit_result(deps, runtime, task, merged, token, final=False)
    return LocalToolResult(
        ok=True,
        payload={"task_id": str(saved.task_id), "result": saved.result.model_dump(mode="json")},
        refs=tuple(map(str, merged.evidence_ids)),
    )


def register_result_tool(registry):
    registry.register(
        LocalToolDescriptor(
            tool_id="task.publish_result",
            contribution_id="central-investigation-result",
            version="1",
            description="Publish an intermediate investigation result and its follow-up questions "
            "while retaining the task. Cite preserved originals. Confirmed results require "
            "task.settle review. Follow-ups refer to this task/result/question triple.",
            input_schema=ResultPublication.model_json_schema(),
        ),
        publish_result_handler,
    )


async def complete_reviewed_result(
    deps, runtime, task, token, receipt, proposal, *, validity_read, graph_proof,
):
    """The existing positive origin/conditions/claims review has already succeeded."""
    result = result_for(
        task,
        outcome="confirmed" if proposal.outcome == "success" else "refuted",
        summary=proposal.reason or receipt.summary,
        evidence_ids=proposal.evidence_ids,
        receipt_id=receipt.evidence_id,
    )
    await commit_result(deps, runtime, task, result, token, final=True,
                        validity_read=validity_read, graph_proof=graph_proof)


async def complete_unresolved_result(deps, runtime, task, token, proposal):
    """Shared public/native settlement; an investigation gap grants no success proof."""
    errors = await validate_unresolved(
        proposal, evidence_store=deps.evidence_store, runtime=runtime,
        target_node_ids=list(task.related_attack_graph_node_ids),
    )
    if errors:
        return errors
    gap_id = await record_unresolved(
        evidence_store=deps.evidence_store, runtime=runtime, proposal=proposal,
        path=None, task_id=str(task.task_id),
    )
    questions = [ResultQuestion(
        question_id="question_" + content_hash([str(task.task_id), question]),
        question=question, evidence_ids=[EvidenceId(ref) for ref in gap.evidence_ids],
    ) for gap in proposal.gaps for question in gap.unresolved_questions]
    result = result_for(
        task, outcome=proposal.outcome, summary=proposal.reason,
        evidence_ids=[*map(EvidenceId, proposal.evidence_ids), gap_id], questions=questions,
    )
    await commit_result(deps, runtime, task, result, token, final=True)
    runtime.unresolved_evidence_ids = list(dict.fromkeys([
        *runtime.unresolved_evidence_ids, gap_id,
    ]))
    return []


async def settle_central(scope, proposal: CompletionProposal, context):
    deps, runtime = context.deps, context.runtime
    if scope == "goal":
        return LocalToolResult(ok=False, error="goal settlement belongs to the owned main planner")
    token = await runtime_token(deps, runtime)
    task = await deps.central_state.get_task(str(runtime.active_task_id))
    if not proposal.unresolved:
        from redteam.orchestration.engine import InvestigationEngine
        from redteam.orchestration.semantic_completion import complete_task

        engine = InvestigationEngine(deps)
        engine._event_runtime = runtime
        errors = await complete_task(
            engine,
            None,
            SimpleNamespace(
                completion=proposal.model_dump(mode="json"),
                reason=proposal.reason,
            ),
            runtime,
            {},
            budget=context.budget,
        )
        if errors:
            return LocalToolResult(ok=False, error="; ".join(errors))
    else:
        errors = await complete_unresolved_result(deps, runtime, task, token, proposal)
        if errors:
            return LocalToolResult(ok=False, error="; ".join(errors))
    return LocalToolResult(
        ok=True,
        payload={"scope": scope, "outcome": proposal.outcome},
        refs=tuple(proposal.evidence_ids),
        runtime_outcome="task_completed",
    )
