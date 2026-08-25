"""GT 대비 채점 — VVR, Precision, FP Rate. ★ 지표 계산은 여기서만 한다 (§9).

에이전트와 완전히 분리된 별도 실행:
    python -m finredteam.evaluator.scorer runs/<run_id>

에이전트(`finredteam/agent/`, `reporting/`)는 이 모듈을 import 하지 않는다 —
그러면 "에이전트가 스스로를 채점"하게 되어 Black-box 전제가 깨진다(§9).
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from finredteam.storage.db import DB

_DEFAULT_GT_PATH = Path(__file__).resolve().parent / "ground_truth.yaml"
_ID_SEGMENT_RE = re.compile(r"^[0-9]+$|^[0-9]+[A-Za-z0-9_-]*[0-9][A-Za-z0-9_-]*$")


def _normalize_path(path: str) -> str:
    """`/check_balance/2344073575` -> `/check_balance/{id}` 처럼 식별자 세그먼트를
    뭉갠다 — GT 의 `{id}` 표기와 후보 실행 시점의 실제 계좌번호가 항상 다르므로."""
    segments = [seg for seg in path.split("/") if seg != ""]
    normalized = ["{id}" if _ID_SEGMENT_RE.match(seg) else seg for seg in segments]
    return "/" + "/".join(normalized)


def _endpoint_of(record: dict) -> str:
    candidate = record.get("candidate", {})
    method = (candidate.get("method") or "").upper()
    path = _normalize_path(candidate.get("path") or "")
    return f"{method} {path}"


def load_ground_truth(path: Path | None = None) -> list[dict]:
    raw = yaml.safe_load((path or _DEFAULT_GT_PATH).read_text(encoding="utf-8"))
    return raw or []


@dataclass
class ScoreReport:
    f_vvr: tuple[int, int]
    generic_vvr: tuple[int, int]
    precision: tuple[int, int]
    fp_count: int
    missed: list[dict]

    def format(self) -> str:
        f_matched, f_total = self.f_vvr
        g_matched, g_total = self.generic_vvr
        p_matched, p_total = self.precision
        f_rate = round(f_matched / f_total, 2) if f_total else 0.0
        g_rate = round(g_matched / g_total, 2) if g_total else 0.0
        p_rate = round(p_matched / p_total, 2) if p_total else 0.0
        missed_str = (
            ", ".join(f"{m['id']} {m['attack_class']} {m['description'][:40]}" for m in self.missed)
            if self.missed
            else ""
        )
        return (
            f"F-VVR {f_matched}/{f_total} ({f_rate}) - Generic-VVR {g_matched}/{g_total} ({g_rate}) "
            f"- Precision {p_matched}/{p_total} ({p_rate}) - FP {self.fp_count}\n"
            f"Missed: [{missed_str}]"
        )


def score(run_dir: Path, ground_truth: list[dict] | None = None) -> ScoreReport:
    """VIOLATION Finding 을 attack_class + endpoint 기준으로 GT 와 대조한다."""
    db = DB(run_dir)
    violations = [r for r in db.read_all("findings") if r.get("verdict") == "VIOLATION"]
    gt = ground_truth if ground_truth is not None else load_ground_truth()

    gt_by_key: dict[tuple[str, str], dict] = {
        (item["attack_class"], item["endpoint"]): item for item in gt
    }

    matched_findings = 0
    matched_gt_ids: set[str] = set()
    for record in violations:
        candidate = record.get("candidate", {})
        key = (candidate.get("attack_class"), _endpoint_of(record))
        gt_item = gt_by_key.get(key)
        if gt_item is not None:
            matched_findings += 1
            matched_gt_ids.add(gt_item["id"])

    finance_gt = [g for g in gt if g["layer"] == "finance"]
    generic_gt = [g for g in gt if g["layer"] == "generic"]
    f_matched = sum(1 for g in finance_gt if g["id"] in matched_gt_ids)
    g_matched = sum(1 for g in generic_gt if g["id"] in matched_gt_ids)

    fp_count = len(violations) - matched_findings
    missed = [g for g in gt if g["id"] not in matched_gt_ids]

    return ScoreReport(
        f_vvr=(f_matched, len(finance_gt)),
        generic_vvr=(g_matched, len(generic_gt)),
        precision=(matched_findings, len(violations)),
        fp_count=fp_count,
        missed=missed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finredteam.evaluator.scorer")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--ground-truth", type=Path, default=None)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    gt = load_ground_truth(args.ground_truth) if args.ground_truth else None
    report = score(args.run_dir, gt)
    print(report.format())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
