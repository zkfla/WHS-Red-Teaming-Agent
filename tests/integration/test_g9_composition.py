"""G9 composed functional ablation; not a speed or priority-quality benchmark.

Every arm uses the same response/clock tape and dispatches the same two requests.
Priority selects the context focus, not the response tape: its fixed-budget
quality tradeoff is independently measured by g8_priority_benchmark.
"""

import asyncio
import json
from dataclasses import replace
from itertools import product

import pytest

from redteam_core.extension import RelationTypeRegistration, compose_bundles
from redteam_core.graph.frontier import build_attack_frontiers
from redteam_core.graph.semantics import RelationSemantics
from redteam_core.investigation.coverage import reconcile_candidate_state
from redteam_core.investigation.priority import (
    BaselinePriorityProvider,
    ExpectedUtilityPriorityProvider,
)
from redteam_core.knowledge import apply_observations_atomic, refresh_freshness
from redteam_core.knowledge.recompute import effective_payload_envelope
from redteam_core.models import Relation, TriState
from redteam_core.models.base import canonical_json, sha256_text
from redteam_core.models.operational_budget import OperationalBudgetLimits
from redteam_core.models.runtime import RuntimeBudgetLimits
from redteam_core.reasoning import (
    ContextProjectionPolicy,
    FocusedContextPolicy,
    project_decision_context,
    project_focused_decision_context,
)
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
from tests.helpers import FP_A, reference_bundle
from tests.integration.test_durable_resolver_dispatch import FakeResolver, registry
from tests.performance.workloads import NOW, knowledge_input
from tests.unit.test_candidate_coverage import _candidate

CONFIGS = tuple(product((False, True), repeat=4))


def fixture():
    session, _, tape = knowledge_input(40)
    semantics = {
        "source": RelationSemantics(traversable=True, source_tags=("input",)),
        "sink": RelationSemantics(traversable=True, sink_tags=("effect",)),
    }
    bundle = reference_bundle()
    cat = compose_bundles(
        (
            replace(
                bundle,
                relation_registrations=(
                    *bundle.relation_registrations,
                    *(
                        RelationTypeRegistration(
                            type_id=f"reference.relation.{role}@1",
                            schema_version="1",
                            owner_bundle_id="reference",
                            implementation_fingerprint=FP_A,
                            semantics=value,
                        )
                        for role, value in semantics.items()
                    ),
                ),
            ),
        )
    )
    sources = tuple(
        Relation.create(
            source_ref=f"source:{i}",
            target_ref="shared",
            verdict=TriState.FALSE,
            relation_type_id="reference.relation.source@1",
        )
        for i in range(4)
    )
    sink = Relation.create(
        source_ref="shared",
        target_ref="sink",
        verdict=TriState.TRUE,
        relation_type_id="reference.relation.sink@1",
    )
    session.semantic.relations = {row.relation_id: row for row in (*sources, sink)}
    requirements = sorted(session.investigation.requirements)
    candidates = tuple(
        _candidate(
            f"family{i % 2}",
            f"root{i}",
            relation_ids=(sources[i].relation_id,),
        ).model_copy(update={"requirement_ids": [requirements[i]]})
        for i in range(4)
    )
    reconcile_candidate_state(session, candidates)
    # Two known roots and two genuinely unresolved roots, shared graph suffix.
    initial = tuple(row for row in tape if row.requirement_id in requirements[:2])
    return session, cat, initial, requirements[2:]


def projection(session, cat, selected, *, indexed, memo, focused):
    before = session.model_dump(mode="json")
    frontier = build_attack_frontiers(session, catalog=cat, memoize_sink=memo)
    assert frontier.search_complete and len(frontier.frontiers) == 4
    assert all(row.potential_sink_ref == "sink" for row in frontier.frontiers)
    payloads = tuple(
        effective_payload_envelope(
            session,
            key,
            lookup_mode="indexed" if indexed else "full_scan",
        )
        for key in sorted(session.investigation.requirements)
    )
    if focused:
        context = project_focused_decision_context(
            session,
            active_candidate_ids=selected,
            policy=FocusedContextPolicy(max_characters=100_000, max_utf8_bytes=400_000),
        )
        assert context.complete_bundles == len(selected) and context.incomplete_bundles == 0
        bundles = json.loads(context.content)["channels"]["CANONICAL_STATE"]["candidate_bundles"]
        assert {row["candidate"]["candidate_id"] for row in bundles} == set(selected)
    else:
        context = project_decision_context(
            session,
            active_portfolio_candidate_ids=selected,
            policy=ContextProjectionPolicy(max_characters=100_000, max_observations=100),
        )
        assert not context.compacted
    for key in selected:
        candidate = session.candidate_state.candidates[key]
        assert key in context.content
        assert all(
            ref in context.content
            for ref in (
                *candidate.supporting_relation_ids,
                *candidate.requirement_ids,
            )
        )
    assert session.model_dump(mode="json") == before
    return {
        "frontier": frontier.model_dump(mode="json"),
        "payloads": [row.model_dump(mode="json") if row else None for row in payloads],
        "context": context.content,
    }


async def run_arm(directory, config, concurrency):
    indexed, memo, focused, utility = config
    session, cat, initial, unresolved = fixture()
    mode = "indexed" if indexed else "full_scan"
    apply_observations_atomic(
        session,
        initial,
        catalog=cat,
        evaluation_time=NOW,
        expected_revision=0,
        lookup_mode=mode,
    )
    resolver = FakeResolver(max_calls=2)
    original = resolver.resolve

    async def reversed_tape(requirement):
        # Completion order differs in concurrent mode; semantic clock is unchanged.
        if requirement.requirement_id == unresolved[0]:
            await asyncio.sleep(0.01)
        return await original(requirement)

    resolver.resolve = reversed_tape
    reg = registry(resolver)
    providers = BaselinePriorityProvider(), ExpectedUtilityPriorityProvider()
    composition = build_runtime_composition_fingerprint(
        catalog=cat,
        resolver_descriptors=reg.descriptors(),
        priority_provider_ids=tuple(row.descriptor.binding_id for row in providers),
        semantic_pipeline_hash="sha256:" + sha256_text(canonical_json(list(config))),
    )
    clock = FixedClock(NOW)

    def manager():
        return RuntimeOrchestrator(
            cat,
            composition,
            clock,
            EventJournal(directory / "events.jsonl", run_id="g9:composition", clock=clock),
            AtomicCheckpointStore(directory / "state.json"),
            RuntimeBudgetLimits(max_requests=2),
        )

    orchestrator = manager()
    runtime = orchestrator.start()
    DurableResolverDispatcher.initialize(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        limits=OperationalBudgetLimits(max_requests=2, max_tokens=0, max_concurrent=2),
    )

    def priority():
        return PriorityCoordinator(
            session=session,
            runtime=runtime,
            orchestrator=orchestrator,
            registry=reg,
            permissions=lambda: frozenset({"read"}),
        )

    def scheduler():
        return PassiveResolutionScheduler(
            DurableResolverDispatcher(
                session=session,
                runtime=runtime,
                orchestrator=orchestrator,
                registry=reg,
                permissions=lambda: frozenset({"read"}),
            ),
            auth_context=lambda: "fixed-principal",
        )

    active_scheduler = scheduler()
    try:
        selected = priority().select(providers[int(utility)], limit=2).selected_candidate_ids
        kwargs = dict(indexed=indexed, memo=memo, focused=focused)
        cold = projection(session, cat, selected, **kwargs)
        assert projection(session, cat, selected, **kwargs) == cold  # warm, no durable cache
        active_scheduler.admit(
            batch_id="fixed",
            requests=tuple((key, key) for key in unresolved),
            concurrency=concurrency,
            per_provider_concurrency=concurrency,
        )
        await active_scheduler.run("fixed")
        active_scheduler.commit("fixed", expected_revision=session.knowledge.revision)
        refresh_freshness(
            session,
            evaluation_time=NOW,
            expected_revision=session.knowledge.revision,
            lookup_mode=mode,
        )
        orchestrator.checkpoint(session, runtime, reason="g9-after-refresh")
        final = projection(session, cat, selected, **kwargs)
        canonical = session.model_dump(mode="json")
        saved_priority = runtime.priority.model_dump(mode="json")
        assert len(session.investigation.observations) == 22
        assert len(session.knowledge.slots) == 4
        assert all(row.status.value == "CONSISTENT" for row in session.knowledge.slots.values())
        assert len(selected) == 2
        assert resolver.calls == runtime.usage.requests == 2
        assert runtime.operational_budget.charged_requests == 2
        assert len(runtime.resolution_receipts) == 2
        await active_scheduler.close()
        orchestrator.close()
        # New manager, registry and adapter, not merely a same-object serialization check.
        orchestrator = manager()
        resumed = orchestrator.resume(expected_run_id="g9:composition")
        session, runtime = resumed.session, resumed.runtime_state
        resolver = FakeResolver(max_calls=2)
        reg = registry(resolver)
        active_scheduler = scheduler()
        await active_scheduler.run("fixed")
        active_scheduler.commit("fixed", expected_revision=session.knowledge.revision)
        assert resolver.calls == 0 and runtime.usage.requests == 2
        assert session.model_dump(mode="json") == canonical
        assert runtime.priority.model_dump(mode="json") == saved_priority
        assert projection(session, cat, selected, **kwargs) == final
        return {
            "canonical": canonical,
            "frontier": final["frontier"],
            "payloads": final["payloads"],
            "selected": selected,
            "priority": saved_priority,
            "requests": runtime.usage.requests,
        }
    finally:
        await active_scheduler.close()
        orchestrator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("config", CONFIGS)
async def test_all_feature_combinations_preserve_cold_warm_resume_and_serial_parity(
    tmp_path, config
):
    outputs = []
    for concurrency in (1, 2):
        directory = tmp_path / str(concurrency)
        directory.mkdir()
        outputs.append(await run_arm(directory, config, concurrency))
    # Full canonical objects, including IDs, timestamps and provenance; no normalization.
    assert outputs[0] == outputs[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("utility", [False, True])
async def test_structural_switches_preserve_policy_specific_selection_and_canonical_state(
    tmp_path,
    utility,
):
    expected = None
    for index, switches in enumerate(product((False, True), repeat=3)):
        directory = tmp_path / str(index)
        directory.mkdir()
        result = await run_arm(directory, (*switches, utility), 2)
        # Composition-bound priority input hash intentionally differs across configurations.
        result.pop("priority")
        if expected is None:
            expected = result
        assert result == expected
