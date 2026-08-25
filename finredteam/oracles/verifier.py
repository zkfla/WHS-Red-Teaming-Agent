"""CandidateFinding + ExecutionResult → Finding. `registry.py` 로 선언된 오라클을
전부 돌리고, 하나라도 VIOLATION 이면 그걸로 확정한다(§설계원칙 4 — VIOLATION 만 Verified).
"""
from __future__ import annotations

from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.models.core import Verdict
from finredteam.models.finding import CandidateFinding, ExecutionResult, Finding
from finredteam.oracles import registry

_PRIORITY = {Verdict.VIOLATION: 0, Verdict.ANOMALY: 1, Verdict.UNKNOWN: 2, Verdict.PASS: 3}


def verify(
    candidate: CandidateFinding, result: ExecutionResult, graph: KnowledgeGraph | None = None
) -> Finding:
    outcomes = registry.run_all(candidate, result, graph=graph)
    if not outcomes:
        return Finding(
            candidate=candidate,
            result=result,
            verdict=Verdict.UNKNOWN,
            reason=f"candidate.invariants={candidate.invariants} 중 registry 에 구현된 오라클 없음",
            evidence_refs=result.evidence_refs,
        )

    best = min(outcomes, key=lambda o: _PRIORITY[o.verdict])
    return Finding(
        candidate=candidate,
        result=result,
        verdict=best.verdict,
        invariant=best.invariant,
        reason=best.reason,
        evidence_refs=result.evidence_refs,
    )
