"""Explicit original-only CLI, with synthetic providers and real durable stores."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from redteam.app.bootstrap import build_application
from redteam.app.cli import _build_parser
from redteam.app.cli.commands import collect_originals as command
from redteam.capability.settings import MCPConfigurationError
from redteam.core.config import AppConfig, EngagementConfig
from redteam.core.contracts.execution import ExecutionRecoveryAnswer, ExecutionState
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.planning_admission import PlanningAdmission
from redteam.orchestration.task_activation import (
    read_activation_collection_identity,
    read_activation_identity,
)
from tests.integration.test_checkpointless_original_collection import collection_world
from tests.unit.test_activation_identity import reservation


async def cli_world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    world = await collection_world(tmp_path, True)
    deps, activation, pending, peer, provider, control, gateway, recovery = world
    config = AppConfig(engagement=EngagementConfig.model_validate(
        deps.engagement.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    monkeypatch.setattr(command, "load_app_config_for_run", lambda _: config)
    monkeypatch.setattr(command.OrchestrationSettings, "for_durable_run", lambda *a, **k:
                        deps.settings)
    build = AsyncMock(return_value=(deps.engagement, deps))
    monkeypatch.setattr(command, "build_resume_application", build)
    args = SimpleNamespace(run_id=str(peer.run_id), engagement_path="synthetic.yaml", json=True)
    return world, args, build


def test_collection_command_is_registered_without_resume_overrides():
    args = _build_parser().parse_args(["collect-originals", "run_1", "engagement.yaml", "--json"])
    assert args.handler is command.run and args.json
    assert not hasattr(args, "max_steps") and not hasattr(args, "instance")


async def test_public_cli_collects_original_once_without_settlement_or_dispatch(
    tmp_path, monkeypatch, capsys,
):
    world, args, build = await cli_world(tmp_path, monkeypatch)
    deps, activation, pending, peer, provider, control, gateway, recovery = world
    ledger = RunRequestLedger(activation.coordinator)
    before = await ledger.snapshot(args.run_id)
    task = await deps.task_graph_store.get_task(peer.active_task_id)
    requests = len(provider.requests)
    assert await command.run(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["outcome"] == "collected"
    assert not output["replay_permitted"] and not output["claim_release_permitted"]
    assert output["instances"][0]["instance_id"] == str(peer.instance_id)
    assert output["instances"][0]["originals"][0]["state"] == "known"
    assert "provider_handle" not in json.dumps(output) and "output" not in json.dumps(output)
    assert build.call_args.kwargs["engagement_id"] == peer.engagement_id
    assert build.call_args.kwargs["settings"] == deps.settings
    assert await command.run(args) == 0
    assert json.loads(capsys.readouterr().out) == output
    assert recovery.queries == recovery.fetches == 1
    assert await ledger.snapshot(args.run_id) == before
    assert await deps.task_graph_store.get_task(peer.active_task_id) == task
    assert len(provider.requests) == requests and not gateway.invocations and not control.restored
    with pytest.raises(StateConflict, match="activation_checkpoint_required"):
        await activation.unstarted(args.run_id, checkpoint_instances={str(pending.instance_id)})


@pytest.mark.parametrize("condition", ["provider_missing", "running", "empty", "invalid"])
async def test_collection_not_available_is_held_not_green_or_replayed(
    tmp_path, monkeypatch, capsys, condition,
):
    world, args, build = await cli_world(tmp_path, monkeypatch)
    deps, activation, pending, peer, provider, control, gateway, recovery = world
    if condition == "provider_missing":
        build.return_value = (deps.engagement, replace(deps, execution_recovery=None))
    elif condition == "running":
        recovery.query_execution = AsyncMock(return_value=ExecutionRecoveryAnswer(
            state=ExecutionState.RUNNING, verified=True, uncertain=False,
        ))
    elif condition == "empty":
        evidence = deps.evidence_store
        original_list = evidence.list_for_run
        async def without_peer(run):
            return [record for record in await original_list(run)
                    if record.metadata.get("instance_id") != str(peer.instance_id)]
        monkeypatch.setattr(evidence, "list_for_run", without_peer)
    else:
        recovery.prepare_reference = lambda invocation: None
    requests = len(provider.requests)
    assert await command.run(args) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["outcome"] == "held" and not output["replay_permitted"]
    originals = output["instances"][0]["originals"]
    if condition == "empty":
        assert originals == []
    else:
        assert originals[0]["state"] != "known"
    assert len(provider.requests) == requests and not gateway.invocations and not control.restored
    task = await deps.task_graph_store.get_task(peer.active_task_id)
    assert task.occupant_owner == peer.work_owner


@pytest.mark.parametrize("condition", [
    "profile", "policy", "database", "legacy", "runtime", "claim", "peer_components",
    "checkpoint_identity", "checkpoint_unreadable", "no_actor",
])
async def test_constructor_free_preflight_refuses_without_build_or_provider(
    tmp_path, monkeypatch, capsys, condition,
):
    world, args, build = await cli_world(tmp_path, monkeypatch)
    deps, activation, pending, peer, _provider, _control, _gateway, recovery = world
    if condition in {"profile", "policy", "database"}:
        updates = {"profile": {"specialist_profile_path": None},
                   "policy": {"llm_request_policy": deps.settings.llm_request_policy.model_copy(
                       update={"max_requests": 99},
                   )}, "database": {"task_graph_path": str(tmp_path / "missing.sqlite")}}[condition]
        monkeypatch.setattr(command.OrchestrationSettings, "for_durable_run", lambda *a, **k:
                            deps.settings.model_copy(update=updates))
    elif condition in {"legacy", "runtime", "claim", "peer_components", "no_actor"}:
        async def mutate(tx):
            row = tx.records[("task_activation", f"{peer.run_id}:{peer.active_task_id}")]
            if condition == "legacy":
                row.pop("launch_proof_version")
            elif condition == "runtime":
                row["runtime"]["actions_executed"] = 99
            elif condition == "no_actor":
                row["launch_stage"] = "reserved"
            elif condition == "claim":
                task = await tx.tasks.get_task(peer.active_task_id)
                await tx.tasks.update_task(task.model_copy(update={"occupant_owner": "foreign"}))
            else:
                row = tx.records[("task_activation", f"{pending.run_id}:{pending.active_task_id}")]
                row["runtime"]["component_binding"]["specialist_fingerprint"] = "foreign"
                row["runtime_digest"] = content_hash(row["runtime"])
        await activation.coordinator.change(mutate)
    elif condition == "checkpoint_unreadable":
        monkeypatch.setattr(command, "list_checkpoint_threads", AsyncMock(
            side_effect=OSError("SECRET unreadable payload"),
        ))
    else:
        original = command.list_checkpoint_threads
        async def changed_threads(**kwargs):
            return [replace(thread, mission_id="foreign") for thread in await original(**kwargs)]
        monkeypatch.setattr(command, "list_checkpoint_threads", changed_threads)
    assert await command.run(args) == 1
    assert not build.await_count and recovery.queries == recovery.fetches == 0
    assert "SECRET" not in capsys.readouterr().err


@pytest.mark.parametrize("condition", ["after_hint", "scope", "ownership", "pending_write"])
async def test_owned_preflight_rechecks_before_collection(tmp_path, monkeypatch, condition):
    world, args, build = await cli_world(tmp_path, monkeypatch)
    deps, activation, pending, peer, provider, control, gateway, recovery = world
    requests = len(provider.requests)
    if condition == "ownership":
        async with InstanceDriver(deps) as owner:
            await owner.own_run(args.run_id)
            assert await command.run(args) == 1
    else:
        if condition == "scope":
            build.return_value = (deps.engagement, replace(deps, engagement=
                deps.engagement.model_copy(update={"name": "different policy input"})))
        else:
            async def changed_build(*a, **kw):
                if condition == "after_hint":
                    async def mutate(tx):
                        key = ("task_activation", f"{peer.run_id}:{peer.active_task_id}")
                        row = tx.records[key]
                        row["state"] = "held"
                    await activation.coordinator.change(mutate)
                else:
                    await PlanningAdmission(activation.coordinator).begin_reference_update(
                        args.run_id, ["graph:x"],
                    )
                return deps.engagement, deps
            build.side_effect = changed_build
        assert await command.run(args) == 1
    assert recovery.queries == recovery.fetches == 0
    assert len(provider.requests) == requests and not gateway.invocations and not control.restored


async def test_cancellation_releases_driver_ownership_not_task_claim(tmp_path, monkeypatch):
    world, args, _build = await cli_world(tmp_path, monkeypatch)
    deps, activation, pending, peer, provider, control, gateway, recovery = world
    recovery.query_execution = AsyncMock(side_effect=asyncio.CancelledError)
    before = await RunRequestLedger(activation.coordinator).snapshot(args.run_id)
    with pytest.raises(asyncio.CancelledError):
        await command.run(args)
    assert await RunRequestLedger(activation.coordinator).snapshot(args.run_id) == before
    task = await deps.task_graph_store.get_task(peer.active_task_id)
    assert task.occupant_owner == peer.work_owner
    async with InstanceDriver(deps) as new_owner:
        await new_owner.own_run(args.run_id)  # teardown did not strand exclusive ownership
    assert not gateway.invocations and not control.restored


@pytest.mark.parametrize("error,code", [(MCPConfigurationError, 2), (RuntimeError, 1)])
async def test_bootstrap_errors_never_print_sensitive_exception_payload(
    tmp_path, monkeypatch, capsys, error, code,
):
    world, args, build = await cli_world(tmp_path, monkeypatch)
    build.side_effect = error("SECRET provider handle and body")
    assert await command.run(args) == code
    assert "SECRET" not in capsys.readouterr().err
    assert world[-1].queries == 0


async def test_historical_identity_is_read_only_distinct_from_zero_entry_resume(tmp_path):
    _world, activation, originals, path = await reservation(tmp_path)
    for original in originals:
        await activation.entering(original)
    with sqlite3.connect(path) as db:
        before = tuple(db.iterdump())
    hint = await read_activation_collection_identity(str(path), str(originals[0].run_id),
                                                    checkpoint_instances=set())
    assert hint.engagement_id == originals[0].engagement_id
    assert hint.component_binding == originals[0].component_binding
    with sqlite3.connect(path) as db:
        assert tuple(db.iterdump()) == before
    with pytest.raises(StateConflict, match="activation_checkpoint_required"):
        await read_activation_identity(str(path), str(originals[0].run_id))
    missing = tmp_path / "missing" / "state.sqlite"
    assert await read_activation_collection_identity(str(missing), "none",
                                                    checkpoint_instances=set()) is None
    assert not missing.parent.exists()


async def test_cli_rebuilds_canonical_application_over_saved_filesystem_originals(
    tmp_path, monkeypatch, capsys,
):
    from redteam.evidence.stores.filesystem import FilesystemEvidenceStore

    world, args, build = await cli_world(tmp_path, monkeypatch)
    deps, activation, pending, peer, provider, control, gateway, recovery = world
    root = tmp_path / "saved-originals"
    base = FilesystemEvidenceStore(root)
    for record in await deps.evidence_store.list_for_run(peer.run_id):
        await base.append(record)
    rebuilt = []
    async def canonical(config, **kwargs):
        engagement, new_deps = await build_application(
            config, engagement_id=kwargs["engagement_id"],
            orchestration_settings=kwargs["settings"],
            component_profile=kwargs["component_profile"], llm=provider, control_plane=control,
            tool_gateway=gateway, tool_catalog=gateway,
            evidence_store=FilesystemEvidenceStore(root),
            evidence_dir=root, audit_dir=tmp_path / "audit", approval_dir=tmp_path / "approvals",
        )
        rebuilt.append(new_deps)
        return engagement, replace(new_deps, execution_recovery=recovery)
    build.side_effect = canonical
    requests = len(provider.requests)
    before = await RunRequestLedger(activation.coordinator).snapshot(args.run_id)
    assert await command.run(args) == 0
    result = json.loads(capsys.readouterr().out)
    receipt = result["instances"][0]["originals"][0]["original_receipt_id"]
    assert (await FilesystemEvidenceStore(root).get(receipt)).metadata["receipt_kind"] == (
        "execution_resolution"
    )
    assert rebuilt[0].component_binding == deps.component_binding
    assert await RunRequestLedger(activation.coordinator).snapshot(args.run_id) == before
    assert len(provider.requests) == requests and not gateway.invocations and not control.restored
    assert await base.verify_run_integrity(peer.run_id) == []


async def test_zero_checkpoint_actor_empty_inventory_stays_held(tmp_path, monkeypatch, capsys):
    from tests.integration.test_zero_checkpoint_resume import reserved_run

    engagement, deps, gateway, provider, control, original, config, args = await reserved_run(
        tmp_path, monkeypatch,
    )
    from redteam.orchestration.coordination import coordinator_for
    from redteam.orchestration.task_activation import TaskActivation

    await TaskActivation(coordinator_for(deps.task_graph_store, deps.path_store),
                         deps.agents).entering(original)
    monkeypatch.setattr(command, "load_app_config_for_run", lambda _: config)
    build = AsyncMock(return_value=(engagement, deps))
    monkeypatch.setattr(command, "build_resume_application", build)
    args.json = True
    assert await command.run(args) == 1
    output = json.loads(capsys.readouterr().out)
    assert build.await_count == 1 and output["outcome"] == "held"
    assert output["instances"][0]["originals"] == []
    assert not gateway.invocations and not control.restored
    assert (await deps.task_graph_store.get_task(original.active_task_id)).occupant_owner == (
        original.work_owner
    )


async def test_text_output_is_collection_not_run_success(tmp_path, monkeypatch, capsys):
    _world, args, _build = await cli_world(tmp_path, monkeypatch)
    args.json = False
    assert await command.run(args) == 0
    output = capsys.readouterr().out
    assert "Originals only: no resume, Task completion or claim release" in output
    assert "known" in output and "Empty inventory does not prove non-entry" in output


@pytest.mark.parametrize("cancel", [False, True])
async def test_teardown_failure_is_bounded_and_does_not_mask_body_cancellation(
    tmp_path, monkeypatch, capsys, cancel,
):
    world, args, _build = await cli_world(tmp_path, monkeypatch)
    actual_close = InstanceDriver.close
    async def failing_close(driver):
        await actual_close(driver)
        raise RuntimeError("SECRET teardown contents")
    monkeypatch.setattr(InstanceDriver, "close", failing_close)
    if cancel:
        world[-1].query_execution = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await command.run(args)
    else:
        assert await command.run(args) == 1
    assert "SECRET" not in capsys.readouterr().err


async def test_transport_cleanup_error_does_not_replace_collection_cancellation(
    tmp_path, monkeypatch, capsys,
):
    world, args, build = await cli_world(tmp_path, monkeypatch)
    deps, _activation, _pending, _peer, _provider, _control, _gateway, recovery = world
    recovery.query_execution = AsyncMock(side_effect=asyncio.CancelledError)
    async def cleanup():
        raise OSError("SECRET transport cleanup body")
    async def assembled(*a, **kw):
        kw["mcp_stack"].push_async_callback(cleanup)
        return deps.engagement, deps
    build.side_effect = assembled
    with pytest.raises(asyncio.CancelledError):
        await command.run(args)
    assert "SECRET" not in capsys.readouterr().err
