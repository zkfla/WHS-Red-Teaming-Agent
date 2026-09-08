import json
import sys

import pytest

from tests.performance import g9_release_benchmark as benchmark


@pytest.mark.parametrize("failure", [None, "ERROR", "TIMEOUT", "mismatch"])
def test_composed_runner_preserves_failures_and_only_summarizes_complete_pairs(
    tmp_path,
    monkeypatch,
    failure,
):
    output = tmp_path / "receipt.json"
    monkeypatch.setattr(sys, "argv", ["benchmark", "--output", str(output)])
    monkeypatch.setattr(benchmark, "available_memory", lambda: 1024**3)
    calls = []

    def sample(arm, memory, limit, timeout):
        calls.append((arm, memory))
        assert limit == 1024**3 // 4 and 0 < timeout <= 60
        if failure in {"ERROR", "TIMEOUT"} and len(calls) == 5:
            return {"status": failure}
        canonical = {"fixed": 1 if failure != "mismatch" or arm != "all" else 2}
        return {
            "status": "PASS",
            "elapsed_ns": 1000,
            "python_peak_bytes": 100,
            "output": {"canonical": canonical},
        }

    monkeypatch.setattr(benchmark, "sample", sample)
    assert benchmark.main() == int(failure is not None)
    receipt = json.loads(output.read_text())
    if failure is None:
        assert receipt["status"] == "PASS"
        assert len(receipt["records"]) == 105
        assert len(receipt["separate_memory"]) == 3
        assert receipt["statistics"]["arms"]["all"]["count"] == 30
    else:
        assert receipt["statistics"] == {}
        assert receipt["status"] == ("CANONICAL_MISMATCH" if failure == "mismatch" else failure)
        assert not receipt["separate_memory"]
    assert len(receipt["config_hash"]) == 64
    with pytest.raises(SystemExit):
        benchmark.main()  # Existing raw receipt cannot be silently replaced.
