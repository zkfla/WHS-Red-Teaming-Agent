from __future__ import annotations

from redteam_core.acquisition.models import AnalysisHint


def rank_hints(hints: tuple[AnalysisHint, ...]) -> tuple[AnalysisHint, ...]:
    """Return advisory ordering only; acquisition APIs never accept hints as scope filters."""

    return tuple(sorted(hints, key=lambda item: (-item.score, item.hint_id)))
