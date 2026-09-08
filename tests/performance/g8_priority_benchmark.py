"""Fixed-budget, provider-free priority comparison with predeclared toy ground truth.

Uses real durable priority/resolution/commit paths; no LLM or network. Timing is
descriptive local wall time, not a statistical speed/default-enable claim.
"""

import argparse
import asyncio
import json
import platform
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from redteam_core.investigation.coverage import reconcile_candidate_state
from redteam_core.investigation.priority import (
    BaselinePriorityProvider,
    ExpectedUtilityPriorityProvider,
    root_evidence_fingerprint,
)
from redteam_core.investigation.resolution import (
    EvidenceResolutionError,
    ResolverDescriptor,
    ResolverOutput,
    ResolverRuntimeRegistry,
)
from redteam_core.knowledge.commit import apply_observations_atomic
from redteam_core.knowledge.state import CoreSession, KnowledgeSlotStatus
from redteam_core.models.base import canonical_json, sha256_text
from redteam_core.models.operational_budget import OperationalBudgetLimits
from redteam_core.models.requirements import EvidenceRequirement, ResolutionFailureKind
from redteam_core.models.runtime import RuntimeBudgetLimits
from redteam_core.models.security import CandidateDisposition
from redteam_core.runtime import FixedClock
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    build_runtime_composition_fingerprint,
)
from redteam_core.runtime.journal import EventJournal
from redteam_core.runtime.orchestrator import RuntimeOrchestrator
from redteam_core.runtime.passive_scheduler import PassiveResolutionScheduler
from redteam_core.runtime.priority import PriorityCoordinator
from redteam_core.runtime.resolver_dispatch import DurableResolverDispatcher
from tests.helpers import REQUIREMENT_TYPE, AlternatePayload, SamplePayload, catalog, observation
from tests.performance.baseline import ROOT, current_core_hash, tree_hash
from tests.unit.test_candidate_coverage import _candidate
from tests.unit.test_priority import NOW


@dataclass(frozen=True)
class RootFixture:
    key: str
    requirements: tuple[str, ...]
    truth: str  # Independent expected root label; never supplied to the provider.
    family: str = "default"


@dataclass(frozen=True)
class Fixture:
    name: str
    roots: tuple[RootFixture, ...]
    tape: tuple[tuple[str, str], ...]
    initial: tuple[tuple[str, tuple[str, ...]], ...] = ()


FIXTURES = (
    Fixture(
        "shared-evidence",
        (
            RootFixture("r0", ("shared",), "PASS", "a"),
            RootFixture("r1", ("shared",), "PASS", "b"),
            RootFixture("r2", ("long0", "long1"), "VIOLATION", "c"),
            RootFixture("r3", ("solo",), "PASS", "a"),
        ),
        (("shared", "good"), ("long0", "good"), ("long1", "bad"), ("solo", "good")),
    ),
    Fixture(
        "cached-negative",
        (
            RootFixture("r0", ("cached",), "VIOLATION", "a"),
            RootFixture("r1", ("long0", "long1"), "PASS", "a"),
            RootFixture("r2", ("solo",), "VIOLATION", "b"),
        ),
        (("cached", "bad"), ("long0", "good"), ("long1", "good"), ("solo", "bad")),
        (("cached", ("bad",)),),
    ),
    Fixture(
        "uncertain-response",
        (
            RootFixture("r0", ("failure",), "UNRESOLVED", "a"),
            RootFixture("r1", ("good0",), "PASS", "a"),
            RootFixture("r2", ("good1",), "VIOLATION", "b"),
            RootFixture("r3", ("good2",), "PASS", "b"),
        ),
        (("failure", "ERROR"), ("good0", "good"), ("good1", "bad"), ("good2", "good")),
    ),
    Fixture(
        "conflicting-evidence",
        (
            RootFixture("r0", ("conflict",), "UNKNOWN_CONFLICT", "a"),
            RootFixture("r1", ("good",), "PASS", "a"),
            RootFixture("r2", ("bad",), "VIOLATION", "b"),
        ),
        (("conflict", "good"), ("good", "good"), ("bad", "bad")),
        (("conflict", ("good", "bad")),),
    ),
    Fixture(
        "repeated-invalid-response",
        (
            RootFixture("r0", ("failure",), "UNRESOLVED", "a"),
            RootFixture("r1", ("good0",), "PASS", "a"),
            RootFixture("r2", ("good1",), "VIOLATION", "b"),
            RootFixture("r3", ("good2",), "PASS", "b"),
        ),
        (("failure", "INVALID"), ("good0", "good"), ("good1", "bad"), ("good2", "good")),
    ),
)


class TapeResolver:
    def __init__(self, tape):
        self.tape = dict(tape)
        self.calls = []
        self.descriptor = ResolverDescriptor(
            resolver_id="reference.priority-tape@1",
            provider_id="reference.local",
            supported_requirement_type_ids=frozenset({REQUIREMENT_TYPE}),
            max_calls=64,
            max_tokens_per_call=0,
        )

    async def resolve(self, requirement):
        self.calls.append(requirement.subject_ref)
        value = self.tape[requirement.subject_ref]
        if value == "ERROR":
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                "recorded transport completion unknown",
                retryable=False,
                resolver_id=self.descriptor.resolver_id,
            )
        if value == "INVALID":
            return ResolverOutput(payload=AlternatePayload(count=0), actual_tokens=0)
        return ResolverOutput(payload=SamplePayload(value=value), actual_tokens=0)


def classify(session, candidate):
    """Toy oracle execution rule, separate from ground-truth labels and ranking code."""
    values = []
    for key in candidate.requirement_ids:
        slot = session.knowledge.slots.get(key)
        if slot is None or slot.status is KnowledgeSlotStatus.UNKNOWN:
            return None
        if slot.status is KnowledgeSlotStatus.CONFLICTING:
            return "UNKNOWN_CONFLICT"
        obs = session.investigation.observations[slot.authoritative_observation_ids[0]]
        values.append(json.loads(obs.payload.canonical_payload_json)["value"])
    return "VIOLATION" if "bad" in values else "PASS"


async def run_case(fixture: Fixture, *, budget: int, arm: str, directory: Path):
    cat = catalog()
    session = CoreSession()
    requirements = {
        name: EvidenceRequirement.create(
            requirement_type_id=REQUIREMENT_TYPE,
            provider_id="reference.local",
            subject_ref=name,
            reason="Synthetic observed flag",
        )
        for name, _ in fixture.tape
    }
    session.investigation.requirements = {req.requirement_id: req for req in requirements.values()}
    candidates = tuple(
        _candidate(root.family, root.key).model_copy(
            update={
                "requirement_ids": [requirements[key].requirement_id for key in root.requirements],
            }
        )
        for root in fixture.roots
    )
    reconcile_candidate_state(session, candidates)
    truth = {
        candidate.root_cause.qualified_key: root.truth
        for root, candidate in zip(fixture.roots, candidates, strict=True)
    }
    initial = tuple(
        observation(
            req=requirements[name],
            value=value,
            acquired_at=NOW,
            resolver_id=f"reference.initial{index}@1",
        )
        for name, values in fixture.initial
        for index, value in enumerate(values)
    )
    if initial:
        apply_observations_atomic(
            session, observations=initial, catalog=cat, evaluation_time=NOW, expected_revision=0
        )
    resolver = TapeResolver(fixture.tape)
    registry = ResolverRuntimeRegistry()
    registry.register(resolver)
    registry.freeze()
    providers = {
        "baseline": BaselinePriorityProvider(),
        "utility": ExpectedUtilityPriorityProvider(),
    }
    provider = providers[arm]
    composition = build_runtime_composition_fingerprint(
        catalog=cat,
        resolver_descriptors=registry.descriptors(),
        priority_provider_ids=tuple(item.descriptor.binding_id for item in providers.values()),
    )
    clock = FixedClock(NOW)
    manager = RuntimeOrchestrator(
        cat,
        composition,
        clock,
        EventJournal(directory / "events.jsonl", run_id="priority:benchmark", clock=clock),
        AtomicCheckpointStore(directory / "state.json"),
        RuntimeBudgetLimits(max_requests=max(1, budget)),
    )
    runtime = manager.start()
    DurableResolverDispatcher.initialize(
        session=session,
        runtime=runtime,
        orchestrator=manager,
        limits=OperationalBudgetLimits(max_requests=budget, max_tokens=0),
    )
    dispatcher = DurableResolverDispatcher(
        session=session,
        runtime=runtime,
        orchestrator=manager,
        registry=registry,
        permissions=lambda: frozenset(),
    )
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "synthetic")
    coordinator = PriorityCoordinator(
        session=session,
        runtime=runtime,
        orchestrator=manager,
        registry=registry,
        permissions=lambda: frozenset(),
    )
    decisions, coverage = {}, set()
    first_valid_ms = first_valid_calls = None
    start = time.perf_counter()
    stop = "BATCH_CAP"
    idle = 0
    try:
        for batch in range(32):
            if time.perf_counter() - start > 60:
                raise TimeoutError("priority case exceeded its 60 second watchdog")
            selection = coordinator.select(provider, limit=2)
            if not selection.selected_candidate_ids:
                stop = "NO_ELIGIBLE_ROOT"
                break
            before = len(decisions) + len(resolver.calls)
            by_candidate = {row.facts.candidate_id: row for row in selection.assessments}
            for candidate_id in selection.selected_candidate_ids:
                assessment = by_candidate[candidate_id]
                candidate = session.candidate_state.candidates[candidate_id]
                root = candidate.root_cause.qualified_key
                coverage.add(root)
                label = classify(session, candidate)
                if label is not None:
                    # Re-select after shared/new evidence before recording a bound decision.
                    if (
                        root_evidence_fingerprint(session, (candidate,), now=NOW)
                        != assessment.evidence_hash
                    ):
                        continue
                    disposition = {
                        "PASS": CandidateDisposition.REJECTED,
                        "VIOLATION": CandidateDisposition.ACCEPTED,
                        "UNKNOWN_CONFLICT": CandidateDisposition.BLOCKED_EXTERNAL,
                    }[label]
                    coordinator.record_decision(
                        root_key=root,
                        expected_evidence_hash=assessment.evidence_hash,
                        disposition=disposition,
                        reason="synthetic outcome:" + label,
                    )
                    decisions[root] = label
                    if first_valid_ms is None:
                        first_valid_ms = (time.perf_counter() - start) * 1000
                        first_valid_calls = len(resolver.calls)
                elif runtime.operational_budget.charged_requests < budget:
                    missing = next(
                        key
                        for key in sorted(candidate.requirement_ids)
                        if key not in session.knowledge.slots
                        or session.knowledge.slots[key].status is KnowledgeSlotStatus.UNKNOWN
                    )
                    batch_id = f"benchmark:{batch}:{candidate_id}"
                    scheduler.admit(batch_id=batch_id, requests=((candidate_id, missing),))
                    await scheduler.run(batch_id)
                    scheduler.commit(batch_id, expected_revision=session.knowledge.revision)
            idle = idle + 1 if len(decisions) + len(resolver.calls) == before else 0
            if idle >= 2:
                stop = "NO_ACTIONABLE_PROGRESS"
                break
        assert runtime.operational_budget.charged_requests == len(resolver.calls) <= budget
        correct = sum(label == truth[root] for root, label in decisions.items())
        result = {
            "fixture": fixture.name,
            "arm": arm,
            "request_budget": budget,
            "status": "PASS" if stop != "BATCH_CAP" else "TRUNCATED",
            "stop_reason": stop,
            "correct_root_decisions": correct,
            "incorrect_root_decisions": len(decisions) - correct,
            "ground_truth_root_count": len(truth),
            "unresolved_ground_truth_roots": sum(label == "UNRESOLVED" for label in truth.values()),
            "distinct_root_coverage": len(coverage),
            "solved_requirements": sum(
                slot.status is KnowledgeSlotStatus.CONSISTENT
                for slot in session.knowledge.slots.values()
            ),
            "conflicting_requirements": sum(
                slot.status is KnowledgeSlotStatus.CONFLICTING
                for slot in session.knowledge.slots.values()
            ),
            "resolver_calls": len(resolver.calls),
            "duplicate_calls": len(resolver.calls) - len(set(resolver.calls)),
            "first_valid_outcome_ms": first_valid_ms,
            "first_valid_outcome_calls": first_valid_calls,
            "elapsed_ms": (time.perf_counter() - start) * 1000,
            "outcome_labels": sorted(decisions.values()),
            "call_tape": resolver.calls,
            "provider_binding": provider.descriptor.binding_id,
            "known_failed_dispatches": sum(
                row.status.value == "FAILED" for row in runtime.operational_budget.reservations
            ),
            "uncertain_dispatches": sum(
                row.status.value == "UNCERTAIN" for row in runtime.operational_budget.reservations
            ),
            "max_durable_root_failures": max(
                (len(row.seen_failure_ids) for row in runtime.priority.roots.values()), default=0
            ),
        }
        return result
    finally:
        await scheduler.close()
        manager.close()


async def run(*, budgets=(3, 6), fixtures=FIXTURES):
    source = current_core_hash()
    harness_paths = (
        "tests/performance/g8_priority_benchmark.py",
        "tests/helpers.py",
        "tests/unit/test_candidate_coverage.py",
        "tests/unit/test_priority.py",
    )

    def harness_hash():
        return tree_hash([(name, (ROOT / name).read_bytes()) for name in harness_paths])

    harness = harness_hash()
    results = []
    with TemporaryDirectory(prefix="generic-priority-benchmark-") as temporary:
        root = Path(temporary)
        for index, fixture in enumerate(fixtures):
            for budget in budgets:
                for arm in ("baseline", "utility") if index % 2 == 0 else ("utility", "baseline"):
                    directory = root / f"{index}-{budget}-{arm}"
                    directory.mkdir()
                    results.append(
                        await run_case(fixture, budget=budget, arm=arm, directory=directory)
                    )
    if current_core_hash() != source or harness_hash() != harness:
        raise RuntimeError("source or harness changed during benchmark")
    regressions = []
    for fixture in fixtures:
        for budget in budgets:
            pair = {
                row["arm"]: row
                for row in results
                if row["fixture"] == fixture.name and row["request_budget"] == budget
            }
            for metric in ("correct_root_decisions", "distinct_root_coverage"):
                if pair["utility"][metric] < pair["baseline"][metric]:
                    regressions.append(
                        {"fixture": fixture.name, "budget": budget, "metric": metric}
                    )
    return {
        "schema_version": 1,
        "fixture_version": 2,
        "source_hash": source,
        "harness_hash": harness,
        "source_integrity": "PASS",
        "fixture_hash": sha256_text(canonical_json([asdict(item) for item in fixtures])),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "baseline_contract": "legacy score with common G8 admission and terminal-binding controls",
        "timing_claim": "single local descriptive sample per case, no statistical speed claim",
        "real_provider_calls": 0,
        "request_budgets": list(budgets),
        "portfolio_limit": 2,
        "coverage_definition": "distinct roots whose canonical state was inspected after selection",
        "quality_regressions": regressions,
        "default_enable": "WITHHELD_PENDING_G9"
        if not regressions
        else "WITHHELD_QUALITY_REGRESSION",
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("preserve existing benchmark receipts")
    result = asyncio.run(run())
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cases": len(result["results"]), "default_enable": result["default_enable"]}))


if __name__ == "__main__":
    main()
