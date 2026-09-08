import os

import pytest

from tests.performance.runner import available_memory, resident_memory, run_case
from tests.performance.workloads import Case


def test_memory_queries_are_available():
    assert available_memory() > 0
    assert resident_memory(os.getpid()) > 0


def test_spawned_sample_is_verified_and_measured():
    result = run_case(
        Case("commit", 10), repetitions=2, warmup=1, timeout_seconds=10, memory_limit=512 * 1024**2
    )
    assert result["status"] == "PASS"
    assert result["timing_ns"]["count"] == 2
    assert result["timing_ns"]["release_sample_count_met"] is False
    assert result["python_allocation_peak_bytes"] > 0
    assert len(result["semantic_hash"]) == 64


def test_timeout_is_not_success():
    result = run_case(
        Case("commit", 100_000),
        repetitions=30,
        warmup=5,
        timeout_seconds=0.001,
        memory_limit=512 * 1024**2,
    )
    assert result["status"] == "TIMEOUT"
    assert "timing_ns" not in result


def test_memory_limit_is_not_success():
    result = run_case(
        Case("commit", 100_000), repetitions=30, warmup=5, timeout_seconds=10, memory_limit=1
    )
    assert result["status"] == "RESOURCE_LIMIT"
    assert "timing_ns" not in result


def test_invalid_limits_fail_before_spawn():
    with pytest.raises(ValueError):
        run_case(Case("commit", 10), repetitions=0, warmup=0, timeout_seconds=1, memory_limit=1)


def test_paired_control_runs_inside_process_watchdog():
    result = run_case(
        Case("commit", 10),
        repetitions=2,
        warmup=0,
        timeout_seconds=10,
        memory_limit=512 * 1024**2,
        paired=True,
    )
    assert result["status"] == "PASS"
    assert result["comparison_kind"] == "IDENTICAL_ARMS_HARNESS_CONTROL"
    assert result["paired"]["baseline_ns"]["count"] == 2
    assert result["paired"]["memory"] == "NOT_MEASURED_IN_TIMING_SAMPLES"


def test_g1_paired_runner_has_two_modes_and_separate_memory():
    result = run_case(
        Case("payload", 10),
        repetitions=2,
        warmup=0,
        timeout_seconds=10,
        memory_limit=512 * 1024**2,
        paired=True,
        comparison="g1",
    )
    assert result["status"] == "PASS"
    assert result["comparison_kind"] == "G1_FULL_SCAN_VS_INDEXED"
    memory = result["paired"]["separate_memory_samples"]
    assert memory["baseline_python_peak_bytes"] > 0
    assert memory["candidate_python_peak_bytes"] > 0


def test_g2_paired_runner_shared_suffix_is_semantically_identical():
    result = run_case(
        Case("frontier_partial", 30),
        repetitions=2,
        warmup=0,
        timeout_seconds=10,
        memory_limit=512 * 1024**2,
        paired=True,
        comparison="g2",
    )
    assert result["status"] == "PASS"
    assert result["comparison_kind"] == "G2_MEMO_OFF_VS_ON"
    assert result["paired"]["baseline_ns"]["count"] == 2
