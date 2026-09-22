"""``redteam run`` — the only command that executes an Engagement.

Canonical path, no CLI-only shortcut:

    engagement.yaml (+ --config app.yaml) -> AppConfig
        -> app.bootstrap.build_application(config)   (real LLM, real MCP, real Control)
        -> app.bootstrap.run_engagement(engagement, deps=deps)
        -> Orchestration -> ActionProposal -> Control -> Tool Selection
        -> ToolGateway -> MCP Adapter

This module never decides ROE/Scope/Tool-selection/Approval policy — it
only builds the config, starts the run, prints the events it's handed,
and (interactive mode only) polls for a resolved Approval using
``AgentLoopRunner.reconcile_once`` — the same reconciliation primitive
built for exactly this, not a bespoke wait loop.

``execute()`` below is the shared core (build -> subscribe -> run ->
approval wait) also used by the default ``redteam`` interactive
entrypoint (``commands.interactive``) — only the event renderer and the
handful of plain-text lines (``echo``) differ between the two; the
call sequence, exception types, and this command's own plain
``[EventType] k=v``/``--json`` output are unchanged from before that
split (section 23: this explicit command stays script/CI-safe).
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from redteam.app.bootstrap import build_application, run_engagement
from redteam.app.cli._shared import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_OK,
    eprint,
    load_app_config_for_run,
    mask_secrets,
)
from redteam.capability.settings import MCPConfigurationError
from redteam.core.config import AppConfig
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.ports.event_bus import EventHandler
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.events import (
    AGENT_NO_EXECUTABLE_CANDIDATES,
    AUTHENTICATION_CHECK_DEFERRED,
    AUTHENTICATION_CHECKED,
    CANDIDATE_GENERATED,
    CANDIDATE_REJECTED,
    GRAPH_MUTATION_APPLIED,
    HYPOTHESIS_RESOLVED,
    REPLAN_TRIGGERED,
    RESULT_MAPPER_FAILED,
)
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason

Echo = Callable[[str], None]

# Events worth a human's/CI's attention; TOOL_EXECUTION_STARTED and
# AGENT_STARTED/FINISHED are structural noise for a single-line stream.
_RENDERED_EVENTS = frozenset(
    {
        EventType.RUN_STARTED,
        EventType.ACTION_PROPOSED,
        EventType.POLICY_EVALUATED,
        EventType.ACTION_APPROVED,
        EventType.ACTION_DENIED,
        EventType.APPROVAL_REQUIRED,
        EventType.TOOL_EXECUTION_FINISHED,
        EventType.TOOL_EXECUTION_FAILED,
        EventType.EVIDENCE_CREATED,
        EventType.FINDING_CREATED,
        EventType.FINDING_VERIFIED,
        EventType.GRAPH_UPDATED,
        EventType.RUN_FINISHED,
        # Not part of the Core vocabulary (redteam.orchestration.events) --
        # a plain string, same as every EventType member; included so an
        # agent silently producing zero executable candidates is visible
        # by default, not only under --verbose (section 10).
        AGENT_NO_EXECUTABLE_CANDIDATES,
        CANDIDATE_GENERATED,
        CANDIDATE_REJECTED,
        GRAPH_MUTATION_APPLIED,
        HYPOTHESIS_RESOLVED,
        REPLAN_TRIGGERED,
        RESULT_MAPPER_FAILED,
        AUTHENTICATION_CHECK_DEFERRED,
        AUTHENTICATION_CHECKED,
    }
)

#: How often the interactive wait re-checks whether a pending Approval
#: has been resolved -- also the cap on how stale "still waiting" looks.
_APPROVAL_POLL_SECONDS = 5.0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("run", help="Run an Engagement end-to-end")
    parser.add_argument("engagement_path")
    parser.add_argument("--config", dest="config_path", default=None)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=run)


def _event_subscriber(*, json_mode: bool):  # noqa: ANN202
    async def _handler(event: EventEnvelope) -> None:
        _render_event(event, json_mode=json_mode)

    return _handler


def _render_event(event: EventEnvelope, *, json_mode: bool) -> None:
    if json_mode:
        # JSON Lines: one compact object per event, matching this
        # project's own evidence/audit `.jsonl` convention. Not a single
        # JSON document, because `run` streams rather than batching.
        print(json.dumps(mask_secrets(event.model_dump(mode="json")), separators=(",", ":")))
        return
    if event.event_type not in _RENDERED_EVENTS:
        return
    if event.event_type == CANDIDATE_REJECTED:
        payload = event.payload
        field = f" field={payload['field']}" if payload.get("field") else ""
        print(
            "candidate rejected: "
            f"operation={payload.get('operation', 'unknown')} "
            f"reason={payload.get('reason', 'unknown')}{field}"
        )
        return
    if event.event_type == RESULT_MAPPER_FAILED:
        payload = event.payload
        print(
            "result mapper failed: "
            f"operation={payload.get('operation', 'unknown')} "
            f"mapper={payload.get('mapper', 'unknown')} "
            f"reason={payload.get('reason', 'unknown')}"
        )
        return
    if event.event_type == AGENT_NO_EXECUTABLE_CANDIDATES:
        payload = event.payload
        print(
            "no executable candidates: "
            f"offered={payload.get('drafts_offered', 0)} "
            f"rejected={len(payload.get('rejections', []) or [])}"
        )
        for item in (payload.get("rejections", []) or [])[:3]:
            if not isinstance(item, dict):
                continue
            field = f"({item['field']})" if item.get("field") else ""
            print(
                "  - "
                f"{item.get('operation', 'unknown')}: "
                f"{item.get('reason', 'unknown')}{field}"
            )
        return
    fields = " ".join(f"{k}={v}" for k, v in mask_secrets(event.payload).items() if v is not None)
    print(f"[{event.event_type}] {fields}")


async def build(
    config: AppConfig, *, max_steps: int | None
) -> tuple[Any, RuntimeDeps]:
    """Phase 1 of the shared core (section 17): config -> live
    ``(Engagement, RuntimeDeps)``, via the canonical
    ``app.bootstrap.build_application``. Kept separate from
    :func:`execute` so a caller can give this phase's failures a
    different message than a failure once the run is actually
    underway -- exactly what ``run()`` below does, unchanged from
    before this was split out.
    """
    if not config.capability.mcp_servers:
        eprint(
            "WARNING: no MCP servers configured (capability.mcp_servers is empty) -- "
            "using the in-process ExampleMCPAdapter (development/test only; no real "
            "tool execution). Configure --config app.yaml capability.mcp_servers for "
            "a production run."
        )

    orchestration_settings = OrchestrationSettings.from_config(
        config.orchestration,
        max_steps=max_steps,
        checkpointing=True,
        checkpoint_backend="sqlite",
    )

    return await build_application(config, orchestration_settings=orchestration_settings)


async def execute(
    engagement: Any,
    deps: RuntimeDeps,
    *,
    config: AppConfig,
    non_interactive: bool,
    on_event: EventHandler,
    echo: Echo,
) -> RuntimeState:
    """Phase 2 of the shared core: subscribe -> run -> approval wait.
    Callers own exception handling/exit codes and choose the renderer
    (``on_event``) and plain-text sink (``echo``) -- this function makes
    neither decision itself. Used by both this module's own ``run()``
    (plain renderer, ``echo=print``) and ``commands.interactive`` (Rich
    renderer/echo).
    """
    await deps.event_bus.subscribe("*", on_event)
    echo(f"Engagement: {engagement.name}")

    final = await run_engagement(
        engagement, deps=deps, orchestration_config=config.orchestration
    )

    if final.termination_reason == TerminationReason.APPROVAL_PENDING:
        if non_interactive:
            echo(
                f"\nAction pending approval: {final.pending_approval_id} "
                f"-- --non-interactive never auto-approves, so this run stops here. "
                f"`redteam approvals approve {final.pending_approval_id}` will mark "
                f"it resolved, but this v1 CLI has no way to resume THIS specific "
                f"run afterwards (that needs a still-live interactive `redteam run` "
                f"process watching it, which --non-interactive deliberately isn't)."
            )
        else:
            final = await _wait_for_approval(deps, final, echo=echo)

    return final


async def run(args: argparse.Namespace) -> int:
    try:
        config = load_app_config_for_run(args.engagement_path, args.config_path)
    except (OSError, ValidationError, ValueError) as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_CONFIG_ERROR

    log_level = args.log_level or config.logging.level
    logging.basicConfig(level=log_level, force=True)

    try:
        engagement, deps = await build(config, max_steps=args.max_steps)
    except MCPConfigurationError as exc:
        eprint(f"ERROR: MCP configuration invalid: {exc}")
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 - fail closed with a readable message, not a traceback
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.exception("Application bootstrap failed")
        eprint(f"ERROR: failed to start application: {exc}")
        return EXIT_FAILURE

    echo: Echo = (lambda _line: None) if args.json else print

    try:
        final = await execute(
            engagement,
            deps,
            config=config,
            non_interactive=args.non_interactive,
            on_event=_event_subscriber(json_mode=args.json),
            echo=echo,
        )
    except Exception as exc:  # noqa: BLE001 - same fail-closed contract as bootstrap
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.exception("Run failed")
        eprint(f"ERROR: run failed: {exc}")
        return EXIT_FAILURE

    if not args.json:
        _print_summary(final)
    return _exit_code_for(final)


async def _wait_for_approval(
    deps: RuntimeDeps, final: RuntimeState, *, echo: Echo
) -> RuntimeState:
    echo(f"\nAction pending approval: {final.pending_approval_id}")
    echo(
        f"Resolve from another terminal: "
        f"redteam approvals approve {final.pending_approval_id}"
    )
    echo(f"                           or: redteam approvals deny {final.pending_approval_id}")
    echo("Waiting for resolution (Ctrl+C to stop watching -- the Run stays pending)...")

    resume_settings = deps.settings.model_copy(update={"thread_id": str(final.run_id)})
    resume_runner = AgentLoopRunner(deps, settings=resume_settings)
    # deps.event_bus already has the subscriber `run()` registered before
    # the first run_engagement() call -- it's the same bus for the whole
    # process, subscribing again here would print every event twice.
    current = final
    try:
        while current.termination_reason == TerminationReason.APPROVAL_PENDING:
            result = await resume_runner.reconcile_once(
                current, timeout_seconds=_APPROVAL_POLL_SECONDS
            )
            current = result.runtime
    except KeyboardInterrupt:
        echo(
            f"\nStopped watching. Run {current.run_id} is still pending approval "
            f"{current.pending_approval_id}. This CLI does not resume a run across "
            f"separate invocations in v1 -- keep a `redteam run` watching, or "
            f"inspect it later with `redteam status {current.run_id}`."
        )
        raise  # let main()'s top-level handler set the conventional SIGINT exit code
    finally:
        await resume_runner.aclose()
    return current


def _print_summary(final: RuntimeState) -> None:
    print(
        f"\nRun finished: phase={final.phase.value} "
        f"termination_reason={final.termination_reason}"
    )
    print(f"RunId: {final.run_id}")
    print(f"Actions executed: {final.actions_executed}")
    print(f"Report: redteam report generate {final.run_id}")


def _exit_code_for(final: RuntimeState) -> int:
    # A policy-correct DENY/APPROVAL_PENDING/budget-exhausted stop is not
    # a CLI failure -- only a run that actually crashed is (see section 20).
    if final.phase.value == "failed":
        return EXIT_FAILURE
    return EXIT_OK
