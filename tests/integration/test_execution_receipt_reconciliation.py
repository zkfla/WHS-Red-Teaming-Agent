"""Lost publication, missing originals, and late provider recovery never invoke again."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.events import EventType
from redteam.core.contracts.execution import (
    ExecutionRecoveryAnswer,
    ExecutionReference,
    ExecutionState,
)
from redteam.core.contracts.tool import (
    EXECUTION_STARTED_METADATA_KEY,
    EXECUTION_UNCERTAIN_METADATA_KEY,
    ToolError,
    ToolResult,
)
from redteam.core.convert import digest_payload
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import EvidenceId, RunId
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.event_bus.memory import InMemoryEventBus
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.execution_journal import (
    execution_processed,
    preserved_result,
    unsettled_execution_receipts,
)
from redteam.orchestration.execution_reconciliation import reconcile_execution_receipts
from redteam.orchestration.graph import build_graph
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.state import initial_graph_state
from redteam.orchestration.work.models import WorkStatus
from tests.fakes import DummyToolGateway
from tests.integration.test_native_react_loop import (
    ScriptedNativeLLM,
    _engagement,
    _native_deps,
    deps_runtime,
)


class CompletionPublicationFailure(InMemoryEventBus):
    def __init__(self, *, before=False):
        super().__init__()
        self.failure_type = (
            EventType.TOOL_EXECUTION_STARTED if before else EventType.TOOL_EXECUTION_FINISHED
        )

    async def publish(self, event):
        if event.event_type is self.failure_type:
            raise RuntimeError("synthetic publication failure")
        await super().publish(event)


class LateOriginalGateway(DummyToolGateway):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.original = None
        self.queries = 0
        self.fetches = 0

    def prepare_reference(self, invocation):
        return ExecutionReference(
            engagement_id=invocation.execution_context.engagement_id,
            run_id=invocation.run_id, instance_id=invocation.execution_context.instance_id,
            action_id=invocation.action_id, invocation_id=invocation.invocation_id,
            tool_id=invocation.tool_id, input_digest=digest_payload(invocation.arguments),
            provider_handle={"saved": str(invocation.invocation_id)},
        )

    async def invoke(self, invocation):
        self.original = await super().invoke(invocation)
        self.original.metadata[EXECUTION_STARTED_METADATA_KEY] = True
        now = datetime.now(timezone.utc)
        return ToolResult(
            invocation_id=invocation.invocation_id, action_id=invocation.action_id,
            tool_id=invocation.tool_id, success=False,
            error=ToolError(code="tool_timeout", message="original not collected yet"),
            started_at=now, finished_at=now,
            metadata={EXECUTION_STARTED_METADATA_KEY: True,
                      EXECUTION_UNCERTAIN_METADATA_KEY: True},
        )

    async def query_execution(self, reference):
        self.queries += 1
        return ExecutionRecoveryAnswer(
            state=ExecutionState.SUCCEEDED, verified=True, uncertain=False, result_available=True,
        )

    async def fetch_execution(self, reference):
        self.fetches += 1
        return self.original


async def _world(tmp_path, *, late=False, before=False):
    engagement = _engagement()
    llm = ScriptedNativeLLM([])
    base = _native_deps(engagement, llm)
    descriptor = base.tool_catalog.all_descriptors()[0].model_copy(update={
        "requires_target": False,
    })
    gateway = (LateOriginalGateway if late else DummyToolGateway)(
        tools=[descriptor], output={"original": "bounded preserved output"},
    )
    llm._native = [NativeCompletion(tool_calls=(NativeToolCall(
        call_id="recover-original", name=exposed_tool_name(str(descriptor.tool_id)), arguments={},
    ),))]
    store = FilesystemEvidenceStore(tmp_path / "evidence")
    deps = replace(
        base, tool_gateway=gateway, tool_catalog=gateway, evidence_store=store,
        execution_recovery=gateway if late else None,
        event_bus=InMemoryEventBus() if late else CompletionPublicationFailure(before=before),
    )
    run_id = RunId("run-receipt-recovery")
    initial = deps_runtime(run_id, engagement)
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement, run_id=run_id)
    final = await build_graph(deps).ainvoke(initial_graph_state(initial))
    assert len(gateway.invocations) == (0 if before else 1)
    work = (await deps.work_store.list_items(run_id=run_id))[0]
    assert work.status is WorkStatus.FAILED
    stale = final["runtime"].model_copy(update={
        "actions_executed": 0, "retry_tool_calls": 0, "actions_by_agent": {},
        "remaining_budget": initial.remaining_budget,
    })
    return replace(deps, event_bus=InMemoryEventBus()), gateway, store, stale, work


async def test_proved_non_entry_settles_without_provider_or_action_charge(tmp_path):
    deps, gateway, store, stale, work = await _world(tmp_path, before=True)
    records = await store.list_for_run(stale.run_id)
    original = next(r for r in records if r.metadata.get("receipt_kind") == "execution_result")
    assert original.metadata["original_result"]["metadata"][EXECUTION_STARTED_METADATA_KEY] is False
    restored = await reconcile_execution_receipts(deps, stale)
    assert restored.ready, restored
    assert restored.runtime.actions_executed == 0
    assert restored.runtime.remaining_budget == stale.remaining_budget
    assert (await deps.work_store.get(work.work_id)).status is WorkStatus.FAILED
    assert gateway.invocations == []


async def test_publication_loss_recovers_original_settles_work_and_counters_once(tmp_path):
    deps, gateway, store, stale, work = await _world(tmp_path)
    restored = await reconcile_execution_receipts(deps, stale)
    assert restored.ready and len(restored.processed_invocations) == 1, restored
    assert restored.runtime.actions_executed == 1
    assert restored.runtime.remaining_budget == stale.remaining_budget - 1
    assert (await deps.work_store.get(work.work_id)).status is WorkStatus.SUCCEEDED
    original = await preserved_result(
        store, run_id=stale.run_id, action_id=gateway.invocations[0].action_id,
        instance_id=stale.instance_id, work_id=work.work_id,
    )
    assert original is not None and original[0].success
    assert await execution_processed(store, await store.get(original[1]))
    assert await unsettled_execution_receipts(store, run_id=stale.run_id) == []
    again = await reconcile_execution_receipts(deps, restored.runtime)
    assert again.ready and again.runtime.actions_executed == 1
    assert again.runtime.remaining_budget == restored.runtime.remaining_budget
    assert len(gateway.invocations) == 1
    records = await store.list_for_run(stale.run_id)
    assert len([r for r in records if r.source_type is EvidenceSourceType.TOOL]) == 1


async def test_missing_original_remains_held_without_invocation_or_budget_reset(tmp_path):
    deps, gateway, store, stale, work = await _world(tmp_path)
    # Simulate a process loss after the submission receipt but before any
    # host-side original was written. Preserve the actual immutable receipt
    # in another real store; do not fabricate a successful replacement.
    missing = FilesystemEvidenceStore(tmp_path / "missing")
    for record in await store.list_for_run(stale.run_id):
        if record.metadata.get("receipt_kind") == "execution_submission":
            await missing.append(record)
    deps = replace(deps, evidence_store=missing)
    held = await reconcile_execution_receipts(deps, stale)
    assert not held.ready and len(held.pending_invocations) == 1
    assert len(await unsettled_execution_receipts(missing, run_id=stale.run_id)) == 1
    assert held.runtime.actions_executed == 1
    again = await reconcile_execution_receipts(deps, held.runtime)
    assert again.runtime.actions_executed == 1
    assert again.runtime.remaining_budget == held.runtime.remaining_budget
    assert (await deps.work_store.get(work.work_id)).status is WorkStatus.FAILED
    assert len(gateway.invocations) == 1


async def test_late_verified_original_resolves_timeout_preserving_old_record(tmp_path):
    deps, gateway, store, stale, work = await _world(tmp_path, late=True)
    before = await store.list_for_run(stale.run_id)
    uncertain = next(r for r in before if r.metadata.get("receipt_kind") == "execution_result")
    original_json = uncertain.model_dump_json()
    recovered = await reconcile_execution_receipts(deps, stale)
    assert recovered.ready, recovered
    assert gateway.queries == gateway.fetches == 1 and len(gateway.invocations) == 1
    assert (await store.get(uncertain.evidence_id)).model_dump_json() == original_json
    assert (await deps.work_store.get(work.work_id)).status is WorkStatus.SUCCEEDED
    assert await unsettled_execution_receipts(store, run_id=stale.run_id) == []
    engine = InvestigationEngine(deps)
    intent = SimpleNamespace(
        target_node_id=None, binding_mode="selected_tool", tool_id=gateway.invocations[0].tool_id,
        arguments={},
    )
    assert not await engine.has_ambiguous_execution(intent, recovered.runtime)
    again = await reconcile_execution_receipts(deps, recovered.runtime)
    assert again.ready and gateway.queries == gateway.fetches == 1
    assert again.runtime.actions_executed == 1


async def test_conflicting_originals_remain_held_and_do_not_overwrite(tmp_path):
    deps, gateway, store, stale, work = await _world(tmp_path)
    records = await store.list_for_run(stale.run_id)
    original = next(r for r in records if r.metadata.get("receipt_kind") == "execution_result")
    conflicting = original.model_copy(deep=True, update={"evidence_id": EvidenceId("conflict")})
    conflicting.metadata["original_result"]["output"] = {"foreign": "conflicting body"}
    conflicting.digest = digest_payload(conflicting.metadata)
    await store.append(conflicting)
    held = await reconcile_execution_receipts(deps, stale)
    assert not held.ready
    assert list(held.errors.values()) == ["conflicting_execution_results"]
    assert (await store.get(original.evidence_id)) == original
    assert (await deps.work_store.get(work.work_id)).status is WorkStatus.FAILED
    assert len(gateway.invocations) == 1


async def test_same_run_other_instance_receipts_are_not_reconciled(tmp_path):
    deps, gateway, store, stale, work = await _world(tmp_path)
    sibling = stale.model_copy(update={"instance_id": "sibling"})
    ignored = await reconcile_execution_receipts(deps, sibling)
    assert ignored.ready and ignored.processed_invocations == []
    assert ignored.runtime.actions_executed == 0
    assert ignored.runtime.remaining_budget == stale.remaining_budget
    assert await unsettled_execution_receipts(
        store, run_id=stale.run_id, instance_id="sibling",
    ) == []
    pending = await unsettled_execution_receipts(store, run_id=stale.run_id, all_instances=True)
    assert len(pending) == 1
    assert (await deps.work_store.get(work.work_id)).status is WorkStatus.FAILED
    assert len(gateway.invocations) == 1


async def test_corrupt_receipt_reports_integrity_failure_without_provider_read(tmp_path):
    deps, gateway, store, stale, work = await _world(tmp_path, late=True)
    records = await store.list_for_run(stale.run_id)
    bad = FilesystemEvidenceStore(tmp_path / "bad")
    for record in records:
        copy = record.model_copy(deep=True)
        if copy.metadata.get("receipt_kind") == "execution_submission":
            copy.metadata["accounting_before"]["remaining_budget"] = 999
        await bad.append(copy)
    held = await reconcile_execution_receipts(replace(deps, evidence_store=bad), stale)
    assert not held.ready and "execution_receipt_digest_mismatch" in held.errors.values()
    assert gateway.queries == gateway.fetches == 0
    assert held.runtime.remaining_budget == stale.remaining_budget
    assert (await deps.work_store.get(work.work_id)).status is WorkStatus.FAILED


async def test_legacy_uncertainty_needs_clean_matching_invocation_resolution(tmp_path):
    deps, gateway, store, stale, _work = await _world(tmp_path, late=True)
    records = await store.list_for_run(stale.run_id)
    original = next(r for r in records if r.metadata.get("receipt_kind") == "execution_result")
    submission = next(
        r for r in records if r.metadata.get("receipt_kind") == "execution_submission"
    )
    legacy = FilesystemEvidenceStore(tmp_path / "legacy")
    metadata = {
        "tool_result": original.metadata["original_result"],
        "proposal": submission.metadata["proposal"], "target_node_id": None,
    }
    uncertain = original.model_copy(update={
        "source_type": EvidenceSourceType.TOOL, "metadata": metadata,
    })
    await legacy.append(uncertain)
    engine = InvestigationEngine(replace(deps, evidence_store=legacy))
    intent = SimpleNamespace(
        target_node_id=None, binding_mode="selected_tool",
        tool_id=gateway.invocations[0].tool_id, arguments={},
    )
    assert await engine.has_ambiguous_execution(intent, stale)
    for index, (invocation_id, instance_id, remains_uncertain) in enumerate([
        ("other-invocation", stale.instance_id, False),
        (original.invocation_id, "foreign-instance", False),
        (original.invocation_id, stale.instance_id, True),
        (original.invocation_id, stale.instance_id, False),
    ]):
        raw = {**metadata["tool_result"], "metadata": {
            "execution_reconciled": True, "execution_started": True,
            "execution_uncertain": remains_uncertain,
        }}
        sibling_meta = {**metadata, "tool_result": raw, "proposal": {
            **metadata["proposal"], "instance_id": instance_id,
        }}
        await legacy.append(uncertain.model_copy(update={
            "evidence_id": EvidenceId(f"resolution-{index}"),
            "invocation_id": invocation_id, "metadata": sibling_meta,
        }))
        assert await engine.has_ambiguous_execution(intent, stale) is (index < 3)
    assert len(gateway.invocations) == 1
