"""Explicit offline composition for original stores; no runtime/default switch."""

from contextlib import asynccontextmanager
from pathlib import Path

from redteam.control.snapshot import ControlSnapshotInspector
from redteam.orchestration import legacy_migration as migration
from redteam.orchestration.legacy_migration import MigrationRequest as MigrationRequest


async def migrate_legacy_storage(request, *, evidence_store=None, approval_store=None,
                                  audit_store=None):
    return await migration.migrate_legacy_storage(
        request, evidence_store=evidence_store,
        control=ControlSnapshotInspector(audit_store, approval_store),
    )


async def validate_migrated_run(state, *, approval_store=None, audit_store=None, **validation):
    return await migration.validate_migrated_run(
        state, control=ControlSnapshotInspector(audit_store, approval_store), **validation,
    )


@asynccontextmanager
async def migrated_run_guard(state, *, approval_store=None, audit_store=None, **validation):
    async with migration.migrated_run_guard(
        state, control=ControlSnapshotInspector(audit_store, approval_store), **validation,
    ) as identity:
        yield identity


def original_planning_inputs(deps):
    """Historical source identity: remove central additions, map legacy schema back.

    Only the legacy investigation planner (schema 4, no probe limits) maps to the
    schema 2 identity the offline import accepts. Other schemas keep their value and
    fail the exact hash comparison.
    """
    from redteam.orchestration.component_binding import RuntimeComponentBinding
    from redteam.orchestration.specialist_planner import specialist_planning_inputs

    inputs = specialist_planning_inputs(deps)
    binding = RuntimeComponentBinding.model_validate(inputs["components"])
    versions = {key: value for key, value in binding.extension_versions.items()
                if key not in {"central-runtime", "central-selection-inputs"}}
    inputs["components"] = binding.model_copy(update={
        "extension_versions": versions,
    }).model_dump(mode="json")
    if inputs["schema_version"] == 4 and deps.specialist_policy.probe_limits is None:
        inputs["schema_version"] = 2
        # This key is part of current central execution identity, but schema-2
        # source Runs predate Worker conversations.  The zero-entry adapter
        # compares the exact frozen source identity before rebinding the current
        # schema-4 identity, so do not project a new field into that old hash.
        inputs.pop("worker_conversation_version", None)
    return inputs


@asynccontextmanager
async def migrated_runtime_guard(deps):
    """Native central resumes hold plan1c source ownership and original checks."""
    state = deps.central_state
    migrated = await state.transaction(lambda tx: tx.db.execute(
        "SELECT 1 FROM state_migrated_runs WHERE run_id=?", (tx.run_id,),
    ).fetchone() is not None, write=False)
    if not migrated:
        yield
        return
    from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
    from redteam.orchestration.coordination import StateConflict
    inputs = original_planning_inputs(deps)
    def check(binding):
        if binding.model_dump(mode="json") != inputs["components"]:
            raise StateConflict("migration_current_components_changed")
    async with migration.migrated_run_guard(
        state, current_inputs=inputs, current_policy=deps.settings.llm_request_policy,
        component_check=check, evidence_store=deps.evidence_store,
        control=ControlSnapshotInspector(
            FilesystemAuditStore(state.original_store_paths["audit"], create=False),
            FilesystemApprovalStore(state.original_store_paths["approvals"], create=False),
        ),
    ) as identity:
        yield identity


async def prepare_migrated_runtime(deps):
    """Explicit adapter for validated zero-entry Runs. Never migrates source DBs.

    Call after plan1c's offline import, before native resume. Entered legacy SDK
    actors/executions need their own verified adapter and are refused here.
    This converts only a binding/launch proof; identities, results, usage and
    the goal are preserved. Original sources stay under the migration fence.
    """
    from redteam.app.central_run import bind_run_metadata
    from redteam.orchestration.coordination import StateConflict, content_hash
    from redteam.orchestration.specialist_planner import specialist_planning_inputs

    async with migrated_runtime_guard(deps) as identity:
        if identity is None:
            raise StateConflict("migration_run_missing")
        # No SDK checkpointer/provider/LLM has been opened by this operation.
        def convert(tx):
            tx.admit()
            inventory = tx.db.execute(
                "SELECT launch_stage,proof_version FROM state_activations WHERE run_id=?",
                (tx.run_id,),
            ).fetchall()
            if any(stage != "reserved" or version != 1 for stage, version in inventory):
                raise StateConflict("migration_entered_checkpoint_requires_runtime_adapter")
            if tx.db.execute("SELECT 1 FROM state_executions WHERE run_id=? LIMIT 1",
                             (tx.run_id,)).fetchone():
                raise StateConflict("migration_original_execution_requires_runtime_adapter")
            manifest = tx.db.execute(
                "SELECT m.manifest FROM state_migrations m JOIN state_migrated_runs r "
                "ON m.migration_id=r.migration_id WHERE r.run_id=?", (tx.run_id,),
            ).fetchone()
            import json
            source_paths = {Path(path).resolve() for path in
                            json.loads(manifest[0])["databases"].values()}
            if Path(deps.settings.checkpoint_path).resolve() in source_paths:
                raise StateConflict("migration_native_checkpoint_must_not_write_frozen_source")
            current_hash = content_hash(specialist_planning_inputs(deps))
            old_hash = content_hash(original_planning_inputs(deps))
            _, bound = tx.binding()
            if bound not in {old_hash, current_hash}:
                raise StateConflict("migration_current_binding_changed")
            tx.db.execute("UPDATE state_runs SET binding_hash=? WHERE run_id=?",
                          (current_hash, tx.run_id))
            tx.db.execute("UPDATE state_activations SET binding_hash=? WHERE run_id=?",
                          (current_hash, tx.run_id))
        await bind_run_metadata(deps, before_bind=convert)
