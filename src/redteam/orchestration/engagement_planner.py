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

from datetime import datetime
from typing import Any

from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.mission import MissionPlan, Provenance
from redteam.core.enums import MissionStatus
from redteam.core.ids import MissionId

#: Deterministic focus keywords -> a canonical workstream/priority label.
#: Korean and English, because operators type either.
_FOCUS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "authentication": ("auth", "authn", "인증", "login", "로그인"),
    "authorization": ("authz", "authorization", "인가", "권한", "access control", "접근"),
    "api": ("api", "endpoint", "rest"),
    "web": ("web", "웹", "http"),
    "mobile": ("mobile", "모바일", "apk", "android", "ios"),
    "reconnaissance": ("recon", "정찰", "discovery", "탐색"),
}


def extract_focus_areas(text: str) -> list[str]:
    """Canonical focus labels mentioned in ``text``, order-stable, deduped."""
    lowered = text.lower()
    found = [
        label
        for label, needles in _FOCUS_KEYWORDS.items()
        if any(needle in lowered for needle in needles)
    ]
    return list(dict.fromkeys(found))


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

    objective_text = (objective or "").strip() or (
        f"Assess the declared scope of {engagement.name}"
    )
    recorded = engagement.metadata.get("focus_areas")
    focus = (
        list(focus_areas)
        or extract_focus_areas(objective_text)
        or (list(recorded) if isinstance(recorded, list) else [])
    )
    priorities = [str(item) for item in focus] or ["reconnaissance", "web/api assessment"]
    success_criteria = [f"verified evidence for {priority}" for priority in priorities]

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

    cleaned = utterance.strip()
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
    return candidate, focus or extract_focus_areas(candidate)


def _looks_secret(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("password", "token", "bearer", "cookie", "secret"))
