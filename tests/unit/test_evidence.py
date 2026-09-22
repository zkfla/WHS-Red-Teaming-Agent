"""Evidence Test: a ToolResult is converted into an EvidenceRecord and persisted,
keeping correlation to the Action/Invocation that produced it."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from redteam.core.contracts.tool import ToolResult
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId
from redteam.evidence.service import EvidenceService
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore


async def test_record_tool_result_persists_evidence(tmp_path: Path) -> None:
    store = FilesystemEvidenceStore(tmp_path)
    service = EvidenceService(store)

    run_id = RunId("run_1")
    engagement_id = EngagementId("eng_1")
    result = ToolResult(
        invocation_id=InvocationId("inv_1"),
        action_id=ActionId("action_1"),
        tool_id=ToolId("example.ping"),
        success=True,
        output={"echo": {"message": "hi"}},
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )

    evidence = await service.record_tool_result(run_id, engagement_id, result)

    stored = await service.list_for_run(run_id)
    assert stored == [evidence]
    assert stored[0].action_id == ActionId("action_1")
    assert stored[0].invocation_id == InvocationId("inv_1")
    assert stored[0].engagement_id == engagement_id
    assert stored[0].digest

    by_action = await service.list_for_action(ActionId("action_1"))
    assert by_action == [evidence]
