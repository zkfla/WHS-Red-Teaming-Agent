"""Offline paired G4 scheduler benchmark. Writes diagnostics, never canonical state."""

import argparse
import asyncio
import ctypes
import json
import platform
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from redteam_core.investigation.requirements import EvidenceRequirement
from redteam_core.knowledge import CoreSession
from redteam_core.models.operational_budget import OperationalBudgetLimits
from redteam_core.models.runtime import RuntimeBudgetLimits
from redteam_core.runtime import FixedClock
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    build_runtime_composition_fingerprint,
)
from redteam_core.runtime.journal import EventJournal
from redteam_core.runtime.orchestrator import RuntimeOrchestrator
from redteam_core.runtime.passive_scheduler import PassiveResolutionScheduler
from redteam_core.runtime.resolver_dispatch import DurableResolverDispatcher
from tests.helpers import REQUIREMENT_TYPE, catalog
from tests.integration.test_durable_resolver_dispatch import FakeResolver, registry
from tests.performance.baseline import current_core_hash
from tests.performance.statistics import paired_ratio_interval, samples_summary


def median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    return (ordered[middle - 1] + ordered[middle]) / 2 if len(ordered) % 2 == 0 else ordered[middle]


def process_memory():
    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("page_faults", ctypes.c_ulong),
            ("peak_working_set", ctypes.c_size_t),
            ("working_set", ctypes.c_size_t),
            ("quota_peak_paged", ctypes.c_size_t),
            ("quota_paged", ctypes.c_size_t),
            ("quota_peak_nonpaged", ctypes.c_size_t),
            ("quota_nonpaged", ctypes.c_size_t),
            ("pagefile", ctypes.c_size_t),
            ("peak_pagefile", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(Counters),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    success = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    )
    if not success:
        raise OSError("GetProcessMemoryInfo failed")
    return {
        "working_set_bytes": counters.working_set,
        "peak_working_set_bytes": counters.peak_working_set,
    }


async def run_case(concurrency: int, *, latency_seconds: float):
    with tempfile.TemporaryDirectory(prefix="g4-bench-") as directory:
        root = Path(directory)
        resolver = FakeResolver(max_calls=4)
        original = resolver.resolve

        async def delayed(requirement):
            await asyncio.sleep(latency_seconds)
            return await original(requirement)

        resolver.resolve = delayed
        reg = registry(resolver)
        cat = catalog()
        clock = FixedClock(datetime(2026, 9, 8, tzinfo=UTC))
        journal = EventJournal(root / "events.jsonl", run_id="g4-benchmark", clock=clock)
        composition = build_runtime_composition_fingerprint(
            catalog=cat, resolver_descriptors=reg.descriptors()
        )
        orchestrator = RuntimeOrchestrator(
            cat,
            composition,
            clock,
            journal,
            AtomicCheckpointStore(root / "state.json"),
            RuntimeBudgetLimits(max_requests=4),
        )
        runtime = orchestrator.start()
        session = CoreSession()
        requests = []
        for index in range(4):
            requirement = EvidenceRequirement.create(
                requirement_type_id=REQUIREMENT_TYPE,
                provider_id="reference.local",
                subject_ref=f"subject-{index}",
                reason="offline benchmark",
            )
            session.investigation.requirements[requirement.requirement_id] = requirement
            requests.append((f"waiter-{index}", requirement.requirement_id))
        DurableResolverDispatcher.initialize(
            session=session,
            runtime=runtime,
            orchestrator=orchestrator,
            limits=OperationalBudgetLimits(max_requests=4, max_tokens=0, max_concurrent=4),
        )
        dispatcher = DurableResolverDispatcher(
            session=session,
            runtime=runtime,
            orchestrator=orchestrator,
            registry=reg,
            permissions=lambda: frozenset({"read"}),
        )
        scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "benchmark")
        started = time.perf_counter_ns()
        scheduler.admit(
            batch_id="one",
            requests=tuple(requests),
            concurrency=concurrency,
            per_provider_concurrency=concurrency,
        )
        await scheduler.run("one")
        result = scheduler.commit("one", expected_revision=0)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        projection = (
            len(result.recorded_observation_ids),
            session.knowledge.revision,
            runtime.usage.requests,
            len(runtime.resolution_receipts),
        )
        orchestrator.close()
        return elapsed, projection


async def main(args):
    samples = {"serial_ms": [], "concurrent4_ms": []}
    projections = set()
    for index in range(args.warmup + args.samples):
        order = (1, 4) if index % 2 == 0 else (4, 1)
        for concurrency in order:
            elapsed, projection = await run_case(concurrency, latency_seconds=args.latency_seconds)
            projections.add(projection)
            if index >= args.warmup:
                samples["serial_ms" if concurrency == 1 else "concurrent4_ms"].append(elapsed)
    if projections != {(4, 1, 4, 4)}:
        raise AssertionError(f"semantic projection mismatch: {projections}")
    serial = samples["serial_ms"]
    concurrent = samples["concurrent4_ms"]
    receipt = {
        "schema_version": 1,
        "scope": "offline fake resolver; admission, checkpoint fsync, dispatch, receipts, commit",
        "core_sha256": current_core_hash(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "config": {**vars(args), "output": str(args.output)},
        "samples": samples,
        "statistics": {
            "serial": samples_summary(serial),
            "concurrent4": samples_summary(concurrent),
            "paired": paired_ratio_interval(serial, concurrent, seed=404, draws=10_000),
        },
        "summary": {
            "serial_p50_ms": median(serial),
            "concurrent4_p50_ms": median(concurrent),
            "p50_ratio": median(concurrent) / median(serial),
            "canonical_mismatch": 0,
        },
    }
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt["summary"], sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--latency-seconds", type=float, default=0.1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--memory-profile", type=int, choices=(1, 4))
    options = parser.parse_args()
    if options.memory_profile:
        before = process_memory()
        elapsed, projection = asyncio.run(
            run_case(options.memory_profile, latency_seconds=options.latency_seconds)
        )
        payload = {
            "schema_version": 1,
            "profile": options.memory_profile,
            "core_sha256": current_core_hash(),
            "before": before,
            "after": process_memory(),
            "elapsed_ms": elapsed,
            "projection": projection,
        }
        options.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, sort_keys=True))
        raise SystemExit(0)
    if not 30 <= options.samples <= 100 or not 5 <= options.warmup <= 20:
        raise SystemExit("release benchmark requires 30..100 samples and 5..20 warmups")
    asyncio.run(main(options))
