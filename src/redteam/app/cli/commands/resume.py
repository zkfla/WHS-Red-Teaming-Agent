"""``redteam resume <run_id> <engagement.yaml>`` -- continue a durable run.

A ``redteam run`` that stops at ``APPROVAL_PENDING`` leaves two *durable*
artifacts: the LangGraph checkpoint holding the exact
``ActionProposal``/``PlanStep`` the original PLAN turn produced, and the
``Approval`` record. This command rebuilds the same configuration through the
canonical ``app.bootstrap.build_application`` (with durable orchestration
settings -- see ``OrchestrationSettings.for_durable_run``), recovers the run
from its checkpoint with ``AgentLoopRunner.recover_runtime``, and resumes it
with ``AgentLoopRunner.resume_recovered``.

The run's original ``engagement_id`` is read straight from the checkpoint
(before the application is even built) and handed to ``build_application``, so
the rebuilt Control Plane and Attack Graph namespace match the recovered run
exactly -- a fresh random id would silently strand the pending proposal and
split the graph.

For a pending Approval, ``resume_recovered`` re-links the checkpointed
proposal to the resolved Approval via
``DefaultControlPlane.register_pending_proposal`` before re-entering the graph
exactly at POLICY. It never re-plans and never reconstructs an action from the
redacted Audit snapshot.

Without any checkpoint, only a coherent versioned, never-entered specialist
reservation may supply an assembly identity. It is NOT replay permission;
the owned driver verifies current state and actual absence of effects again.

`collect-originals` (same module) only gathers already-existing originals
for entered actors that never wrote a checkpoint. It reuses the central
assembly for queries, never drives actors, and never resumes tasks, releases
claims, or settles usage. See docs/agent/collect-originals-cli.md.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

from pydantic import ValidationError

from redteam.app.cli._shared import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_OK,
    eprint,
    load_app_config_for_run,
)
from redteam.orchestration.component_binding import (
    ComponentRegistry,
)
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "resume", help="Resume a durable run (e.g. one stopped for approval)"
    )
    parser.add_argument("run_id")
    parser.add_argument("engagement_path")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--state-db", default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument(
        "--instance",
        default=None,
        help="Resume exactly one instance thread (default: the whole cohort)",
    )
    parser.set_defaults(handler=run)


async def run(
    args: argparse.Namespace,
    *,
    on_finished: Callable[[RuntimeState], None] | None = None,
    session_channel: Any | None = None,
    on_event: Any | None = None,
    component_registry: ComponentRegistry | None = None,
) -> int:
    """Resume args.run_id; when the resumed run settles, on_finished runs.

    The callback only fires after settle_engagement actually completes,
    so the interactive session can project the settled state via
    session.record_run(final). Every early return (missing checkpoint,
    unsafe snapshot, failed bootstrap, unresumed run) skips it, and the CLI
    parser keeps calling run(args) exactly as before.
    """
    async with AsyncExitStack() as stack:
        return await _run(
            args,
            mcp_stack=stack,
            on_finished=on_finished,
            session_channel=session_channel,
            on_event=on_event,
            component_registry=component_registry,
        )


async def _run(
    args: argparse.Namespace,
    *,
    mcp_stack: AsyncExitStack,
    on_finished: Callable[[RuntimeState], None] | None = None,
    session_channel: Any | None = None,
    on_event: Any | None = None,
    component_registry: ComponentRegistry | None = None,
) -> int:
    try:
        from redteam.app.web_policy import select_engagement_startup
        from redteam.core.environment import environment_context

        startup = await select_engagement_startup(
            args.engagement_path, require_recorded_preparation=True,
        )
        with environment_context(startup.environment):
            config = load_app_config_for_run(args.engagement_path)
            from redteam.app.cli._shared import runtime_overrides
            config = runtime_overrides(config, args)
    except (OSError, ValidationError, ValueError) as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_CONFIG_ERROR

    log_level = args.log_level or config.logging.level
    if session_channel is None:
        logging.basicConfig(level=log_level, force=True)

    return await _run_central(
        args, config, mcp_stack=mcp_stack, on_finished=on_finished,
        on_event=on_event, component_registry=component_registry,
        session_channel=session_channel,
    )


async def _run_central(args, config, *, mcp_stack, on_finished=None,
                       on_event=None, component_registry=None, session_channel=None):
    from pathlib import Path

    from redteam.app.bootstrap import build_central_storage
    from redteam.app.central_run import resume_central_engagement
    from redteam.app.central_views import inspect_central_run
    from redteam.app.cli._shared import print_json
    from redteam.app.cli.commands.run import build
    from redteam.orchestration.coordination import StateConflict
    observing = True
    operator = None
    try:
        inspected, original, _ = await inspect_central_run(
            config.orchestration.central_state_path, args.run_id,
        )
        if not Path(original["storage_refs"]["checkpoints"]).is_file():
            chosen = getattr(args, "instance", None)
            inventory = await inspected.activation_inventory(
                instance_ids={str(chosen)} if chosen is not None else None,
            )
            terminal = False
            if chosen is not None and inventory:
                task = await inspected.get_task(inventory[0]["task_id"])
                terminal = task.status.value in {"completed", "cancelled", "failed"}
            if (not terminal and (not inventory or any(row["launch_stage"] != "reserved"
                                      or row["proof_version"] != 1 for row in inventory))):
                raise StateConflict("central_original_checkpoint_missing")
        selected = OrchestrationSettings.model_validate(original["settings"])
        overrides = {}
        # The normal config now has a schema-3 default for *new* Runs.  Resume
        # keeps the stored profile unless the operator explicitly supplied the
        # profile environment variable; otherwise an old schema-2 Run would be
        # silently rebuilt with new candidate-planner meaning.
        from redteam.core.environment import env_snapshot

        selected_environment = env_snapshot()
        if selected_environment.get("REDTEAM_SPECIALIST_PROFILE"):
            overrides["specialist_profile_path"] = config.orchestration.specialist_profile_path
        if config.orchestration.llm_request_policy is not None:
            overrides["llm_request_policy"] = config.orchestration.llm_request_policy
        if "REDTEAM_GLOBAL_CONCURRENCY" in selected_environment:
            overrides["global_concurrency"] = config.orchestration.global_concurrency
        if "REDTEAM_PER_TARGET_CONCURRENCY" in selected_environment:
            overrides["per_target_concurrency"] = config.orchestration.per_target_concurrency
        if getattr(args, "workers", None) is not None:
            overrides["max_workers"] = args.workers
        if getattr(args, "max_steps", None) is not None:
            overrides["max_steps"] = args.max_steps
        selected = selected.with_overrides(**overrides)
        state, _ = await build_central_storage(
            config.engagement.to_engagement(inspected.engagement_id),
            run_id=args.run_id, path=inspected.path,
        )
        _, deps = await build(
            config, max_steps=None, mcp_stack=mcp_stack, central_state=state,
            original_stores=original["storage_refs"], original_settings=selected,
            original_component_binding=original.get("component_binding"),
            component_registry=component_registry,
            engagement_path=getattr(args, "engagement_path", None),
        )
        if session_channel is not None:
            from redteam.app.central_run import central_root_runtime
            from redteam.app.cli.commands.run import (
                OperatorConsole,
                RuntimeIdentity,
                wait_for_central_suspensions,
            )
            runtime = await central_root_runtime(deps)
            identity = RuntimeIdentity(run_id=runtime.run_id, engagement_id=runtime.engagement_id,
                                       mission_id=runtime.mission_id)
            operator = OperatorConsole(deps, session_channel.output.emit, identity=identity)
            session_channel.attach(operator)
            async def observe(event):
                if not observing or event.run_id != runtime.run_id:
                    return
                identity.observe(event)
                session_channel.observe(event)
                if on_event is not None:
                    await on_event(event)
            await deps.event_bus.subscribe("*", observe)
            session_channel.ready.set()
            async def watch(session, current):
                return await wait_for_central_suspensions(
                    session, current, operator_console=operator,
                    instance_id=getattr(args, "instance", None),
                    input_live=lambda: session_channel.input_available,
                    echo=session_channel.output.emit, session_channel=session_channel,
                )
        else:
            watch = None
        if on_event is not None and session_channel is None:
            await deps.event_bus.subscribe("*", on_event)
        final = await resume_central_engagement(
            deps, instance_id=getattr(args, "instance", None), watch=watch,
        )
    except Exception as exc:  # noqa: BLE001 - originals stay held on any assembly/proof failure
        from redteam.orchestration.coordination import CoordinationError, StateConflict
        detail = (str(exc) if isinstance(exc, (CoordinationError, StateConflict))
                  else type(exc).__name__)
        eprint(f"ERROR: central resume refused: {detail}")
        return EXIT_FAILURE
    finally:
        observing = False
        if session_channel is not None:
            await session_channel.detach()
    if on_finished is not None:
        on_finished(final)
    if args.json:
        print_json(final)
    elif session_channel is None:
        _print_summary(final)
    return _exit_code_for(final)












def _print_summary(final: RuntimeState) -> None:
    print(f"\nRun resumed: phase={final.phase.value} termination_reason={final.termination_reason}")
    print(f"RunId: {final.run_id}")
    if final.mission_status is not None:
        print(f"Mission: {final.mission_status.value}")
    print(f"Actions executed: {final.actions_executed}")
    if final.termination_reason != "operator_paused":
        print(f"Report: redteam report generate {final.run_id}")


def _exit_code_for(final: RuntimeState) -> int:
    # A policy-correct DENY/budget-exhausted stop is not a CLI failure --
    # only a run that actually crashed is (same contract as `run`).
    if final.phase.value == "failed":
        return EXIT_FAILURE
    return EXIT_OK


def add_collect_originals_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "collect-originals",
        help="Collect existing originals only; never resume or release claims",
    )
    parser.add_argument("run_id")
    parser.add_argument("engagement_path")
    parser.add_argument(
        "--instance",
        default=None,
        help="Collect one instance thread only (default: every entered actor)",
    )
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--state-db", default=None)
    parser.set_defaults(handler=run_collect_originals)


def _collection_reason(exc: Exception) -> str:
    from redteam.orchestration.coordination import CoordinationError, StateConflict

    if isinstance(exc, (CoordinationError, StateConflict)):
        return str(exc)
    return type(exc).__name__


async def run_collect_originals(args, *, component_registry=None) -> int:
    async with AsyncExitStack() as stack:
        try:
            from redteam.app.web_policy import select_engagement_startup
            from redteam.core.environment import environment_context

            startup = await select_engagement_startup(
                args.engagement_path, require_recorded_preparation=True,
            )
            with environment_context(startup.environment):
                config = load_app_config_for_run(args.engagement_path)
                from redteam.app.cli._shared import runtime_overrides

                config = runtime_overrides(config, args)
        except (OSError, ValidationError, ValueError) as exc:
            eprint(f"ERROR: {exc}")
            return EXIT_CONFIG_ERROR
        log_level = args.log_level or config.logging.level
        logging.basicConfig(level=log_level, force=True)
        return await _run_collect_central(
            args, config, mcp_stack=stack, component_registry=component_registry,
        )


async def _run_collect_central(args, config, *, mcp_stack, component_registry=None) -> int:
    from pathlib import Path

    from redteam.app.bootstrap import build_central_storage
    from redteam.app.central_views import inspect_central_run
    from redteam.app.cli._shared import print_json
    from redteam.app.cli.commands.run import build
    from redteam.orchestration.coordination import StateConflict
    from redteam.orchestration.instance_driver import InstanceDriver
    from redteam.orchestration.state import RuntimeState

    chosen = getattr(args, "instance", None)
    selected_ids = {str(chosen)} if chosen is not None else None
    try:
        inspected, original, _ = await inspect_central_run(
            getattr(args, "state_db", None) or config.orchestration.central_state_path,
            args.run_id,
        )
    except Exception as exc:
        eprint(f"ERROR: original collection refused: {_collection_reason(exc)}")
        return EXIT_FAILURE
    try:
        before = await inspected.activation_inventory(instance_ids=selected_ids)
        selected = OrchestrationSettings.model_validate(original["settings"])
        overrides = {}
        if config.orchestration.specialist_profile_path is not None:
            overrides["specialist_profile_path"] = config.orchestration.specialist_profile_path
        if config.orchestration.llm_request_policy is not None:
            overrides["llm_request_policy"] = config.orchestration.llm_request_policy
        selected = selected.with_overrides(**overrides)
        state, _ = await build_central_storage(
            config.engagement.to_engagement(inspected.engagement_id),
            run_id=args.run_id, path=inspected.path,
        )
        _, deps = await build(
            config, max_steps=None, mcp_stack=mcp_stack, central_state=state,
            original_stores=original["storage_refs"], original_settings=selected,
            original_component_binding=original.get("component_binding"),
            component_registry=component_registry,
            engagement_path=getattr(args, "engagement_path", None),
        )
        probe_checkpoint = Path(original["storage_refs"]["checkpoints"]).is_file()
        driver_settings = selected
        if not probe_checkpoint:
            driver_settings = selected.model_copy(update={"checkpointing": False})
        driver = InstanceDriver(deps, settings=driver_settings)
        body_error = None
        try:
            await driver.open()
            await driver.own_run(args.run_id)
            current = await state.activation_inventory(instance_ids=selected_ids)
            if sorted(map(_inventory_key, current)) != sorted(map(_inventory_key, before)):
                raise StateConflict("activation_collection_inventory_changed")
            checkpoint_instances = set()
            if probe_checkpoint:
                for row in await state.activation_inventory(instance_ids=selected_ids):
                    if row["launch_stage"] == "reserved":
                        continue
                    probe = RuntimeState(
                        run_id=args.run_id, engagement_id=inspected.engagement_id,
                        instance_id=row["instance_id"],
                    )
                    if await driver.runner_for(probe).checkpoint_exists(probe):
                        checkpoint_instances.add(row["instance_id"])
            results = await driver.collect_checkpointless_originals(
                args.run_id, checkpoint_instances=checkpoint_instances,
                instance_ids=selected_ids,
            )
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            try:
                await driver.close()
            except Exception as exc:
                if body_error is None:
                    raise
                eprint(f"ERROR: original collection teardown incomplete: {type(exc).__name__}")
    except Exception as exc:
        eprint(f"ERROR: original collection refused: {_collection_reason(exc)}")
        return EXIT_FAILURE
    collected = bool(results) and all(
        item.originals and all(original.state == "known" for original in item.originals)
        for item in results
    )
    if not results:
        eprint(
            "NOTE: no proved entered/checkpointless actor; nothing collected. "
            "Resume and claim release are not authorized."
        )
    if getattr(args, "json", False):
        print_json({
            "schema_version": 1,
            "run_id": args.run_id,
            "outcome": "collected" if collected else "held",
            "replay_permitted": False,
            "claim_release_permitted": False,
            "instances": [item.model_dump(mode="json") for item in results],
        })
    else:
        outcome = "collected" if collected else "held"
        print(f"Original collection for Run {args.run_id}: " + outcome)
        for item in results:
            print(f"  instance={item.instance_id}; originals={len(item.originals)}")
            for original in item.originals:
                print(f"    {original.invocation_id}: {original.state}, "
                      f"reason={original.reason_code}, receipt={original.original_receipt_id}")
        print("Originals only: no resume, Task completion or claim release. "
              "Empty inventory does not prove non-entry.")
    return EXIT_OK if collected else EXIT_FAILURE


def _inventory_key(row) -> tuple:
    return (
        row["instance_id"], row["task_id"], row["owner"], row["ownership_version"],
        row["launch_stage"], row["proof_version"], row["binding_hash"],
    )
