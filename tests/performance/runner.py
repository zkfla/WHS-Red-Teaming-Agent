"""Offline bounded benchmark: python -m tests.performance.runner --output receipt.json.

Fixture creation and correctness verification are excluded from operation timing,
but included in the per-case watchdog and resident-memory limit. No network I/O.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import multiprocessing as mp
import os
import platform
import sys
import time
import tracemalloc
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.performance.baseline import ROOT, current_core_hash, tree_hash
from tests.performance.paired import paired_samples
from tests.performance.statistics import samples_summary
from tests.performance.workloads import Case, prepare, semantic_digest


def available_memory() -> int:
    if sys.platform == "win32":

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong)
                for name in (
                    "total",
                    "available",
                    "page",
                    "page_available",
                    "virtual",
                    "virtual_available",
                    "extended",
                )
            ]

        value = MemoryStatus()
        value.length = ctypes.sizeof(value)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
            raise OSError("available memory query failed")
        return value.available
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise OSError("available memory query unsupported")


def resident_memory(pid: int) -> int:
    if sys.platform == "win32":

        class Counters(ctypes.Structure):
            _fields_ = [("size", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "peak",
                    "working",
                    "pool_peak",
                    "pool",
                    "nonpaged_peak",
                    "nonpaged",
                    "page",
                    "page_peak",
                )
            ]

        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
        if not handle:
            raise OSError("process memory query failed")
        try:
            value = Counters()
            value.size = ctypes.sizeof(value)
            query = ctypes.windll.psapi.GetProcessMemoryInfo
            query.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            if not query(handle, ctypes.byref(value), value.size):
                raise OSError("process memory query failed")
            return value.working
        finally:
            kernel.CloseHandle(handle)
    pages = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
    return pages * os.sysconf("SC_PAGE_SIZE")


def _worker(
    connection,
    case: Case,
    repetitions: int,
    warmup: int,
    artifact_root: str,
    paired: bool,
    comparison: str,
) -> None:
    try:
        if paired:

            def factory():
                return prepare(case, artifact_root=artifact_root)

            def candidate_factory():
                return prepare(
                    case,
                    artifact_root=artifact_root,
                    lookup_mode="indexed" if comparison == "g1" else "full_scan",
                    memoize_sink=comparison == "g2",
                )

            result = paired_samples(
                factory, candidate_factory, repetitions=repetitions, warmup=warmup
            )
            if comparison in {"g1", "g2"}:
                peaks = []
                for make in (factory, candidate_factory):
                    tracemalloc.start()
                    operation, verify = make()
                    digest = semantic_digest(verify(operation()))
                    _, peak = tracemalloc.get_traced_memory()
                    tracemalloc.stop()
                    if digest != result["semantic_hash"]:
                        raise AssertionError("memory arm canonical mismatch")
                    peaks.append(peak)
                    del operation, verify
                result["separate_memory_samples"] = {
                    "baseline_python_peak_bytes": peaks[0],
                    "candidate_python_peak_bytes": peaks[1],
                    "kind": "fixture plus operation plus verification Python allocations",
                }
            connection.send(
                {
                    "status": "PASS",
                    "paired": result,
                    "comparison_kind": "G1_FULL_SCAN_VS_INDEXED"
                    if comparison == "g1"
                    else "G2_MEMO_OFF_VS_ON"
                    if comparison == "g2"
                    else "IDENTICAL_ARMS_HARNESS_CONTROL",
                }
            )
            return
        durations = []
        expected = None
        for index in range(warmup + repetitions):
            operation, verify = prepare(case, artifact_root=artifact_root)
            start = time.perf_counter_ns()
            result = operation()
            elapsed = time.perf_counter_ns() - start
            digest = semantic_digest(verify(result))
            if expected is not None and digest != expected:
                raise AssertionError("canonical result changed across samples")
            expected = digest
            if index >= warmup:
                durations.append(elapsed)
            del operation, verify, result
        # Separate sample, including fixture construction, for Python allocation peak.
        # This is NOT resident memory; the parent independently limits process RSS.
        tracemalloc.start()
        operation, verify = prepare(case, artifact_root=artifact_root)
        digest = semantic_digest(verify(operation()))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if digest != expected:
            raise AssertionError("memory sample changed canonical result")
        connection.send(
            {
                "status": "PASS",
                "timing_ns": samples_summary(durations),
                "python_allocation_peak_bytes": peak,
                "semantic_hash": expected,
            }
        )
    except MemoryError:
        connection.send({"status": "OOM"})
    except AssertionError as error:
        connection.send({"status": "ASSERTION_FAILURE", "error": str(error)})
    except Exception as error:
        connection.send({"status": "PREREQUISITE_ERROR", "error_type": type(error).__name__})
    finally:
        connection.close()


def run_case(
    case: Case,
    *,
    repetitions: int,
    warmup: int,
    timeout_seconds: float,
    memory_limit: int,
    paired: bool = False,
    comparison: str = "control",
) -> dict:
    if repetitions < 1 or warmup < 0 or timeout_seconds <= 0 or memory_limit < 1:
        raise ValueError("invalid benchmark limits")
    if paired and repetitions < 2:
        raise ValueError("paired benchmark requires two or more samples")
    if comparison not in {"control", "g1", "g2"} or (comparison != "control" and not paired):
        raise ValueError("invalid paired comparison")
    context = mp.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    directory = TemporaryDirectory(prefix="generic-core-case-")
    process = context.Process(
        target=_worker, args=(sender, case, repetitions, warmup, directory.name, paired, comparison)
    )
    start = time.monotonic()
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
            if time.monotonic() - start > timeout_seconds:
                result = {"status": "TIMEOUT"}
                break
            try:
                peak = max(peak, resident_memory(process.pid))
            except OSError:
                if process.is_alive():
                    result = {"status": "PREREQUISITE_ERROR", "error_type": "MemoryQuery"}
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
    return {
        "case": asdict(case),
        **result,
        "sampled_rss_peak_bytes": peak,
        "wall_seconds": time.monotonic() - start,
    }


def source_fingerprint() -> dict[str, str]:
    paths = [
        *(ROOT / "tests/performance").glob("*.py"),
        ROOT / "uv.lock",
        ROOT / "tests/helpers.py",
    ]
    return {
        "core_hash": current_core_hash(),
        "fixture_and_lock_hash": tree_hash(
            [(p.relative_to(ROOT).as_posix(), p.read_bytes()) for p in paths]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--paired", action="store_true", help="bounded identical-arm A/A control")
    parser.add_argument("--comparison", choices=("control", "g1", "g2"), default="control")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.comparison != "control" and not args.paired:
        parser.error("optimization comparison requires --paired")
    if args.output.exists():
        parser.error("refusing to overwrite an existing receipt")
    config = {
        "repetitions": 30 if args.release else 5,
        "warmup": 5 if args.release else 1,
        "case_timeout_seconds": 60 if args.release else 5,
        "total_timeout_seconds": 900 if args.release else 60,
        "memory_limit_bytes": min(2 * 1024**3, available_memory() // 4),
        "paired": args.paired,
        "comparison": args.comparison,
    }
    sizes = (100, 1000, 10_000) if args.release else (100, 1000)
    cases = [
        Case(stage, size)
        for stage in ("commit", "freshness", "frontier", "context", "checkpoint", "resolve")
        for size in sizes
    ]
    cases += [Case("commit", 100, cycles) for cycles in (10, 100)]
    if args.release:
        cases += [Case("freshness", 100_000)]
    if args.comparison == "g1":
        cases = [
            Case(stage, size) for stage in ("commit", "freshness", "payload") for size in sizes
        ] + [Case("commit", 100, 10)]
    elif args.comparison == "g2":
        cases = [Case(stage, size) for stage in ("frontier", "frontier_partial") for size in sizes]
    before_source = source_fingerprint()
    started = time.monotonic()
    results = []
    for case in cases:
        remaining = config["total_timeout_seconds"] - (time.monotonic() - started)
        if remaining <= 0:
            results.append({"case": asdict(case), "status": "NOT_RUN_TOTAL_BUDGET"})
            continue
        results.append(
            run_case(
                case,
                repetitions=config["repetitions"],
                warmup=config["warmup"],
                timeout_seconds=min(remaining, config["case_timeout_seconds"]),
                memory_limit=config["memory_limit_bytes"],
                paired=config["paired"],
                comparison=config["comparison"],
            )
        )
    after_source = source_fingerprint()
    receipt = {
        "schema_version": 1,
        "mode": "release" if args.release else "quick",
        "approval": "NOT_EVALUATED",
        "environment": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        **before_source,
        "source_after_run": after_source,
        "source_integrity": "PASS" if before_source == after_source else "SOURCE_CHANGED",
        "config": config,
        "config_hash": semantic_digest(config),
        "rss_sampling_interval_seconds": 0.01,
        "memory_limit_kind": "sampled RSS watchdog, not OS hard allocation limit",
        "cases": results,
    }
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(receipt, output, indent=2)
        output.write("\n")
    print(
        json.dumps(
            {"receipt": str(args.output.resolve()), "statuses": [row["status"] for row in results]}
        )
    )
    return (
        0
        if before_source == after_source and all(row["status"] == "PASS" for row in results)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
