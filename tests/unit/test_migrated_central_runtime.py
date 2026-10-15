"""Explicit zero-entry adapter only; entered legacy execution is preserved/refused."""

from types import SimpleNamespace

import pytest

from redteam.app.legacy_migration import (
    migrated_runtime_guard,
    prepare_migrated_runtime,
)
from redteam.core.contracts.engagement import Engagement
from redteam.orchestration.component_binding import RuntimeComponentBinding
from redteam.orchestration.coordination import CoordinationError, StateConflict, content_hash
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.specialist_planner import specialist_planning_inputs
from redteam.orchestration.storage_snapshot import source_manifest
from tests.unit.test_legacy_migration import fixture, migrate_legacy_storage


@pytest.mark.parametrize("entered", [False, True])
async def test_explicit_adapter_preserves_originals_and_refuses_entered_sdk(tmp_path, entered):
    request, state, evidence, approvals, source, _, validation = await fixture(
        tmp_path, entered=entered, execution=entered,
    )
    try:
        await migrate_legacy_storage(
            request, evidence_store=evidence, approval_store=approvals,
            audit_store=validation["audit_store"],
        )
        inputs = validation["current_inputs"]
        binding = RuntimeComponentBinding.model_validate(inputs["components"])
        binding = binding.model_copy(update={"extension_versions": {
            "central-runtime": "1", "central-selection-inputs": "a" * 64,
        }})
        original_engagement = Engagement.model_validate({
            **inputs["engagement"], "created_at": "2026-01-01T00:00:00Z",
        })
        policy = SimpleNamespace(max_instances=2, probe_limits=None,
                                 model_dump=lambda **_: inputs["specialists"])
        state.original_store_paths = {
            "evidence": str(request.references["evidence"]),
            "audit": str(request.references["audit"]),
            "approvals": str(request.references["approval"]),
            "checkpoints": str(tmp_path / "native-checkpoints.sqlite3"),
        }
        deps = SimpleNamespace(
            central_state=state, component_binding=binding, specialist_policy=policy,
            settings=OrchestrationSettings(llm_request_policy=validation["current_policy"],
                                           checkpoint_path=state.original_store_paths["checkpoints"]),
            evidence_store=evidence, engagement=original_engagement,
        )
        before = await state.list_tasks()
        usage = await state.request_ledger().snapshot(state.run_id)
        if entered:
            with pytest.raises(StateConflict, match="requires_runtime_adapter"):
                await prepare_migrated_runtime(deps)
            assert (await state.list_executions())[0].job_ref == "job-original"
        else:
            await prepare_migrated_runtime(deps)
            assert await state.list_tasks() == before
            async with migrated_runtime_guard(deps):
                assert await state.transaction(lambda tx: tx.admit())
                assert await state.transaction(lambda tx: tx.binding()[1], write=False) == \
                    content_hash(specialist_planning_inputs(deps))
            with pytest.raises(CoordinationError, match="migration_resume_guard_required"):
                await state.transaction(lambda tx: tx.admit())
            activation, = await state.activation_inventory()
            assert activation["instance_id"] == "instance-a"
        assert await state.request_ledger().snapshot(state.run_id) == usage
        assert not (tmp_path / "native-checkpoints.sqlite3").exists()
    finally:
        source.close()


def _deps(tmp_path, state, request, evidence, validation, *, probe_limits=None, specialists=None):
    """Current assembly: original identity plus central extension versions only."""
    inputs = validation["current_inputs"]
    binding = RuntimeComponentBinding.model_validate(inputs["components"]).model_copy(
        update={"extension_versions": {
            "central-runtime": "1", "central-selection-inputs": "a" * 64,
        }},
    )
    engagement = Engagement.model_validate({
        **inputs["engagement"], "created_at": "2026-01-01T00:00:00Z",
    })
    specialist_doc = inputs["specialists"] if specialists is None else specialists
    policy = SimpleNamespace(max_instances=2, probe_limits=probe_limits,
                             model_dump=lambda **_: specialist_doc)
    state.original_store_paths = {
        "evidence": str(request.references["evidence"]),
        "audit": str(request.references["audit"]),
        "approvals": str(request.references["approval"]),
        "checkpoints": str(tmp_path / "native-checkpoints.sqlite3"),
    }
    return SimpleNamespace(
        central_state=state, component_binding=binding, specialist_policy=policy,
        settings=OrchestrationSettings(
            llm_request_policy=validation["current_policy"],
            checkpoint_path=state.original_store_paths["checkpoints"],
        ),
        evidence_store=evidence, engagement=engagement,
    )


async def test_zero_entry_reentry_keeps_manifest_and_bound_hash(tmp_path):
    request, state, evidence, approvals, source, _, validation = await fixture(tmp_path)
    try:
        await migrate_legacy_storage(
            request, evidence_store=evidence, approval_store=approvals,
            audit_store=validation["audit_store"],
        )
        manifest = source_manifest(dict(request.databases), dict(request.references))
        deps = _deps(tmp_path, state, request, evidence, validation)
        assert specialist_planning_inputs(deps)["schema_version"] == 4
        before = await state.list_tasks()
        await prepare_migrated_runtime(deps)
        await prepare_migrated_runtime(deps)
        assert await state.list_tasks() == before
        assert source_manifest(dict(request.databases), dict(request.references)) == manifest
        async with migrated_runtime_guard(deps):
            assert await state.transaction(lambda tx: tx.binding()[1], write=False) == \
                content_hash(specialist_planning_inputs(deps))
    finally:
        source.close()


async def test_candidate_policy_identity_is_refused(tmp_path):
    request, state, evidence, approvals, source, _, validation = await fixture(tmp_path)
    try:
        await migrate_legacy_storage(
            request, evidence_store=evidence, approval_store=approvals,
            audit_store=validation["audit_store"],
        )
        deps = _deps(tmp_path, state, request, evidence, validation,
                     probe_limits=SimpleNamespace(max_probes=1))
        assert specialist_planning_inputs(deps)["schema_version"] == 3
        with pytest.raises(StateConflict, match="migration_current_binding_changed"):
            await prepare_migrated_runtime(deps)
    finally:
        source.close()


async def test_changed_specialists_are_refused(tmp_path):
    request, state, evidence, approvals, source, _, validation = await fixture(tmp_path)
    try:
        await migrate_legacy_storage(
            request, evidence_store=evidence, approval_store=approvals,
            audit_store=validation["audit_store"],
        )
        deps = _deps(tmp_path, state, request, evidence, validation,
                     specialists={"test": False})
        with pytest.raises(StateConflict, match="migration_current_binding_changed"):
            await prepare_migrated_runtime(deps)
    finally:
        source.close()


async def test_current_planner_identity_original_is_refused_at_import(tmp_path):
    # Migration accepts only schema 2 originals; a schema 4 original is recorded as refused.
    request, state, evidence, approvals, source, _, validation = await fixture(
        tmp_path, planning_schema_version=4,
    )
    try:
        await migrate_legacy_storage(
            request, evidence_store=evidence, approval_store=approvals,
            audit_store=validation["audit_store"],
        )
        deps = _deps(tmp_path, state, request, evidence, validation)
        with pytest.raises(
            StateConflict,
            match="migration_run_resume_refused:original_inputs_components_or_policy_unverified",
        ):
            await prepare_migrated_runtime(deps)
    finally:
        source.close()
