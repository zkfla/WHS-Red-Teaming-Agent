"""Public CLI bootstrap of an original reservation; synthetic providers only."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from redteam.app.bootstrap import build_application, run_engagement
from redteam.app.cli.commands import resume as command
from redteam.core.config import AppConfig, EngagementConfig
from redteam.orchestration.checkpointing import list_checkpoint_threads
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.task_activation import TaskActivation
from tests.fakes import FakeEvidenceStore
from tests.integration.test_activation_recovery import NoFollowupProvider
from tests.integration.test_native_specialist_closed_loop import assembled
from tests.integration.test_specialist_resume import OneApprovalControl


async def reserved_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider, control = NoFollowupProvider(), OneApprovalControl()
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    original_ready = TaskActivation.ready
    originals = []

    async def interrupt_after_commit(self, *args, **kwargs):
        originals.extend(await original_ready(self, *args, **kwargs))
        raise RuntimeError("synthetic process exit before graph entry")

    with monkeypatch.context() as patch:
        patch.setattr(TaskActivation, "ready", interrupt_after_commit)
        with pytest.raises(RuntimeError, match="synthetic process exit"):
            await run_engagement(engagement, deps)
    assert len(originals) == 1 and not gateway.invocations
    original = originals[0]
    assert await list_checkpoint_threads(
        enabled=True, backend="sqlite", path=deps.settings.checkpoint_path,
        run_id=str(original.run_id),
    ) == []
    config = AppConfig(engagement=EngagementConfig.model_validate(
        engagement.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    monkeypatch.setattr(command, "load_app_config_for_run", lambda _: config)
    monkeypatch.setattr(command.OrchestrationSettings, "for_durable_run", lambda *a, **k:
                        deps.settings)
    args = SimpleNamespace(run_id=str(original.run_id), engagement_path="synthetic.yaml",
                           max_steps=None, log_level="WARNING", json=False, instance=None)
    return engagement, deps, gateway, provider, control, original, config, args


async def test_public_cli_rebuilds_same_identity_and_policy_then_original_approval(
    tmp_path, monkeypatch,
):
    engagement, deps, gateway, provider, control, original, config, args = await reserved_run(
        tmp_path, monkeypatch,
    )
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    before = await RunRequestLedger(coordinator).snapshot(str(original.run_id))
    builds, rebuilt, finals = [], [], []

    async def build(config, **kwargs):
        builds.append(kwargs)
        new_engagement, new_deps = await build_application(
            config, engagement_id=kwargs["engagement_id"], llm=provider,
            tool_gateway=gateway, tool_catalog=gateway, control_plane=control,
            evidence_store=FakeEvidenceStore(), orchestration_settings=kwargs["settings"],
            component_profile=kwargs["component_profile"],
            evidence_dir=tmp_path / "evidence", audit_dir=tmp_path / "audit",
            approval_dir=tmp_path / "approvals",
        )
        assert new_engagement.created_at != engagement.created_at  # ordinary CLI reconstruction
        rebuilt.append(new_deps)
        return new_engagement, new_deps

    monkeypatch.setattr(command, "build_resume_application", build)
    assert await command.run(args, on_finished=finals.append) == 0
    assert builds[0]["engagement_id"] == original.engagement_id
    assert len(finals) == 1 and finals[0].pending_approval_id is not None
    assert finals[0].instance_id == original.instance_id
    assert finals[0].run_id == original.run_id and finals[0].started_at == original.started_at
    assert not gateway.invocations  # no approval bypass
    task = await deps.task_graph_store.get_task(original.active_task_id)
    assert task.occupant_owner == original.work_owner
    assert len(await deps.task_graph_store.list_tasks(mission_id=original.mission_id)) == 1
    ledger = RunRequestLedger(coordinator)
    after = await ledger.snapshot(str(original.run_id))
    assert after["spent"] > before["spent"] and after["spent"] == len(provider.requests)
    assert after["policy"] == before["policy"]
    assert len(control.restored) == 1 and control.restored[0]["actions_consumed"] == 0
    # Second CLI resume uses the newly stored checkpoint, not the initial reservation.
    control.approve(finals[0])
    assert await command.run(args) == 0
    assert len(gateway.invocations) == 1
    assert (await ledger.snapshot(str(original.run_id)))["spent"] == len(provider.requests)


@pytest.mark.parametrize("change", ["entered", "legacy", "claim", "stop", "policy", "profile",
                                         "partial", "database", "after_hint"])
async def test_zero_checkpoint_refusal_never_dispatches_or_resets_accounting(
    tmp_path, monkeypatch, change,
):
    _, deps, gateway, provider, control, original, _, args = await reserved_run(
        tmp_path, monkeypatch,
    )
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    async def mutate(tx):
        row = tx.records[("task_activation", f"{original.run_id}:{original.active_task_id}")]
        if change in {"entered", "after_hint"}:
            row["launch_stage"] = "entered"
        elif change == "legacy":
            row.pop("launch_proof_version")
        elif change == "claim":
            task = await tx.tasks.get_task(original.active_task_id)
            await tx.tasks.update_task(task.model_copy(update={"occupant_owner": "other"}))
        elif change == "stop":
            tx.records[("binding", str(original.run_id))]["closed"] = True
    if change in {"entered", "legacy", "claim", "stop"}:
        await coordinator.change(mutate)
    elif change == "partial":
        args.instance = str(original.instance_id)
    elif change in {"policy", "profile", "database"}:
        updates = {"policy": {"llm_request_policy": deps.settings.llm_request_policy.model_copy(
            update={"max_requests": 31},
        )}, "profile": {"specialist_profile_path": None},
            "database": {"task_graph_path": str(tmp_path / "no-database.sqlite3")}}[change]
        monkeypatch.setattr(command.OrchestrationSettings, "for_durable_run", lambda *a, **k:
                            deps.settings.model_copy(update=updates))
    calls = len(provider.requests)
    before = await RunRequestLedger(coordinator).snapshot(str(original.run_id))
    build = AsyncMock(side_effect=AssertionError("invalid identity must not build"))
    if change == "after_hint":
        async def raced_build(*a, **k):
            await coordinator.change(mutate)
            return deps.engagement, replace(deps)
        build = AsyncMock(side_effect=raced_build)
    monkeypatch.setattr(command, "build_resume_application", build)
    assert await command.run(args) == 1
    assert build.await_count == (1 if change == "after_hint" else 0)
    assert not gateway.invocations and len(provider.requests) == calls
    assert not control.restored
    assert await RunRequestLedger(coordinator).snapshot(str(original.run_id)) == before


@pytest.mark.parametrize("change", ["scope", "selectors", "owned"])
async def test_zero_checkpoint_live_rebuild_and_owner_checks_precede_dispatch(
    tmp_path, monkeypatch, change,
):
    _, deps, gateway, provider, control, original, config, args = await reserved_run(
        tmp_path, monkeypatch,
    )
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    before = await RunRequestLedger(coordinator).snapshot(str(original.run_id))
    if change == "scope":
        config = config.model_copy(update={"engagement": config.engagement.model_copy(update={
            "scope": config.engagement.scope.model_copy(update={"denied_hosts": ["a.local"]}),
        })})
        monkeypatch.setattr(command, "load_app_config_for_run", lambda _: config)
    elif change == "selectors":
        # Current availability is dynamic; the persisted role SELECTORS are not.
        selection = tmp_path / "changed-selectors.json"
        selection.write_text(json.dumps({"roles": [{
            "agent_id": str(original.current_agent), "capability_tags": ["web"],
        }]}))
        config = config.model_copy(update={"capability": config.capability.model_copy(update={
            "role_manifest_path": str(selection),
        })})
        monkeypatch.setattr(command, "load_app_config_for_run", lambda _: config)

    async def build(config, **kwargs):
        return await build_application(
            config, engagement_id=kwargs["engagement_id"], llm=provider,
            tool_gateway=gateway, tool_catalog=gateway, control_plane=control,
            evidence_store=FakeEvidenceStore(), orchestration_settings=kwargs["settings"],
            component_profile=kwargs["component_profile"],
            evidence_dir=tmp_path / "evidence", audit_dir=tmp_path / "audit",
            approval_dir=tmp_path / "approvals",
        )
    monkeypatch.setattr(command, "build_resume_application", build)
    calls = len(provider.requests)
    async with InstanceDriver(deps, settings=deps.settings) as owner:
        if change == "owned":
            await owner.own_run(str(original.run_id))
        assert await command.run(args) == 1
    assert not gateway.invocations and len(provider.requests) == calls
    assert not control.restored
    assert await RunRequestLedger(coordinator).snapshot(str(original.run_id)) == before
