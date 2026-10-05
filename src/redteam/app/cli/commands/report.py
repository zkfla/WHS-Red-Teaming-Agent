"""``redteam report generate <run-id>`` — render the persisted terminal report.

``TerminalReportProjection`` (``orchestration.reporting.ReportingAgent``)
is the only report generator in this codebase; this command renders its
already-computed, already-persisted output (see ``app.report_store``) to
Markdown or JSON. No new report generator, no new format the domain
layer does not already produce.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from redteam.app.cli._shared import EXIT_FAILURE, EXIT_OK, eprint
from redteam.app.report_store import load_terminal_report
from redteam.orchestration.diagnostics import DecisionDiagnostic
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


def _diagnostic_detail_lines(diagnostic: DecisionDiagnostic) -> list[str]:
    lines = []
    if diagnostic.error_type:
        lines.append(f"- Exception type: {diagnostic.error_type}")
    if diagnostic.exception_chain:
        lines.append(f"- Exception chain: {json.dumps(diagnostic.exception_chain)}")
    if diagnostic.parse_error is not None:
        lines.append(f"- Parse structure: {diagnostic.parse_error.model_dump_json()}")
    if diagnostic.failure_counts:
        lines.append(f"- Failure counts: {json.dumps(diagnostic.failure_counts)}")
    return lines


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
        f"- Outcome: {getattr(report, 'run_outcome', 'unknown')}",
        f"- Model requests: {getattr(report, 'model_requests_used', 0)} "
        f"(tokens: {getattr(report, 'model_tokens_used', 0)}, "
        f"unknown usage: {getattr(report, 'model_usage_unknown', 0)})",
        "",
        "## Effectiveness",
        "",
        f"- Successful work / action: {report.effectiveness.successful_work_per_action}",
        f"- Evidence / action: {report.effectiveness.evidence_per_action}",
        f"- Verified findings / action: {report.effectiveness.verified_findings_per_action}",
        f"- Dead-end work ratio: {report.effectiveness.dead_end_work_ratio}",
        "",
    ]
    for observation in report.runtime_observations:
        if observation.diagnostic is None:
            continue
        diagnostic = observation.diagnostic
        lines += [
            "", "## Execution diagnostic", "",
            f"- Step: {observation.step}; outcome: {observation.outcome}",
            f"- Stage: {diagnostic.stage}; code: {diagnostic.code}",
            f"- Reason: {diagnostic.summary}",
            f"- Retryable: {diagnostic.retryable}",
        ]
        lines += _diagnostic_detail_lines(diagnostic)
    if report.mission is not None:
        outstanding = [task.task_id for task in report.mission.tasks if task.status == "running"]
        if outstanding:
            lines += [
                "", "## Unfinished task settlement", "",
                "The run has ended; these stored task states remain unfinished.",
                f"- Task references: {json.dumps(outstanding)}",
            ]
    if report.planning_diagnostic is not None:
        diagnostic = report.planning_diagnostic
        lines += [
            "",
            "## Planning diagnostic",
            "",
            f"- Stage: {diagnostic.stage}",
            f"- Code: {diagnostic.code}",
            f"- Reason: {diagnostic.summary}",
            f"- Attempts: {diagnostic.attempts} / {diagnostic.attempt_limit or 'unknown'}",
        ]
        if diagnostic.stage_attempts:
            lines.append(f"- Requests by stage: {json.dumps(diagnostic.stage_attempts)}")
        if diagnostic.provider_status is not None:
            lines.append(f"- Provider status: {diagnostic.provider_status}")
        if diagnostic.provider_code:
            lines.append(f"- Provider code: {diagnostic.provider_code}")
        if diagnostic.retry_after is not None:
            lines.append(f"- Provider retry after: {diagnostic.retry_after}s")
        lines.append(f"- Provider retryable: {diagnostic.retryable}")
        if diagnostic.validation_issues:
            lines.append(f"- Validation issues: {json.dumps(diagnostic.validation_issues)}")
        lines += _diagnostic_detail_lines(diagnostic)
        cause = diagnostic.last_cause
        while cause is not None:
            lines.append(f"- Cause [{cause.stage}/{cause.code}]: {cause.feedback()}")
            lines += _diagnostic_detail_lines(cause)
            if cause.validation_issues:
                lines.append(f"- Validation issues: {json.dumps(cause.validation_issues)}")
            cause = cause.last_cause
        if diagnostic.trace:
            lines += ["", "### Decision trace", ""]
            for item in diagnostic.trace:
                lines.append(f"- {json.dumps(item, ensure_ascii=False)}")
        else:
            lines.append("Detailed decision trace was not recorded for this run.")
    if report.planning_diagnostic_history:
        lines += ["", "## Earlier planning diagnostics", ""]
        for prior in report.planning_diagnostic_history:
            lines.append(
                f"- [{prior.stage}/{prior.code}] {prior.summary} "
                f"(attempts={prior.attempts}, retryable={prior.retryable})"
            )
            if prior.provider_status is not None:
                lines.append(f"  - Provider status: {prior.provider_status}")
            if prior.provider_code:
                lines.append(f"  - Provider code: {prior.provider_code}")
            if prior.retry_after is not None:
                lines.append(f"  - Provider retry after: {prior.retry_after}s")
    if report.request_observations:
        lines += ["", "## Actual requests", ""]
        for item in report.request_observations:
            lines.append(f"- {json.dumps(item, ensure_ascii=False)}")
    lines += ["", "## Findings", ""]
    if not report.findings:
        lines.append("(none)")
    for finding in report.findings:
        status = "VERIFIED" if finding.verified else "CANDIDATE"
        lines.append(
            f"- **[{status}/{finding.severity.upper()}]** {finding.title} (`{finding.finding_id}`)"
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
    lines += ["", "## Current goal gaps", ""]
    if report.coverage is None or report.coverage.unknown:
        lines.append("Coverage is unknown for this report.")
    else:
        lines += [f"- Unmet condition: {item}" for item in report.coverage.unmet_conditions]
        lines += [
            f"- Required task incomplete: {item}"
            for item in report.coverage.required_incomplete_tasks
        ]
        lines += [
            f"- Legacy completion without receipt: {item}"
            for item in report.coverage.completed_without_receipt
        ]
        if not (
            report.coverage.unmet_conditions
            or report.coverage.required_incomplete_tasks
            or report.coverage.completed_without_receipt
        ):
            lines.append("No gaps are recorded. Completion is determined by the mission review.")
    if report.mission is not None:
        lines += _render_mission(report.mission)
    if report.result_processing:
        lines += ["", "## Result processing", ""]
        for result_status in report.result_processing:
            lines.append(
                f"- {result_status.evidence_id}: interpretation={result_status.mapping_status}, "
                f"application={result_status.processing_status}, "
                f"recovery={result_status.recovery_status or 'unknown'}"
            )
    # F1 additive tail: existing section order above never changes. New
    # sections only project IDs plus safe summaries; raw text stays in the
    # evidence store and is never copied here.
    lines += ["", "## Run outcome", ""]
    lines.append(f"- Outcome: {getattr(report, 'run_outcome', 'unknown')}")
    lines.append(
        f"- Model usage: {getattr(report, 'model_requests_used', 0)} requests, "
        f"{getattr(report, 'model_tokens_used', 0)} tokens, "
        f"{getattr(report, 'model_usage_unknown', 0)} unknown"
    )
    unresolved_details = getattr(report, "unresolved_details", []) or []
    unresolved_refs = getattr(report, "unresolved_evidence_ids", []) or []
    lines += ["", "## Investigation unresolved", ""]
    if not unresolved_details and not unresolved_refs:
        lines.append("(none — no unresolved investigation record for this run.)")
    else:
        for identity in unresolved_refs:
            lines.append(f"- Unresolved ref: {identity}")
        for detail in unresolved_details:
            lines.append(
                f"- {detail.evidence_id}: outcome={detail.outcome}, "
                f"reason={detail.reason or '(none)'}"
            )
            for gap in detail.gaps:
                lines.append(f"  - Gap [{gap.code}]: {gap.description or '(none)'}")
                for question in gap.unresolved_questions:
                    lines.append(f"    - Question: {question}")
                for condition in gap.required_conditions:
                    lines.append(f"    - Needs: {condition}")
                for suggestion in gap.next_suggestions:
                    lines.append(f"    - Next: {suggestion}")
                if gap.evidence_refs:
                    lines.append(f"    - Evidence refs: {', '.join(gap.evidence_refs)}")
                if gap.target_node_ids:
                    lines.append(f"    - Targets: {', '.join(gap.target_node_ids)}")
            if detail.diagnostic is not None:
                diagnostic = detail.diagnostic
                lines.append(f"  - Diagnostic: {diagnostic.code}; {diagnostic.summary}")
                lines.append(
                    f"  - Provider: status={diagnostic.provider_status}, "
                    f"code={diagnostic.provider_code}, retryable={diagnostic.retryable}, "
                    f"retry_after={diagnostic.retry_after}"
                )
    evidence_reads = getattr(report, "evidence_reads", []) or []
    lines += ["", "## Evidence reads", ""]
    if not evidence_reads:
        lines.append("(none)")
    for entry in evidence_reads:
        query = f", query={entry.query}" if entry.query else ""
        delegation = f", delegation={entry.delegation_id}" if entry.delegation_id else ""
        lines.append(
            f"- {entry.evidence_id}: {entry.status} "
            f"(offset={entry.offset}, limit={entry.limit}{query}{delegation})"
        )
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
            f"- **[{mark}]** {criterion.criterion} (evidence: {criterion.evidence_count}){detail}"
        )
    return lines
