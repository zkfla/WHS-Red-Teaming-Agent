"""Deterministic reporting math; no automatic performance approval."""

from math import ceil, isfinite
from random import Random
from statistics import median


def samples_summary(values: list[int | float]) -> dict[str, object]:
    if not values or any(not isfinite(v) or v < 0 for v in values):
        raise ValueError("finite nonnegative samples required")
    ordered = sorted(values)
    return {
        "count": len(values),
        "p50": median(values),
        "p95": ordered[ceil(0.95 * len(ordered)) - 1],
        "minimum": ordered[0],
        "maximum": ordered[-1],
        "samples": list(values),
        "p95_method": "nearest-rank",
        "release_sample_count_met": len(values) >= 30,
    }


def paired_ratio_interval(
    baseline: list[int | float],
    candidate: list[int | float],
    *,
    seed: int = 0,
    draws: int = 2000,
) -> dict[str, object]:
    """Bootstrap ratio of medians with paired resampling, candidate / baseline."""
    if len(baseline) != len(candidate) or len(baseline) < 2 or not 100 <= draws <= 100_000:
        raise ValueError("matching paired samples and bounded draws required")
    if any(not isfinite(v) or v <= 0 for v in (*baseline, *candidate)):
        raise ValueError("positive finite durations required")
    rng = Random(seed)
    size = len(baseline)
    ratios = []
    for _ in range(draws):
        indices = [rng.randrange(size) for _ in range(size)]
        ratios.append(median(candidate[i] for i in indices) / median(baseline[i] for i in indices))
    ratios.sort()
    low, high = ratios[int(draws * 0.025)], ratios[min(draws - 1, ceil(draws * 0.975) - 1)]
    return {
        "p50_ratio": median(candidate) / median(baseline),
        "ci95_low": low,
        "ci95_high": high,
        "seed": seed,
        "draws": draws,
        "sample_pairs": size,
        "assessment": "INCONCLUSIVE" if size < 30 or low <= 1 <= high else "DIRECTION_OBSERVED",
        "approval": "NOT_EVALUATED",  # correctness and resource gates are separate
    }
