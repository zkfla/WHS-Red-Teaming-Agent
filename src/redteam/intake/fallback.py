"""Conservative, deterministic Intake when structured LLM parsing fails."""

from __future__ import annotations

import re

from redteam.intake.models import (
    CANONICAL_CAPABILITIES,
    EngagementDraft,
    IntakeTurn,
    missing_required_fields,
)
from redteam.intake.serializer import merge_draft

_HOST = re.compile(
    r"(?<![\w.-])(?:https?://)?"
    r"((?:(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}|(?:\d{1,3}\.){3}\d{1,3})(?::\d{1,5})?)"
)
_BUDGET = re.compile(r"\b(?:max_actions\s*[:=]\s*|최대\s*)(\d{1,6})(?!\d)", re.IGNORECASE)
_NAME = re.compile(r"(?:^|\s)(?:name|대상\s*이름)\s*[:=]\s*([^,;]+)", re.IGNORECASE)
_QUESTIONS = {
    "name": "테스트 대상의 이름은 무엇인가요?",
    "scope (at least one allowed host/domain/network)": (
        "허용된 테스트 호스트 또는 도메인은 무엇인가요?"
    ),
    "max_actions (execution budget)": "최대 실행 횟수는 몇 회인가요?",
    "allowed_capabilities": "허용된 기능을 recon, web, mobile 중에서 명시해 주세요.",
}


def deterministic_intake_turn(current: EngagementDraft, message: str) -> IntakeTurn:
    """Extract only explicit values; never infer ROE or authorize new operations."""
    text = message.strip()
    patch: dict[str, object] = {}
    host_match = (
        None
        if re.search(r"\b(?:deny|exclude|forbid)\b|금지|제외", text, re.I)
        else _HOST.search(text)
    )
    has_scope = current.allowed_hosts or current.allowed_domains or current.allowed_networks
    if host_match and not has_scope:
        patch["allowed_hosts"] = [host_match.group(1)]

    if not current.name:
        named = _NAME.search(text)
        if named:
            patch["name"] = named.group(1).strip()[:100]
        elif host_match:
            patch["name"] = host_match.group(1)
        elif len(text.split()) == 1 and not text.isdigit() and not _BUDGET.search(text):
            patch["name"] = text[:100]

    if current.max_actions is None:
        budget = _BUDGET.search(text)
        budget_value: int | None
        if budget is None and text.isdigit():
            budget_value = int(text)
        else:
            budget_value = int(budget.group(1)) if budget else None
        if budget_value is not None and budget_value > 0:
            patch["max_actions"] = budget_value

    if not current.allowed_capabilities:
        words = {word.lower() for word in re.findall(r"[A-Za-z]+", text)}
        declared = [cap for cap in CANONICAL_CAPABILITIES if cap in words]
        explicit_allow = "허용" in text or "allow" in words or words <= set(CANONICAL_CAPABILITIES)
        if declared and explicit_allow:
            patch["allowed_capabilities"] = declared

    proposed = EngagementDraft.model_validate(patch)
    merged = merge_draft(current, proposed)
    missing = missing_required_fields(merged)
    if not merged.allowed_capabilities:
        missing.append("allowed_capabilities")
    return IntakeTurn(
        draft=proposed,
        follow_up_questions=[_QUESTIONS[missing[0]]] if missing else [],
        ready=not missing,
    )
