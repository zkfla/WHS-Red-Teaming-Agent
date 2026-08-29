from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from cicd_redteam_agent.domain.graph_frontier import FrontierQuery, build_attack_frontiers
from cicd_redteam_agent.domain.investigation_policy import PortfolioEntry, select_frontier_portfolio
from cicd_redteam_agent.domain.models import (
    AnalysisReport,
    AttackPath,
    Evidence,
    FindingStatus,
    ReportStatus,
)
from cicd_redteam_agent.domain.proposal_draft import indexed_relations


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
                    "metadata": {
                        key: value
                        for key, value in report.run_metadata.items()
                        if key != "objective"
                    },
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


def _review_sort_key(path: AttackPath) -> tuple[int, int, int, int, str]:
    status_rank = {
        FindingStatus.SUPPORTED: 0,
        FindingStatus.CANDIDATE: 1,
        FindingStatus.REJECTED: 2,
        FindingStatus.CONFIRMED: 3,
    }
    has_guard = any(
        marker in edge.relation_type
        for edge in path.edges
        for marker in ("guard", "verified", "validates")
    )
    return (
        status_rank[path.status],
        len(path.missing_evidence),
        int(has_guard),
        len(path.edges),
        path.path_id,
    )


def _distinct_global_paths(paths: list[AttackPath]) -> list[AttackPath]:
    selected: dict[tuple[FindingStatus, str, str], AttackPath] = {}
    for path in sorted(paths, key=_review_sort_key):
        first_hop = path.nodes[1] if len(path.nodes) > 1 else "unknown"
        sink = path.nodes[-1] if path.nodes else "unknown"
        if sink.startswith("credential:github-token:"):
            sink_kind = "github-token"
        elif sink.startswith(("identity_", "cloud-identity_")):
            sink_kind = "cloud-identity"
        else:
            sink_kind = sink
        selected.setdefault((path.status, first_hop, sink_kind), path)
    return sorted(selected.values(), key=_review_sort_key)


def _priority_reason(path: AttackPath) -> str:
    sink = path.nodes[-1] if path.nodes else "unknown sink"
    evidence_state = (
        "locally supported"
        if path.status is FindingStatus.SUPPORTED
        else f"requires {len(path.missing_evidence)} missing Evidence item(s)"
    )
    return f"{evidence_state}; reaches {_code(sink)} in {len(path.edges)} edge(s)"


def _frontier_query(report: AnalysisReport) -> FrontierQuery:
    goal = report.run_metadata.get("objective_goal") or {}
    if not isinstance(goal, dict):
        return FrontierQuery()
    return FrontierQuery(
        source_hints=tuple(str(item) for item in goal.get("source_hints") or ()),
        mechanism_hints=tuple(str(item) for item in goal.get("mechanism_hints") or ()),
        sink_hints=tuple(str(item) for item in goal.get("sink_hints") or ()),
    )


def unresolved_frontier_portfolio(
    report: AnalysisReport, *, limit: int = 5
) -> tuple[PortfolioEntry, ...]:
    if not report.state.relations or limit < 1:
        return ()
    result = build_attack_frontiers(report.state, _frontier_query(report))
    return select_frontier_portfolio(result, report.state, limit=limit)


def _frontier_workflow_paths(report: AnalysisReport, entry: PortfolioEntry) -> list[str]:
    relation_index = dict(indexed_relations(report.state))
    paths: list[str] = []
    for reference in entry.frontier.relation_refs:
        relation = relation_index.get(reference)
        if relation is None:
            continue
        for evidence_id in relation.evidence_ids:
            evidence = report.state.evidence.get(evidence_id)
            if evidence is not None and evidence.source_path.startswith(".github/workflows/"):
                paths.append(evidence.source_path)
    return list(dict.fromkeys(paths))


def _frontier_sink_label(entry: PortfolioEntry) -> str:
    labels = {
        "github_token_write": "write-capable GITHUB_TOKEN",
        "oidc_token": "OIDC token",
        "cloud_identity": "cloud identity",
        "privileged_execution": "privileged execution",
        "secret_exposure": "secret exposure",
    }
    sink = entry.frontier.sink_tag or entry.frontier.potential_sink_node_id or "unresolved sink"
    return labels.get(str(sink), str(sink))


def _recommendations(path: AttackPath) -> list[str]:
    relation_types = {edge.relation_type for edge in path.edges}
    actions: list[str] = []

    if any("interpreted_as_command" in item for item in relation_types):
        actions.append("Pass untrusted values through environment variables and quote at the sink.")
    if any("artifact" in item for item in relation_types):
        actions.append(
            "Bind artifact download to the producing run and verify trusted integrity data."
        )
    if any("cache" in item for item in relation_types):
        actions.append("Separate cache keys by trust level and avoid restoring executable paths.")
    if any("mutable" in item or "external_code" in item for item in relation_types):
        actions.append(
            "Pin external code to an immutable commit or digest and minimize credentials."
        )
    if any("oidc" in item or "cloud_identity" in item for item in relation_types):
        actions.append(
            "Restrict the cloud trust subject and verify repository, ref, and environment claims."
        )
    if path.nodes and path.nodes[-1].startswith("credential:github-token:"):
        actions.append("Reduce GITHUB_TOKEN permissions to the minimum required by this job.")
    if "repository_file_executes_in" in relation_types:
        actions.append("Do not execute pull-request-controlled files in a privileged job.")
    if not actions:
        actions.append("Break the earliest attacker-controlled edge before the sensitive sink.")
    return list(dict.fromkeys(actions))


def render_summary(report: AnalysisReport) -> str:
    state = report.state
    runtime = report.run_metadata.get("agent_runtime") or {}
    all_global_paths = sorted(
        [
        path for path in state.paths if path.nodes and path.nodes[0].startswith("attacker:")
        ],
        key=_review_sort_key,
    )
    global_paths = _distinct_global_paths(all_global_paths)
    local_paths = [path for path in state.paths if path not in all_global_paths]
    jobs = [job for workflow in state.workflows for job in workflow.jobs]
    steps = [step for job in jobs for step in job.steps]
    analysis_mode = report.run_metadata.get("analysis_mode", "unknown")
    provider = report.run_metadata.get("provider", report.trace.provider or "none")
    objective = report.run_metadata.get("objective")
    objective_digest = report.run_metadata.get("objective_hash")
    objective_goal = report.run_metadata.get("objective_goal") or {}
    objective_preview = None
    if objective:
        objective_text = str(objective)
        objective_preview = objective_text[:500] + ("…" if len(objective_text) > 500 else "")
    objective_focus = None
    if isinstance(objective_goal, dict) and objective_goal.get("focused"):
        focus_parts = []
        for label, key in (
            ("sources", "source_hints"),
            ("mechanisms", "mechanism_hints"),
            ("sinks", "sink_hints"),
        ):
            values = objective_goal.get(key) or []
            if values:
                focus_parts.append(f"{label}={','.join(str(item) for item in values)}")
        objective_focus = "; ".join(focus_parts) or "focused"

    partial_frontiers = (
        unresolved_frontier_portfolio(report, limit=5)
        if report.status is ReportStatus.PARTIAL
        else ()
    )

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
        f"| Global source-to-credential paths | {len(all_global_paths)} |",
        f"| Cloud identity candidates | {len(state.identities)} |",
        *(
            [f"| Objective hash | {_code(objective_digest)} |"]
            if objective_digest
            else []
        ),
        *(
            [f"| Objective focus | {_inline(objective_focus)} |"]
            if objective_focus
            else []
        ),
        *(
            [
                f"| Model attempts / retries | {runtime.get('model_attempts', 0)} / "
                f"{runtime.get('retries', 0)} |",
                f"| Context compactions | {runtime.get('context_compactions', 0)} |",
                f"| Checkpoints written | {runtime.get('checkpoints_written', 0)} |",
            ]
            if runtime
            else []
        ),
        "",
        *(
            ["## User objective", "", _inline(objective_preview), ""]
            if objective_preview
            else []
        ),
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
    ]
    if partial_frontiers:
        lines.extend(
            [
                "## Unresolved attack frontiers",
                "",
                "> The Agent stopped before final proposal submission, but deterministic analysis "
                "already found the following evidence-grounded investigation candidates.",
                "",
            ]
        )
        for entry in partial_frontiers:
            frontier = entry.frontier
            paths = _frontier_workflow_paths(report, entry)
            missing = [item.description for item in frontier.missing]
            lines.extend(
                [
                    f"### {entry.rank}. {_inline(frontier.source_tag)} → "
                    f"{_inline(_frontier_sink_label(entry))}",
                    "",
                    f"- Connectivity: {_code(frontier.connectivity)}",
                    f"- Status ceiling: {_code(frontier.proposal_status_ceiling)}",
                    "- Workflow evidence: "
                    f"{_inline(', '.join(paths) if paths else 'not isolated')}",
                    f"- Missing evidence: {_missing_list(missing)}",
                    f"- Relation refs: {_inline(', '.join(frontier.relation_refs))}",
                    "",
                ]
            )

    lines.extend(["## Findings", ""])
    if not state.findings:
        if report.status is ReportStatus.PARTIAL and partial_frontiers:
            lines.extend(
                [
                    "> No final Finding was submitted before the partial stop. "
                    "This is not equivalent to finding no vulnerability.",
                    "",
                    "Review the unresolved attack frontiers above and resume the run if deeper "
                    "model investigation is required.",
                ]
            )
        else:
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
    if not all_global_paths:
        lines.append("> No attacker-to-credential path was generated.")
    elif len(global_paths) < len(all_global_paths):
        lines.extend(
            [
                f"> Showing {len(global_paths)} highest-priority distinct paths from "
                f"{len(all_global_paths)} total. All paths remain in `analysis.json`.",
                "",
            ]
        )
    for index, path in enumerate(global_paths, start=1):
        lines.extend(
            [
                f"### {index}. {_code(path.path_id)} · {_code(path.status)}",
                "",
                "```text",
                " → ".join(str(node).replace("`", "ˋ") for node in path.nodes),
                "```",
                "",
                f"- Review priority: **{index} of {len(global_paths)}**",
                f"- Priority basis: {_priority_reason(path)}",
                f"- Evidence: {_evidence_list(report, path.evidence_ids)}",
                f"- Missing evidence: {_missing_list(path.missing_evidence)}",
                "- Recommended actions:",
                *[f"  - {_inline(item)}" for item in _recommendations(path)],
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
    active_hypotheses = [
        hypothesis
        for hypothesis in state.working_memory.hypotheses
        if hypothesis.status.value in {"ACTIVE", "BLOCKED", "READY_FOR_PROPOSAL"}
    ]
    if report.status is ReportStatus.PARTIAL and active_hypotheses:
        lines.extend(["## Active investigation state", ""])
        for hypothesis in active_hypotheses:
            next_check = (
                _inline(hypothesis.next_check.objective)
                if hypothesis.next_check
                else "none"
            )
            lines.extend(
                [
                    f"### {_inline(hypothesis.claim)}",
                    "",
                    f"- Status: {_code(hypothesis.status)}",
                    f"- Missing evidence: {_missing_list(hypothesis.missing_evidence)}",
                    f"- Next check: {next_check}",
                    "",
                    _inline(hypothesis.decision_summary),
                    "",
                ]
            )
    if runtime:
        lines.extend(
            [
                "## Agent runtime",
                "",
                f"- Model attempts: {runtime.get('model_attempts', 0)}",
                f"- Model retries: {runtime.get('retries', 0)}",
                f"- Context compactions: {runtime.get('context_compactions', 0)}",
                f"- Checkpoints written: {runtime.get('checkpoints_written', 0)}",
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


def _terminal_text(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return "".join(
        character if character >= " " and character != "\x7f" else " "
        for character in text
    )


def render_terminal_brief(report: AnalysisReport, run_dir: Path) -> str:
    state = report.state
    lines = [
        "",
        "=== CI/CD 보안 분석 결과 ===",
        f"상태: {report.status}",
        f"저장 위치: {run_dir}",
        f"분석 범위: workflow {len(state.workflows)}개 / Evidence {len(state.evidence)}개 / "
        f"Relation {len(state.relations)}개",
    ]

    if state.findings:
        lines.extend(["", f"최종 취약점/후보: {len(state.findings)}개"])
        for index, finding in enumerate(state.findings[:5], start=1):
            lines.append(
                f"  {index}. [{finding.status}] {_terminal_text(finding.title)} "
                f"(confidence={finding.confidence})"
            )
            lines.append(f"     {_terminal_text(finding.summary)}")
            if finding.missing_evidence:
                missing = ", ".join(_terminal_text(item) for item in finding.missing_evidence[:3])
                lines.append(f"     미확인: {missing}")
    elif report.status is ReportStatus.PARTIAL:
        portfolio = unresolved_frontier_portfolio(report, limit=5)
        if portfolio:
            lines.extend(
                [
                    "",
                    "최종 Finding 제출 전 분석이 중단됐지만, 아래 공격 경로 후보는 이미 잡혔음:",
                ]
            )
            for entry in portfolio:
                frontier = entry.frontier
                paths = _frontier_workflow_paths(report, entry)
                missing = [item.description for item in frontier.missing]
                source = frontier.source_tag.replace("_", " ")
                lines.append(
                    f"  {entry.rank}. {source} → {_frontier_sink_label(entry)} "
                    f"[{frontier.proposal_status_ceiling}]"
                )
                if paths:
                    workflow_text = ", ".join(_terminal_text(path) for path in paths[:2])
                    lines.append(f"     관련 workflow: {workflow_text}")
                if missing:
                    missing_text = ", ".join(_terminal_text(item) for item in missing[:3])
                    lines.append(f"     막힌 이유: {missing_text}")
        else:
            lines.extend(
                ["", "최종 Finding은 아직 없고 제출 가능한 공격 경로 후보도 확인되지 않았음."]
            )
    else:
        lines.extend(["", "최종 Finding은 생성되지 않았음."])

    if report.status is ReportStatus.PARTIAL:
        reason_messages = {
            "insufficient_token_reserve": (
                "다음 AI 라운드를 시작할 토큰 예산이 부족해서 안전하게 중단됨."
            ),
            "no_progress": "반복 조사로 새 정보가 늘지 않아 안전하게 중단됨.",
            "limit_reached": "설정된 실행 한도에 도달해 중단됨.",
        }
        reason = report.termination_reason or "partial"
        lines.extend(["", f"중단 이유: {reason_messages.get(reason, reason)}"])
        if (run_dir / "checkpoint.json").exists():
            used_tokens = 0
            termination = getattr(state, "termination", None)
            if termination is not None:
                used_tokens = int(getattr(termination, "tokens", 0) or 0)
            configured = int(report.run_metadata.get("max_tokens") or 0)
            suggested = max(400_000, configured * 2, used_tokens * 4)
            lines.extend(
                [
                    "이어가기:",
                    f"  uv run cicd-agent resume \"{run_dir}\" --max-tokens {suggested} "
                    "--max-rounds 24 --max-duration 1800",
                ]
            )

    lines.extend(
        [
            "",
            f"상세 자연어/근거 보고서: {run_dir / 'summary.md'}",
            f"전체 구조화 결과: {run_dir / 'analysis.json'}",
        ]
    )
    return "\n".join(lines)
