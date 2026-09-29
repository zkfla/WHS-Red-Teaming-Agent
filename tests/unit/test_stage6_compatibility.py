"""Stage 6 compatibility: the surfaces that must keep working for old users.

This module covers only the *compatibility* seams that no focused feature test
already owns:

- ``redteam run``'s machine-readable ``--json`` event stream and its
  non-interactive human output;
- the read-only CLI commands that project *already-persisted* data
  (``status``/``findings``/``report``/``approvals --json``);
- a legacy ``engagement.yaml`` (``auto_approve`` and no new risk fields);
- legacy persisted records (an Approval JSON / an event envelope written
  before newer optional fields existed);
- the MCP adapter's backward-compatible class name.

Behaviour of the newer features themselves (operator console, durable resume,
bounded auto-approval, ...) is deliberately left to their own test modules.
Nothing here starts a transport or a full run; commands are invoked directly
with the same fixtures the rest of the unit suite uses.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from redteam.app.cli import _build_parser
from redteam.app.cli._shared import EXIT_FAILURE, EXIT_OK, load_app_config_for_run, print_json
from redteam.app.cli.commands import approvals as approvals_command
from redteam.app.cli.commands import findings as findings_command
from redteam.app.cli.commands import report as report_command
from redteam.app.cli.commands import run as run_command
from redteam.app.cli.commands import status as status_command
from redteam.capability.adapters.mcp.streamable_http import (
    MCPServerAdapter,
    StreamableHTTPMCPAdapter,
)
from redteam.control.approval import ApprovalService
from redteam.control.stores.filesystem import FilesystemApprovalStore
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.enums import AgentPhase, ApprovalStatus
from redteam.core.ids import ActionId, EngagementId, EventId, FindingId, RunId
from redteam.orchestration.reporting import FindingProjection, TerminalReportProjection
from redteam.orchestration.state import RuntimeState

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(event_type: str, payload: dict | None = None) -> EventEnvelope:  # noqa: ANN001
    return EventEnvelope(
        event_id=EventId("evt_1"),
        event_type=event_type,
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        source="test",
        timestamp=NOW,
        payload=payload or {},
    )


def _engagement_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "engagement.yaml"
    path.write_text(
        """
name: "Compatibility"
scope:
  allowed_hosts: ["a.local"]
execution_budget:
  max_actions: 1
rate_limit_policy:
  max_actions_per_minute: 10
approval_policy:
  require_approval_for: []
""",
        encoding="utf-8",
    )
    return path


def _finished(run_id: str = "run_compat") -> RuntimeState:
    return RuntimeState(
        run_id=RunId(run_id),
        engagement_id=EngagementId("eng_compat"),
        phase=AgentPhase.FINISHED,
        termination_reason="no_actionable_plan",
        actions_executed=0,
    )


def _report(
    *, run_id: str = "run_compat", findings: list[FindingProjection] | None = None
) -> TerminalReportProjection:
    return TerminalReportProjection(
        run_id=RunId(run_id),
        phase=AgentPhase.FINISHED,
        termination_reason="no_actionable_plan",
        ready_for_release=True,
        actions_executed=2,
        finding_count=len(findings or []),
        evidence_count=1,
        findings=findings or [],
    )


# --- `redteam run --json`: one compact, self-describing event per line --------


def test_json_event_stream_is_one_compact_object_per_event(capsys) -> None:  # noqa: ANN001
    run_command._render_event(
        _event(EventType.ACTION_PROPOSED, {"action_id": "action_1"}),
        json_mode=True,
    )

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_type"] == "ActionProposed"
    assert record["event_id"] == "evt_1"
    assert record["run_id"] == "run_1"
    assert record["engagement_id"] == "eng_1"
    assert record["source"] == "test"
    assert record["payload"] == {"action_id": "action_1"}
    assert {"correlation_id", "causation_id"} <= set(record)
    # Compact JSON Lines: no pretty-printing newlines/indentation to confuse a
    # line-oriented consumer.
    assert lines[0] == json.dumps(record, separators=(",", ":"))


def test_json_event_stream_keeps_every_event_type(capsys) -> None:  # noqa: ANN001
    # Structural events are suppressed in the human renderer; a machine
    # consumer must still see them.
    run_command._render_event(
        _event(EventType.AGENT_STARTED, {"agent_id": "recon-agent"}),
        json_mode=True,
    )

    record = json.loads(capsys.readouterr().out)
    assert record["event_type"] == "AgentStarted"


def test_json_event_stream_never_emits_secret_material(capsys) -> None:  # noqa: ANN001
    run_command._render_event(
        _event(
            EventType.TOOL_EXECUTION_FINISHED,
            {"api_key": "sk-live-should-never-appear", "success": True},
        ),
        json_mode=True,
    )

    raw = capsys.readouterr().out
    record = json.loads(raw)
    assert record["payload"]["api_key"] == "***REDACTED***"
    assert "sk-live-should-never-appear" not in raw


def test_plain_event_stream_skips_unrendered_types_and_masks_secrets(capsys) -> None:  # noqa: ANN001
    run_command._render_event(
        _event(EventType.AGENT_STARTED, {"agent_id": "recon-agent"}),
        json_mode=False,
    )
    assert capsys.readouterr().out == ""

    run_command._render_event(
        _event(
            EventType.ACTION_PROPOSED,
            {"capability": "recon", "operation": "ping", "token": "raw-secret"},
        ),
        json_mode=False,
    )
    out = capsys.readouterr().out
    assert out.startswith("[ActionProposed] ")
    assert "capability=recon" in out
    assert "raw-secret" not in out


async def test_run_json_mode_suppresses_human_echo_and_summary(
    tmp_path: Path, monkeypatch, capsys  # noqa: ANN001
) -> None:
    # build/execute are mocked below, but the real load_app_config_for_run()
    # still runs first and now requires an explicit provider (section 4/20)
    # -- there is no --config app.yaml path any more.
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")
    path = _engagement_yaml(tmp_path)

    async def fake_build(config, *, max_steps, mcp_stack=None):  # noqa: ANN001, ANN202
        return SimpleNamespace(name="Compatibility"), SimpleNamespace()

    async def fake_execute(engagement, deps, **kwargs):  # noqa: ANN001, ANN202
        kwargs["echo"]("Engagement: human line")
        return _finished()

    monkeypatch.setattr(run_command, "build", fake_build)
    monkeypatch.setattr(run_command, "execute", fake_execute)
    args = argparse.Namespace(
        engagement_path=str(path),
        non_interactive=True,
        max_steps=None,
        log_level="WARNING",
        json=True,
        objective=None,
    )

    code = await run_command.run(args)

    assert code == EXIT_OK
    assert capsys.readouterr().out == ""


async def test_run_plain_mode_still_prints_the_human_summary(
    tmp_path: Path, monkeypatch, capsys  # noqa: ANN001
) -> None:
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")
    path = _engagement_yaml(tmp_path)

    async def fake_build(config, *, max_steps, mcp_stack=None):  # noqa: ANN001, ANN202
        return SimpleNamespace(name="Compatibility"), SimpleNamespace()

    async def fake_execute(engagement, deps, **kwargs):  # noqa: ANN001, ANN202
        kwargs["echo"]("Engagement: human line")
        return _finished()

    monkeypatch.setattr(run_command, "build", fake_build)
    monkeypatch.setattr(run_command, "execute", fake_execute)
    args = argparse.Namespace(
        engagement_path=str(path),
        non_interactive=True,
        max_steps=None,
        log_level="WARNING",
        json=False,
        objective=None,
    )

    code = await run_command.run(args)

    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "Engagement: human line" in out
    assert "Run finished: phase=finished" in out


# --- the read-only commands still parse their compatibility flags ------------


def test_compatibility_commands_keep_their_json_flags() -> None:
    parser = _build_parser()

    assert parser.parse_args(["status", "run_1", "--json"]).json is True
    assert parser.parse_args(["findings", "list", "run_1", "--json"]).json is True
    parsed = parser.parse_args(["report", "generate", "run_1", "--format", "json"])
    assert parsed.format == "json"
    assert parser.parse_args(["approvals", "list", "--json"]).json is True
    resume = parser.parse_args(["resume", "run_1", "engagement.yaml", "--json"])
    assert resume.json is True and resume.engagement_path == "engagement.yaml"


# --- approvals list --json ---------------------------------------------------


async def test_approvals_list_json_is_a_machine_readable_array(tmp_path: Path, capsys) -> None:  # noqa: ANN001
    approval_dir = tmp_path / "approvals"
    approvals = ApprovalService(FilesystemApprovalStore(approval_dir))
    approval = await approvals.request(ActionId("action_json"))

    code = await approvals_command.run_list(
        argparse.Namespace(run_id=None, approval_dir=str(approval_dir), json=True)
    )

    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["approval_id"] == str(approval.approval_id)
    assert payload[0]["status"] == "pending"


# --- status / findings / report read the persisted terminal projection -------


async def test_status_json_projects_the_persisted_report(monkeypatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setattr(status_command, "load_terminal_report", lambda run_id: _report())

    code = await status_command.run(
        argparse.Namespace(run_id="run_compat", approval_dir="unused", json=True)
    )

    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "run_compat"
    assert payload["phase"] == "finished"
    assert payload["actions_executed"] == 2


async def test_status_unknown_run_is_reported_as_unknown_not_guessed(
    monkeypatch, capsys  # noqa: ANN001
) -> None:
    monkeypatch.setattr(status_command, "load_terminal_report", lambda run_id: None)

    code = await status_command.run(
        argparse.Namespace(run_id="run_missing", approval_dir="unused", json=True)
    )

    assert code == EXIT_FAILURE
    assert json.loads(capsys.readouterr().out) == {
        "run_id": "run_missing",
        "status": "unknown",
    }


async def test_findings_list_projects_persisted_findings(monkeypatch, capsys) -> None:  # noqa: ANN001
    report = _report(
        findings=[
            FindingProjection(
                finding_id=FindingId("find_1"),
                title="IDOR on /orders",
                severity="high",
                verified=True,
                evidence_ids=["ev_1"],
            )
        ]
    )
    monkeypatch.setattr(findings_command, "load_terminal_report", lambda run_id: report)

    code = await findings_command.run_list(
        argparse.Namespace(run_id="run_compat", json=True)
    )

    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["finding_id"] == "find_1"
    assert payload[0]["verified"] is True
    assert payload[0]["severity"] == "high"


async def test_findings_list_without_a_report_fails_loudly(monkeypatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setattr(findings_command, "load_terminal_report", lambda run_id: None)

    code = await findings_command.run_list(
        argparse.Namespace(run_id="run_missing", json=True)
    )

    assert code == EXIT_FAILURE
    assert "no terminal report" in capsys.readouterr().err


async def test_report_generate_json_writes_the_persisted_projection(
    tmp_path: Path, monkeypatch, capsys  # noqa: ANN001
) -> None:
    monkeypatch.setattr(report_command, "load_terminal_report", lambda run_id: _report())

    code = await report_command.run_generate(
        argparse.Namespace(
            run_id="run_compat", format="json", output_dir=str(tmp_path / "reports")
        )
    )

    assert code == EXIT_OK
    path = tmp_path / "reports" / "run_compat" / "report.json"
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "run_compat"
    assert "Report generated" in capsys.readouterr().out


async def test_report_generate_defaults_to_markdown(
    tmp_path: Path, monkeypatch  # noqa: ANN001
) -> None:
    monkeypatch.setattr(report_command, "load_terminal_report", lambda run_id: _report())

    code = await report_command.run_generate(
        argparse.Namespace(
            run_id="run_compat", format="md", output_dir=str(tmp_path / "reports")
        )
    )

    assert code == EXIT_OK
    markdown = (tmp_path / "reports" / "run_compat" / "report.md").read_text(
        encoding="utf-8"
    )
    assert "# Run Report: run_compat" in markdown
    assert "Ready for release: True" in markdown


async def test_report_generate_unknown_run_writes_nothing(
    tmp_path: Path, monkeypatch, capsys  # noqa: ANN001
) -> None:
    monkeypatch.setattr(report_command, "load_terminal_report", lambda run_id: None)
    output_dir = tmp_path / "reports"

    code = await report_command.run_generate(
        argparse.Namespace(run_id="run_missing", format="json", output_dir=str(output_dir))
    )

    assert code == EXIT_FAILURE
    assert "no terminal report" in capsys.readouterr().err
    assert not output_dir.exists()


# --- legacy engagement.yaml --------------------------------------------------


def test_legacy_engagement_yaml_keeps_the_auto_approve_flag(tmp_path: Path) -> None:
    path = tmp_path / "engagement.yaml"
    path.write_text(
        """
name: "Legacy Engagement"
scope:
  allowed_hosts: ["legacy.local"]
execution_budget:
  max_actions: 3
approval_policy:
  require_approval_for: ["recon"]
  auto_approve: true
""",
        encoding="utf-8",
    )

    # This test is only about EngagementConfig compatibility, not LLM
    # selection -- require_llm=False matches redteam validate/tools'
    # own reasoning (section 4/20 only targets commands that actually
    # build an LLM client).
    config = load_app_config_for_run(str(path), require_llm=False)
    policy = config.engagement.approval_policy

    assert policy.auto_approve is True
    # The new risk fields stay inert so the pre-configurable behaviour is kept.
    assert policy.risk_threshold is None
    assert policy.auto_approve_up_to is None


# --- legacy persisted records ------------------------------------------------


async def test_legacy_persisted_approval_missing_new_fields_still_loads(
    tmp_path: Path,
) -> None:
    approval_dir = tmp_path / "approvals"
    approval_dir.mkdir()
    (approval_dir / "appr_legacy.json").write_text(
        json.dumps(
            {
                "approval_id": "appr_legacy",
                "action_id": "action_legacy",
                "status": "pending",
                "requested_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    loaded = await FilesystemApprovalStore(approval_dir).get("appr_legacy")

    assert loaded is not None
    assert loaded.status is ApprovalStatus.PENDING
    assert loaded.resolved_at is None
    assert loaded.resolved_by is None
    assert loaded.notes == ""


def test_legacy_persisted_event_without_optional_fields_loads_and_redacts() -> None:
    event = EventEnvelope.model_validate(
        {
            "event_id": "evt_legacy",
            "event_type": "ToolExecutionFinished",
            "run_id": "run_legacy",
            "engagement_id": "eng_legacy",
            "source": "orchestration.execute",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "payload": {"tool_id": "recon.ping", "token": "raw-secret"},
        }
    )

    assert event.correlation_id is None
    assert event.causation_id is None
    assert event.payload["token"] == "***REDACTED***"
    assert "raw-secret" not in event.model_dump_json()


# --- MCP adapter backward-compatible name ------------------------------------


def test_streamable_http_adapter_keeps_its_legacy_name() -> None:
    assert StreamableHTTPMCPAdapter is MCPServerAdapter


# --- shared JSON printer (the one stdout contract of --json) -----------------


def test_print_json_is_sorted_valid_json_and_masks_secrets(capsys) -> None:  # noqa: ANN001
    print_json({"zeta": 1, "api_key": "sk-live", "alpha": {"password": "hunter2"}})

    raw = capsys.readouterr().out
    payload = json.loads(raw)
    assert payload == {
        "alpha": {"password": "***REDACTED***"},
        "api_key": "***REDACTED***",
        "zeta": 1,
    }
    assert raw.index('"alpha"') < raw.index('"api_key"') < raw.index('"zeta"')
