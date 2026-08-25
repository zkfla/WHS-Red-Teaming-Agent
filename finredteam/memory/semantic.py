"""확립된 사실(Fact) 저장소. `confidence="observed"` 인 것만 판정 근거로 쓴다 (§14)."""
from __future__ import annotations

from finredteam.models.knowledge import Fact


class SemanticMemory:
    def __init__(self) -> None:
        self._facts: list[Fact] = []

    def add_fact(self, fact: Fact) -> None:
        self._facts.append(fact)

    def add_facts(self, facts: list[Fact]) -> None:
        self._facts.extend(facts)

    def observed(self) -> list[Fact]:
        return [f for f in self._facts if f.confidence == "observed"]

    def all(self) -> list[Fact]:
        return list(self._facts)

    def query(self, subject: str | None = None, predicate: str | None = None) -> list[Fact]:
        return [
            f
            for f in self._facts
            if (subject is None or f.subject == subject)
            and (predicate is None or f.predicate == predicate)
        ]
