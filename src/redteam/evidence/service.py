"""Evidence convenience API. Owned by Control Team.

Builds on ``redteam.core.convert.tool_result_to_evidence`` (the
mechanical ToolResult -> EvidenceRecord conversion PM defines once so
it isn't duplicated between this service and orchestration's
``verify_node``).

TODO(control-team): retention policy, digest verification on read,
richer ``metadata`` conventions per ``source_type``.
"""

from __future__ import annotations

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.tool import ToolResult
from redteam.core.convert import tool_result_to_evidence
from redteam.core.ids import ActionId, EngagementId, HypothesisId, RunId
from redteam.core.ports.evidence_store import EvidenceStore


class EvidenceService:
    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    async def record_tool_result(
        self,
        run_id: RunId,
        engagement_id: EngagementId,
        result: ToolResult,
        *,
        hypothesis_id: HypothesisId | None = None,
        summary: str | None = None,
    ) -> EvidenceRecord:
        evidence = tool_result_to_evidence(
            result,
            run_id=run_id,
            engagement_id=engagement_id,
            hypothesis_id=hypothesis_id,
            summary=summary,
        )
        await self._store.append(evidence)
        return evidence

    async def list_for_run(self, run_id: RunId) -> list[EvidenceRecord]:
        return await self._store.list_for_run(run_id)

    async def list_for_action(self, action_id: ActionId) -> list[EvidenceRecord]:
        return await self._store.list_for_action(action_id)
