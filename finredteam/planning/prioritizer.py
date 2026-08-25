"""후보 순위 결정 + 중복 제거. episodic memory 의 lesson 을 실제로 반영한다 — 이게
빠지면 §설계원칙 3(학습)이 "기록만 하고 안 쓰는" 장식이 된다.
"""
from __future__ import annotations

from finredteam.memory.episodic import EpisodicMemory
from finredteam.models.finding import CandidateFinding


def _dedup_key(candidate: CandidateFinding) -> tuple:
    return (
        candidate.attack_class,
        candidate.principal_id,
        candidate.method,
        candidate.path,
        tuple(sorted(candidate.params.items())),
    )


def rank_and_dedup(
    candidates: list[CandidateFinding], episodic: EpisodicMemory
) -> list[CandidateFinding]:
    seen: set[tuple] = set()
    deduped: list[CandidateFinding] = []
    for candidate in candidates:
        key = _dedup_key(candidate)
        if key in seen:
            continue
        # 이미 똑같은 (attack_class, 주체, 경로) 조합을 시도해서 실패했다면 그대로
        # 반복하지 않는다 — episodic lesson 이 반영되는 지점.
        if episodic.similar_tried(candidate.attack_class, candidate.path):
            already_failed = all(
                e.verdict.value != "VIOLATION"
                for e in episodic.similar_tried(candidate.attack_class, candidate.path)
            )
            if already_failed and key in {
                _dedup_key(e.candidate) for e in episodic.similar_tried(candidate.attack_class, candidate.path)
            }:
                continue
        seen.add(key)
        deduped.append(candidate)

    # 아직 한 번도 시도하지 않은 attack_class 를 우선한다(넓게 훑고 나서 깊게 판다).
    tried = episodic.tried_attack_classes()
    deduped.sort(key=lambda c: (c.attack_class in tried, c.attack_class))
    return deduped
