"""Bounded G9 whole-composition A/B/C; synthetic, provider-free, local only."""

import argparse
import asyncio
import json
import multiprocessing as mp
import platform
import tempfile
import time
import tracemalloc
from pathlib import Path

from redteam_core.models.base import canonical_json, sha256_text
from tests.integration.test_g9_composition import run_arm
from tests.performance.baseline import ROOT, current_core_hash, tree_hash
from tests.performance.runner import available_memory, resident_memory
from tests.performance.statistics import paired_ratio_interval, samples_summary

ARMS = {
    "baseline": (False, False, False, False),
    "structural": (True, True, True, False),
    "all": (True, True, True, True),
}


def worker(connection, arm, memory, directory):
    try:
        if memory:
            tracemalloc.start()
        started = time.perf_counter_ns()
        output = asyncio.run(run_arm(Path(directory), ARMS[arm], 2))
        elapsed = time.perf_counter_ns() - started
        peak = tracemalloc.get_traced_memory()[1] if memory else None
        connection.send(
            {"status": "PASS", "elapsed_ns": elapsed, "python_peak_bytes": peak, "output": output}
        )
    except BaseException as exc:
        connection.send({"status": "ERROR", "error_type": type(exc).__name__})
    finally:
        connection.close()


def sample(arm, memory, memory_limit, timeout_seconds=60):
    context = mp.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    directory = tempfile.TemporaryDirectory(prefix="g9-composition-")
    process = context.Process(target=worker, args=(sender, arm, memory, directory.name))
    started = time.monotonic()
    process.start()
    sender.close()
    peak = 0
    result = {"status": "WORKER_FAILURE"}
    try:
        while True:
            if receiver.poll(0.01):
                try:
                    result = receiver.recv()
                except EOFError:
                    pass
                break
            if not process.is_alive():
                break
            if time.monotonic() - started > timeout_seconds:
                result = {"status": "TIMEOUT"}
                break
            try:
                peak = max(peak, resident_memory(process.pid))
            except OSError:
                if process.is_alive():
                    result = {"status": "MEMORY_QUERY_ERROR"}
                break
            if peak > memory_limit:
                result = {"status": "RESOURCE_LIMIT"}
                break
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        receiver.close()
        directory.cleanup()
    return {**result, "sampled_rss_peak_bytes": peak}


def fingerprint():
    paths = [
        *(ROOT / "tests/performance").glob("*.py"),
        ROOT / "tests/integration/test_g9_composition.py",
        ROOT / "tests/integration/test_durable_resolver_dispatch.py",
        ROOT / "tests/unit/test_candidate_coverage.py",
        ROOT / "tests/helpers.py",
        ROOT / "uv.lock",
    ]
    return {
        "core": current_core_hash(),
        "harness_and_lock": tree_hash(
            [(path.relative_to(ROOT).as_posix(), path.read_bytes()) for path in paths]
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite an existing receipt")
    before = fingerprint()
    memory_limit = min(2 * 1024**3, available_memory() // 4)
    started = time.monotonic()
    records, expected = [], {}
    stop = None
    # Reversed order alternates systematic startup and machine-load bias.
    for pair in range(35):
        for arm in tuple(ARMS) if pair % 2 == 0 else tuple(reversed(ARMS)):
            remaining = 900 - (time.monotonic() - started)
            if remaining <= 0:
                stop = "TOTAL_BUDGET"
                break
            row = sample(arm, False, memory_limit, min(60, remaining))
            output = row.pop("output", None)
            if row["status"] == "PASS":
                expected.setdefault(arm, output)
                if output != expected[arm]:
                    row["status"] = "REPEAT_MISMATCH"
            records.append({"pair": pair, "arm": arm, "warmup": pair < 5, **row})
            if row["status"] != "PASS":
                stop = row["status"]
                break
        if stop:
            break
    if (
        len(expected) == 3
        and len({json.dumps(row["canonical"], sort_keys=True) for row in expected.values()}) != 1
    ):
        stop = "CANONICAL_MISMATCH"
    memory = {}
    if stop is None:
        for arm in ARMS:
            remaining = 900 - (time.monotonic() - started)
            if remaining <= 0:
                stop = "TOTAL_BUDGET"
                break
            row = sample(arm, True, memory_limit, min(60, remaining))
            output = row.pop("output", None)
            if row["status"] == "PASS" and output != expected[arm]:
                row["status"] = "MEMORY_ARM_MISMATCH"
            memory[arm] = row
            if row["status"] != "PASS":
                stop = row["status"]
    samples = {
        arm: [
            row["elapsed_ns"]
            for row in records
            if row["arm"] == arm and not row["warmup"] and row["status"] == "PASS"
        ]
        for arm in ARMS
    }
    after = fingerprint()
    if before != after:
        stop = "SOURCE_CHANGED"
    statistics = (
        {}
        if stop
        else {
            "arms": {arm: samples_summary(values) for arm, values in samples.items()},
            "structural_vs_baseline": paired_ratio_interval(
                samples["baseline"], samples["structural"]
            ),
            "all_vs_baseline": paired_ratio_interval(samples["baseline"], samples["all"]),
        }
    )
    receipt = {
        "schema_version": 1,
        "status": stop or "PASS",
        "source_before": before,
        "source_after": after,
        "config": {
            "arms": ARMS,
            "pairs": 30,
            "warmup": 5,
            "case_seconds": 60,
            "total_seconds": 900,
            "memory_limit_bytes": memory_limit,
        },
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "scope": "fixture, cold/warm projection, priority, two calls, commit and resume",
        "quality_claim": "NONE; fixed tape is independent of ranking; use G8 quality receipt",
        "records": records,
        "statistics": statistics,
        "separate_memory": memory,
        "elapsed_seconds": time.monotonic() - started,
    }
    receipt["config_hash"] = sha256_text(canonical_json(receipt["config"]))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"status": receipt["status"], "samples": len(records)}))
    return int(stop is not None)


if __name__ == "__main__":
    raise SystemExit(main())
