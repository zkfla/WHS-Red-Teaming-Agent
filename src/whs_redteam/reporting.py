"""Deterministic report rendering for machine and human consumers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _text(value: object, limit: int = 800) -> str:
    """Keep report text readable and prevent untrusted values from becoming markup."""

    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def build_markdown_report(
    report: dict[str, Any],
    *,
    ai_explanation: dict[str, Any] | None = None,
) -> str:
    decision = report.get("exploitability_decision") or {}
    findings = report.get("findings") or []
    injection_tests = report.get("injection_tests") or []
    planning = report.get("planning") or {}
    ai_report = report.get("ai_report") or {}
    lines = [
        f"# Red-teaming report: {_text(report.get('engagement_name', 'engagement'), 200)}",
        "",
        f"- Run ID: `{_text(report.get('run_id', ''), 100)}`",
        f"- Verdict: **{_text(decision.get('verdict', 'UNKNOWN'), 80)}**",
        f"- Confirmed findings: {_text(decision.get('confirmed_findings', 0), 20)}",
        f"- Inconclusive findings: {_text(decision.get('inconclusive_findings', 0), 20)}",
        "",
        "## Scope and planning",
        "",
        f"- Planner mode: `{_text(planning.get('mode', 'rules'), 80)}`",
        f"- Planner provider: `{_text(planning.get('provider', 'rules'), 80)}`",
        f"- Planner model: `{_text(planning.get('model', 'deterministic'), 120)}`",
        f"- Planner round: {_text(planning.get('planner_round', 0), 20)}",
        f"- Fallback used: {_text(planning.get('fallback_used', False), 20)}",
        f"- Planner AI API calls: {_text(planning.get('api_calls', 0), 20)}",
        f"- Planner AI tokens (input/output): {_text(planning.get('input_tokens', 0), 20)}/"
        f"{_text(planning.get('output_tokens', 0), 20)}",
        f"- Report AI API calls: {_text(ai_report.get('api_calls', 0), 20)}",
        f"- Report AI tokens (input/output): {_text(ai_report.get('input_tokens', 0), 20)}/"
        f"{_text(ai_report.get('output_tokens', 0), 20)}",
        "- Selected SQLi candidates: "
        + (_text(", ".join(planning.get("selected_candidate_ids", [])), 500) or "none"),
        "",
        "## Findings",
        "",
    ]
    if findings:
        for finding in findings:
            lines.extend(
                [
                    f"### {_text(finding.get('title', finding.get('category', 'finding')), 200)}",
                    f"- Category: `{_text(finding.get('category', ''), 80)}`",
                    f"- Status: **{_text(finding.get('status', ''), 40)}**",
                    f"- Severity: `{_text(finding.get('severity', ''), 40)}`",
                    f"- Target template: `{_text(finding.get('target_url', ''), 300)}`",
                    f"- Summary: {_text(finding.get('summary', ''), 800)}",
                    "- Negative control passed: "
                    + _text(finding.get("negative_control_passed", False), 20),
                    "",
                ]
            )
    else:
        lines.extend(["No confirmed or inconclusive findings were recorded.", ""])
    lines.extend(
        [
            "## SQLi probe results",
            "",
            f"Executed bounded probe actions: **{len(injection_tests)}**",
            "",
        ]
    )
    for result in injection_tests:
        if not isinstance(result, dict):
            continue
        lines.append(
            f"- `{_text(result.get('fixture_id', 'unknown'), 100)}`: "
            f"confirmed={_text(result.get('confirmed', False), 20)}, "
            f"modes={_text(', '.join(result.get('confirmed_modes', [])), 120) or 'none'}"
        )
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            _text(
                decision.get(
                    "rationale", "Results are limited to approved bounded checks."
                ),
                1000,
            ),
            "",
            "Raw request bodies, credentials, and generated exploit payloads are "
            "intentionally omitted.",
        ]
    )
    if ai_explanation is not None:
        lines.extend(
            [
                "",
                "## AI explanation",
                "",
                f"**Summary:** {_text(ai_explanation.get('summary', ''), 1200)}",
                f"**Limitations:** {_text(ai_explanation.get('limitations', ''), 1200)}",
                f"**Suggested next steps:** {_text(ai_explanation.get('next_steps', ''), 1200)}",
            ]
        )
    return "\n".join(lines) + "\n"


def write_markdown_report(
    path: str | Path,
    report: dict[str, Any],
    *,
    ai_explanation: dict[str, Any] | None = None,
) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(
        build_markdown_report(report, ai_explanation=ai_explanation),
        encoding="utf-8",
    )
    temporary_path.replace(output_path)
    return output_path


def write_json_report(path: str | Path, report: dict[str, Any]) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary_path.replace(output_path)
    return output_path
