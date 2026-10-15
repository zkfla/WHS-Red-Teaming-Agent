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

from redteam.app.cli._shared import EXIT_FAILURE, EXIT_OK, eprint, read_run_report
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.reporting import MissionProjection, TerminalReportProjection


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("report", help="Generate a Run report")
    sub = parser.add_subparsers(dest="report_command", required=True)

    generate = sub.add_parser("generate", help="Render the terminal report for a Run")
    generate.add_argument("run_id")
    generate.add_argument("--format", choices=["md", "json"], default="md")
    generate.add_argument("--output-dir", default="reports")
    generate.add_argument("--state-db", default=None)
    generate.set_defaults(handler=run_generate)


async def run_generate(args: argparse.Namespace) -> int:
    try:
        report = await read_run_report(args)
    except (ValueError, OSError, RuntimeError) as exc:
        eprint(f"ERROR: central report unavailable: {exc}")
        return EXIT_FAILURE
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
    if report.investigations or report.effective_limits:
        lines += ["## Central investigations", "",
                  f"- Applied limits: {json.dumps(report.effective_limits, ensure_ascii=False)}"]
        for investigation in report.investigations:
            result = investigation.get("result") or {}
            lines += [f"- {investigation['task_id']}: {investigation['status']}; "
                      f"owner={investigation.get('occupant_owner')}; "
                      f"result={result.get('outcome')}; required={investigation['required']}"]
        lines += ["", "## Criterion evidence", ""]
        for item in report.criterion_evidence:
            lines += [f"- {item['criterion']}: met={item['met']}; "
                      f"goal revision={item.get('goal_revision')}; "
                      f"evidence={item['evidence_ids']}"]
        lines += ["", "## Questions and original waits", ""]
        for question in report.unresolved_questions:
            lines += [f"- {question['question_id']}: required={question['required_for_goal']}; "
                      f"resolved={question['resolved']}; {question['question']}"]
        for execution in report.executions:
            if execution.get("wait_reason") or execution["state"] == "unknown":
                lines += [f"- {execution['instance_id']}/{execution['invocation_id']}: "
                          f"{execution['state']}; approval={execution.get('approval_ref')}; "
                          f"job={execution.get('job_ref')}"]
        lines += ["", "## Resource cleanup", ""]
        for item in report.cleanup_obligations:
            lines += [f"- {item['node_id']}: {item['status']}; "
                      f"created={item.get('created_evidence_id')}; "
                      f"cleanup={item.get('cleanup_evidence_id')}"]
        if report.cleanup_unmapped_tools:
            lines += ["- Resource effects unavailable for tools: "
                      + ", ".join(report.cleanup_unmapped_tools)]
        lines += [""]
    if report.runtime_records:
        lines += ["## Historical runtime diagnostics and reviews", "",
                  "- These records do not prove execution or describe current waits."]
        for view in report.runtime_records.get("records", []):
            lines += [f"- {view['evidence_id']}: {view['view_kind']}; "
                      f"truncated={view['truncated']}", "```json", view["text"], "```"]
        lines += [f"- Omitted records: {report.runtime_records.get('omitted_count', 0)}", ""]
    if report.candidates:
        lines += ["## Vulnerability candidates", ""]
        for candidate in report.candidates:
            lines += [
                f"- {candidate.hypothesis_id}: status={candidate.status}; "
                f"revision={candidate.revision}; probe tasks={candidate.probe_task_ids or []}",
                f"  - {candidate.description}",
                f"  - supporting={candidate.supporting_evidence_ids}; "
                f"refuting={candidate.refuting_evidence_ids}",
            ]
        lines += [
            "- Candidate judgment is separate from Task completion, Finding verification, "
            "and Mission completion.",
            "",
        ]
    if report.llm_request_accounting is not None:
        usage = report.llm_request_accounting
        policy = usage.get("policy") or {}
        lines += [
            "## Shared LLM request ledger", "",
            f"- Mode: {policy.get('mode', 'unknown')}",
            f"- Request cap: {policy.get('max_requests')}",
            f"- Charged requests: {usage['spent']}; pending reservations: {usage['reserved']}",
            f"- Known tokens: {usage['known_tokens']}; unknown usage: {usage['unknown_usage']}",
            "- This ledger, not per-instance decision counters, owns request admission.", "",
        ]
        if "unknown_dispatch" in usage:
            lines.append(f"- Dispatched requests with unknown outcome: {usage['unknown_dispatch']}")
        if usage.get("unknown_outcomes"):
            lines.append("- Recorded unknown-outcome categories: "
                         + json.dumps(usage["unknown_outcomes"], sort_keys=True))
        for stage, counts in usage.get("by_stage", {}).items():
            lines += [f"- {stage}: requests={counts['spent']}; "
                      f"reserved={counts['reserved']}; known tokens={counts['known_tokens']}; "
                      f"unknown usage={counts['unknown_usage']}"]
    if report.mission is not None:
        contracted = [t for t in report.mission.tasks if t.completion_contract_version == 2]
        if contracted:
            lines += ["## Fixed task completion contracts", ""]
            for task in contracted:
                lines += [f"- {task.task_id}: {task.completion_contract_hash}"]
                for condition in task.completion_criteria:
                    lines += [f"  - {json.dumps(condition, ensure_ascii=False)}"]
            lines += [""]
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
    validity = getattr(report, "evidence_validity", []) or []
    lines += ["", "## Current evidence reuse validity", ""]
    if not validity:
        lines.append("(no registered conditions; original provenance checks still apply)")
    for item in validity:
        lines.append(f"- {item['evidence_id']}: {item['status']} "
                     "(not a vulnerability or success verdict)")
    for identity in getattr(report, "evidence_validity_blockers", []) or []:
        lines.append(f"- Current release requires revalidation: {identity}")
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
