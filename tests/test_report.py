from finredteam.reporting import report
from finredteam.storage.db import DB


def test_report_build_counts_and_no_vvr(tmp_path):
    db = DB(tmp_path)
    db.append("findings", {"verdict": "VIOLATION", "candidate": {"layer": "finance", "operation": "transfer"}})
    db.append("findings", {"verdict": "PASS", "candidate": {"layer": "finance", "operation": "transfer"}})
    db.append("findings", {"verdict": "ANOMALY", "candidate": {"layer": "generic", "operation": "probe"}})
    db.append("findings", {"verdict": "UNKNOWN", "candidate": {"layer": "finance", "operation": "read_balance"}})

    manifest = {
        "run_id": "test", "target": "vulnbank", "llm": {"provider": "anthropic", "model": "x"},
        "termination_reason": "max_rounds", "round": 2, "strategy_history": [], "requests": 4,
        "tokens_in": 10, "tokens_out": 10, "cost_estimate_usd": 0.0,
    }

    result = report.build(db, manifest)

    assert result["verified_count"] == 1
    assert result["candidate_count"] == 4
    assert result["anomaly_count"] == 1
    assert result["unknown_count"] == 1
    assert result["pass_count"] == 1
    assert result["verification_yield"] == 0.25
    assert result["by_layer"]["finance"]["VIOLATION"] == 1
    assert result["by_layer"]["generic"]["ANOMALY"] == 1
    assert result["by_operation"]["transfer"] == 2

    # ★ §9 — 에이전트 측 리포트는 VVR/Precision 을 계산하지 않는다.
    assert "vvr" not in {k.lower() for k in result}
    assert "precision" not in {k.lower() for k in result}


def test_report_empty_run(tmp_path):
    result = report.build(DB(tmp_path), {})
    assert result["candidate_count"] == 0
    assert result["verification_yield"] == 0.0


def test_report_includes_objectives_from_findings(tmp_path):
    """report.build 이 campaign 오라클로 '달성한 공격 목표'를 포함하는지 (INV-2 → 권한우회 달성)."""
    from finredteam.models.core import Verdict
    from finredteam.models.finding import CandidateFinding, ExecutionResult, Finding

    db = DB(tmp_path)
    cand = CandidateFinding(
        id="c1", layer="finance", operation="read_balance",
        principal_id="attacker_A", method="GET", path="/check_balance/222",
    )
    finding = Finding(
        candidate=cand, result=ExecutionResult(candidate=cand, status_codes=[200]),
        verdict=Verdict.VIOLATION, invariant="INV-2", reason="entitlement 없는 객체 접근",
    )
    db.append("findings", finding.model_dump(mode="json"))

    manifest = {
        "run_id": "t", "target": "vulnbank", "llm": {},
        "principals": [{"id": "attacker_A", "account_number": "111", "role": "user"}],
    }
    result = report.build(db, manifest)

    objs = {o["objective"]: o for o in result["objectives"]}
    assert objs["OBJ-AUTHZ-BYPASS"]["verdict"] == "VIOLATION"       # 권한우회 달성
    assert objs["OBJ-FUNDS-EXFIL"]["verdict"] == "ANOMALY"          # 접근만, 자금이동 없음 → 발판
    # 재료 없는 목표는 UNKNOWN 이라 리포트 objectives 목록에서 빠진다
    assert "OBJ-PRIV-ESC" not in objs


def test_report_objectives_empty_without_principals_or_valid_findings(tmp_path):
    # 최소 dict finding(모델 검증 실패) → objectives 는 조용히 빈 목록
    db = DB(tmp_path)
    db.append("findings", {"verdict": "VIOLATION", "candidate": {"layer": "finance", "operation": "transfer"}})
    result = report.build(db, {})
    assert result["objectives"] == []
