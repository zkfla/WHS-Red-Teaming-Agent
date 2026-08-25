"""시도 이력 · 교훈 — planning/prioritizer.py(Day5) 가 다음 후보 순위에 반영한다."""
from __future__ import annotations

from dataclasses import dataclass

from finredteam.models.core import Verdict
from finredteam.models.finding import CandidateFinding, Finding
from finredteam.models.knowledge import Lesson


@dataclass
class Episode:
    candidate: CandidateFinding
    verdict: Verdict
    lesson: Lesson | None = None


class EpisodicMemory:
    def __init__(self) -> None:
        self._episodes: list[Episode] = []

    def record(self, candidate: CandidateFinding, finding: Finding, lesson: Lesson | None = None) -> None:
        self._episodes.append(Episode(candidate=candidate, verdict=finding.verdict, lesson=lesson))

    def all(self) -> list[Episode]:
        return list(self._episodes)

    def lessons(self) -> list[Lesson]:
        return [e.lesson for e in self._episodes if e.lesson is not None]

    def tried_operations(self) -> set[str]:
        return {e.candidate.operation for e in self._episodes}

    def success_rate(self, operation: str) -> float:
        relevant = [e for e in self._episodes if e.candidate.operation == operation]
        if not relevant:
            return 0.0
        hits = sum(1 for e in relevant if e.verdict == Verdict.VIOLATION)
        return hits / len(relevant)

    def similar_tried(self, operation: str, path: str) -> list[Episode]:
        return [e for e in self._episodes if e.candidate.operation == operation and e.candidate.path == path]

    def consecutive_failures(self) -> int:
        """가장 최근부터 연속으로 VIOLATION 이 아니었던 시도 수 — agent/strategy.py(Day6) 가 PIVOT 판단에 쓴다."""
        count = 0
        for episode in reversed(self._episodes):
            if episode.verdict == Verdict.VIOLATION:
                break
            count += 1
        return count
