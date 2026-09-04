from __future__ import annotations

from pathlib import Path

import pytest

from tests.benchmarks.p0_goat_benchmark import (
    DEFAULT_GOAT_ROOT,
    GOAT_COMMIT,
    run_p0_goat_benchmark,
)


@pytest.mark.asyncio
async def test_pinned_goat_p0_benchmark(tmp_path: Path) -> None:
    if not DEFAULT_GOAT_ROOT.is_dir():
        pytest.skip("pinned GitHub Actions Goat checkout is not available")

    result = await run_p0_goat_benchmark(DEFAULT_GOAT_ROOT, tmp_path / "goat-benchmark")

    assert result["repository"]["commit"] == GOAT_COMMIT
    assert result["repository"]["workflow_execution"] == "not_run"
    assert result["candidate_parity"]["passed"]
    assert result["gates"]["B_no_silent_drop"]
    assert result["gates"]["C_multibatch_root_cause_coverage"]
    assert result["gates"]["D_execution_not_privileged"]
    assert result["gates"]["candidate_backed_submit"]
    assert result["counts"]["candidates"] > 0
    assert len(result["portfolio"]["unique_root_causes"]) >= 2
    assert len(result["portfolio"]["unique_candidate_kinds"]) >= 2
    assert result["reasoning_smoke"]["accepted_candidate_id"]
    assert result["reasoning_smoke"]["observation_count"] == 0
    assert result["coverage"]["batch_count"] >= 2
    assert not result["coverage"]["uncovered_root_cause_instances"]
    assert result["frontier_semantics"]["execution_frontiers"] > 0
    assert result["frontier_semantics"]["privileged_execution_frontiers"] == 0
