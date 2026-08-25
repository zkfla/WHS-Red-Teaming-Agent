from finredteam.evaluator import scorer
from finredteam.storage.db import DB


def _gt():
    return [
        {"id": "VB-1", "layer": "finance", "invariant": "INV-1B", "endpoint": "POST /transfer", "description": "neg amount"},
        {"id": "VB-2", "layer": "generic", "invariant": "G-SQLI", "endpoint": "GET /check_balance/{id}", "description": "sqli"},
        {"id": "VB-3", "layer": "finance", "invariant": "INV-4", "endpoint": "POST /charge", "description": "idempotency"},
    ]


def test_scorer_matches_by_invariant_and_normalized_endpoint(tmp_path):
    db = DB(tmp_path)
    db.append(
        "findings",
        {"verdict": "VIOLATION", "invariant": "INV-1B", "candidate": {"operation": "transfer", "method": "POST", "path": "/transfer"}},
    )
    db.append(
        "findings",
        {"verdict": "VIOLATION", "invariant": "G-SQLI", "candidate": {"operation": "probe", "method": "GET", "path": "/check_balance/9254444264"}},
    )
    db.append(
        "findings",
        {"verdict": "PASS", "invariant": "INV-4", "candidate": {"operation": "charge", "method": "POST", "path": "/charge"}},
    )
    db.append(
        "findings",
        {"verdict": "VIOLATION", "invariant": "G-XSS", "candidate": {"operation": "probe", "method": "GET", "path": "/whatever"}},
    )

    report = scorer.score(tmp_path, _gt())

    assert report.f_vvr == (1, 2)
    assert report.generic_vvr == (1, 1)
    assert report.precision == (2, 3)
    assert report.fp_count == 1
    assert {m["id"] for m in report.missed} == {"VB-3"}


def test_scorer_no_violations_means_zero_precision_denominator(tmp_path):
    db = DB(tmp_path)
    db.append(
        "findings",
        {"verdict": "PASS", "invariant": "INV-1B", "candidate": {"operation": "transfer", "method": "POST", "path": "/transfer"}},
    )
    report = scorer.score(tmp_path, _gt())
    assert report.precision == (0, 0)
    assert report.fp_count == 0
    assert len(report.missed) == 3


def test_normalize_path_collapses_numeric_and_alnum_ids():
    assert scorer._normalize_path("/check_balance/9254444264") == "/check_balance/{id}"
    assert scorer._normalize_path("/api/virtual-cards/5/fund") == "/api/virtual-cards/{id}/fund"
    assert scorer._normalize_path("/transfer") == "/transfer"


def test_ground_truth_loads_default_file():
    gt = scorer.load_ground_truth()
    assert len(gt) >= 5
    assert all({"id", "layer", "invariant", "endpoint"} <= set(item) for item in gt)
