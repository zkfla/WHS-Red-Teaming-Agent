"""Alternating paired samples; factories isolate mutation and run outside timing.

Call inside the runner's process watchdog for bounded benchmark runs. This helper
does not impose an unsafe in-process timeout on authoritative operations.
"""

from time import perf_counter_ns

from tests.performance.statistics import paired_ratio_interval, samples_summary
from tests.performance.workloads import semantic_digest


def paired_samples(
    baseline_factory, candidate_factory, *, repetitions=30, warmup=5, clock=perf_counter_ns
):
    if not 2 <= repetitions <= 1000 or not 0 <= warmup <= 100:
        raise ValueError("invalid sample bounds")
    factories = (baseline_factory, candidate_factory)
    samples = ([], [])
    order_log = []
    expected = None
    for pair in range(warmup + repetitions):
        order = (0, 1) if pair % 2 == 0 else (1, 0)
        hashes = {}
        for arm in order:
            operation, verify = factories[arm]()
            started = clock()
            result = operation()
            elapsed = clock() - started
            if elapsed <= 0:
                raise ValueError("nonpositive benchmark duration")
            hashes[arm] = semantic_digest(verify(result))
            if pair >= warmup:
                samples[arm].append(elapsed)
            del operation, verify, result
        if hashes[0] != hashes[1] or (expected is not None and hashes[0] != expected):
            raise AssertionError("canonical mismatch in paired benchmark")
        expected = hashes[0]
        if pair >= warmup:
            order_log.append(list(order))
    return {
        "baseline_ns": samples_summary(samples[0]),
        "candidate_ns": samples_summary(samples[1]),
        "comparison": paired_ratio_interval(*samples),
        "execution_order": order_log,
        "semantic_hash": expected,
        "memory": "NOT_MEASURED_IN_TIMING_SAMPLES",
    }
