"""``redteam findings list <run-id>`` — read the persisted terminal report.

No separate Finding model for the CLI: ``FindingProjection``
(``orchestration.reporting``) is exactly what the Attack Graph/Evidence
Store already produced for this run — see ``app.report_store`` for why
that has to be read from the persisted terminal report rather than the
(non-durable) Attack Graph directly once the run's process has exited.
"""

from __future__ import annotations

import argparse

from redteam.app.cli._shared import EXIT_FAILURE, EXIT_OK, eprint, print_json, render_table
from redteam.app.report_store import load_terminal_report


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("findings", help="Inspect Findings for a Run")
    sub = parser.add_subparsers(dest="findings_command", required=True)

    list_parser = sub.add_parser("list", help="List Findings for a Run")
    list_parser.add_argument("run_id")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(handler=run_list)


async def run_list(args: argparse.Namespace) -> int:
    report = load_terminal_report(args.run_id)
    if report is None:
        eprint(f"ERROR: no terminal report for run {args.run_id} (unknown or still in progress).")
        return EXIT_FAILURE

    if args.json:
        print_json([f.model_dump(mode="json") for f in report.findings])
        return EXIT_OK

    rows = [
        [f.finding_id, "VERIFIED" if f.verified else "CANDIDATE", f.severity.upper(), f.title]
        for f in report.findings
    ]
    render_table(["ID", "STATUS", "SEVERITY", "TITLE"], rows)
    return EXIT_OK
