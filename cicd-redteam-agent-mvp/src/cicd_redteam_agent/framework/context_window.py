from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


def _field(message: Any, name: str, default: Any = None) -> Any:
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


def _role(message: Any) -> str:
    return str(_field(message, "role", "assistant"))


def _is_limit_message(message: Any) -> bool:
    content = str(_field(message, "content", "") or "")
    return _role(message) == "user" and content.startswith("Your limits are:")


@dataclass(frozen=True)
class ContextWindowResult:
    messages: list[Any]
    compacted: bool = False
    omitted_messages: int = 0


def _round_groups(messages: list[Any]) -> list[list[Any]]:
    groups: list[list[Any]] = []
    current: list[Any] = []
    for message in messages:
        if _role(message) == "user" and current:
            groups.append(current)
            current = [message]
        else:
            current.append(message)
    if current:
        groups.append(current)
    return groups


def _compact_tool_messages(messages: list[Any], max_characters: int = 2_000) -> list[Any]:
    compacted: list[Any] = []
    for message in messages:
        content = _field(message, "content", "") or ""
        if _role(message) != "tool" or len(content) <= max_characters:
            compacted.append(message)
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        replacement = (
            f'{{"context_compacted":true,"original_characters":{len(content)},'
            f'"sha256_prefix":"{digest}","guidance":"Re-query this capability with '
            'narrower filters or pagination before citing its facts."}'
        )
        if isinstance(message, dict):
            compacted.append({**message, "content": replacement})
        else:
            compacted.append(message)
    return compacted


def compact_history(
    history: list[Any],
    llm: Any,
    *,
    budget_fraction: float = 0.7,
    minimum_recent_groups: int = 3,
) -> ContextWindowResult:
    context_size = int(getattr(llm, "context_size", 0) or 0)
    count_tokens = getattr(llm, "count_tokens", None)
    if context_size <= 0 or count_tokens is None or len(history) <= 2:
        return ContextWindowResult(list(history))

    limit_indices = [index for index, message in enumerate(history) if _is_limit_message(message)]
    latest_limit = limit_indices[-1] if limit_indices else None
    working_history = [
        message
        for index, message in enumerate(history)
        if not _is_limit_message(message) or index == latest_limit
    ]
    removed_limits = len(history) - len(working_history)
    budget = max(1, int(context_size * budget_fraction))
    try:
        if count_tokens(working_history) <= budget:
            return ContextWindowResult(
                working_history,
                compacted=removed_limits > 0,
                omitted_messages=removed_limits,
            )
    except Exception:
        return ContextWindowResult(list(history))

    system_messages = [message for message in working_history if _role(message) == "system"][:1]
    non_system = [message for message in working_history if _role(message) != "system"]
    groups = _round_groups(non_system)
    if not groups:
        return ContextWindowResult(list(history))

    kept = groups[-minimum_recent_groups:]
    omitted = groups[:-minimum_recent_groups]
    while True:
        summary = {
            "role": "user",
            "content": (
                f"{sum(len(group) for group in omitted)} earlier agent-history messages were "
                "omitted to stay within the model context budget. Re-query the bounded "
                "capabilities for any facts or relation IDs needed for the final proposal."
            ),
        }
        candidate = [*system_messages, summary, *(item for group in kept for item in group)]
        try:
            if count_tokens(candidate) <= budget:
                return ContextWindowResult(
                    candidate,
                    compacted=True,
                    omitted_messages=removed_limits + sum(len(group) for group in omitted),
                )
        except Exception:
            return ContextWindowResult(list(history))
        if len(kept) <= 1:
            break
        omitted.append(kept.pop(0))

    if kept:
        compacted_group = _compact_tool_messages(kept[0])
        summary = {
            "role": "user",
            "content": (
                f"{sum(len(group) for group in omitted)} earlier agent-history messages were "
                "omitted and an oversized tool result was compacted. Re-query the bounded "
                "capabilities with narrower filters or pagination before citing omitted facts."
            ),
        }
        candidate = [*system_messages, summary, *compacted_group]
        try:
            if count_tokens(candidate) <= budget:
                return ContextWindowResult(
                    candidate,
                    compacted=True,
                    omitted_messages=removed_limits + sum(len(group) for group in omitted),
                )
        except Exception:
            pass

    return ContextWindowResult(list(history))
