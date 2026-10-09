"""TASK-004A: shell evidence through the existing graph.record pointers.

Existing-contract parts only (no remote subject needed): a shell output
envelope is an ordinary preserved dict, so the stable raw_path / value /
run / same-run checks already address it; targetless shell results stay
local (refused, never invented); tool success is never a fact. The
remote L3 hook/response subject contract stays on HOLD: nothing here
wires admissions into _write_fact.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redteam.attack_graph.identity import node_id_for_target, node_type_for_target
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolError, ToolResult
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import (
    ActionId,
    AgentId,
    EngagementId,
    EvidenceId,
    GraphNodeId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.orchestration.graph_recording import GraphRecordingHandler, RecordedFact
from redteam.orchestration.observations.mapper import ResultMapperRegistry
from redteam.orchestration.observations.shell_execution import (
    is_shell_execution_envelope,
    shell_execution_accounting,
)

NOW = datetime.now(timezone.utc)
RUN = RunId("run_shell_004a")
ENGAGEMENT = EngagementId("eng_shell_004a")
AGENT = AgentId("recon-agent")
TOOL = ToolId("analysis.shell_run")
HOST = "target.local"
TARGET = HostTarget(host=HOST)
TARGET_NODE = str(node_id_for_target(TARGET))
WORKROOM_KEY = str(RUN) + ".instance-1"

ENVELOPE = {
    "exit_code": 0,
    "stdout": "uid=65532 gid=65532 groups=65532",
    "stderr": "",
    "timed_out": False,
    "truncated": False,
    "duration_ms": 41,
    "script_sha256": "a" * 64,
    "workroom_key": WORKROOM_KEY,
}


class _MemoryStore:
    """Minimal evidence store fake: get by id only."""

    def __init__(self, records: list[EvidenceRecord]) -> None:
        self._records = {str(item.evidence_id): item for item in records}

    async def get(self, evidence_id: str) -> EvidenceRecord | None:
        return self._records.get(str(evidence_id))


def _proposal(*, targeted: bool) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action-1"),
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        agent_id=AGENT,
        binding_mode="selected_tool",
        tool_id=TOOL,
        capability="analysis",
        operation="shell_run",
        target=TARGET if targeted else None,
        arguments={"language": "bash", "script": "id"},
        reason="run a local shell probe",
        created_at=NOW,
    )


def _result(*, success: bool) -> ToolResult:
    return ToolResult(
        invocation_id=InvocationId("inv-1"),
        action_id=ActionId("action-1"),
        tool_id=TOOL,
        success=success,
        output=dict(ENVELOPE),
        error=None if success else ToolError(code="shell_exit", message="exit 1"),
        started_at=NOW,
        finished_at=NOW,
        metadata={},
    )


def _record(proposal: ActionProposal, result: ToolResult, targeted: bool) -> EvidenceRecord:
    metadata: dict[str, Any] = {
        "success": result.success,
        "tool_result": result.model_dump(mode="json"),
        "action_id": str(proposal.action_id),
        "invocation_id": str(result.invocation_id),
        "tool_id": str(result.tool_id),
        "capability": proposal.capability,
        "operation": proposal.operation,
        "proposal": proposal.model_dump(mode="json"),
    }
    if targeted:
        metadata["target_node_id"] = TARGET_NODE
    return EvidenceRecord(
        evidence_id=EvidenceId("ev-shell-1"),
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        source_type=EvidenceSourceType.TOOL,
        source_id=str(TOOL),
        action_id=proposal.action_id,
        invocation_id=result.invocation_id,
        content_type="application/json",
        digest="test-digest",
        summary="shell evidence",
        created_at=NOW,
        metadata=metadata,
    )


async def _write(record: EvidenceRecord, fact: RecordedFact, svc: AttackGraphService):
    handler = GraphRecordingHandler()
    accepted: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    await handler._write_fact(
        fact,
        store=_MemoryStore([record]),
        service=svc,
        run_id=str(RUN),
        engagement_id=str(ENGAGEMENT),
        step=1,
        accepted_facts=accepted,
        refused_facts=refused,
        record_ids=[],
    )
    return accepted, refused


async def _service_with_target() -> AttackGraphService:
    service = AttackGraphService(InMemoryAttackGraphStore())
    await service.upsert_node(node_type_for_target(TARGET), node_id=GraphNodeId(TARGET_NODE))
    return service


def _fact(**overrides: Any) -> RecordedFact:
    params: dict[str, Any] = {
        "evidence_id": "ev-shell-1",
        "raw_path": ["output", "exit_code"],
        "value": 0,
        "fact_name": "shell_exit_code",
        "target_node_id": TARGET_NODE,
    }
    params.update(overrides)
    return RecordedFact(**params)
    params.update(overrides)
    return RecordedFact(**params)


async def test_shell_envelope_pointer_records_fact_with_subject():
    svc = await _service_with_target()
    record = _record(_proposal(targeted=True), _result(success=True), True)
    accepted, refused = await _write(record, _fact(), svc)
    assert refused == []
    assert len(accepted) == 1
    assert accepted[0]["target_node_id"] == TARGET_NODE
    assert accepted[0]["fact_name"] == "shell_exit_code"
    node = await svc.get_node(GraphNodeId(TARGET_NODE))
    assert node is not None


async def test_targetless_shell_evidence_names_no_target():
    svc = await _service_with_target()
    record = _record(_proposal(targeted=False), _result(success=True), False)
    accepted, refused = await _write(record, _fact(), svc)
    assert accepted == []
    assert [item["reason"] for item in refused] == ["evidence names no target"]


async def test_targetless_shell_fact_without_target_linkage():
    svc = await _service_with_target()
    record = _record(_proposal(targeted=False), _result(success=True), False)
    accepted, refused = await _write(record, _fact(target_node_id=" "), svc)
    assert accepted == []
    assert [item["reason"] for item in refused] == ["no target linkage"]


async def test_failed_shell_result_still_records_matching_pointer():
    svc = await _service_with_target()
    record = _record(_proposal(targeted=True), _result(success=False), True)
    accepted, refused = await _write(record, _fact(), svc)
    assert refused == []
    assert len(accepted) == 1


async def test_tool_success_flag_is_not_addressable_as_fact():
    svc = await _service_with_target()
    record = _record(_proposal(targeted=True), _result(success=True), True)
    fact = _fact(raw_path=["success"], value=True, fact_name="shell_succeeded")
    accepted, refused = await _write(record, fact, svc)
    assert accepted == []
    assert [item["reason"] for item in refused] == ["raw path missing"]


async def test_shell_success_alone_records_nothing():
    svc = await _service_with_target()
    record = _record(_proposal(targeted=True), _result(success=True), True)
    fact = _fact(raw_path=["output", "missing_key"], value="x", fact_name="shell_claim")
    accepted, refused = await _write(record, fact, svc)
    assert accepted == []
    assert [item["reason"] for item in refused] == ["raw path missing"]


async def test_shell_mapper_outcome_stays_evidence_only():
    registry = ResultMapperRegistry()
    result = _result(success=True)
    targetless = registry.map_result_with_outcome(
        tool_id=TOOL, target=None, arguments={}, result=result
    )
    assert targetless.status == "not_applicable"
    assert targetless.mutation.nodes == []
    assert targetless.mutation.edges == []
    assert targetless.mutation.findings == []
    targeted = registry.map_result_with_outcome(
        tool_id=TOOL, target=TARGET, arguments={}, result=result
    )
    assert targeted.status == "no_mapper"
    assert targeted.mutation.nodes == []
    assert targeted.mutation.edges == []
    assert targeted.mutation.findings == []


def test_shell_accounting_counts_never_content():
    accounting = shell_execution_accounting(dict(ENVELOPE))
    assert accounting is not None
    assert accounting["exit_code"] == 0
    assert accounting["stdout_chars"] == len(ENVELOPE["stdout"])
    assert accounting["stderr_chars"] == 0
    assert "uid=65532" not in str(accounting)
    assert shell_execution_accounting({"output": "not-an-envelope"}) is None
    assert shell_execution_accounting(None) is None


def test_shell_envelope_shape_checks():
    assert is_shell_execution_envelope(dict(ENVELOPE)) is True
    bad = dict(ENVELOPE)
    bad["exit_code"] = True
    assert is_shell_execution_envelope(bad) is False
    missing = dict(ENVELOPE)
    del missing["stdout"]
    assert is_shell_execution_envelope(missing) is False
    assert is_shell_execution_envelope("stdout") is False
