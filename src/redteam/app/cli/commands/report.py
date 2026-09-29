"""``redteam report generate <run-id>`` — render the persisted terminal report.

``TerminalReportProjection`` (``orchestration.reporting.ReportingAgent``)
is the only report generator in this codebase; this command renders its
already-computed, already-persisted output (see ``app.report_store``) to
Markdown or JSON. No new report generator, no new format the domain
layer does not already produce.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from redteam.app.cli._shared import EXIT_FAILURE, EXIT_OK, eprint
from redteam.app.report_store import load_terminal_report
from redteam.orchestration.reporting import MissionProjection, TerminalReportProjection


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("report", help="Generate a Run report")
    sub = parser.add_subparsers(dest="report_command", required=True)

    generate = sub.add_parser("generate", help="Render the terminal report for a Run")
    generate.add_argument("run_id")
    generate.add_argument("--format", choices=["md", "json"], default="md")
    generate.add_argument("--output-dir", default="reports")
    generate.set_defaults(handler=run_generate)


async def run_generate(args: argparse.Namespace) -> int:
    report = load_terminal_report(args.run_id)
    if report is None:
        eprint(f"ERROR: no terminal report for run {args.run_id} (unknown or still in progress).")
        return EXIT_FAILURE

    out_dir = Path(args.output_dir) / str(report.run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.format == "json":
        path = out_dir / "report.json"
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    else:
        path = out_dir / "report.md"
        path.write_text(_render_markdown(report), encoding="utf-8")

    print(f"Report generated:\n  {path}")
    return EXIT_OK


def _render_markdown(report: TerminalReportProjection) -> str:
    lines = [
        f"# Run Report: {report.run_id}",
        "",
        f"- Phase: {report.phase.value}",
        f"- Termination reason: {report.termination_reason or '(none)'}",
        f"- Ready for release: {report.ready_for_release}",
        f"- Actions executed: {report.actions_executed}",
        f"- Evidence recorded: {report.evidence_count}",
        f"- Findings: {report.finding_count}",
        "",
        "## Effectiveness",
        "",
        f"- Successful work / action: {report.effectiveness.successful_work_per_action}",
        f"- Evidence / action: {report.effectiveness.evidence_per_action}",
        f"- Verified findings / action: {report.effectiveness.verified_findings_per_action}",
        f"- Dead-end work ratio: {report.effectiveness.dead_end_work_ratio}",
        "",
        "## Findings",
        "",
    ]
    if not report.findings:
        lines.append("(none)")
    for finding in report.findings:
        status = "VERIFIED" if finding.verified else "CANDIDATE"
        lines.append(
            f"- **[{status}/{finding.severity.upper()}]** {finding.title} "
            f"(`{finding.finding_id}`)"
        )
    if report.integrity_blockers:
        lines += ["", "## Integrity blockers", ""]
        lines += [f"- {item}" for item in report.integrity_blockers]
    if report.unresolved_hypotheses:
        lines += ["", "## Unresolved hypotheses", ""]
        lines += [f"- {item}" for item in report.unresolved_hypotheses]
    if report.outstanding_cleanup:
        lines += ["", "## Outstanding cleanup", ""]
        lines += [f"- {item}" for item in report.outstanding_cleanup]
    if report.mission is not None:
        lines += _render_mission(report.mission)
    return "\n".join(lines) + "\n"


def _render_mission(mission: MissionProjection) -> list[str]:
    lines = [
        "",
        "## Mission",
        "",
        f"- Objective: {mission.objective}",
        f"- Status: {mission.status}",
        f"- Tasks: {mission.completed_task_count} completed / "
        f"{mission.partial_task_count} in progress / {mission.blocked_task_count} blocked",
        f"- Verified findings: {mission.verified_finding_count}",
        f"- Evidence recorded: {mission.evidence_count}",
        "",
        "### Workstreams",
        "",
    ]
    if not mission.workstreams:
        lines.append("(none)")
    for workstream in mission.workstreams:
        detail = f" — {workstream.reason}" if workstream.reason else ""
        lines.append(f"- **[{workstream.status.upper()}]** {workstream.name}{detail}")
    blocked = [task for task in mission.tasks if task.status in {"blocked", "failed"}]
    if blocked:
        lines += ["", "### Blocked tasks", ""]
        for task in blocked:
            reason = task.blocked_reason or "no blocking reason recorded"
            lines.append(f"- {task.title} [{task.status}]: {reason}")
    lines += ["", "### Success criteria", ""]
    if not mission.success_criteria:
        lines.append("(none)")
    for criterion in mission.success_criteria:
        mark = "MET" if criterion.met else "UNMET"
        detail = f" — {criterion.reason}" if criterion.reason else ""
        lines.append(
            f"- **[{mark}]** {criterion.criterion} "
            f"(evidence: {criterion.evidence_count}){detail}"
        )
    return lines
