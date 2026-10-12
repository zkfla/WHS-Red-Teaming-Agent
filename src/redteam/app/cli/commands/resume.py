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
from redteam.capability.settings import (
    MCPConfigurationError,
    gated_network_policy_path_from_environment,
    shell_policy_path_from_environment,
)
from redteam.capability.shell_assembly import build_gated_provider_config
from redteam.capability.shell_runtime import build_shell_runtime
from redteam.core.config import AppConfig
from redteam.core.contracts.events import EventType
from redteam.core.ids import RunId
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.checkpointing import (
    CheckpointIdentity,
    CheckpointThreadIdentity,
    list_checkpoint_threads,
    read_checkpoint_identity,
)
from redteam.orchestration.component_binding import (
    ComponentBindingError,
    ComponentProfile,
    ComponentRegistry,
    selection_of,
)
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import CLOSED, CoordinationError, StateConflict
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.operator_intents import OperatorIntentService
from redteam.orchestration.runner import AgentLoopRunner, CheckpointingDisabledError
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_activation import (
    ActivationRecoveryIdentity,
    read_activation_identity,
)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "resume", help="Resume a durable run (e.g. one stopped for approval)"
    )
    parser.add_argument("run_id")
    parser.add_argument("engagement_path")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--json", action="store_true")
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
    except (OSError, ValidationError, ValueError) as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_CONFIG_ERROR

    log_level = args.log_level or config.logging.level
    if session_channel is None:
        logging.basicConfig(level=log_level, force=True)

    settings = OrchestrationSettings.for_durable_run(config.orchestration, max_steps=args.max_steps)

    instance_arg = getattr(args, "instance", None)
    try:
        enumerated = await list_checkpoint_threads(
            enabled=settings.checkpointing,
            backend=settings.checkpoint_backend,
            path=settings.checkpoint_path,
            run_id=args.run_id,
        )
    except Exception as exc:  # noqa: BLE001 - bounded, before any build/transport
        eprint(
            f"ERROR: could not enumerate checkpoints for run {args.run_id}: "
            f"{type(exc).__name__}"
        )
        return EXIT_FAILURE
    if not enumerated and instance_arg is None:
        # Never disguise a reservation as a checkpoint thread. A read-only
        # canonical projection supplies the original assembly identity only;
        # existing cohort recovery then proves every actor before any dispatch.
        try:
            launch_identity = await read_activation_identity(settings.task_graph_path, args.run_id)
        except Exception as exc:  # noqa: BLE001 - unreadable is not absence
            reason = (str(exc) if isinstance(exc, (CoordinationError, StateConflict))
                      else type(exc).__name__)
            eprint(f"ERROR: activation bootstrap refused: {reason}")
            return EXIT_FAILURE
        if launch_identity is not None:
            return await _run_cohort(
                args, config, [], cohort=[], launch_identity=launch_identity,
                on_finished=on_finished, session_channel=session_channel, on_event=on_event,
                component_registry=component_registry,
            )
    try:
        # Coherence over the ENTIRE enumerated cohort first: a mismatch in
        # unselected instances is still an unprovable aggregate.
        if enumerated:
            _check_cohort_coherence(enumerated)
        selected = _select_cohort_identities(enumerated, instance_arg)
    except _CohortRefused as exc:
        eprint(f"ERROR: refusing resume before any transport or build: {exc}")
        return EXIT_FAILURE
    cohort_mode = instance_arg is not None or any(
        identity.instance_id is not None for identity in selected
    )
    if cohort_mode and selected:
        return await _run_cohort(
            args,
            config,
            selected,
            cohort=enumerated,
            on_finished=on_finished,
            session_channel=session_channel,
            on_event=on_event,
            component_registry=component_registry,
        )

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
        from redteam.llm.opencode_go_config import llm_connection_fingerprint_for_config

        stored_binding = binding_for_resume(
            hint.component_binding, registry=component_registry,
            llm_connection_fingerprint=llm_connection_fingerprint_for_config(config.llm),
        )
    except (ComponentBindingError, ValueError) as exc:
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
    # Same shell assembly as run.build (None means disabled; invalid policy
    # raises MCPConfigurationError for the config-error path).
    shell_runtime = build_shell_runtime(shell_policy_path_from_environment())
    gated_provider_config = build_gated_provider_config(
        gated_network_policy_path_from_environment()
    )
    if gated_provider_config is not None and shell_runtime is None:
        raise MCPConfigurationError(
            "gated network policy requires the shell provider: set "
            "REDTEAM_SHELL_POLICY_PATH"
        )
    if not config.capability.mcp_servers and shell_runtime is None:
        eprint(
            "WARNING: no MCP servers configured (capability.mcp_servers is empty) -- "
            "this run has an empty tool catalog and cannot execute any Action. "
            "Set REDTEAM_MCP_SERVERS (comma-separated server ids) and each server's "
            "REDTEAM_MCP_<ID>_* environment variables."
        )
    # Same conditional wiring as run.build: omit when unset so the current
    # composition root takes its builtin default.
    component_options: dict[str, Any] = {}
    if component_profile is not None:
        component_options["component_profile"] = component_profile
    if component_registry is not None:
        component_options["component_registry"] = component_registry
    if shell_runtime is not None:
        component_options["builtin_adapters"] = [shell_runtime.adapter]
        component_options["builtin_concurrency"] = dict(shell_runtime.concurrency)
        component_options["builtin_output_limits"] = dict(shell_runtime.output_limits)
        component_options["shell_programs"] = shell_runtime.program_source
    if gated_provider_config is not None:
        # ONLY when the operator network policy is configured (absent means
        # closed): the composition root validates the same builtin adapter.
        component_options["gated_shell"] = (shell_runtime, gated_provider_config)
    return await build_application(
        config,
        engagement_id=engagement_id,
        orchestration_settings=settings,
        mcp_stack=mcp_stack,
        **component_options,
    )


class _CohortRefused(ValueError):
    """A resume cohort fails the constructor-free coherence pass."""


def _select_cohort_identities(
    found: list[CheckpointThreadIdentity], instance: str | None
) -> list[CheckpointThreadIdentity]:
    """Partial targets the exact instance; otherwise the whole cohort.

    A MIXED legacy-singleton plus instance-thread set is refused outright
    on every path: no logical-cohort authority can prove the legacy thread
    belongs with the instances, so admitting both (or accounting both in a
    partial) would risk double driving / double counting one logical
    worker. Raises ``_CohortRefused`` likewise when the request names an
    instance with no checkpoint. Pure: unit-testable without any
    application.
    """
    kinds = {identity.instance_id is None for identity in found}
    if len(kinds) > 1:
        raise _CohortRefused(
            "mixed legacy singleton and instance threads; "
            "resume exactly one cohort generation"
        )
    if instance is None:
        return list(found)
    for identity in found:
        if identity.instance_id is not None and identity.instance_id == instance:
            return [identity]
    raise _CohortRefused(f"no checkpoint thread for instance {instance!r}")


def _check_cohort_coherence(
    identities: list[CheckpointThreadIdentity],
) -> tuple[Any, Any, Any]:
    """Common (engagement, mission, binding) or refuse before any build.

    A mixed cohort never reaches transport, bootstrap, or rehydrate. Pure.
    """
    if not identities:
        raise _CohortRefused("no checkpoint identities")
    first = identities[0]
    for identity in identities[1:]:
        if identity.engagement_id != first.engagement_id:
            raise _CohortRefused("cohort mixes engagement ids")
        if identity.mission_id != first.mission_id:
            raise _CohortRefused("cohort mixes mission ids")
        if identity.component_binding != first.component_binding:
            raise _CohortRefused("cohort mixes component bindings")
    return (first.engagement_id, first.mission_id, first.component_binding)


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


async def _run_cohort(
    args: argparse.Namespace,
    config: AppConfig,
    identities: list[CheckpointThreadIdentity],
    *,
    cohort: list[CheckpointThreadIdentity] | None = None,
    launch_identity: ActivationRecoveryIdentity | None = None,
    on_finished: Callable[[RuntimeState], None] | None = None,
    session_channel: Any | None = None,
    on_event: Any | None = None,
    component_registry: ComponentRegistry | None = None,
) -> int:
    """Resume one run's instance cohort: partial exact or full as-ready.

    Identity enumeration and the coherence pass already ran before any
    transport or build. Each instance recovers independently (unsafe ones
    reconcile first, pending-unknown stays held); Control rehydrates once
    from post-reconciliation cohort sums before any external start; then
    safe instances take the owned-runner ``resume_recovered`` path (a safe
    APPROVAL_PENDING re-enters original POLICY, never a fresh drive) while
    ready reconciliations enter a fresh OBSERVE. The interactive operator
    console stays singleton-only; a cohort session is marked ready without
    one.
    """
    run_id = args.run_id
    log_level = args.log_level or config.logging.level
    logging.basicConfig(level=log_level, force=True)
    settings = OrchestrationSettings.for_durable_run(config.orchestration, max_steps=args.max_steps)
    if launch_identity is None:
        engagement_id, _mission_id, binding_raw = _check_cohort_coherence(identities)
    else:
        if identities or cohort or getattr(args, "instance", None) is not None:
            eprint("ERROR: activation bootstrap requires a full zero-checkpoint cohort")
            return EXIT_FAILURE
        engagement_id = launch_identity.engagement_id
        binding_raw = launch_identity.component_binding
        from redteam.app.agent_profile import selected_specialists

        try:
            specialists = selected_specialists(settings)
            if (specialists is None or specialists.fingerprint != binding_raw.specialist_fingerprint
                    or settings.llm_request_policy != launch_identity.llm_request_policy):
                raise ValueError("activation_configuration_changed_new_run_required")
        except ValueError as exc:
            eprint(f"ERROR: activation bootstrap configuration refused: {exc}")
            return EXIT_FAILURE

    try:
        from redteam.llm.opencode_go_config import llm_connection_fingerprint_for_config

        stored_binding = binding_for_resume(
            binding_raw, registry=component_registry,
            llm_connection_fingerprint=llm_connection_fingerprint_for_config(config.llm),
        )
    except (ComponentBindingError, ValueError) as exc:
        eprint(f"ERROR: stored component profile for run {run_id} is not registered: {exc}")
        return EXIT_FAILURE
    config = config.model_copy(
        update={
            "extensions": list(stored_binding.extension_entry_points),
            "planning_provider": stored_binding.planning_provider_id,
        }
    )

    try:
        async with AsyncExitStack() as mcp_stack:
            engagement, deps = await build_resume_application(
                config,
                engagement_id=engagement_id,
                settings=settings,
                mcp_stack=mcp_stack,
                component_profile=selection_of(stored_binding),
                component_registry=component_registry,
            )
            return await _drive_cohort(
                args, engagement, deps, settings, identities,
                cohort=cohort if cohort is not None else identities,
                on_finished=on_finished,
                session_channel=session_channel,
            )
    except MCPConfigurationError as exc:
        eprint(f"ERROR: MCP configuration invalid: {exc}")
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 - fail closed with a readable message
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.exception("Cohort resume bootstrap failed")
        eprint(f"ERROR: failed to start cohort resume: {exc}")
        return EXIT_FAILURE


async def _drive_cohort(
    args: argparse.Namespace,
    engagement: Any,
    deps: RuntimeDeps,
    settings: OrchestrationSettings,
    identities: list[CheckpointThreadIdentity],
    *,
    cohort: list[CheckpointThreadIdentity],
    on_finished: Callable[[RuntimeState], None] | None = None,
    session_channel: Any | None = None,
) -> int:
    driver = InstanceDriver(deps, settings=settings)
    await driver.open()
    # Body errors always survive teardown: close runs in finally, and a
    # bounded teardown failure only replaces a clean exit, never a body
    # error (cancellation included).
    body_error: BaseException | None = None
    try:
        exit_code = await _drive_cohort_guarded(
            driver, args, deps, selected=identities, cohort=cohort,
            on_finished=on_finished, session_channel=session_channel,
        )
    except BaseException as exc:  # noqa: BLE001 - close first, then propagate
        body_error = exc
    try:
        await driver.close()
    except RuntimeError as exc:
        # Bounded driver-teardown failure (class names only, raised after
        # all close work): report loudly instead of masking settled results.
        eprint(f"ERROR: cohort driver teardown failed: {exc}")
        if body_error is None:
            body_error = exc
    if body_error is not None:
        raise body_error
    return exit_code


async def _cohort_mission_terminal(deps: RuntimeDeps, accounted: list[RuntimeState]) -> bool:
    """Live mission terminal state for the settle gate (no caching)."""
    missions = {str(item.mission_id) for item in accounted if item.mission_id is not None}
    if len(missions) != 1:
        return False
    (mission_id,) = missions
    mission = await deps.task_graph_store.get_mission(mission_id)  # type: ignore[arg-type]
    return mission is not None and mission.status in CLOSED


def _cohort_rehydrate_totals(
    accounted: list[RuntimeState], *, now: datetime
) -> tuple[Any, int, int, float | None]:
    """Post-reconciliation Control totals over the FULL cohort (held incl.).

    Pure: the same sums the single rehydrate call uses, so partial resume
    can be proven to count every cohort member, not just the selection.
    """
    starts = [
        (now - item.started_at).total_seconds()
        for item in accounted
        if item.started_at is not None
    ]
    return (
        accounted[0].engagement_id,
        sum(item.actions_executed for item in accounted),
        sum(item.actions_executed + item.retry_tool_calls for item in accounted),
        max(starts) if starts else None,
    )


async def _drive_cohort_guarded(
    driver: InstanceDriver,
    args: argparse.Namespace,
    deps: RuntimeDeps,
    selected: list[CheckpointThreadIdentity],
    cohort: list[CheckpointThreadIdentity],
    *,
    on_finished: Callable[[RuntimeState], None] | None = None,
    session_channel: Any | None = None,
    resume_paused: bool = True,
    report_results: bool = True,
) -> int:
    """Recover the FULL cohort for accounting, start only the selection.

    A healthy resume never claims an unknown budget: every cohort member
    is recovered and reconciled first (held receipts still project
    counters), and any unprovable member refuses the whole external
    start. ``selected`` is the partial/full subset that actually drives.
    """
    run_id = args.run_id
    unstarted: list[RuntimeState] = []
    if getattr(deps, "specialist_policy", None) is not None:
        try:
            await driver.own_run(str(run_id))
            await driver.reconcile_planning_writes(str(run_id))
            unstarted = await driver.recover_unstarted_activations(
                str(run_id),
                checkpoint_instances={str(identity.instance_id) for identity in cohort},
            )
        except (CoordinationError, StateConflict) as exc:
            eprint(f"ERROR: specialist resume refused: {exc}")
            return EXIT_FAILURE
        except Exception as exc:  # noqa: BLE001 - unavailable is not a zero-use proof
            eprint(f"ERROR: recovery inventory unreadable: {type(exc).__name__}; refusing start")
            return EXIT_FAILURE
    recovered: list[tuple[Any, RuntimeState, Any | None]] = []
    held: list[tuple[str | None, str]] = []
    accounted: list[RuntimeState] = list(unstarted)
    for identity in cohort:
        runner = driver.runner_for(
            RuntimeState(
                run_id=RunId(identity.run_id),
                engagement_id=identity.engagement_id,
                instance_id=identity.instance_id,  # type: ignore[arg-type]
            )
        )
        try:
            runtime, reconciliation = await runner.recover_reconciling(
                run_id, instance_id=identity.instance_id
            )
        except Exception as exc:  # noqa: BLE001 - aggregate must be provable
            eprint(
                f"ERROR: instance {identity.instance_id!r} unreadable: "
                f"{type(exc).__name__}; refusing start"
            )
            return EXIT_FAILURE
        if runtime is None:
            eprint(
                f"ERROR: instance {identity.instance_id!r} has no provable recovery; refusing start"
            )
            return EXIT_FAILURE
        accounted.append(
            reconciliation.runtime if reconciliation is not None else runtime
        )
        if reconciliation is not None and not reconciliation.ready:
            held.append((identity.instance_id, "pending-unknown reconciliation held"))
            continue
        recovered.append((runner, runtime, reconciliation))
    if not accounted:
        eprint(f"ERROR: run {run_id} has no provable cohort accounting; refusing start")
        return EXIT_FAILURE
    # Aggregate Control rehydrate from POST-reconciliation counters,
    # once over the FULL cohort (held receipts project too), before any
    # external start -- never a fresh-budget reset.
    rehydrate = getattr(deps.control_plane, "rehydrate", None)
    if callable(rehydrate) and accounted:
        counted = accounted
        engagement_id, actions, tool_calls, elapsed = _cohort_rehydrate_totals(
            counted, now=datetime.now(timezone.utc)
        )
        try:
            await rehydrate(
                engagement_id,
                actions_consumed=actions,
                tool_calls_consumed=tool_calls,
                elapsed_duration_seconds=elapsed,
            )
        except Exception as exc:  # noqa: BLE001 - never restore a fresh budget on error
            eprint(f"ERROR: could not restore Control accounting: {type(exc).__name__}")
            return EXIT_FAILURE
    if session_channel is not None:
        session_channel.ready.set()
    paused = [item for item in recovered if item[1].termination_reason == "operator_paused"]
    if paused and resume_paused:
        service = OperatorIntentService.from_deps(deps)
        if service is None or paused[0][1].mission_id is None:
            eprint("ERROR: paused cohort has no durable Mission/Operator Intent channel")
            return EXIT_FAILURE
        try:
            application = await service.receive_and_apply(
                "resume",
                mission_id=paused[0][1].mission_id,
                run_id=paused[0][1].run_id,
                publisher=EventPublisher(
                    deps.event_bus,
                    run_id=paused[0][1].run_id,
                    engagement_id=paused[0][1].engagement_id,
                    source="cli.operator",
                ),
            )
        except Exception as exc:  # noqa: BLE001 - never continue after failed persistence
            eprint(f"ERROR: paused cohort resume intent failed: {type(exc).__name__}")
            return EXIT_FAILURE
        if not application.applied:
            eprint("ERROR: paused cohort refused the operator resume instruction")
            return EXIT_FAILURE
    # Drive only the SELECTED runnable subset through the driver's
    # owned lifecycle (no parallel CLI loop shadowing it).
    selected_ids = {identity.instance_id for identity in selected}
    resume_items = [
        (runtime, reconciliation)
        for _runner, runtime, reconciliation in recovered
        if runtime.instance_id in selected_ids
    ]
    full_selection = (getattr(args, "instance", None) is None
                      and selected_ids == {identity.instance_id for identity in cohort})
    # Explicit partial selection never starts a missing-checkpoint reservation,
    # even if it happens to be the sole enumerated checkpoint.
    unstarted_items = unstarted if full_selection and not held else []
    covers_cohort = (selected_ids == {identity.instance_id for identity in cohort}
                     and (not unstarted or bool(unstarted_items))
                     and (getattr(deps, "specialist_policy", None) is None or full_selection))
    driving = [runtime for runtime, _ in resume_items] + unstarted_items
    specialist_loop = None
    specialist_events = []
    if getattr(deps, "specialist_policy", None) is not None and driving:
        from redteam.app.specialist_run import SpecialistLifecycle

        specialist_loop = SpecialistLifecycle(driving[0], deps)
        # Set before native resume so a worker cannot close the Mission before
        # its followup judgment. Unknown/partial cohorts never launch new Tasks.
        await specialist_loop.state("active")
    finals: list[tuple[str | None, RuntimeState]] = []
    # Event kind for every run that did not complete. Its event runtime is the
    # BASE state, not an outcome: such a run is never settled, never passed to
    # on_finished, and always fails the cohort.
    abnormal: dict[str, str] = {}
    # All held: report held below and start nothing (an empty drive
    # would raise instead of reporting).
    if driving:
        if specialist_loop is None:
            stream = driver.drive_resumed(resume_items)
        else:
            policy = deps.specialist_policy
            assert policy is not None
            stream = driver.drive_ready_tasks(
                driving[0], resume_items=resume_items, unstarted_items=unstarted_items,
                max_instances=policy.max_instances, max_activations=policy.max_activations,
                allow_new_tasks=covers_cohort and not held,
                on_wakeup=specialist_loop.evidence_wakeup,
                wake_events=(EventType.EVIDENCE_VALIDITY_CHANGED,),
            )
        async for event in stream:
            if event.kind == "failed":
                eprint(
                    f"WARNING: instance {event.instance_id!r} failed: {event.error}"
                )
            elif event.kind == "terminated":
                eprint(f"WARNING: instance {event.instance_id!r} terminated")
            if event.kind != "completed":
                abnormal[str(event.instance_id)] = event.kind
            finals.append((str(event.instance_id), event.runtime))
            if specialist_loop is not None:
                specialist_events.append(event)
                await specialist_loop.returned(event, allow_followups=covers_cohort and not held)
    if specialist_loop is not None:
        await specialist_loop.finish(specialist_events, covers_cohort=covers_cohort and not held)
    # Settle gate: read live mission state first. Only a terminal
    # mission or a genuinely all-settled cohort (nothing held, every
    # selected runnable final) authorizes a mission-closing settle;
    # otherwise the final reports with its settle deferred so a held
    # or sibling instance is never stranded by our close.
    mission_terminal = await _cohort_mission_terminal(deps, accounted)
    # Partial covers only its subset: full-settled needs the WHOLE
    # cohort selected (or a terminal mission); otherwise defer.
    # A failed/terminated sibling is not a settled final, so it blocks the
    # closing settle exactly like a held one (it would be stranded by our close).
    all_settled = (
        covers_cohort and not held and not abnormal
        and {str(runtime.instance_id) for runtime in driving}
        <= {instance_id for instance_id, _ in finals}
        and not any(final.pending_approval_id or final.termination_reason
                    == TerminationReason.OPERATOR_PAUSED for _, final in finals)
    )
    settled: list[tuple[str | None, RuntimeState]] = []
    settle_failed = False
    for instance_id, final in finals:
        if specialist_loop is not None and specialist_loop.diagnostic is not None:
            final = final.model_copy(update={"planning_diagnostic": specialist_loop.diagnostic})
        if instance_id in abnormal:
            eprint(f"instance {instance_id!r}: settle skipped (run {abnormal[instance_id]})")
            settled.append((instance_id, final))
            continue
        if mission_terminal or all_settled:
            try:
                final = await settle_engagement(final, deps)
            except Exception as exc:  # noqa: BLE001 - report, keep other instances
                settle_failed = True
                eprint(f"ERROR: instance {instance_id!r} settle failed: {type(exc).__name__}")
                settled.append((instance_id, final))
                continue
        else:
            if report_results:
                eprint(f"instance {instance_id!r}: settle deferred (sibling unsettled)")
        settled.append((instance_id, final))
        if on_finished is not None:
            on_finished(final)
    for instance_id, final in settled:
        if report_results:
            eprint(
                f"instance {instance_id!r}: phase={final.phase.value} "
                f"reason={final.termination_reason}"
            )
    for instance_id, why in held:
        eprint(f"instance {instance_id!r}: held ({why})")
    if abnormal or settle_failed or any(final.phase.value == "failed" for _id, final in settled):
        return EXIT_FAILURE
    return EXIT_OK


async def watch_specialist_approval(
    deps: RuntimeDeps, pending: RuntimeState, *, poll_seconds: float,
) -> RuntimeState:
    """Watch using full-cohort recovery, not a single fresh-budget runner.

    Approval still re-enters its original POLICY through the existing driver.
    A watch tick is NOT an operator resume instruction for a paused sibling.
    """
    if not deps.settings.checkpointing or deps.settings.checkpoint_backend != "sqlite":
        raise ValueError("specialist_approval_watch_requires_durable_cohort")
    current = pending
    async with InstanceDriver(deps, settings=deps.settings) as driver:
        await driver.own_run(str(current.run_id))
        while current.termination_reason == TerminationReason.APPROVAL_PENDING:
            await driver.runner_for(current).wait_for_change(current, timeout_seconds=poll_seconds)
            cohort = await list_checkpoint_threads(
                enabled=True, backend=deps.settings.checkpoint_backend,
                path=deps.settings.checkpoint_path, run_id=str(current.run_id),
            )
            if not cohort:
                raise ValueError("specialist_approval_cohort_missing")
            finals: list[RuntimeState] = []
            code = await _drive_cohort_guarded(
                driver, argparse.Namespace(run_id=str(current.run_id)), deps, cohort, cohort,
                on_finished=finals.append, resume_paused=False, report_results=False,
            )
            if code != EXIT_OK or not finals:
                raise RuntimeError("specialist_approval_cohort_resume_failed")
            mission = (await deps.task_graph_store.get_mission(current.mission_id)
                       if current.mission_id is not None else None)
            if mission is not None and mission.status.value == "cancelled":
                return current.model_copy(update={
                    "termination_reason": TerminationReason.OPERATOR_STOPPED,
                    "pending_approval_id": None, "pending_action_id": None,
                })
            current = next((item for item in finals if item.pending_approval_id),
                           next((item for item in finals if item.termination_reason
                                 == TerminationReason.OPERATOR_PAUSED), finals[-1]))
    return current
