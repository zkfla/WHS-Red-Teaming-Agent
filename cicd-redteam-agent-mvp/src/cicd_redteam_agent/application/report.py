from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from cicd_redteam_agent.domain.models import AnalysisReport, Evidence


def write_report(report: AnalysisReport, run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "analysis.json").write_text(
        report.model_dump_json(indent=2, by_alias=True), encoding="utf-8"
    )
    (run_dir / "summary.md").write_text(render_summary(report), encoding="utf-8")
    trace_path = run_dir / "trace.jsonl"
    if not trace_path.exists():
        trace_path.write_text(
            json.dumps(
                {
                    "event": "analysis.complete",
                    "status": report.status,
                    "termination_reason": report.termination_reason,
                    "metadata": report.run_metadata,
                },
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )


def _inline(value: object) -> str:
    """Render untrusted repository/model text without changing Markdown structure."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    for character in ("\\", "`", "*", "_", "{", "}", "[", "]", "<", ">", "|", "#"):
        text = text.replace(character, f"\\{character}")
    return text


def _code(value: object) -> str:
    text = str(value).replace("`", "ˋ").replace("\n", " ").replace("\r", " ")
    return f"`{text}`"


def _count_table(values: list[object]) -> str:
    counts = Counter(str(value) for value in values)
    return ", ".join(f"{_code(key)} {counts[key]}" for key in sorted(counts)) or "—"


def _evidence_reference(evidence: Evidence) -> str:
    location = (
        str(evidence.line_start)
        if evidence.line_start == evidence.line_end
        else f"{evidence.line_start}-{evidence.line_end}"
    )
    return f"{_code(evidence.evidence_id)} ({_inline(evidence.source_path)}:{location})"


def _evidence_list(report: AnalysisReport, evidence_ids: list[str]) -> str:
    if not evidence_ids:
        return "—"
    references = []
    for evidence_id in evidence_ids:
        evidence = report.state.evidence.get(evidence_id)
        references.append(_evidence_reference(evidence) if evidence else _code(evidence_id))
    return "<br>".join(references)


def _missing_list(items: list[str]) -> str:
    return "<br>".join(_inline(item) for item in items) if items else "—"


def render_summary(report: AnalysisReport) -> str:
    state = report.state
    global_paths = [
        path for path in state.paths if path.nodes and path.nodes[0].startswith("attacker:")
    ]
    local_paths = [path for path in state.paths if path not in global_paths]
    jobs = [job for workflow in state.workflows for job in workflow.jobs]
    steps = [step for job in jobs for step in job.steps]
    analysis_mode = report.run_metadata.get("analysis_mode", "unknown")
    provider = report.run_metadata.get("provider", report.trace.provider or "none")

    lines = [
        "# CI/CD Red Team Analysis",
        "",
        "> Read-only static analysis. `SUPPORTED` means locally evidenced, "
        "not exploited or confirmed.",
        "",
        "## At a glance",
        "",
        "| Item | Result |",
        "|---|---|",
        f"| Report status | {_code(report.status)} |",
        f"| Termination | {_code(report.termination_reason)} |",
        f"| Analysis mode | {_code(analysis_mode)} |",
        f"| Provider | {_code(provider)} |",
        f"| Repository commit | {_code(state.repository.git_commit or 'unknown')} |",
        f"| Workflows / jobs / steps | {len(state.workflows)} / {len(jobs)} / {len(steps)} |",
        f"| Evidence / relations | {len(state.evidence)} / {len(state.relations)} |",
        f"| Findings / attack paths | {len(state.findings)} / {len(state.paths)} |",
        f"| Global source-to-credential paths | {len(global_paths)} |",
        f"| Cloud identity candidates | {len(state.identities)} |",
        "",
        "## Analysis coverage",
        "",
        "| Signal | Distribution |",
        "|---|---|",
        f"| Relation verdicts | {_count_table([item.verdict for item in state.relations])} |",
        f"| Finding statuses | {_count_table([item.status for item in state.findings])} |",
        f"| Path statuses | {_count_table([item.status for item in state.paths])} |",
        "| Attacker-control assessments | "
        f"{_count_table([item.verdict for item in state.assessments])} |",
        "",
        "## Findings",
        "",
    ]

    if not state.findings:
        lines.extend(
            [
                "> No supported or candidate findings were generated.",
                "",
                "This does not prove the repository is secure; "
                "review the warnings and unknowns below.",
            ]
        )
    else:
        lines.extend(
            [
                "| ID | Finding | Status | Confidence |",
                "|---|---|---|---|",
                *[
                    f"| {_code(item.finding_id)} | {_inline(item.title)} | "
                    f"{_code(item.status)} | {_code(item.confidence)} |"
                    for item in state.findings
                ],
            ]
        )
        for index, finding in enumerate(state.findings, start=1):
            lines.extend(
                [
                    "",
                    f"### {index}. {_inline(finding.title)}",
                    "",
                    "| Field | Value |",
                    "|---|---|",
                    f"| Finding ID | {_code(finding.finding_id)} |",
                    f"| Rule | {_code(finding.rule_id)} |",
                    f"| Status | {_code(finding.status)} |",
                    f"| Confidence | {_code(finding.confidence)} |",
                    f"| Evidence | {_evidence_list(report, finding.evidence_ids)} |",
                    f"| Missing evidence | {_missing_list(finding.missing_evidence)} |",
                    "",
                    _inline(finding.summary),
                ]
            )

    lines.extend(["", "## Global attack paths", ""])
    if not global_paths:
        lines.append("> No attacker-to-credential path was generated.")
    for index, path in enumerate(global_paths, start=1):
        lines.extend(
            [
                f"### {index}. {_code(path.path_id)} · {_code(path.status)}",
                "",
                "```text",
                " → ".join(str(node).replace("`", "ˋ") for node in path.nodes),
                "```",
                "",
                f"- Evidence: {_evidence_list(report, path.evidence_ids)}",
                f"- Missing evidence: {_missing_list(path.missing_evidence)}",
                "",
            ]
        )

    lines.extend(["## Local attack paths", ""])
    if not local_paths:
        lines.append("> No local attack path was generated.")
    for path in local_paths:
        lines.extend(
            [
                f"- {_code(path.path_id)} · {_code(path.status)}",
                f"  - Path: {' → '.join(_code(node) for node in path.nodes)}",
                f"  - Evidence: {_evidence_list(report, path.evidence_ids)}",
                f"  - Missing evidence: {_missing_list(path.missing_evidence)}",
            ]
        )

    lines.extend(["", "## Cloud identity handoff", ""])
    if not state.identities:
        lines.append("> No cloud identity candidate was generated.")
    for index, identity in enumerate(state.identities, start=1):
        lines.extend(
            [
                f"### {index}. {_inline(identity.provider)} / {_inline(identity.identity_type)}",
                "",
                "| Field | Value |",
                "|---|---|",
                f"| Identity ID | {_code(identity.identity_id)} |",
                f"| Mechanism | {_code(identity.mechanism)} |",
                "| Reference | "
                f"{_code(identity.identity_reference or identity.role_arn or 'unknown')} |",
                f"| OIDC enabled | {_code(identity.oidc_enabled)} |",
                f"| Attacker control | {_code(identity.attacker_control)} |",
                f"| Trust verified | {_code(identity.trust_verified)} |",
                f"| Confidence | {_code(identity.confidence)} |",
                f"| Evidence | {_evidence_list(report, identity.evidence_ids)} |",
                "",
                _inline(identity.summary),
                "",
            ]
        )

    lines.extend(["## Warnings and unknowns", ""])
    pending = list(dict.fromkeys([*report.warnings, *state.pending_questions]))
    lines.extend(f"- {_inline(item)}" for item in pending)
    if not pending:
        lines.append("- None")

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `SUPPORTED`: every decisive edge is backed by local repository Evidence.",
            "- `CANDIDATE`: the path is plausible but requires missing or external Evidence.",
            "- `REJECTED`: a concrete guard blocks the proposed path.",
            "- `UNKNOWN`: static repository analysis cannot determine the external "
            "or runtime state.",
            "- Static analysis never reports `CONFIRMED` exploitation.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"
