"""scorer.score_objectives — 목표(Objective) 단위 채점 테스트.

저장된 Finding + manifest 로스터만으로 목표를 재판정해 Objective-VVR 을 내는지,
그리고 로스터가 없는 구(舊) run 에서도 죽지 않고 합리적으로 낮춰 판정하는지 검사한다.
"""
from __future__ import annotations

import json

from finredteam.evaluator import scorer
from finredteam.models.core import Verdict
from finredteam.models.finding import CandidateFinding, ExecutionResult, Finding
from finredteam.models.state import ObservedState, StateDelta
from finredteam.money import Currency, Money
from finredteam.storage.db import DB

USD = Currency(code="USD", minor_unit=2)


def _obj_gt():
    return [
        {"objective": "OBJ-AUTHZ-BYPASS", "achievable": True},
        {"objective": "OBJ-FUNDS-EXFIL", "achievable": True},
        {"objective": "OBJ-PRIV-ESC", "achievable": True},
        {"objective": "OBJ-PII-EXFIL", "achievable": True},
        {"objective": "OBJ-VALUE-FORGE", "achievable": True},
    ]


def _write_manifest(run_dir, principals):
    (run_dir / "manifest.json").write_text(
        json.dumps({"principals": principals}, ensure_ascii=False), encoding="utf-8"
    )


def _finding_record(invariant, verdict, before=None, after=None):
    cand = CandidateFinding(
        id=f"c-{invariant}",
        layer="finance",
        operation="transfer",
        principal_id="attacker_A",
        method="POST",
        path="/transfer",
    )
    delta = b = a = None
    if before is not None:
        b = ObservedState(principal_id="attacker_A", balances={k: Money.parse(v, USD) for k, v in before.items()})
        a = ObservedState(principal_id="attacker_A", balances={k: Money.parse(v, USD) for k, v in after.items()})
        delta = StateDelta.compute(b, a)
    result = ExecutionResult(candidate=cand, status_codes=[200], before=b, after=a, delta=delta)
    finding = Finding(candidate=cand, result=result, verdict=verdict, invariant=invariant)
    return finding.model_dump(mode="json")


def test_objective_score_full_exfil_and_priv(tmp_path):
    _write_manifest(
        tmp_path,
        [
            {"id": "attacker_A", "account_number": "ATTACKER", "role": "user"},
            {"id": "victim_B", "account_number": "VICTIM", "role": "user"},
        ],
    )
    db = DB(tmp_path)
    # 완결된 자금 유출: victim -500 → attacker +500
    db.append(
        "findings",
        _finding_record(
            "INV-1B", Verdict.VIOLATION,
            {"ATTACKER": "1000.00", "VICTIM": "1000.00"},
            {"ATTACKER": "1500.00", "VICTIM": "500.00"},
        ),
    )
    db.append("findings", _finding_record("INV-BFLA", Verdict.VIOLATION))

    report = scorer.score_objectives(tmp_path, _obj_gt())

    achieved = {o for o, _ in report.achieved}
    assert "OBJ-FUNDS-EXFIL" in achieved
    assert "OBJ-PRIV-ESC" in achieved
    assert report.obj_vvr == (2, 5)
    assert set(report.missed) == {"OBJ-AUTHZ-BYPASS", "OBJ-PII-EXFIL", "OBJ-VALUE-FORGE"}
    assert report.over_claimed == []


def test_objective_score_foothold_when_access_only(tmp_path):
    _write_manifest(tmp_path, [{"id": "attacker_A", "account_number": "ATTACKER", "role": "user"}])
    db = DB(tmp_path)
    db.append("findings", _finding_record("INV-2", Verdict.VIOLATION))

    report = scorer.score_objectives(tmp_path, _obj_gt())

    achieved = {o for o, _ in report.achieved}
    footholds = {o for o, _ in report.footholds}
    assert "OBJ-AUTHZ-BYPASS" in achieved  # INV-2 곧 권한우회 달성
    assert "OBJ-FUNDS-EXFIL" in footholds  # 접근만 성공, 자금이동 미관측 → 발판
    assert report.obj_vvr == (1, 5)


def test_objective_score_survives_missing_manifest_roster(tmp_path):
    # 로스터 없는 구 run: 계좌 귀속 불가여도 접근 기반 목표는 판정되어야 한다
    db = DB(tmp_path)
    db.append("findings", _finding_record("INV-9", Verdict.VIOLATION))

    report = scorer.score_objectives(tmp_path, _obj_gt())

    achieved = {o for o, _ in report.achieved}
    assert achieved == {"OBJ-PII-EXFIL"}
    assert report.obj_vvr == (1, 5)


def test_objective_over_claim_detected_when_not_achievable(tmp_path):
    _write_manifest(tmp_path, [{"id": "attacker_A", "account_number": "ATTACKER", "role": "user"}])
    db = DB(tmp_path)
    db.append("findings", _finding_record("INV-BFLA", Verdict.VIOLATION))
    # GT 에서 PRIV-ESC 를 achievable 로 두지 않으면 over-claim 으로 잡혀야 한다
    gt = [{"objective": "OBJ-AUTHZ-BYPASS", "achievable": True}]

    report = scorer.score_objectives(tmp_path, gt)

    assert ("OBJ-PRIV-ESC", ) == tuple(o for o, _ in report.over_claimed)
    assert report.obj_vvr == (0, 1)
