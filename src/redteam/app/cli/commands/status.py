"""``redteam status <run-id>`` — read what's durably known about a Run.

Only the persisted terminal report (``app.report_store``, written once by
the canonical bootstrap when a run reaches a terminal phase) plus the
Approval store are queried here. A Run still executing in another,
still-live process has no durable status row anywhere in this codebase
today (``RuntimeState`` is in-memory, checkpointed only if the caller
opted into SQLite checkpointing) — this command says so explicitly
rather than guessing.
"""

from __future__ import annotations

import argparse

from redteam.app.cli._shared import EXIT_FAILURE, EXIT_OK, print_json
from redteam.app.report_store import load_terminal_report
from redteam.control.approval import ApprovalService
from redteam.control.stores.filesystem import FilesystemApprovalStore


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("status", help="Show a Run's terminal status")
    parser.add_argument("run_id")
    parser.add_argument("--approval-dir", default="data/approvals")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=run)


async def run(args: argparse.Namespace) -> int:
    report = load_terminal_report(args.run_id)
    if report is None:
        if args.json:
            print_json({"run_id": args.run_id, "status": "unknown"})
        else:
            print(f"Run: {args.run_id}")
            print(
                "Status: UNKNOWN -- no terminal report on disk. Either this Run is "
                "still executing in another process (this CLI does not support "
                "querying an in-progress Run in a separate process), the run_id is "
                "wrong, or it has not reached a terminal phase yet."
            )
        return EXIT_FAILURE

    if args.json:
        print_json(report)
        return EXIT_OK

    print(f"Run: {report.run_id}")
    print(f"Status: {report.phase.value.upper()}")
    if report.termination_reason:
        print(f"Termination reason: {report.termination_reason}")
    print(f"\nActions executed: {report.actions_executed}")
    if report.work_status_counts:
        print("Work items by status:")
        for status, count in report.work_status_counts.items():
            print(f"  {status}: {count}")
    print(f"Evidence recorded: {report.evidence_count}")
    print(f"Findings: {report.finding_count}")
    if report.pending_approval_id:
        approvals = ApprovalService(FilesystemApprovalStore(args.approval_dir))
        approval = await approvals.get(report.pending_approval_id)
        approval_status = approval.status.value if approval else "unknown"
        print(f"Pending approval: {report.pending_approval_id} ({approval_status})")
    if report.integrity_blockers:
        print(f"Integrity blockers: {len(report.integrity_blockers)}")
    print(f"\nReady for release: {report.ready_for_release}")
    return EXIT_OK
