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
        if config.orchestration.specialist_profile_path is not None:
            overrides["specialist_profile_path"] = config.orchestration.specialist_profile_path
        if config.orchestration.llm_request_policy is not None:
            overrides["llm_request_policy"] = config.orchestration.llm_request_policy
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
            component_registry=component_registry,
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
