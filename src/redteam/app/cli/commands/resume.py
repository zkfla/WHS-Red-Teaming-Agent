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

Safety: every failure leaves the run exactly as it was. The run is refused
when no checkpoint exists, or when the recovered checkpoint still has an
EXECUTE/WORKER super-step pending (replaying it could repeat a side effect).
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from redteam.app.bootstrap import build_application, settle_engagement
from redteam.app.cli._shared import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_OK,
    eprint,
    load_app_config_for_run,
)
from redteam.app.runtime_components import binding_for_resume
from redteam.capability.settings import MCPConfigurationError
from redteam.core.config import AppConfig
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.checkpointing import (
    CheckpointIdentity,
    read_checkpoint_identity,
)
from redteam.orchestration.component_binding import (
    ComponentBindingError,
    ComponentProfile,
    ComponentRegistry,
    selection_of,
)
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.operator_intents import OperatorIntentService
from redteam.orchestration.runner import AgentLoopRunner, CheckpointingDisabledError
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "resume", help="Resume a durable run (e.g. one stopped for approval)"
    )
    parser.add_argument("run_id")
    parser.add_argument("engagement_path")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--json", action="store_true")
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
    except (OSError, ValidationError, ValueError) as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_CONFIG_ERROR

    log_level = args.log_level or config.logging.level
    if session_channel is None:
        logging.basicConfig(level=log_level, force=True)

    settings = OrchestrationSettings.for_durable_run(config.orchestration, max_steps=args.max_steps)

    try:
        hint = await _read_checkpoint_identity(settings, args.run_id)
    except Exception as exc:  # noqa: BLE001 - never leave a broken run half-resumed
        eprint(f"ERROR: could not read checkpoint for run {args.run_id}: {exc}")
        return EXIT_FAILURE
    if hint is None:
        eprint(
            f"ERROR: no checkpoint for run {args.run_id} -- it does not exist or "
            f"was never durably checkpointed. Refusing to resume."
        )
        return EXIT_FAILURE

    # Stored selection check before any MCP transport is opened: unknown
    # profile id/version or changed component ids fail here. A missing
    # record is a legacy checkpoint and resumes as builtin.
    try:
        stored_binding = binding_for_resume(hint.component_binding, registry=component_registry)
    except ComponentBindingError as exc:
        eprint(
            f"ERROR: stored component profile for run {args.run_id} is not "
            f"registered: {exc} -- refusing to resume."
        )
        return EXIT_FAILURE

    config = config.model_copy(
        update={
            "extensions": list(stored_binding.extension_entry_points),
            "planning_provider": stored_binding.planning_provider_id,
        }
    )

    try:
        engagement, deps = await build_resume_application(
            config,
            engagement_id=hint.engagement_id,
            settings=settings,
            mcp_stack=mcp_stack,
            component_profile=selection_of(stored_binding),
            component_registry=component_registry,
        )
    except MCPConfigurationError as exc:
        eprint(f"ERROR: MCP configuration invalid: {exc}")
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 - fail closed with a readable message
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.exception("Application bootstrap failed")
        eprint(f"ERROR: failed to start application: {exc}")
        return EXIT_FAILURE

    runner = AgentLoopRunner(deps, settings=settings)
    observing = True
    operator = None
    try:
        try:
            runtime = await runner.recover_runtime(args.run_id)
        except CheckpointingDisabledError as exc:
            eprint(f"ERROR: cannot resume run {args.run_id}: {exc}")
            return EXIT_CONFIG_ERROR
        if runtime is None:
            eprint(
                f"ERROR: no safe checkpoint for run {args.run_id} -- its latest "
                f"checkpoint still has a tool-execution step pending. Refusing to resume."
            )
            return EXIT_FAILURE

        rehydrate = getattr(deps.control_plane, "rehydrate", None)
        if callable(rehydrate):
            try:
                await rehydrate(
                    runtime.engagement_id,
                    actions_consumed=runtime.actions_executed,
                    tool_calls_consumed=(runtime.actions_executed + runtime.retry_tool_calls),
                    elapsed_duration_seconds=(
                        max(
                            0.0,
                            (datetime.now(timezone.utc) - runtime.started_at).total_seconds(),
                        )
                        if runtime.started_at is not None
                        else None
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - never restore a fresh budget on error
                eprint(f"ERROR: could not restore Control accounting: {type(exc).__name__}")
                return EXIT_FAILURE

        if session_channel is not None:
            from redteam.app.cli.commands.run import OperatorConsole, RuntimeIdentity

            identity = RuntimeIdentity(
                run_id=runtime.run_id,
                engagement_id=runtime.engagement_id,
                mission_id=runtime.mission_id,
            )
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

        if runtime.termination_reason == TerminationReason.OPERATOR_PAUSED:
            service = OperatorIntentService.from_deps(deps)
            if service is None or runtime.mission_id is None:
                eprint("ERROR: paused run has no durable Mission/Operator Intent channel")
                return EXIT_FAILURE
            try:
                application = await service.receive_and_apply(
                    "resume",
                    mission_id=runtime.mission_id,
                    run_id=runtime.run_id,
                    publisher=EventPublisher(
                        deps.event_bus,
                        run_id=runtime.run_id,
                        engagement_id=runtime.engagement_id,
                        source="cli.operator",
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - never continue after failed intent persistence
                eprint(f"ERROR: paused Mission resume intent failed: {type(exc).__name__}")
                return EXIT_FAILURE
            if not application.applied:
                eprint("ERROR: paused Mission refused the operator resume instruction")
                return EXIT_FAILURE

        try:
            final = await runner.resume_recovered(runtime)
        except Exception as exc:  # noqa: BLE001 - same fail-closed contract as run
            if logging.getLogger().isEnabledFor(logging.DEBUG):
                logging.exception("Resume failed")
            eprint(f"ERROR: resume failed: {exc}")
            return EXIT_FAILURE

        if final == runtime:
            eprint(
                f"ERROR: run {args.run_id} did not resume; approval may still be pending, "
                "the Mission may still be paused, or its termination is not resumable."
            )
            return EXIT_FAILURE

        final = await settle_engagement(final, deps, last_event_id=runner.last_event_id)
        if session_channel is not None:
            from redteam.app.cli.commands.run import wait_for_suspensions

            final = await wait_for_suspensions(
                final,
                deps,
                operator_console=operator,
                input_live=lambda: session_channel.input_available,
                echo=session_channel.output.emit,
                session_channel=session_channel,
            )
    finally:
        observing = False
        if session_channel is not None:
            await session_channel.detach()
        await runner.aclose()

    if on_finished is not None:
        on_finished(final)
    if not args.json and session_channel is None:
        _print_summary(final)
    return _exit_code_for(final)


async def build_resume_application(
    config: AppConfig,
    *,
    engagement_id: Any,
    settings: OrchestrationSettings,
    mcp_stack: AsyncExitStack | None = None,
    component_profile: ComponentProfile | None = None,
    component_registry: ComponentRegistry | None = None,
) -> tuple[Any, RuntimeDeps]:
    """Rebuild the canonical runtime for a durable resume.

    The same ``build_application`` the original run used, with the recovered
    run's original ``engagement_id`` and durable orchestration settings, so
    the Control Plane and Attack Graph namespace line up with the checkpoint.
    """
    if not config.capability.mcp_servers:
        eprint(
            "WARNING: no MCP servers configured (capability.mcp_servers is empty) -- "
            "using the in-process ExampleMCPAdapter (development/test only; no real "
            "tool execution)."
        )
    # Same conditional wiring as run.build: omit when unset so the current
    # composition root takes its builtin default.
    component_options: dict[str, Any] = {}
    if component_profile is not None:
        component_options["component_profile"] = component_profile
    if component_registry is not None:
        component_options["component_registry"] = component_registry
    return await build_application(
        config,
        engagement_id=engagement_id,
        orchestration_settings=settings,
        mcp_stack=mcp_stack,
        **component_options,
    )


async def _read_checkpoint_identity(
    settings: OrchestrationSettings, run_id: str
) -> CheckpointIdentity | None:
    """Read the run's resume identity without a typed checkpoint restore.

    Needed before the application is built: the original engagement_id and
    the recorded component binding live inside the checkpoint, and the
    per-run codec selection needed for a full typed restore is itself
    derived from that binding. The identity reader therefore runs
    constructor-free (raw projection only), so a checkpoint written with
    selected codecs can still be read here. Full replay-safety validation
    still happens later in AgentLoopRunner.recover_runtime with the strict
    serializer and the recovered selection.
    """
    return await read_checkpoint_identity(
        enabled=settings.checkpointing,
        backend=settings.checkpoint_backend,
        path=settings.checkpoint_path,
        run_id=str(run_id),
    )


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
