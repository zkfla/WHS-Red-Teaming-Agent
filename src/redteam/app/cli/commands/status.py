"""Read current central Run state, including active and suspended investigations.

The default reader inspects the central database and preserved originals without
starting actors, replaying checkpoints or creating/upgrading a database. Explicit
legacy selection reads a derived terminal report. --activation-db and its optional
original Work/Evidence snapshots remain read-only compatibility diagnostics;
they do not authorize reconciliation, resource release or budget reset.
"""

from __future__ import annotations

import argparse

from redteam.app.cli._shared import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_OK,
    eprint,
    print_json,
    read_run_report,
)
from redteam.control.approval import ApprovalService
from redteam.control.stores.filesystem import FilesystemApprovalStore


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("status", help="Show a Run's terminal status")
    parser.add_argument("run_id")
    parser.add_argument("--approval-dir", default="data/approvals")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--state-db", default=None)
    parser.add_argument(
        "--activation-db", default=None,
        help="Read-only specialist activation snapshot from an existing coordination SQLite DB",
    )
    parser.add_argument(
        "--work-db", default=None,
        help="Inspect existing Work SQLite with --activation-db and --evidence-dir; no recovery",
    )
    parser.add_argument(
        "--evidence-dir", default=None,
        help="Inspect original evidence with --activation-db and --work-db; no provider calls",
    )
    parser.set_defaults(handler=run)


async def run(args: argparse.Namespace) -> int:
    external = (getattr(args, "work_db", None), getattr(args, "evidence_dir", None))
    if any(value is not None for value in external) and (
        not all(value is not None for value in external)
        or getattr(args, "activation_db", None) is None
    ):
        eprint("ERROR: --work-db and --evidence-dir require each other and --activation-db")
        return EXIT_CONFIG_ERROR
    if getattr(args, "activation_db", None) is not None:
        return await _activation_diagnostics(args)
    try:
        report = await read_run_report(args)
    except (ValueError, OSError, RuntimeError) as exc:
        eprint(f"ERROR: central status unavailable: {exc}")
        return EXIT_FAILURE
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
    if report.previous_termination_reason:
        print(f"Previous termination reason: {report.previous_termination_reason}")
    print(f"\nActions executed: {report.actions_executed}")
    if report.work_status_counts:
        print("Work items by status:")
        for status, count in report.work_status_counts.items():
            print(f"  {status}: {count}")
    if report.effective_limits:
        print(f"Applied limits: {report.effective_limits}")
    for execution in report.executions:
        if execution.get("wait_reason") or execution["state"] == "unknown":
            print(f"  {execution['task_id']} / {execution['invocation_id']}: "
                  f"{execution['state']}, wait={execution.get('wait_reason')}, "
                  f"approval={execution.get('approval_ref')}, job={execution.get('job_ref')}")
    print(f"Evidence recorded: {report.evidence_count}")
    if report.candidates:
        candidate_counts: dict[str, int] = {}
        for candidate in report.candidates:
            candidate_counts[candidate.status] = candidate_counts.get(candidate.status, 0) + 1
        print(f"Candidates: {dict(sorted(candidate_counts.items()))}")
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

    execution = None
    changed = False
    try:
        result = await read_activation_diagnostics(args.activation_db, args.run_id)
        if result is not None and getattr(args, "work_db", None) is not None:
            from redteam.core.ids import RunId
            from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
            from redteam.orchestration.execution_diagnostics import inspect_execution_diagnostics
            from redteam.orchestration.work.sqlite import SQLiteWorkStore

            run_id = RunId(args.run_id)
            work = await SQLiteWorkStore.inspect_run(args.work_db, run_id)
            evidence = await FilesystemEvidenceStore.inspect_run(args.evidence_dir, run_id)
            execution = await inspect_execution_diagnostics(
                run_id, work_items=work, evidence_store=evidence, activations=result.activations,
            )
            after = await read_activation_diagnostics(args.activation_db, args.run_id)
            changed = after is None or result.model_dump(exclude={"observed_at"}) != (
                after.model_dump(exclude={"observed_at"})
            )
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
        payload = result.model_dump(mode="json")
        if execution is not None:
            payload = {
                "schema_version": 2, "run_id": result.run_id, "replay_permitted": False,
                "external_stores": "independently_inspected", "activation": payload,
                "activation_projection_changed": changed,
                "execution": execution.model_dump(mode="json"),
            }
        print_json(payload)
    else:
        print(f"Run: {result.run_id} (read-only activation snapshot)")
        print(f"Observed at: {result.observed_at.isoformat()}")
        for row in result.activations:
            print(f"  {row.instance_id} / {row.task_id}: {row.state}, "
                  f"stage={row.launch_stage or 'legacy'}, reason={row.reason_code}, "
                  f"claim_matches={row.task_claim_matches}, requests={row.request_states}")
        if execution is not None:
            print(f"Work: {execution.work_store}; Evidence: {execution.evidence_store}; "
                  f"activation_projection_changed={changed}")
            for executed in execution.executions:
                print(f"  invocation={executed.invocation_id or 'none'}, "
                      f"work={executed.work_id or 'none'}: {executed.state}, "
                      f"reason={executed.reason_code}, "
                      f"activation_matches={executed.activation_matches}, "
                      f"work_result_matches={executed.work_result_matches}")
            print("Stores inspected independently; checkpoints/providers not inspected.")
        else:
            print("Checkpoints/Work/Evidence not inspected.")
        print("No replay, release or budget reset authorized.")
    return EXIT_OK
