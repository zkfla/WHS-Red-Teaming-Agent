import pytest

from tests.performance.statistics import paired_ratio_interval, samples_summary
from tests.performance.workloads import Case, prepare, semantic_digest


def test_statistics_preserve_raw_samples_and_nearest_rank():
    summary = samples_summary(list(range(1, 101)))
    assert summary["p50"] == 50.5 and summary["p95"] == 95
    assert summary["samples"] == list(range(1, 101))
    assert not samples_summary([1, 2])["release_sample_count_met"]


def test_paired_bootstrap_is_seeded_and_does_not_approve_a_feature():
    baseline = list(range(1, 31))
    candidate = [n / 2 for n in baseline]
    first = paired_ratio_interval(baseline, candidate)
    assert first == paired_ratio_interval(baseline, candidate)
    assert first["ci95_low"] == first["ci95_high"] == 0.5
    assert first["approval"] == "NOT_EVALUATED"
    assert paired_ratio_interval([1, 2], [1, 2])["assessment"] == "INCONCLUSIVE"


@pytest.mark.parametrize("bad", [[], [-1], [float("nan")], [float("inf")]])
def test_invalid_samples_are_not_silently_excluded(bad):
    with pytest.raises(ValueError):
        samples_summary(bad)


@pytest.mark.parametrize(
    "stage", ["commit", "freshness", "frontier", "context", "checkpoint", "resolve"]
)
@pytest.mark.parametrize("cycles", [1, 10, 100])
def test_scale_fixture_repeated_fresh_construction_has_identical_semantics(stage, cycles):
    hashes = []
    for _ in range(2):
        operation, verify = prepare(Case(stage, 20, cycles))
        hashes.append(semantic_digest(verify(operation())))
    assert hashes[0] == hashes[1]
