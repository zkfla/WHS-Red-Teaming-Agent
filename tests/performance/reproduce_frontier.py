"""Strict offline G0 correctness gate. Exit 1 on mismatch; never calls a provider.

Run: python -m tests.performance.reproduce_frontier --output <new-receipt.json>
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from itertools import permutations
from pathlib import Path

from tests.performance.baseline import current_core_hash, release_baseline
from tests.performance.graph_adapter import core_reachability
from tests.performance.oracles import Edge, graph_oracle


def evaluate_case(name: str, edges: tuple[Edge, ...]) -> dict[str, object]:
    expected = graph_oracle(edges)
    signatures = set()
    mismatches = 0
    count = 0
    for ordering in permutations(edges):
        actual, result = core_reachability(ordering)
        signatures.add(tuple(sorted(actual)))
        count += 1
        mismatches += actual != expected or result.truncated or not result.search_complete
    actual, result = core_reachability(edges)
    return {
        "case": name,
        "edges": [asdict(e) for e in edges],
        "expected_reachability": sorted(expected),
        "actual_reachability": sorted(actual),
        "missing": sorted(expected - actual),
        "unexpected": sorted(actual - expected),
        "search_complete": result.search_complete,
        "truncated": result.truncated,
        "expansions_used": result.expansions_used,
        "orderings_checked": count,
        "mismatched_orderings": mismatches,
        "distinct_actual_signatures": len(signatures),
        "gate": "FAILED" if mismatches else "PASSED",
    }


def report() -> dict[str, object]:
    merged = (
        Edge("source:one", "a", "source"),
        Edge("source:two", "a", "source"),
        Edge("a", "b"),
        Edge("b", "z", "sink"),
    )
    shortcut = (
        Edge("s", "a", "source"),
        Edge("a", "b", verdict="FALSE"),
        Edge("a", "c"),
        Edge("c", "b"),
        Edge("b", "z", "sink"),
    )
    cases = [
        evaluate_case("source_convergence", merged),
        evaluate_case("false_shortcut_suppresses_viable_detour", shortcut),
        evaluate_case("control_first_source_alone", (merged[0], *merged[2:])),
        evaluate_case("control_second_source_alone", (merged[1], *merged[2:])),
        evaluate_case("control_without_false_shortcut", (shortcut[0], *shortcut[2:])),
    ]
    return {
        "schema_version": 1,
        "purpose": "G0 independent correctness gate, not a performance result",
        "baseline": release_baseline(),
        "actual_core_sha256": current_core_hash(),
        "query": {"max_depth": 8, "max_expansions": 100_000, "max_frontiers": 100_000},
        "external_calls": 0,
        "cases": cases,
        "gate": "FAILED" if any(c["gate"] == "FAILED" for c in cases) else "PASSED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = report()
    content = json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        # Refuse to overwrite an earlier receipt; all data is from synthetic fixtures.
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    print(content)
    return int(result["gate"] != "PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
