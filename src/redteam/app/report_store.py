"""Durable per-run terminal report projection.

``redteam run`` is a blocking, foreground command with no background
daemon (see ``docs/agent/orchestration-implementation-plan.md`` — a
reconciliation *host* exists, but running one is a deployment-layer
choice, not something this CLI does for you). Once that process exits,
``redteam status``/``findings list``/``report generate`` run as a
*separate* process and have no live ``RuntimeState`` to query.

What IS durable across that process boundary: Evidence, Work Ledger and
Approval/Audit stores (all filesystem-backed already), and the Attack
Graph *if* something persisted it — which nothing did, by default, since
``InMemoryAttackGraphStore`` is not durable. Rather than fabricate a new
persistence subsystem, this reuses the ``TerminalReportProjection`` the
canonical bootstrap already computes once per run
(``app.bootstrap._publish_terminal_artifacts``) — a real, already-built
domain projection, not new business logic — and just serializes it to one
JSON file per run. That one file is what makes ``status``/``findings``/
``report`` work after the process that ran it has exited.

A run that never reaches a terminal phase (crashed process, force-killed)
has no file here — callers must treat that as "unknown", never guess.
"""

from __future__ import annotations

from pathlib import Path

from redteam.core.ids import RunId
from redteam.orchestration.reporting import TerminalReportProjection

DEFAULT_REPORTS_DIR = "data/reports"


def report_path(run_id: RunId | str, *, reports_dir: str | Path = DEFAULT_REPORTS_DIR) -> Path:
    return Path(reports_dir) / f"{run_id}.json"


def save_terminal_report(
    report: TerminalReportProjection,
    *,
    reports_dir: str | Path = DEFAULT_REPORTS_DIR,
) -> Path:
    path = report_path(report.run_id, reports_dir=reports_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_terminal_report(
    run_id: RunId | str, *, reports_dir: str | Path = DEFAULT_REPORTS_DIR
) -> TerminalReportProjection | None:
    path = report_path(run_id, reports_dir=reports_dir)
    if not path.exists():
        return None
    return TerminalReportProjection.model_validate_json(path.read_text(encoding="utf-8"))
