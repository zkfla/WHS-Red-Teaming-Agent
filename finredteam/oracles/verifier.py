"""CandidateFinding + ExecutionResult → Finding.

`registry.run_all()` 이 등록된 오라클 전체를 돌린다(candidate 가 좁히지 않음).
여러 개가 동시에 유의미한 판정을 낼 수 있으므로, 보고용 단일 Finding 은
그중 가장 심각한 것(VIOLATION > ANOMALY > UNKNOWN > PASS)으로 만들되,
`run_all_outcomes()`로 전체 목록을 그대로 노출해서 호출부(ReAct Observation
등)가 놓치는 신호 없이 다 보여줄 수 있게 한다.
"""
from __future__ import annotations

from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.models.core import Verdict
from finredteam.models.finding import CandidateFinding, ExecutionResult, Finding
from finredteam.oracles import registry
from finredteam.oracles.base import OracleOutcome

_PRIORITY = {Verdict.VIOLATION: 0, Verdict.ANOMALY: 1, Verdict.UNKNOWN: 2, Verdict.PASS: 3}


def run_all_outcomes(
    candidate: CandidateFinding, result: ExecutionResult, graph: KnowledgeGraph | None = None
) -> list[OracleOutcome]:
    return registry.run_all(candidate, result, graph=graph)


def verify_from_outcomes(
    candidate: CandidateFinding, result: ExecutionResult, outcomes: list[OracleOutcome]
) -> Finding:
    if not outcomes:
        return Finding(
            candidate=candidate,
            result=result,
            verdict=Verdict.UNKNOWN,
            reason="registry 에 등록된 오라클이 없음",
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


def verify(
    candidate: CandidateFinding, result: ExecutionResult, graph: KnowledgeGraph | None = None
) -> Finding:
    return verify_from_outcomes(candidate, result, run_all_outcomes(candidate, result, graph))
