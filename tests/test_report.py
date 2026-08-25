from finredteam.reporting import report
from finredteam.storage.db import DB


def test_report_build_counts_and_no_vvr(tmp_path):
    db = DB(tmp_path)
    db.append("findings", {"verdict": "VIOLATION", "candidate": {"layer": "finance", "attack_class": "F2"}})
    db.append("findings", {"verdict": "PASS", "candidate": {"layer": "finance", "attack_class": "F2"}})
    db.append("findings", {"verdict": "ANOMALY", "candidate": {"layer": "generic", "attack_class": "G-SQLI"}})
    db.append("findings", {"verdict": "UNKNOWN", "candidate": {"layer": "finance", "attack_class": "F1"}})

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
    assert result["by_attack_class"]["F2"] == 2

    # ★ §9 — 에이전트 측 리포트는 VVR/Precision 을 계산하지 않는다.
    assert "vvr" not in {k.lower() for k in result}
    assert "precision" not in {k.lower() for k in result}


def test_report_empty_run(tmp_path):
    result = report.build(DB(tmp_path), {})
    assert result["candidate_count"] == 0
    assert result["verification_yield"] == 0.0
