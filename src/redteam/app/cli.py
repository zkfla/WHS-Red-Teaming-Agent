"""``python -m redteam.app.cli run examples/engagement.yaml``
``python -m redteam.app.cli approvals list``
``python -m redteam.app.cli approvals resolve <approval_id> approved --by <name>``
``python -m redteam.app.cli approvals expire --ttl-seconds <n>``
``python -m redteam.app.cli evidence verify --run <run_id>``
``python -m redteam.app.cli evidence purge --days <n>``

``run`` prints every published event, then the final ``RuntimeState``.

``approvals resolve`` recovers the original run and engagement from the
persisted policy-decision Audit entry before updating the Approval.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os

from redteam.app.bootstrap import build_runtime_deps, load_engagement, run_engagement
from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
from redteam.core.contracts.events import EventEnvelope
from redteam.core.enums import ApprovalStatus
from redteam.core.ids import ApprovalId, RunId
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore


async def _print_event(event: EventEnvelope) -> None:
    print(f"[{event.event_type}] {event.source} :: {event.payload}")


async def _run(engagement_path: str) -> None:
    engagement = load_engagement(engagement_path)
    deps = await build_runtime_deps(engagement)
    await deps.event_bus.subscribe("*", _print_event)

    final_state = await run_engagement(engagement, deps=deps)
    print(
        f"\nRun finished: phase={final_state.phase.value} "
        f"step_count={final_state.step_count} last_verdict={final_state.last_verdict}"
    )


async def _approvals_list(approval_dir: str) -> None:
    service = ApprovalService(FilesystemApprovalStore(approval_dir))
    pending = await service.list_pending()
    if not pending:
        print("No pending approvals.")
        return
    for approval in pending:
        print(
            f"{approval.approval_id}  action={approval.action_id}  "
            f"requested_at={approval.requested_at.isoformat()}"
        )


async def _approvals_resolve(
    approval_dir: str, approval_id: str, status: str, resolved_by: str, audit_dir: str
) -> None:
    service = ApprovalService(FilesystemApprovalStore(approval_dir))
    approval = await service.get(ApprovalId(approval_id))
    if approval is None:
        raise KeyError(f"no such approval: {approval_id}")
    audit = AuditLog(FilesystemAuditStore(audit_dir))
    request = await audit.find_approval_request(approval.action_id, approval.approval_id)
    if request is None:
        raise LookupError(f"no policy decision audit entry for approval: {approval_id}")
    updated = await service.resolve(
        ApprovalId(approval_id), ApprovalStatus(status), resolved_by
    )
    await audit.record_approval_resolution_from_request(request, updated)
    print(f"{updated.approval_id} -> {updated.status.value} (by {updated.resolved_by})")


async def _approvals_expire(approval_dir: str, ttl_seconds: float) -> None:
    service = ApprovalService(FilesystemApprovalStore(approval_dir))
    expired = await service.sweep_expired(ttl_seconds)
    print(f"Expired {len(expired)} approval(s).")
    for approval in expired:
        print(f"  {approval.approval_id}  action={approval.action_id}")


async def _evidence_verify(evidence_dir: str, run_id: str) -> bool:
    store = FilesystemEvidenceStore(evidence_dir)
    tampered = await store.verify_run_integrity(RunId(run_id))
    if not tampered:
        print(f"run {run_id}: OK, no tampering detected")
        return True
    else:
        print(f"run {run_id}: {len(tampered)} record(s) changed, missing, or unindexed:")
        for evidence_id in tampered:
            print(f"  {evidence_id}")
        return False


async def _evidence_purge(evidence_dir: str, days: float) -> None:
    store = FilesystemEvidenceStore(evidence_dir)
    purged = await store.purge_older_than(days)
    if not purged:
        print("Nothing to purge.")
        return
    print(f"Purged {len(purged)} run(s):")
    for run_id in purged:
        print(f"  {run_id}")


def main() -> None:
    logging.basicConfig(level=os.environ.get("REDTEAM_LOG_LEVEL", "INFO"))

    parser = argparse.ArgumentParser(prog="redteam")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Run an engagement end-to-end (minimal vertical slice)"
    )
    run_parser.add_argument("engagement_path")

    approvals_parser = subparsers.add_parser(
        "approvals", help="Inspect/resolve pending Approvals (human-in-the-loop)"
    )
    approvals_sub = approvals_parser.add_subparsers(dest="approvals_command", required=True)

    list_parser = approvals_sub.add_parser("list", help="List pending approvals")
    list_parser.add_argument("--dir", default="data/approvals", dest="approval_dir")

    resolve_parser = approvals_sub.add_parser("resolve", help="Approve or reject one")
    resolve_parser.add_argument("approval_id")
    resolve_parser.add_argument("status", choices=["approved", "rejected"])
    resolve_parser.add_argument("--by", required=True, dest="resolved_by")
    resolve_parser.add_argument("--dir", default="data/approvals", dest="approval_dir")
    resolve_parser.add_argument("--audit-dir", default="data/audit")

    expire_parser = approvals_sub.add_parser("expire", help="Expire pending approvals past a TTL")
    expire_parser.add_argument("--ttl-seconds", required=True, type=float)
    expire_parser.add_argument("--dir", default="data/approvals", dest="approval_dir")

    evidence_parser = subparsers.add_parser(
        "evidence", help="Verify integrity / apply retention on the persisted Evidence Store"
    )
    evidence_sub = evidence_parser.add_subparsers(dest="evidence_command", required=True)

    verify_parser = evidence_sub.add_parser("verify", help="Check a run's evidence for tampering")
    verify_parser.add_argument("--run", required=True, dest="run_id")
    verify_parser.add_argument("--dir", default="data/evidence", dest="evidence_dir")

    purge_parser = evidence_sub.add_parser("purge", help="Delete runs past the retention window")
    purge_parser.add_argument("--days", required=True, type=float)
    purge_parser.add_argument("--dir", default="data/evidence", dest="evidence_dir")

    args = parser.parse_args()
    if args.command == "run":
        asyncio.run(_run(args.engagement_path))
    elif args.command == "approvals":
        if args.approvals_command == "list":
            asyncio.run(_approvals_list(args.approval_dir))
        elif args.approvals_command == "resolve":
            asyncio.run(
                _approvals_resolve(
                    args.approval_dir, args.approval_id, args.status, args.resolved_by,
                    args.audit_dir,
                )
            )
        elif args.approvals_command == "expire":
            if not math.isfinite(args.ttl_seconds) or args.ttl_seconds < 0:
                parser.error("approvals expire --ttl-seconds must be finite and non-negative")
            asyncio.run(_approvals_expire(args.approval_dir, args.ttl_seconds))
    elif args.command == "evidence":
        if args.evidence_command == "verify":
            if not asyncio.run(_evidence_verify(args.evidence_dir, args.run_id)):
                raise SystemExit(1)
        elif args.evidence_command == "purge":
            if not math.isfinite(args.days) or args.days < 0:
                parser.error("evidence purge --days must be finite and non-negative")
            asyncio.run(_evidence_purge(args.evidence_dir, args.days))


if __name__ == "__main__":
    main()
