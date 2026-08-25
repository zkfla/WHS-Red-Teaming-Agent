"""중복 실험 판단. episodic memory 를 실제로 반영한다 — 이게 빠지면 §설계원칙 3
(학습)이 "기록만 하고 안 쓰는" 장식이 된다.

ReAct 로 바뀌면서 "여러 후보를 한꺼번에 순위 매기기"는 없어졌다 — LLM 이 한 번에
하나씩 `propose_hypothesis` → `execute_experiment` 를 낸다. 대신 정확히 같은
조합을 이미 실패로 시도했는지 알려줘서(Observation), LLM 이 스스로 다른 걸
시도하도록 유도한다.
"""
from __future__ import annotations

from finredteam.memory.episodic import EpisodicMemory
from finredteam.models.finding import CandidateFinding


def dedup_key(candidate: CandidateFinding) -> tuple:
    return (
        candidate.operation,
        candidate.principal_id,
        candidate.method,
        candidate.path,
        tuple(sorted(candidate.params.items())),
    )


def already_tried_and_failed(candidate: CandidateFinding, episodic: EpisodicMemory) -> bool:
    """정확히 같은 (operation, 주체, 경로, 파라미터) 조합을 이미 시도했고,
    그중 VIOLATION 이 하나도 없었으면 True — 굳이 다시 실행할 필요가 없다."""
    similar = episodic.similar_tried(candidate.operation, candidate.path)
    if not similar:
        return False
    key = dedup_key(candidate)
    matches = [e for e in similar if dedup_key(e.candidate) == key]
    if not matches:
        return False
    return all(e.verdict.value != "VIOLATION" for e in matches)
