from __future__ import annotations

import json
from pathlib import Path

from cicd_redteam_agent.domain.models import AnalysisReport


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


def render_summary(report: AnalysisReport) -> str:
    state = report.state
    global_paths = [path for path in state.paths if path.nodes[0].startswith("attacker:")]
    local_paths = [path for path in state.paths if not path.nodes[0].startswith("attacker:")]
    lines = [
        "# CI/CD Red Team Analysis",
        "",
        f"- Status: `{report.status}`",
        f"- Termination: `{report.termination_reason}`",
        f"- Workflows: {len(state.workflows)}",
        f"- Findings: {len(state.findings)}",
        f"- Attack paths: {len(state.paths)}",
        f"- Global source-to-credential paths: {len(global_paths)}",
        f"- Cloud identity candidates: {len(state.identities)}",
        "",
        "## Findings",
        "",
    ]
    if not state.findings:
        lines.append("No supported or candidate findings were generated.")
    for finding in state.findings:
        lines.extend(
            [
                f"### {finding.title}",
                "",
                f"- Status: `{finding.status}`",
                f"- Confidence: `{finding.confidence}`",
                f"- Evidence: {', '.join(finding.evidence_ids) or 'none'}",
                f"- Missing evidence: {', '.join(finding.missing_evidence) or 'none'}",
                "",
                finding.summary,
                "",
            ]
        )
    lines.extend(["## Global attack paths", ""])
    if not global_paths:
        lines.append("No attacker-to-credential path was generated.")
    for path in global_paths:
        lines.extend(
            [
                f"- `{path.path_id}` — `{path.status}` — {' → '.join(path.nodes)}",
                f"  - Missing evidence: {', '.join(path.missing_evidence) or 'none'}",
            ]
        )
    lines.extend(["", "## Local attack paths", ""])
    if not local_paths:
        lines.append("No local attack path was generated.")
    for path in local_paths:
        lines.extend(
            [
                f"- `{path.path_id}` — `{path.status}` — {' → '.join(path.nodes)}",
                f"  - Missing evidence: {', '.join(path.missing_evidence) or 'none'}",
            ]
        )
    lines.extend(["", "## Cloud handoff", ""])
    if not state.identities:
        lines.append("No cloud identity candidate was generated.")
    for identity in state.identities:
        lines.extend(
            [
                f"### {identity.provider} / {identity.identity_type}",
                "",
                f"- Role: `{identity.role_arn or 'unknown'}`",
                f"- OIDC: `{identity.oidc_enabled}`",
                f"- Attacker control: `{identity.attacker_control}`",
                f"- Trust verified: `{identity.trust_verified}`",
                f"- Evidence: {', '.join(identity.evidence_ids) or 'none'}",
                "",
                identity.summary,
                "",
            ]
        )
    lines.extend(["## Warnings and unknowns", ""])
    pending = [*report.warnings, *state.pending_questions]
    lines.extend(f"- {item}" for item in pending)
    if not pending:
        lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"
