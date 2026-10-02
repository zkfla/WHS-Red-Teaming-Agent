"""Evidence Store Test: indexed lookup, blocked-action recording,
integrity verification, and retention purge -- the four Track B items
beyond what already existed (append/list_for_run).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.control.audit import AuditLog
from redteam.control.service import DefaultControlPlane
from redteam.control.stores.filesystem import FilesystemAuditStore
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.policy import ControlContext, PolicyDecision
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import EvidenceSourceType, PolicyVerdict
from redteam.core.ids import ActionId, AgentId, ApprovalId, EngagementId, RunId
from redteam.evidence.service import EvidenceService
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from tests.fakes.scope_denials import (
    PERSISTENCE_CASES,
    ScopeDenyCase,
    assert_scope_audit,
    assert_scope_evidence,
)


def _proposal() -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_1"),
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        agent_id=AgentId("agent_1"),
        capability="web",
        operation="auth_attempt",
        target=HostTarget(host="good.local"),
        arguments={"session_token": "super-secret-value"},
        reason="test",
        created_at=datetime.now(timezone.utc),
    )


async def test_record_blocked_action_is_queryable_and_redacted(tmp_path: Path) -> None:
    store = FilesystemEvidenceStore(tmp_path)
    service = EvidenceService(store)
    proposal = _proposal()
    decision = PolicyDecision(
        action_id=proposal.action_id,
        verdict=PolicyVerdict.DENY,
        reasons=["out of scope"],
        matched_rules=["scope"],
        evaluated_at=datetime.now(timezone.utc),
    )

    evidence = await service.record_blocked_action(proposal, decision)
    assert evidence.run_id == proposal.run_id
    assert evidence.engagement_id == proposal.engagement_id
    assert evidence.source_type == EvidenceSourceType.SYSTEM
    assert evidence.source_id == "control_plane"
    assert evidence.action_id == proposal.action_id
    assert evidence.invocation_id is None
    assert evidence.metadata == {
        "record_kind": "blocked_action",
        "action_key": "web.auth_attempt",
        "verdict": "deny",
        "reasons": ["out of scope"],
        "matched_rules": ["scope"],
        "approval_id": None,
    }
    assert evidence.summary == "Blocked action web.auth_attempt: out of scope"
    assert "tool_id" not in evidence.metadata

    by_action = await service.list_for_action(proposal.action_id)
    assert [e.evidence_id for e in by_action] == [evidence.evidence_id]

    fetched = await store.get(evidence.evidence_id)
    assert fetched is not None
    # the raw session token must never reach disk
    stored_line = (tmp_path / "run_1.jsonl").read_text(encoding="utf-8")
    assert "super-secret-value" not in stored_line
    assert "***REDACTED***" not in stored_line  # not embedded verbatim either -- it's hashed


async def test_record_blocked_action_records_require_approval(tmp_path: Path) -> None:
    service = EvidenceService(FilesystemEvidenceStore(tmp_path))
    proposal = _proposal()
    decision = PolicyDecision(
        action_id=proposal.action_id,
        verdict=PolicyVerdict.REQUIRE_APPROVAL,
        reasons=["intrusive action requires approval"],
        matched_rules=["web.auth_attempt"],
        approval_id=ApprovalId("approval_1"),
        evaluated_at=datetime.now(timezone.utc),
    )

    evidence = await service.record_blocked_action(proposal, decision)

    assert evidence.summary == (
        "Approval required for action web.auth_attempt: intrusive action requires approval"
    )
    assert evidence.metadata["verdict"] == "require_approval"
    assert evidence.metadata["approval_id"] == "approval_1"


async def test_record_blocked_action_rejects_allow(tmp_path: Path) -> None:
    service = EvidenceService(FilesystemEvidenceStore(tmp_path))
    proposal = _proposal()
    decision = PolicyDecision(
        action_id=proposal.action_id,
        verdict=PolicyVerdict.ALLOW,
        evaluated_at=datetime.now(timezone.utc),
    )

    with pytest.raises(ValueError, match="only accepts DENY or REQUIRE_APPROVAL"):
        await service.record_blocked_action(proposal, decision)


async def test_record_blocked_action_rejects_action_id_mismatch(tmp_path: Path) -> None:
    service = EvidenceService(FilesystemEvidenceStore(tmp_path))
    proposal = _proposal()
    decision = PolicyDecision(
        action_id=ActionId("different_action"),
        verdict=PolicyVerdict.DENY,
        evaluated_at=datetime.now(timezone.utc),
    )

    with pytest.raises(ValueError, match="decision.action_id does not match"):
        await service.record_blocked_action(proposal, decision)


async def test_record_blocked_action_redacts_sensitive_reason_text(tmp_path: Path) -> None:
    service = EvidenceService(FilesystemEvidenceStore(tmp_path))
    proposal = _proposal()
    decision = PolicyDecision(
        action_id=proposal.action_id,
        verdict=PolicyVerdict.DENY,
        reasons=[
            "token=policy-secret must not be persisted",
            "Cookie: session=browser-secret; csrf=csrf-secret",
            "Authorization: Bearer bearer-secret",
            "session_id=standalone-secret",
        ],
        evaluated_at=datetime.now(timezone.utc),
    )

    evidence = await service.record_blocked_action(proposal, decision)

    assert evidence.metadata["reasons"] == [
        "token=***REDACTED*** must not be persisted",
        "Cookie: ***REDACTED***",
        "Authorization: ***REDACTED***",
        "session_id=***REDACTED***",
    ]
    stored_line = (tmp_path / "run_1.jsonl").read_text(encoding="utf-8")
    for secret in (
        "policy-secret", "browser-secret", "csrf-secret", "bearer-secret", "standalone-secret"
    ):
        assert secret not in stored_line


async def test_record_blocked_action_propagates_store_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemEvidenceStore(tmp_path)
    service = EvidenceService(store)
    proposal = _proposal()
    decision = PolicyDecision(
        action_id=proposal.action_id,
        verdict=PolicyVerdict.DENY,
        evaluated_at=datetime.now(timezone.utc),
    )

    async def fail_append(_evidence: object) -> None:
        raise RuntimeError("evidence store unavailable")

    monkeypatch.setattr(store, "append", fail_append)

    with pytest.raises(RuntimeError, match="evidence store unavailable"):
        await service.record_blocked_action(proposal, decision)


async def test_index_avoids_scanning_unrelated_runs(tmp_path: Path) -> None:
    store = FilesystemEvidenceStore(tmp_path)
    service = EvidenceService(store)
    proposal_a = _proposal()
    proposal_b = _proposal().model_copy(
        update={"action_id": ActionId("action_2"), "run_id": RunId("run_2")}
    )
    decision = PolicyDecision(
        action_id=proposal_a.action_id,
        verdict=PolicyVerdict.DENY,
        evaluated_at=datetime.now(timezone.utc),
    )

    await service.record_blocked_action(proposal_a, decision)
    await service.record_blocked_action(
        proposal_b, decision.model_copy(update={"action_id": proposal_b.action_id})
    )

    only_a = await service.list_for_action(proposal_a.action_id)
    assert len(only_a) == 1
    assert only_a[0].run_id == RunId("run_1")


async def test_verify_run_integrity_detects_tampering(tmp_path: Path) -> None:
    store = FilesystemEvidenceStore(tmp_path)
    service = EvidenceService(store)
    proposal = _proposal()
    decision = PolicyDecision(
        action_id=proposal.action_id,
        verdict=PolicyVerdict.DENY,
        evaluated_at=datetime.now(timezone.utc),
    )
    evidence = await service.record_blocked_action(proposal, decision)

    assert await store.verify_run_integrity(RunId("run_1")) == []

    # Simulate tampering: rewrite the run file with a modified summary.
    run_file = tmp_path / "run_1.jsonl"
    tampered_record = evidence.model_copy(update={"summary": "tampered!"})
    run_file.write_text(tampered_record.model_dump_json() + "\n", encoding="utf-8")

    tampered_ids = await store.verify_run_integrity(RunId("run_1"))
    assert tampered_ids == [evidence.evidence_id]


async def test_verify_run_integrity_detects_deleted_record_and_file(tmp_path: Path) -> None:
    store = FilesystemEvidenceStore(tmp_path)
    service = EvidenceService(store)
    proposal = _proposal()
    decision = PolicyDecision(
        action_id=proposal.action_id,
        verdict=PolicyVerdict.DENY,
        evaluated_at=datetime.now(timezone.utc),
    )
    first = await service.record_blocked_action(proposal, decision)
    second = await service.record_blocked_action(proposal, decision)
    run_file = tmp_path / "run_1.jsonl"
    lines = run_file.read_text(encoding="utf-8").splitlines()

    run_file.write_text(lines[1] + "\n", encoding="utf-8")
    assert await store.verify_run_integrity(proposal.run_id) == [first.evidence_id]

    run_file.unlink()
    assert await store.verify_run_integrity(proposal.run_id) == [
        first.evidence_id, second.evidence_id
    ]


async def test_verify_run_integrity_detects_unindexed_record(tmp_path: Path) -> None:
    store = FilesystemEvidenceStore(tmp_path)
    service = EvidenceService(store)
    proposal = _proposal()
    decision = PolicyDecision(
        action_id=proposal.action_id,
        verdict=PolicyVerdict.DENY,
        evaluated_at=datetime.now(timezone.utc),
    )
    evidence = await service.record_blocked_action(proposal, decision)
    run_file = tmp_path / "run_1.jsonl"
    with run_file.open("a", encoding="utf-8") as fh:
        fh.write(evidence.model_copy(update={"evidence_id": "ev_unindexed"}).model_dump_json())
        fh.write("\n")

    assert await store.verify_run_integrity(proposal.run_id) == ["ev_unindexed"]


async def test_cli_verify_fails_when_run_file_is_missing(tmp_path: Path) -> None:
    store = FilesystemEvidenceStore(tmp_path)
    proposal = _proposal()
    await EvidenceService(store).record_blocked_action(
        proposal,
        PolicyDecision(
            action_id=proposal.action_id,
            verdict=PolicyVerdict.DENY,
            evaluated_at=datetime.now(timezone.utc),
        ),
    )
    (tmp_path / "run_1.jsonl").unlink()

    result = subprocess.run(
        [
            sys.executable, "-m", "redteam.app.cli", "evidence", "verify",
            "--run", "run_1", "--dir", str(tmp_path),
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "missing" in result.stdout


async def test_purge_older_than_removes_old_runs_only(tmp_path: Path) -> None:
    import os
    import time

    store = FilesystemEvidenceStore(tmp_path)
    service = EvidenceService(store)
    proposal = _proposal()
    decision = PolicyDecision(
        action_id=proposal.action_id,
        verdict=PolicyVerdict.DENY,
        evaluated_at=datetime.now(timezone.utc),
    )
    await service.record_blocked_action(proposal, decision)

    # Backdate run_1's file mtime to simulate it being old.
    old_time = time.time() - 90 * 86400
    os.utime(tmp_path / "run_1.jsonl", (old_time, old_time))

    proposal_b = proposal.model_copy(
        update={"action_id": ActionId("action_2"), "run_id": RunId("run_2")}
    )
    await service.record_blocked_action(
        proposal_b, decision.model_copy(update={"action_id": proposal_b.action_id})
    )

    purged = await store.purge_older_than(days=30)
    assert purged == [RunId("run_1")]
    assert not (tmp_path / "run_1.jsonl").exists()
    assert (tmp_path / "run_2.jsonl").exists()

    # index no longer points at the purged run
    remaining_for_action_1 = await service.list_for_action(proposal.action_id)
    assert remaining_for_action_1 == []


@pytest.mark.parametrize("days", [0, -1, float("nan"), float("inf")])
async def test_purge_rejects_invalid_retention_without_deleting(
    tmp_path: Path, days: float
) -> None:
    store = FilesystemEvidenceStore(tmp_path)
    proposal = _proposal()
    decision = PolicyDecision(
        action_id=proposal.action_id,
        verdict=PolicyVerdict.DENY,
        evaluated_at=datetime.now(timezone.utc),
    )
    await EvidenceService(store).record_blocked_action(proposal, decision)

    with pytest.raises(ValueError, match="retention days must be"):
        await store.purge_older_than(days)

    assert (tmp_path / "run_1.jsonl").exists()
    assert len(await store.list_for_run(proposal.run_id)) == 1


@pytest.mark.parametrize("case", PERSISTENCE_CASES, ids=lambda case: case.name)
async def test_scope_deny_survives_filesystem_store_reopen(
    tmp_path: Path, case: ScopeDenyCase
) -> None:
    proposal = _proposal().model_copy(update={"target": case.target, "arguments": {}})
    engagement = Engagement(
        engagement_id=proposal.engagement_id,
        name="scope recording regression",
        scope=case.scope,
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )
    audit_dir = tmp_path / "audit"
    evidence_dir = tmp_path / "evidence"
    audit = AuditLog(FilesystemAuditStore(audit_dir))
    evidence = FilesystemEvidenceStore(evidence_dir)
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        audit_log=audit,
        evidence_service=EvidenceService(evidence),
    )
    decision = await plane.evaluate(
        proposal, ControlContext(engagement_id=engagement.engagement_id)
    )
    initial_audit = await audit.entries_for_run(proposal.run_id)
    initial_evidence = await evidence.list_for_run(proposal.run_id)
    assert len(initial_audit) == len(initial_evidence) == 1

    reopened_audit = FilesystemAuditStore(audit_dir)
    reopened_evidence = FilesystemEvidenceStore(evidence_dir)
    audit_by_run = await reopened_audit.list_for_run(proposal.run_id)
    audit_by_action = await reopened_audit.list_for_action(proposal.action_id)
    evidence_by_run = await reopened_evidence.list_for_run(proposal.run_id)
    evidence_by_action = await reopened_evidence.list_for_action(proposal.action_id)
    assert audit_by_run == audit_by_action == initial_audit
    assert evidence_by_run == evidence_by_action == initial_evidence
    record = evidence_by_run[0]
    assert await reopened_evidence.get(record.evidence_id) == record
    assert_scope_audit(proposal, decision, audit_by_run[0], case)
    assert_scope_evidence(proposal, decision, record)
    assert await reopened_evidence.verify_run_integrity(proposal.run_id) == []
