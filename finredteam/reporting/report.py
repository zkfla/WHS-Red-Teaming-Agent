"""리포트 집계 — ★ VVR/Precision 계산 안 함 (§9).

`verification_yield`(Verified/Candidate)만 자체 진단용으로 낸다. GT 가 없어도
계산 가능한 값이고, GT 대비 실제 정확도는 `evaluator/scorer.py` 의 몫이다 —
에이전트는 자기 성능을 스스로 채점하지 않는다.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from finredteam.models.core import Principal, Verdict
from finredteam.models.finding import Finding
from finredteam.oracles.campaign import run_all_objectives
from finredteam.storage.db import DB

_LAYERS = ("finance", "generic")


def build(db: DB, manifest: dict[str, Any]) -> dict[str, Any]:
    findings = list(db.read_all("findings"))
    verdict_counts = Counter(f.get("verdict") for f in findings)

    by_layer: dict[str, Counter] = {layer: Counter() for layer in _LAYERS}
    by_operation: Counter = Counter()  # 어떤 메커니즘을 시도했는지 (read_balance/transfer 등)
    by_invariant: Counter = Counter()  # 실행 후 오라클이 실제로 무엇을 판정했는지
    for record in findings:
        candidate = record.get("candidate", {})
        layer = candidate.get("layer")
        verdict = record.get("verdict")
        if layer in by_layer:
            by_layer[layer][verdict] += 1
        by_operation[candidate.get("operation", "unknown")] += 1
        if record.get("invariant"):
            by_invariant[record["invariant"]] += 1

    # 목표(Objective) 판정 — campaign 오라클(순수 계층)로 Finding 을 '공격자 목표'로
    # 조합한다. GT 를 읽지 않으므로 self-scoring(§9) 이 아니다 — 취약점 나열을
    # red teaming 서사(무엇을 달성했나)로 바꿔 리포트에 싣는다.
    finding_objs: list[Finding] = []
    for record in findings:
        try:
            finding_objs.append(Finding.model_validate(record))
        except Exception:
            continue
    principals = [
        Principal(
            id=p["id"], username="", password="",
            account_number=p.get("account_number"), role=p.get("role", "user"),
        )
        for p in (manifest.get("principals") or [])
        if p.get("id")
    ]
    objectives = [
        {"objective": o.objective, "title": o.title, "verdict": o.verdict.value,
         "reason": o.reason, "chain": o.chain}
        for o in run_all_objectives(finding_objs, principals)
        if o.verdict in (Verdict.VIOLATION, Verdict.ANOMALY)
    ]

    candidate_count = len(findings)
    verified_count = verdict_counts.get(Verdict.VIOLATION.value, 0)
    yield_ = round(verified_count / candidate_count, 2) if candidate_count else 0.0

    return {
        "run_id": manifest.get("run_id"),
        "target": manifest.get("target"),
        "llm": manifest.get("llm"),
        "verified_count": verified_count,
        "candidate_count": candidate_count,
        "anomaly_count": verdict_counts.get(Verdict.ANOMALY.value, 0),
        "unknown_count": verdict_counts.get(Verdict.UNKNOWN.value, 0),
        "pass_count": verdict_counts.get(Verdict.PASS.value, 0),
        "verification_yield": yield_,
        "by_layer": {layer: dict(counts) for layer, counts in by_layer.items()},
        "by_operation": dict(by_operation),
        "by_invariant": dict(by_invariant),
        "objectives": objectives,
        "termination_reason": manifest.get("termination_reason"),
        "rounds": manifest.get("round"),
        "strategy_history": manifest.get("strategy_history"),
        "requests": manifest.get("requests"),
        "tokens_in": manifest.get("tokens_in"),
        "tokens_out": manifest.get("tokens_out"),
        "cost_estimate_usd": manifest.get("cost_estimate_usd"),
    }
