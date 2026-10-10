"""``redteam status <run-id>`` — read what's durably known about a Run.

By default the persisted terminal report (``app.report_store``, written once by
the canonical bootstrap when a run reaches a terminal phase) plus the
Approval store are queried here. A Run still executing in another,
still-live process has no durable status row anywhere in this codebase
today (``RuntimeState`` is in-memory, checkpointed only if the caller
opted into SQLite checkpointing) — this command says so explicitly
rather than guessing. Explicit --activation-db inspects canonical specialist
launch/request records only. It does not open checkpoints/transports, mutate
claims/accounting or grant replay; it is not a live whole-Run status API.
"""

from __future__ import annotations

import argparse

from redteam.app.cli._shared import EXIT_FAILURE, EXIT_OK, eprint, print_json
from redteam.app.report_store import load_terminal_report
from redteam.control.approval import ApprovalService
from redteam.control.stores.filesystem import FilesystemApprovalStore


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("status", help="Show a Run's terminal status")
    parser.add_argument("run_id")
    parser.add_argument("--approval-dir", default="data/approvals")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--activation-db", default=None,
        help="Read-only specialist activation snapshot from an existing coordination SQLite DB",
    )
    parser.set_defaults(handler=run)


async def run(args: argparse.Namespace) -> int:
    if getattr(args, "activation_db", None) is not None:
        return await _activation_diagnostics(args)
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


async def _activation_diagnostics(args: argparse.Namespace) -> int:
    from redteam.orchestration.task_activation import read_activation_diagnostics

    try:
        result = await read_activation_diagnostics(args.activation_db, args.run_id)
    except Exception as exc:  # noqa: BLE001 - bad storage is not absence, never dump payload
        eprint(f"ERROR: activation diagnostics unavailable: {type(exc).__name__}")
        return EXIT_FAILURE
    if result is None:
        if args.json:
            print_json({"run_id": args.run_id, "status": "unknown", "replay_permitted": False})
        else:
            print("No activation snapshot found; execution state is unknown. "
                  "Replay is not authorized.")
        return EXIT_FAILURE
    if args.json:
        print_json(result)
    else:
        print(f"Run: {result.run_id} (read-only activation snapshot)")
        print(f"Observed at: {result.observed_at.isoformat()}")
        for row in result.activations:
            print(f"  {row.instance_id} / {row.task_id}: {row.state}, "
                  f"stage={row.launch_stage or 'legacy'}, reason={row.reason_code}, "
                  f"claim_matches={row.task_claim_matches}, requests={row.request_states}")
        print("Checkpoints/Work/Evidence not inspected. "
              "No replay, release or budget reset authorized.")
    return EXIT_OK
