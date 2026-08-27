from __future__ import annotations

from cicd_redteam_agent.framework.context_window import compact_history


class CharacterCountingLLM:
    context_size = 1_000

    def count_tokens(self, messages) -> int:
        return len(str(messages))


def test_old_limit_messages_are_removed_without_mutating_canonical_history() -> None:
    history = [
        {"role": "system", "content": "trusted"},
        {"role": "user", "content": "Your limits are: first"},
        {"role": "assistant", "content": "one"},
        {"role": "tool", "content": "result-one", "tool_call_id": "call-1"},
        {"role": "user", "content": "Your limits are: second"},
        {"role": "assistant", "content": "two"},
    ]

    result = compact_history(history, CharacterCountingLLM())

    assert result.compacted
    assert result.omitted_messages == 1
    assert sum("Your limits are:" in str(item.get("content")) for item in result.messages) == 1
    assert len(history) == 6


def test_oversized_history_keeps_recent_tool_protocol_and_compacts_content() -> None:
    llm = CharacterCountingLLM()
    history = [{"role": "system", "content": "trusted"}]
    for index in range(5):
        history.extend(
            [
                {"role": "user", "content": f"Your limits are: {index}"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": f"call-{index}"}],
                },
                {
                    "role": "tool",
                    "content": "x" * 4_000,
                    "tool_call_id": f"call-{index}",
                },
            ]
        )

    result = compact_history(history, llm, budget_fraction=0.7)

    assert result.compacted
    assert result.omitted_messages > 0
    latest_tool = next(item for item in result.messages if item.get("role") == "tool")
    assert latest_tool["tool_call_id"] == "call-4"
    assert "context_compacted" in latest_tool["content"]
