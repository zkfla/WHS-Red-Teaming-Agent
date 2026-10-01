"""``RichEventRenderer``: internal ids hidden by default, shown with
``verbose=True`` (section 16/22); real Rich ``Console`` writing into an
in-memory buffer so assertions are on plain text.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest
from rich.console import Console

from redteam.app.cli.renderer import RichEventRenderer
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.ids import EngagementId, EventId, RunId

pytestmark = pytest.mark.asyncio


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, force_terminal=False, width=120), buffer


def _event(event_type: str, payload: dict) -> EventEnvelope:  # noqa: ANN001
    return EventEnvelope(
        event_id=EventId("evt_1"),
        event_type=event_type,
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        source="test",
        timestamp=datetime.now(timezone.utc),
        payload=payload,
    )


async def test_action_proposed_renders_a_human_readable_line_without_ids() -> None:
    console, buffer = _console()
    renderer = RichEventRenderer(console, verbose=False)

    await renderer(
        _event(
            EventType.ACTION_PROPOSED,
            {
                "action_id": "action_deadbeef",
                "candidate_key": "cand_abc123",
                "agent_id": "recon-agent",
                "capability": "recon",
                "operation": "service_discovery",
                "target": "127.0.0.1",
            },
        )
    )

    output = buffer.getvalue()
    assert "Recon Agent" in output
    assert "Discovering services on 127.0.0.1" in output
    assert "action_deadbeef" not in output
    assert "cand_abc123" not in output


async def test_agent_turn_renders_public_reasoning_without_private_trace() -> None:
    from redteam.orchestration.events import AGENT_TURN_DECIDED

    console, buffer = _console()
    renderer = RichEventRenderer(console, verbose=False)
    await renderer(
        _event(
            AGENT_TURN_DECIDED,
            {
                "response": "HTTP 서비스를 먼저 확인하겠습니다.",
                "decision_summary": "현재 호스트에는 서비스 정보가 없습니다.",
                "assumptions": ["대상은 허용 범위 안입니다."],
                "candidate_source": "llm_turn",
            },
        )
    )

    output = buffer.getvalue()
    assert "[Agent] HTTP 서비스를 먼저 확인하겠습니다." in output
    assert "[Reason] 현재 호스트에는 서비스 정보가 없습니다." in output
    assert "[Assumption] 대상은 허용 범위 안입니다." in output


async def test_verbose_mode_shows_the_hidden_internal_fields() -> None:
    console, buffer = _console()
    renderer = RichEventRenderer(console, verbose=True)

    await renderer(
        _event(
            EventType.ACTION_PROPOSED,
            {
                "action_id": "action_deadbeef",
                "agent_id": "recon-agent",
                "capability": "recon",
                "operation": "service_discovery",
                "target": "127.0.0.1",
            },
        )
    )

    assert "action_deadbeef" in buffer.getvalue()


async def test_secrets_are_masked_even_in_verbose_mode() -> None:
    console, buffer = _console()
    renderer = RichEventRenderer(console, verbose=True)

    await renderer(
        _event(
            EventType.ACTION_PROPOSED,
            {
                "agent_id": "web-api-agent",
                "operation": "http_request",
                "target": "a.local",
                "api_key": "sk-live-should-never-appear",
            },
        )
    )

    assert "sk-live-should-never-appear" not in buffer.getvalue()
    assert "REDACTED" in buffer.getvalue()


async def test_tallies_tool_calls_findings_and_hypotheses() -> None:
    console, _buffer = _console()
    renderer = RichEventRenderer(console, verbose=False)

    await renderer(_event(EventType.HYPOTHESIS_CREATED, {}))
    await renderer(_event(EventType.TOOL_EXECUTION_FINISHED, {"success": True}))
    await renderer(_event(EventType.TOOL_EXECUTION_FAILED, {}))
    await renderer(_event(EventType.FINDING_CREATED, {"title": "X", "severity": "high"}))

    assert renderer.hypotheses == 1
    assert renderer.tool_calls == 2
    assert renderer.findings == 1
    assert renderer.violations == 1


async def test_unrendered_event_type_produces_no_line_but_still_tallies() -> None:
    console, buffer = _console()
    renderer = RichEventRenderer(console, verbose=False)

    await renderer(_event(EventType.AGENT_STARTED, {"agent_id": "recon-agent"}))

    assert buffer.getvalue() == ""


async def test_agent_no_executable_candidates_renders_by_default_not_only_verbose() -> None:
    from redteam.orchestration.events import AGENT_NO_EXECUTABLE_CANDIDATES

    console, buffer = _console()
    renderer = RichEventRenderer(console, verbose=False)

    await renderer(
        _event(
            AGENT_NO_EXECUTABLE_CANDIDATES,
            {"agent_id": "web-api-agent", "drafts_offered": 2},
        )
    )

    output = buffer.getvalue()
    assert "Web API Agent" in output
    assert "no executable" in output


async def test_candidate_rejection_renders_safe_diagnostics() -> None:
    from redteam.orchestration.events import CANDIDATE_REJECTED

    console, buffer = _console()
    renderer = RichEventRenderer(console, verbose=False)

    await renderer(
        _event(
            CANDIDATE_REJECTED,
            {
                "operation": "web.http_request",
                "reason": "missing_required_argument",
                "field": "path",
                "candidate_key": "cand_internal",
            },
        )
    )

    output = buffer.getvalue()
    assert "Candidate rejected" in output
    assert "web.http_request: missing_required_argument (path)" in output
    assert "cand_internal" not in output


async def test_result_mapper_failure_renders_without_internal_payload() -> None:
    from redteam.orchestration.events import RESULT_MAPPER_FAILED

    console, buffer = _console()
    renderer = RichEventRenderer(console, verbose=False)

    await renderer(
        _event(
            RESULT_MAPPER_FAILED,
            {
                "operation": "web.http_request",
                "mapper": "map_web_http_request",
                "reason": "invalid_result_shape",
                "traceback": "secret traceback",
            },
        )
    )

    output = buffer.getvalue()
    assert "Result mapper failed" in output
    assert "invalid_result_shape" in output
    assert "secret traceback" not in output


async def test_mcp_selection_and_safe_tool_error_are_visible() -> None:
    from redteam.orchestration.events import MCP_TOOL_SELECTED

    console, buffer = _console()
    renderer = RichEventRenderer(console, verbose=False)

    await renderer(
        _event(
            MCP_TOOL_SELECTED,
            {
                "server_id": "recon-passive",
                "tool_name": "http_probe",
                "risk_level": "low",
                "health_status": "healthy",
            },
        )
    )
    await renderer(
        _event(
            EventType.TOOL_EXECUTION_FAILED,
            {
                "error_code": "mcp_tool_error",
                "error_message": "target connection failed",
            },
        )
    )

    output = buffer.getvalue()
    assert "[MCP] recon-passive/http_probe selected" in output
    assert "mcp_tool_error: target connection failed" in output
