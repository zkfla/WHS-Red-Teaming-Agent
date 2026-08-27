from datetime import UTC, datetime
from pathlib import Path

import pytest

from cicd_redteam_agent.domain.models import AnalysisState, RepositorySnapshot
from cicd_redteam_agent.framework.codex_llm import (
    CodexChatGPTLLM,
    _isolated_config_overrides,
    _parse_decision_response,
)
from cicd_redteam_agent.framework.decision_context import build_decision_context
from cicd_redteam_agent.framework.runtime_support import RecoverableModelOutputError


def _state() -> AnalysisState:
    return AnalysisState(
        repository=RepositorySnapshot(
            repository_id="repo_test",
            root="C:/sensitive/local/path",
            git_commit="abc123",
            dirty=False,
            analyzed_at=datetime.now(UTC),
        ),
        pending_questions=["Which credential sink is reachable?"],
        inspected_resources=[".github/workflows/ci.yml"],
        repeated_calls={"first": 1, "duplicate": 3},
    )


def test_codex_inference_runtime_disables_plugins_and_configured_mcp_servers(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "config.toml").write_text(
        """
[mcp_servers.chrome-devtools]
command = "npx"

[mcp_servers.semgrep]
command = "semgrep"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    overrides = _isolated_config_overrides()

    assert overrides == (
        "features.plugins=false",
        "mcp_servers.chrome-devtools.enabled=false",
        "mcp_servers.semgrep.enabled=false",
    )


def test_codex_decision_context_keeps_state_and_only_recent_events() -> None:
    history = [{"role": "system", "content": "trusted policy"}]
    history.extend(
        {"role": "tool", "content": f"old-result-{index}", "tool_call_id": f"old-{index}"}
        for index in range(10)
    )
    history.append(
        {"role": "tool", "content": "latest-result", "tool_call_id": "latest"}
    )

    context = build_decision_context(history, _state())

    assert context["trusted_agent_instructions"] == "trusted policy"
    assert context["analysis_state"]["repository"] == {
        "repository_id": "repo_test",
        "git_commit": "abc123",
        "dirty": False,
    }
    assert context["analysis_state"]["pending_questions"] == [
        "Which credential sink is reachable?"
    ]
    assert context["analysis_state"]["call_progress"]["repeated_calls"] == 2
    assert context["omitted_earlier_events"] == 4
    assert context["recent_agent_events"][-1]["content"] == "latest-result"
    assert "old-result-0" not in str(context)
    assert "C:/sensitive/local/path" not in str(context)


def test_codex_prompt_uses_projected_context_instead_of_full_conversation() -> None:
    history = [
        {"role": "system", "content": "trusted policy"},
        *(
            {"role": "tool", "content": f"discard-{index}", "tool_call_id": str(index)}
            for index in range(8)
        ),
        {"role": "tool", "content": "keep-latest", "tool_call_id": "latest"},
    ]
    llm = CodexChatGPTLLM(model=None, context_size=128_000, state=_state())

    prompt = llm._build_prompt(history, {})

    assert "DECISION_CONTEXT" in prompt
    assert "AGENT_CONVERSATION" not in prompt
    assert "keep-latest" in prompt
    assert "discard-0" not in prompt


def test_codex_context_ignores_provider_state_message_and_uses_canonical_state() -> None:
    state = _state()
    history = [
        {"role": "system", "content": "trusted policy"},
        {
            "role": "user",
            "name": "agent_state_projection",
            "content": "stale provider projection",
        },
        {"role": "tool", "content": "latest-result", "tool_call_id": "latest"},
    ]

    context = build_decision_context(history, state)

    assert "stale provider projection" not in str(context)
    assert context["analysis_state"]["working_memory"]["revision"] == 0


def test_malformed_codex_tool_arguments_are_retryable_without_echoing_content() -> None:
    malformed = (
        '{"assistant_content":"sensitive text","tool_name":"ListWorkflows",'
        '"arguments_json":"{broken"}'
    )

    with pytest.raises(RecoverableModelOutputError) as raised:
        _parse_decision_response(malformed, {"ListWorkflows": object()})

    assert "sensitive text" not in str(raised.value)
