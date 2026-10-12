"""Fixed Task conditions reuse the existing original-result proof boundary."""

from __future__ import annotations

from typing import Any

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.completion import criterion_id
from redteam.core.contracts.mission import MissionTask
from redteam.orchestration.completion import validate_result_claims
from redteam.orchestration.result_claims import TaskContractReview


async def check_task_conditions(
    task: MissionTask, evidence_ids: list[str], *, deps: Any, run_id: str,
    review: dict[str, Any] | None = None,
) -> list[str]:
    """AND all stored conditions; a model cannot replace or weaken them."""
    if task.completion_contract_version == 1:
        return []
    selected = {criterion_id(item): evidence_ids for item in task.completion_criteria}
    if review is not None:
        parsed = TaskContractReview.model_validate(review)
        rows = parsed.criterion_results
        if len(rows) != len(selected) or {row.criterion_id for row in rows} != set(selected):
            return ["task review must cover each fixed criterion exactly once"]
        if any(not row.met for row in rows):
            return ["task review has an unmet fixed criterion"]
        if any(set(row.evidence_ids) - set(evidence_ids) for row in rows):
            return ["task criterion cites unrelated evidence"]
        selected = {row.criterion_id: row.evidence_ids for row in rows}
    for item in task.completion_criteria:
        errors = await validate_result_claims(
            criteria=[item], evidence_ids=selected[criterion_id(item)],
            target_node_ids=list(task.related_attack_graph_node_ids), principal_refs=[],
            evidence_store=deps.evidence_store,
            graph_service=AttackGraphService(deps.graph_store),
            run_id=run_id, path_store=deps.path_store, mission_id=str(task.mission_id),
            task=task, task_graph_store=getattr(deps, "investigations", deps.task_graph_store),
            work_store=deps.work_store,
        )
        if errors:
            return errors
    return []
