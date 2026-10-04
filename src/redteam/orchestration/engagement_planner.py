"""Engagement Planner — "What are we trying to achieve?".

Turns an Engagement (which already answers "what is allowed?") plus an
optional natural-language objective into a ``MissionPlan``: objective,
priorities, success criteria and initial workstreams. It never decides a
concrete action, tool, payload or request — that stays with the Specialized
Agents downstream — and it never stores a secret.

Parsing is deterministic first: keyword extraction always works with no LLM.
An LLM, when supplied, only *enriches* the objective text; if it fails or is
absent the deterministic result stands, because scope/ROE authorization is
never sourced from an LLM.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.mission import MissionPlan, Provenance
from redteam.core.enums import MissionStatus
from redteam.core.ids import MissionId
from redteam.core.redaction import redact_sensitive_text

#: Deterministic focus keywords -> a canonical workstream/priority label.
#: Korean and English, because operators type either.
_FOCUS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "authentication": ("authentication", "인증", "login", "로그인"),
    "authorization": ("authz", "authorization", "인가", "권한", "access control", "접근"),
    "api": ("api", "endpoint", "rest"),
    "web": ("web", "웹", "http"),
    "mobile": ("mobile", "모바일", "apk", "android", "ios"),
    "reconnaissance": ("recon", "정찰", "discovery", "탐색"),
}

_INLINE_SECRET = re.compile(
    r"\b(bearer\s+|(?:password|passwd|token|cookie|api[_-]?key|secret)"
    r"\s+(?:is\s+|는\s*|은\s*|:|=)?\s*)([^\s,;]+)",
    re.IGNORECASE,
)


def sanitize_objective(text: str) -> str:
    """Strip common inline credentials before LLM input or durable Mission state."""
    return _INLINE_SECRET.sub(
        lambda match: f"{match.group(1)}***REDACTED***",
        redact_sensitive_text(text),
    )


def extract_focus_areas(text: str) -> list[str]:
    """Canonical focus labels mentioned in ``text``, order-stable, deduped."""
    lowered = text.lower()
    found = [
        label
        for label, needles in _FOCUS_KEYWORDS.items()
        if any(needle in lowered for needle in needles)
        or (label == "authentication" and re.search(r"\bauthn?\b", lowered))
    ]
    return list(dict.fromkeys(found))


def default_objective(name: str, metadata: Mapping[str, Any]) -> str:
    """Use the operator's saved goal when no request explicitly overrides it."""
    recorded = metadata.get("objective")
    if isinstance(recorded, str) and recorded.strip():
        return sanitize_objective(recorded.strip())
    return f"Assess the declared scope of {sanitize_objective(name)}"


def plan_mission(
    engagement: Engagement,
    mission_id: MissionId,
    *,
    objective: str | None = None,
    focus_areas: tuple[str, ...] = (),
    now: datetime,
) -> MissionPlan:
    """Build a MissionPlan from the Engagement and an optional objective.

    Focus areas come from (in order) the explicit argument, the parsed
    objective text, then any recorded on ``Engagement.metadata['focus_areas']``.
    """

    objective_text = sanitize_objective((objective or "").strip()) or default_objective(
        engagement.name, engagement.metadata
    )
    recorded = engagement.metadata.get("focus_areas")
    focus = (
        list(focus_areas)
        or extract_focus_areas(objective_text)
        or (list(recorded) if isinstance(recorded, list) else [])
    )
    priorities = [str(item) for item in focus]
    supplied_criteria = engagement.metadata.get("success_criteria")
    success_criteria = (
        [str(item) for item in supplied_criteria]
        if isinstance(supplied_criteria, list) and supplied_criteria
        else [objective_text]
    )

    return MissionPlan(
        mission_id=mission_id,
        engagement_id=engagement.engagement_id,
        objective=objective_text,
        priorities=priorities,
        success_criteria=success_criteria,
        initial_workstreams=priorities,
        status=MissionStatus.ACTIVE,
        provenance=Provenance(source="engagement:planner"),
        created_at=now,
        updated_at=now,
    )


async def parse_objective(utterance: str, *, llm: Any = None) -> tuple[str, list[str]]:
    """Interpret a natural-language objective into (objective, focus_areas).

    Deterministic by default. If an ``llm`` is supplied it may rewrite the
    objective into a cleaner sentence, but a failure or a suspicious (secret-
    looking) answer falls back to the operator's own words — the LLM is a
    convenience, never the authorization source.
    """

    cleaned = sanitize_objective(utterance.strip())
    focus = extract_focus_areas(cleaned)
    if llm is None:
        return cleaned or "Assess the declared scope", focus
    try:
        reply = await llm.complete_structured(
            messages=[{"role": "user", "content": cleaned}],
            response_schema={
                "type": "object",
                "properties": {"objective": {"type": "string"}},
                "required": ["objective"],
            },
            system=(
                "Rewrite the operator's request as a single concise security-test "
                "objective sentence. Do not include any credential, token or secret."
            ),
        )
    except Exception:  # noqa: BLE001 - planning must not fail on a bad LLM reply
        return cleaned or "Assess the declared scope", focus
    candidate = str(reply.get("objective", "")).strip() if isinstance(reply, dict) else ""
    if not candidate or _looks_secret(candidate):
        return cleaned or "Assess the declared scope", focus
    return sanitize_objective(candidate), focus or extract_focus_areas(candidate)


def _looks_secret(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("password", "token", "bearer", "cookie", "secret"))
