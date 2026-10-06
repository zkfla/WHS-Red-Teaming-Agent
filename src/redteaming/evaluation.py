from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable

from .models import Finding


@dataclass(frozen=True)
class EvaluationCase:
    """Ground-truth labels supplied by a testbed owner, never inferred by LLM."""

    name: str
    expected_categories: frozenset[str]

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "EvaluationCase":
        name = str(data.get("name", "unnamed"))
        raw = data.get("expected_categories", ())
        if not isinstance(raw, (list, tuple, set)):
            raise ValueError("expected_categories must be a list")
        return cls(name, frozenset(str(item) for item in raw))


def load_evaluation_cases(path: Path) -> tuple[EvaluationCase, ...]:
    """Load owner-supplied labels; no label is inferred from an LLM."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("cases", payload) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError("evaluation file must contain a list of cases")
    return tuple(EvaluationCase.from_dict(item) for item in entries if isinstance(item, dict))


@dataclass(frozen=True)
class EvaluationResult:
    case: str
    expected: int
    detected_relevant: int
    false_positives: int
    findings: int
    verified_findings: int
    reproducible_findings: int
    coverage: float
    precision: float
    verification_rate: float
    reproducibility_rate: float
    missing_categories: tuple[str, ...] = ()
    unexpected_categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunComparison:
    """Deterministic comparison of two runs over the same owner scope."""

    first_count: int
    second_count: int
    stable_count: int
    stability: float
    only_first: tuple[str, ...]
    only_second: tuple[str, ...]


@dataclass(frozen=True)
class BenchmarkResult:
    """Repeat-run evaluation over one owner-supplied ground-truth case."""

    case: str
    repetitions: int
    runs: tuple[EvaluationResult, ...]
    comparisons: tuple[RunComparison, ...]
    stability: float


class EvaluationHarness:
    """Small deterministic scorer for repeatable testbed comparisons."""

    def evaluate(self, case: EvaluationCase, findings: Iterable[Finding]) -> EvaluationResult:
        items = tuple(findings)
        relevant = sum(1 for item in items if item.category in case.expected_categories)
        false_positives = len(items) - relevant
        detected_categories = {item.category for item in items}
        verified = sum(1 for item in items if item.status.value == "validated")
        reproducible = sum(1 for item in items if item.evidence_ids and item.reproduction_steps)
        return EvaluationResult(
            case=case.name,
            expected=len(case.expected_categories),
            detected_relevant=relevant,
            false_positives=false_positives,
            findings=len(items),
            verified_findings=verified,
            reproducible_findings=reproducible,
            coverage=(relevant / len(case.expected_categories)) if case.expected_categories else 1.0,
            precision=(relevant / len(items)) if items else 1.0,
            verification_rate=(verified / len(items)) if items else 1.0,
            reproducibility_rate=(reproducible / len(items)) if items else 1.0,
            missing_categories=tuple(sorted(case.expected_categories - detected_categories)),
            unexpected_categories=tuple(sorted(detected_categories - case.expected_categories)),
        )

    @staticmethod
    def compare_runs(first: Iterable[Finding], second: Iterable[Finding]) -> RunComparison:
        """Compare category/asset signatures without using mutable Finding IDs."""
        first_signatures = {
            f"{item.category}:{','.join(sorted(item.asset_ids))}" for item in first
        }
        second_signatures = {
            f"{item.category}:{','.join(sorted(item.asset_ids))}" for item in second
        }
        union = first_signatures | second_signatures
        stable = first_signatures & second_signatures
        return RunComparison(
            first_count=len(first_signatures),
            second_count=len(second_signatures),
            stable_count=len(stable),
            stability=(len(stable) / len(union)) if union else 1.0,
            only_first=tuple(sorted(first_signatures - second_signatures)),
            only_second=tuple(sorted(second_signatures - first_signatures)),
        )

    def benchmark(self, case: EvaluationCase,
                  runs: Iterable[Iterable[Finding]]) -> BenchmarkResult:
        """Score repeated completed runs and compare stable Finding signatures."""
        materialized = tuple(tuple(run) for run in runs)
        evaluated = tuple(self.evaluate(case, run) for run in materialized)
        comparisons = tuple(
            self.compare_runs(materialized[index], materialized[index + 1])
            for index in range(max(0, len(materialized) - 1))
        )
        stability = (
            sum(item.stability for item in comparisons) / len(comparisons)
            if comparisons else 1.0
        )
        return BenchmarkResult(case.name, len(materialized), evaluated, comparisons, stability)


def render_evaluation(result: EvaluationResult) -> str:
    return (
        f"case={result.case}\n"
        f"coverage={result.coverage:.2%} precision={result.precision:.2%} "
        f"verification={result.verification_rate:.2%} "
        f"reproducibility={result.reproducibility_rate:.2%}\n"
        f"findings={result.findings} relevant={result.detected_relevant} "
        f"false_positives={result.false_positives}\n"
        f"missing={','.join(result.missing_categories) or '-'} "
        f"unexpected={','.join(result.unexpected_categories) or '-'}"
    )


def render_benchmark(result: BenchmarkResult) -> str:
    lines = [f"benchmark={result.case} repetitions={result.repetitions} stability={result.stability:.2%}"]
    for index, item in enumerate(result.runs, 1):
        lines.append(
            f"run={index} coverage={item.coverage:.2%} precision={item.precision:.2%} "
            f"verification={item.verification_rate:.2%} reproducibility={item.reproducibility_rate:.2%}"
        )
    return "\n".join(lines)
