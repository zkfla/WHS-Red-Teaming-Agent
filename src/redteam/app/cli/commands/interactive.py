"""``redteam`` with no subcommand — the interactive entrypoint (sections
9-12, 28-29 of the spec this module implements).

    User -> CLI -> Engagement Intake Assistant -> Structured Draft
         -> Config Validator -> Preflight -> Orchestrator -> Agent
         -> Capability -> Control Plane -> Tool

This module owns none of those boxes except the first two arrows: it
collects/edits an ``EngagementConfig`` (via ``redteam.intake``) and
shows a Preflight screen, then hands off to the *exact same*
``commands.run.build``/``commands.run.execute`` the explicit
``redteam run ...`` command uses — the Orchestrator/Agent/Capability/
Control Plane chain is not reimplemented or wrapped here.

Backward compatibility (section 23): this module is only ever reached
when ``redteam`` is invoked with **no** subcommand
(``app.cli.__init__._build_parser``). ``redteam run ...`` never imports
or executes any of this.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from rich.console import Console

from redteam.app.bootstrap import build_llm_client
from redteam.app.cli import renderer
from redteam.app.cli._shared import (
    DEFAULT_ENGAGEMENT_PATH,
    DEFAULT_ENV_PATH,
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_OK,
    env_file_is_gitignored,
    prompt_secret,
    write_env_var,
)
from redteam.app.cli.commands import approvals as approvals_command
from redteam.app.cli.commands import resume as resume_command
from redteam.app.cli.commands import run as run_command
from redteam.app.cli.commands.validate import check_capability, check_roe, check_scope
from redteam.app.cli.conversation import (
    ConversationAssistant,
    ConversationDecision,
    ConversationError,
    catalog_tool_labels,
)
from redteam.app.cli.interactive_commands import InteractiveSession
from redteam.capability.settings import MCPConfigurationError, mcp_server_ids_from_environment
from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
from redteam.core.config import (
    AppConfig,
    CapabilityConfig,
    EngagementConfig,
    LLMConfig,
    LoggingConfig,
    OrchestrationConfig,
    load_engagement_config,
)
from redteam.core.enums import ApprovalStatus
from redteam.core.ids import ApprovalId
from redteam.core.ports.llm import LLMClient
from redteam.intake.assistant import IntakeAssistant, IntakeAssistantError, IntakeSession
from redteam.intake.fallback import deterministic_intake_turn
from redteam.intake.models import missing_required_fields
from redteam.intake.serializer import (
    DraftIncompleteError,
    UnsafeEditError,
    draft_to_engagement_config,
    dump_engagement_yaml,
    engagement_config_to_draft,
    merge_draft,
    scope_diff,
)
from redteam.orchestration.engagement_planner import sanitize_objective
from redteam.orchestration.state import RuntimeState, TerminationReason

logger = logging.getLogger(__name__)


async def run(args: argparse.Namespace) -> int:
    console = Console()
    verbose = bool(getattr(args, "verbose", False))
    renderer.render_banner(console)

    try:
        return await _run(console, verbose=verbose)
    except EOFError:
        # No more input to read (section 23): a script/CI piped this
        # process no stdin at all. The explicit, automation-friendly
        # command is `redteam run <engagement>` (+ REDTEAM_LLM_*/REDTEAM_MCP_*
        # environment variables).
        console.print(
            "\n[dim]No input available -- this is the interactive setup. "
            "Non-interactive environments should use "
            "`redteam run <engagement.yaml> --non-interactive` "
            "instead.[/dim]"
        )
        return EXIT_CONFIG_ERROR
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/dim]")
        raise


async def _run(console: Console, *, verbose: bool) -> int:
    (
        llm_config,
        orchestration_config,
        capability_config,
        logging_config,
    ) = await _ensure_runtime_config(console)
    llm_client = build_llm_client(llm_config)
    conversation = ConversationAssistant(llm_client)

    if not DEFAULT_ENGAGEMENT_PATH.exists():
        console.print("\nNo engagement configuration found.\n")
        console.print("Starting engagement setup.\n")
        engagement_config = await _new_engagement(console, llm_client)
        if engagement_config is None:
            return EXIT_OK
        _write_engagement(DEFAULT_ENGAGEMENT_PATH, engagement_config)
    else:
        engagement_config = load_engagement_config(DEFAULT_ENGAGEMENT_PATH)

    selected = await _existing_config_menu(console, engagement_config, llm_client)
    if selected is None:
        return EXIT_OK
    engagement_config, objective = selected

    capability_config = await _ensure_mcp_config(console, engagement_config, capability_config)

    engagement_config = await _preflight_loop(
        console,
        engagement_config,
        llm_client,
        llm_config,
        capability_config,
        auto_start=objective is not None,
    )
    if engagement_config is None:
        return EXIT_OK
    _write_engagement(DEFAULT_ENGAGEMENT_PATH, engagement_config)

    full_config = AppConfig(
        engagement=engagement_config,
        llm=llm_config,
        orchestration=orchestration_config,
        capability=capability_config,
        logging=logging_config,
    )
    logging.basicConfig(level=logging_config.level, force=True)
    from redteam.app.cli.session_runtime import run_session

    session = InteractiveSession(engagement_config=engagement_config, llm_config=llm_config)
    result = await run_session(
        console,
        full_config,
        session=session,
        objective=objective or f"/new Assess the declared scope of {engagement_config.name}",
        verbose=verbose,
        conversation=conversation,
    )
    return result.exit_code


# --- runtime (LLM/capability) config: a short one-time prompt, never an ----
# --- Intake Assistant conversation (section 3/9 -- see the design note ----
# --- in the completion report on why this is not itself LLM-driven). ------


async def _ensure_runtime_config(
    console: Console,
) -> tuple[LLMConfig, OrchestrationConfig, CapabilityConfig, LoggingConfig]:
    """One-time interactive LLM setup, persisted to ``.env`` -- never a
    ``app.yaml``/application-config file (there is no such file in this
    CLI; see ``_shared.load_app_config_for_run``).

    ``REDTEAM_LLM_PROVIDER`` already set (this process's environment, or a
    previous run's ``.env`` the shell already sourced) means setup already
    happened -- read the same way ``redteam run`` does
    (``LLMConfig.from_environment()``), no prompt.
    """
    if os.environ.get("REDTEAM_LLM_PROVIDER"):
        return (
            LLMConfig.from_environment(),
            OrchestrationConfig.from_environment(),
            CapabilityConfig(mcp_servers=mcp_server_ids_from_environment()),
            LoggingConfig(),
        )

    console.print("\n[dim]No REDTEAM_LLM_PROVIDER set -- one-time runtime setup.[/dim]")
    while True:
        model = console.input("LLM model (e.g. chatgpt/gpt-5.5, gpt-4o-mini): ").strip()
        if model:
            break
        console.print(
            "[red]A real LLM model is required -- runs without a model are not supported.[/red]"
        )
    llm_config = LLMConfig(provider="litellm", default_model=model)
    if not llm_config.resolve_api_key():
        # A `chatgpt/...` model authenticates via the OAuth device flow
        # documented in README, not an API key -- nothing to collect here.
        if not model.startswith("chatgpt/"):
            key = prompt_secret(f"API key for {model} (stored in .env, never shown again)")
            if key:
                if not env_file_is_gitignored(DEFAULT_ENV_PATH):
                    console.print(
                        f"[yellow]{renderer.theme.ICON_WARNING} {DEFAULT_ENV_PATH} is not "
                        f"listed in .gitignore -- add it before committing.[/yellow]"
                    )
                write_env_var(DEFAULT_ENV_PATH, llm_config.api_key_env_var, key)
                os.environ[llm_config.api_key_env_var] = key

    write_env_var(DEFAULT_ENV_PATH, "REDTEAM_LLM_PROVIDER", llm_config.provider)
    os.environ["REDTEAM_LLM_PROVIDER"] = llm_config.provider
    write_env_var(DEFAULT_ENV_PATH, "REDTEAM_LLM_MODEL", llm_config.default_model)
    os.environ["REDTEAM_LLM_MODEL"] = llm_config.default_model
    console.print(f"[dim]Wrote REDTEAM_LLM_PROVIDER to {DEFAULT_ENV_PATH}.[/dim]")

    orchestration_config = OrchestrationConfig.from_environment()
    capability_config = CapabilityConfig(mcp_servers=mcp_server_ids_from_environment())
    logging_config = LoggingConfig()
    return llm_config, orchestration_config, capability_config, logging_config


# Every tool `recon.py`'s `recon-passive` server discovers (servers/recon.py's
# `@server.tool()` list) must appear here -- `build_mcp_tool_gateway` rejects
# a binding whose keys don't exactly match the server's discovered tool
# names. Same canonical operations `deploy/mcp/finance/framework.env.example`
# already uses for this server in production, not a one-off invention.
_RECON_PASSIVE_TOOL_OPERATIONS = {
    "dns_resolve": ["service_discovery"],
    "http_probe": ["service_discovery"],
    "tls_inspect": ["service_discovery"],
    "openapi_inventory": ["endpoint_discovery"],
    "oidc_inventory": ["endpoint_discovery"],
    "html_inventory": ["endpoint_discovery"],
}


async def _ensure_mcp_config(
    console: Console, engagement_config: EngagementConfig, capability_config: CapabilityConfig
) -> CapabilityConfig:
    """One-time, opt-in MCP setup for a brand-new environment, persisted to
    ``.env`` exactly like ``_ensure_runtime_config``'s LLM prompt.

    ``REDTEAM_MCP_SERVERS`` already set means an operator already made this
    choice -- never overridden or re-prompted. With nothing set and no
    scope to point at yet either, this stays a no-op: `run`/`tools` already
    print an honest "no MCP servers configured" warning for that case, and
    there is nothing safe to default to without a real host.

    When it *can* offer something, it only ever offers the single
    lowest-risk, read-only capability (``recon-passive``, risk=low) against
    the first declared ``allowed_hosts`` entry -- never multiple servers,
    never a guess at write-capable/high-risk tooling -- and only after an
    explicit yes. Declining leaves ``capability_config`` exactly as it was.
    """
    if os.environ.get("REDTEAM_MCP_SERVERS"):
        return capability_config
    if not engagement_config.scope.allowed_hosts:
        return capability_config

    host = engagement_config.scope.allowed_hosts[0].strip().lower()
    console.print(
        "\n[dim]No REDTEAM_MCP_SERVERS set -- no MCP server is attached yet, "
        "so no Action can execute.[/dim]"
    )
    answer = (
        console.input(
            f"Attach the local, read-only recon-passive MCP server against {host!r} now? [Y/n]: "
        )
        .strip()
        .lower()
    )
    if answer not in ("", "y", "yes"):
        return capability_config

    try:
        policy_path = _write_recon_policy(host)
    except (OSError, ValueError) as exc:
        console.print(
            f"[yellow]{renderer.theme.ICON_WARNING} could not auto-configure MCP: {exc}[/yellow]"
        )
        return capability_config

    settings = {
        "KIND": "recon",
        "TRANSPORT": "stdio",
        "COMMAND": sys.executable,
        "ARGS_JSON": json.dumps(
            ["-m", "redteam.capability.servers", "recon-passive", "--policy", str(policy_path)]
        ),
        "RISK_LEVEL": "low",
        # Without this, every discovered tool falls back to its own literal
        # name as its "operation" (see `capability.bootstrap`) instead of a
        # canonical one (`service_discovery`/`endpoint_discovery`) -- the
        # Recon Agent's candidates ask for the canonical operation, so a
        # missing binding here means every candidate is rejected as
        # `operation_unsupported` and the Mission blocks with zero actions.
        "TOOL_OPERATIONS_JSON": json.dumps(_RECON_PASSIVE_TOOL_OPERATIONS),
    }
    write_env_var(DEFAULT_ENV_PATH, "REDTEAM_MCP_SERVERS", "recon-passive")
    os.environ["REDTEAM_MCP_SERVERS"] = "recon-passive"
    for suffix, value in settings.items():
        key = f"REDTEAM_MCP_RECON_PASSIVE_{suffix}"
        write_env_var(DEFAULT_ENV_PATH, key, value)
        os.environ[key] = value
    console.print(
        f"[dim]Wrote recon-passive MCP settings to {DEFAULT_ENV_PATH} "
        f"(policy: {policy_path}). Other hosts/servers can be added the same "
        f"way -- see .env.example.[/dim]"
    )
    return CapabilityConfig(mcp_servers=["recon-passive"])


def _write_recon_policy(host: str) -> Path:
    """A minimal, tightly-scoped ``recon-passive`` policy for exactly one
    host: read-only paths, and ``allowed_networks`` pinned to that host's
    own resolved addresses (a /32 or /128 each, not an open range) --
    never a broader default than the single host the operator just
    declared in scope.

    Resolves via ``socket.getaddrinfo`` -- the same dual-stack lookup
    ``servers/network.py``'s ``resolve_target`` actually validates every
    real request against, and which requires *every* resolved address to
    fall inside an allowed network. ``socket.gethostbyname`` (IPv4-only)
    previously missed a host's IPv6 address -- "localhost" resolves to
    both ``127.0.0.1`` and ``::1`` on Windows -- so a real, reachable
    request to that same host still failed "target address outside
    configured networks" the moment resolution returned the IPv6 entry.
    """
    hostname = host.rsplit(":", 1)[0] if ":" in host and not host.startswith("[") else host
    port_text = host.rsplit(":", 1)[1] if ":" in host and not host.startswith("[") else None
    port = int(port_text) if port_text else 80
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"could not resolve {hostname!r}: {exc}") from exc
    addresses = sorted({str(info[4][0]) for info in infos})
    if not addresses:
        raise ValueError(f"could not resolve {hostname!r} to any address")
    networks = [
        f"{address}/32" if ":" not in address else f"{address}/128" for address in addresses
    ]

    policy = {
        "targets": {host: {"origin": f"http://{host}", "allowed_paths": ["/"]}},
        "allowed_networks": networks,
    }
    path = Path("mcp-recon-policy.json")
    path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    return path.resolve()


# --- Engagement Intake (sections 9, 12) ------------------------------------


def _build_intake_assistant(llm_client: LLMClient) -> IntakeAssistant:
    """Use the configured model for structured intake in every environment."""
    return IntakeAssistant(llm_client)


async def _new_engagement(console: Console, llm_client: LLMClient) -> EngagementConfig | None:
    assistant = _build_intake_assistant(llm_client)
    session = IntakeSession()
    first_message = console.input("[cyan]>[/cyan] 어떤 대상을 테스트하려고 하나요? ")
    if not first_message.strip():
        return None
    return await _intake_loop(console, assistant, session, first_message)


async def _edit_engagement(
    console: Console, llm_client: LLMClient, existing: EngagementConfig
) -> EngagementConfig | None:
    try:
        draft = engagement_config_to_draft(existing)
    except UnsafeEditError as exc:
        console.print(f"\n[red]{exc}[/red]")
        return None

    assistant = _build_intake_assistant(llm_client)
    session = IntakeSession(draft=draft, editing=True)
    message = console.input("[cyan]>[/cyan] 뭘 바꿀까요? ")
    if not message.strip():
        return None

    before = draft.model_copy(deep=True)
    updated = await _intake_loop(console, assistant, session, message)
    if updated is None:
        return None

    diff = scope_diff(before, session.draft)
    if diff:
        console.print("\n[bold]Scope changes[/bold]")
        for field_name, (added, removed) in diff.items():
            for host in added:
                console.print(f"  [green]+[/green] {field_name}: {host}")
            for host in removed:
                console.print(f"  [red]-[/red] {field_name}: {host}")
    return updated


async def _intake_loop(
    console: Console,
    assistant: IntakeAssistant | None,
    session: IntakeSession,
    first_message: str,
) -> EngagementConfig | None:
    message = first_message
    while True:
        if assistant is None:
            # No real model configured (see _build_intake_assistant) -- go
            # straight to the deterministic parser every turn instead of
            # round-tripping a canned, unhelpful structured response.
            turn = deterministic_intake_turn(session.draft, sanitize_objective(message))
        else:
            try:
                turn = await assistant.step(session, sanitize_objective(message))
            except IntakeAssistantError as exc:
                if session.editing:
                    console.print(f"[red]{renderer.theme.ICON_FAILURE} {exc}[/red]")
                    return None
                turn = deterministic_intake_turn(session.draft, sanitize_objective(message))

        session.draft = merge_draft(session.draft, turn.draft)
        if turn.acknowledgement:
            console.print(f"\n{turn.acknowledgement}")

        missing = missing_required_fields(session.draft)
        if not missing and turn.ready:
            break

        questions = list(turn.follow_up_questions)
        if not questions and missing:
            questions = [f"아직 확인이 필요합니다: {', '.join(missing)}"]
        if not questions:
            # The model thinks it's done but the code-side check disagrees
            # with nothing to ask -- surface the gap plainly and stop
            # looping forever (section 6: code decides, not the model).
            questions = [f"확인이 더 필요합니다: {', '.join(missing) or 'unspecified'}"]

        console.print("\n테스트 범위를 조금 더 확인하겠습니다.\n")
        # Recheck the draft after each answer; ask only the next missing item.
        console.print(f"  1. {questions[0]}")
        message = console.input("\n[cyan]>[/cyan] ")
        if not message.strip() or message.strip().lower() in {"q", "quit"}:
            return None

    try:
        result = draft_to_engagement_config(session.draft)
    except DraftIncompleteError as exc:
        console.print(f"[red]{exc}[/red]")
        return None

    if result.unenforced_operations:
        console.print(
            f"\n[yellow]{renderer.theme.ICON_WARNING} Documented only, not enforced: "
            f"{', '.join(result.unenforced_operations)}[/yellow]"
        )
    return result.engagement_config


# --- Existing-config menu (section 11) -------------------------------------


async def _existing_config_menu(
    console: Console, engagement_config: EngagementConfig, llm_client: LLMClient
) -> tuple[EngagementConfig, str | None] | None:
    while True:
        renderer.render_engagement_panel(
            console,
            title="Existing Engagement",
            name=engagement_config.name,
            scope=_scope_summary(engagement_config),
            budget=f"{engagement_config.execution_budget.max_actions} actions",
            focus=" · ".join(engagement_config.metadata.get("focus_areas", [])) or None,
        )
        choice = renderer.prompt_choice(
            console,
            "",
            "[ Enter ] Start   [ E ] Edit   [ N ] New   [ Q ] Quit\nOr type a Mission objective:",
            normalize=False,
        )
        command = choice.lower()
        if command in ("", "start", "s"):
            return engagement_config, None
        if command in ("e", "edit"):
            updated = await _edit_engagement(console, llm_client, engagement_config)
            if updated is not None:
                engagement_config = updated
                _write_engagement(DEFAULT_ENGAGEMENT_PATH, engagement_config)
            continue
        if command in ("n", "new"):
            fresh = await _new_engagement(console, llm_client)
            if fresh is not None:
                engagement_config = fresh
                _write_engagement(DEFAULT_ENGAGEMENT_PATH, engagement_config)
            continue
        if command in ("q", "quit"):
            return None
        return engagement_config, choice


# --- Preflight (section 10, 21) --------------------------------------------


async def _preflight_loop(
    console: Console,
    engagement_config: EngagementConfig,
    llm_client: LLMClient,
    llm_config: LLMConfig,
    capability_config: CapabilityConfig,
    *,
    auto_start: bool = False,
) -> EngagementConfig | None:
    while True:
        scope_problems = check_scope(engagement_config)
        roe_problems = check_roe(engagement_config)
        capability_problems = check_capability(
            AppConfig(engagement=engagement_config, capability=capability_config)
        )
        problems = [*scope_problems, *roe_problems, *capability_problems]

        renderer.render_engagement_panel(
            console,
            title="Engagement",
            name=engagement_config.name,
            scope=_scope_summary(engagement_config),
            budget=f"{engagement_config.execution_budget.max_actions} actions",
            focus=" · ".join(engagement_config.metadata.get("focus_areas", [])) or None,
        )
        rules = engagement_config.rules_of_engagement
        rule = rules[0] if rules else None
        renderer.render_roe_checklist(
            console,
            allowed=[cap.capitalize() for cap in (rule.allowed_capabilities if rule else [])]
            or ["Recon", "Web/API testing"],
            denied=[cap.capitalize() for cap in (rule.denied_capabilities if rule else [])],
            documented_only=[rule.notes] if rule and rule.notes else [],
        )
        runtime_checks: list[tuple[str, bool | None, str]] = [
            ("LLM", llm_client is not None, llm_config.default_model),
            ("Config", not problems, "Valid" if not problems else f"{len(problems)} problem(s)"),
            ("Scope", not scope_problems, "Valid" if not scope_problems else "Invalid"),
        ]
        if capability_config.mcp_servers:
            runtime_checks.append(
                (
                    "MCP",
                    None,
                    f"{len(capability_config.mcp_servers)} server(s) configured — 연결 확인 전",
                )
            )
        renderer.render_runtime_checklist(console, checks=runtime_checks)
        if not capability_config.mcp_servers:
            renderer.render_capability_warning(console)

        if problems:
            renderer.render_config_error(
                console,
                target=engagement_config.name,
                problem="; ".join(problems),
                suggested_fix="Choose [E] Edit to fix this through the assistant, "
                "or edit engagement.yaml directly.",
            )
            choice = renderer.prompt_choice(console, "", "[ E ] Edit   [ Q ] Quit")
            if choice in ("e", "edit"):
                updated = await _edit_engagement(console, llm_client, engagement_config)
                if updated is not None:
                    engagement_config = updated
                continue
            return None

        if auto_start:
            return engagement_config
        choice = renderer.prompt_choice(
            console, "Ready to start?", "[ Enter ] Start   [ E ] Edit   [ Q ] Quit"
        )
        if choice in ("", "start", "s"):
            return engagement_config
        if choice in ("e", "edit"):
            updated = await _edit_engagement(console, llm_client, engagement_config)
            if updated is not None:
                engagement_config = updated
            continue
        return None


def _scope_summary(engagement_config: EngagementConfig) -> str:
    scope = engagement_config.scope
    values = [*scope.allowed_hosts, *scope.allowed_domains, *scope.allowed_networks]
    return ", ".join(values) if values else "(none)"


def _write_engagement(path: Path, engagement_config: EngagementConfig) -> None:
    path.write_text(dump_engagement_yaml(engagement_config), encoding="utf-8")


# --- Execution (section 18/19, via commands.run's shared core) ------------


async def _execute(
    console: Console,
    full_config: AppConfig,
    *,
    verbose: bool,
    objective: str | None = None,
    conversation: ConversationAssistant | None = None,
    prepared_decision: ConversationDecision | None = None,
    user_message: str | None = None,
) -> tuple[int, RuntimeState | None]:
    """Runs one Engagement to a terminal/suspended state and returns
    ``(exit_code, final_runtime_state)``.

    The ``RuntimeState`` is returned (not just the exit code) so a caller
    that keeps the CLI process alive after this -- see ``_session_loop``,
    section 4/19: ``RunFinished`` ends the *Run*, not the CLI Session --
    can project it into ``InteractiveSession`` (``run_id``, phase,
    termination reason) for ``/status``/``/findings`` to default against,
    without re-deriving it from event-renderer side effects.
    """
    async with AsyncExitStack() as stack:
        return await _execute_scoped(
            console,
            full_config,
            verbose=verbose,
            mcp_stack=stack,
            objective=objective,
            conversation=conversation,
            prepared_decision=prepared_decision,
            user_message=user_message,
        )


async def _execute_scoped(
    console: Console,
    full_config: AppConfig,
    *,
    verbose: bool,
    mcp_stack: AsyncExitStack,
    objective: str | None = None,
    conversation: ConversationAssistant | None = None,
    prepared_decision: ConversationDecision | None = None,
    user_message: str | None = None,
) -> tuple[int, RuntimeState | None]:
    with console.status("Starting engagement...", spinner="dots"):
        try:
            engagement, deps = await run_command.build(
                full_config, max_steps=None, mcp_stack=mcp_stack
            )
        except MCPConfigurationError as exc:
            renderer.render_config_error(
                console,
                target=full_config.engagement.name,
                problem=f"MCP configuration invalid: {exc}",
                suggested_fix="Fix the REDTEAM_MCP_<ID>_* environment variables for the "
                "configured server(s).",
            )
            return EXIT_CONFIG_ERROR, None
        except Exception as exc:  # noqa: BLE001 - fail closed with a readable message
            if logging.getLogger().isEnabledFor(logging.DEBUG):
                logging.exception("Application bootstrap failed")
            renderer.render_config_error(
                console,
                target=full_config.engagement.name,
                problem=f"failed to start application: {exc}",
                suggested_fix=None,
            )
            return EXIT_FAILURE, None

    shown_message = sanitize_objective(user_message or objective or "")
    effective_objective = sanitize_objective(objective or "").strip() or (
        f"Assess the declared scope of {full_config.engagement.name}"
    )
    if conversation is not None and (user_message or objective):
        # Initial objective normalization through the dialogue model. Only
        # an execute decision proceeds; answer/clarify/guidance are displayed
        # and stop with no execution (prefer_execute is only a wording hint
        # inside decide, never a mode conversion).
        scope = _scope_values(full_config.engagement)
        decision = prepared_decision
        try:
            if decision is None:
                with console.status("Understanding your request...", spinner="dots"):
                    decision = await conversation.decide(
                        user_message or objective or "",
                        engagement_name=full_config.engagement.name,
                        scope=scope,
                        tools=catalog_tool_labels(deps.tool_catalog),
                    )
            if shown_message:
                renderer.render_user_message(console, shown_message)
            renderer.render_conversation_decision(console, decision)
            clarification_count = 0
            while decision.mode == "clarify" and clarification_count < 2:
                try:
                    reply = sanitize_objective(console.input("\n[cyan]operator>[/cyan] ")).strip()
                except EOFError:
                    console.print("[dim]No action started; more detail is required.[/dim]")
                    return EXIT_OK, None
                if not reply:
                    console.print("[dim]No action started; more detail is required.[/dim]")
                    return EXIT_OK, None
                renderer.render_user_message(console, reply)
                clarification_count += 1
                with console.status("Updating the plan...", spinner="dots"):
                    decision = await conversation.decide(
                        f"Clarification for the current request: {reply}",
                        engagement_name=full_config.engagement.name,
                        scope=scope,
                        tools=catalog_tool_labels(deps.tool_catalog),
                    )
                renderer.render_conversation_decision(console, decision)
            if decision.mode == "clarify":
                console.print(
                    "[dim]No action started; the request still needs clarification.[/dim]"
                )
                return EXIT_OK, None
            if decision.mode != "execute":
                return EXIT_OK, None
            effective_objective = decision.objective or effective_objective
        except asyncio.CancelledError:
            raise
        except ConversationError as exc:
            logging.getLogger(__name__).warning(
                "Initial conversation decision failed (%s)", type(exc).__name__
            )
            console.print(
                f"[red]Could not interpret the request ({type(exc).__name__}); "
                "no action started.[/red]"
            )
            return EXIT_FAILURE, None
        except Exception as exc:  # noqa: BLE001 - fail closed, no execution
            logging.getLogger(__name__).warning(
                "Initial conversation decision failed (%s)", type(exc).__name__
            )
            console.print(
                f"[red]Could not interpret the request ({type(exc).__name__}); "
                "no action started.[/red]"
            )
            return EXIT_FAILURE, None
    else:
        console.print()
    event_renderer = renderer.RichEventRenderer(console, verbose=verbose)
    try:
        final = await run_command.execute(
            engagement,
            deps,
            config=full_config,
            non_interactive=False,
            on_event=event_renderer,
            echo=lambda line: console.print(line),
            objective=effective_objective,
            # PROMPT 4+5: keep a persistent operator channel live for the
            # whole run (pause/resume/stop/focus/exclude/status and in-session
            # approve/deny at the approval suspension). This is the
            # interactive entrypoint by definition; the explicit
            # `redteam run --non-interactive`/`--json` paths never set it.
            allow_operator_input=True,
            conversation=conversation,
        )
    except Exception as exc:  # noqa: BLE001 - same fail-closed contract as commands.run
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.exception("Run failed")
        console.print(f"\n[red]{renderer.theme.ICON_FAILURE} run failed: {exc}[/red]")
        return EXIT_FAILURE, None

    console.print()
    renderer.render_run_summary(
        console,
        actions_executed=final.actions_executed,
        findings=event_renderer.findings,
        hypotheses=event_renderer.hypotheses,
        tool_calls=event_renderer.tool_calls,
        violations=event_renderer.violations,
    )
    if final.mission_status is not None:
        console.print(f"Mission: {final.mission_status.value}")
    console.print(f"\n[dim]RunId: {final.run_id}[/dim]")
    if final.termination_reason not in {
        TerminationReason.OPERATOR_PAUSED,
        TerminationReason.APPROVAL_PENDING,
    }:
        console.print(f"[dim]Report: redteam report generate {final.run_id}[/dim]")
    if conversation is not None:
        # Deterministic summary from the actual terminal state (W1 makes
        # zero extra LLM calls here); displayed, never executed.
        answer = await conversation.final_response(final)
        renderer.render_final_answer(console, answer)
    exit_code = EXIT_FAILURE if final.phase.value == "failed" else EXIT_OK
    return exit_code, final


def _scope_values(engagement_config: EngagementConfig) -> list[str]:
    scope = engagement_config.scope
    return [
        *scope.allowed_hosts,
        *scope.allowed_domains,
        *scope.allowed_networks,
        *scope.allowed_assets,
    ]


def _render_conversation_decision(console: Console, decision: ConversationDecision) -> None:
    renderer.render_conversation_decision(console, decision)


async def _handle_idle_approval(
    console: Console, session: InteractiveSession, text: str, head: str
) -> None:
    """Resolve an idle approve/deny, then resume the same original run.

    The approval record itself is re-read from the durable stores (never
    the session cache): it must still be PENDING, its audit trail must
    bind it to this session's last run, and the cached identifier must
    match the freshly-read record. Unknown, already-resolved, unlinked,
    or stale approvals are only announced -- they never mutate and never
    start a new execution. A successful record change resumes through the
    canonical ``commands/resume`` recovery path with ``session.record_run``
    so the session projects the settled state.
    """
    console.print(renderer.operator_user_text(sanitize_objective(text)))
    pending = session.last_pending_approval_id
    if pending is None:
        console.print("[Agent] No action is awaiting approval.")
        return
    if session.last_run_id is None:
        console.print("[Agent] No run is active in this session; cannot resume an approval.")
        return
    approval_id = ApprovalId(pending)
    try:
        record = await ApprovalService(FilesystemApprovalStore("data/approvals")).get(approval_id)
    except Exception as exc:  # noqa: BLE001 - announce, never mutate
        logging.getLogger(__name__).warning("Idle approval lookup failed (%s)", type(exc).__name__)
        console.print("[Agent] Could not verify the pending approval; nothing changed.")
        return
    if record is None:
        console.print("[Agent] That approval no longer exists; nothing changed.")
        return
    if record.status != ApprovalStatus.PENDING:
        console.print(
            f"[Agent] Approval {pending} is already {record.status.value}; nothing changed."
        )
        return
    if str(record.approval_id) != pending:
        console.print("[Agent] The pending approval changed since the last run; nothing changed.")
        return
    try:
        request = await AuditLog(FilesystemAuditStore("data/audit")).find_approval_request(
            record.action_id, record.approval_id
        )
    except Exception as exc:  # noqa: BLE001 - announce, never mutate
        logging.getLogger(__name__).warning(
            "Idle approval audit lookup failed (%s)", type(exc).__name__
        )
        console.print(
            "[Agent] Could not verify which run this approval belongs to; nothing changed."
        )
        return
    if request is None or str(request.run_id) != str(session.last_run_id):
        console.print(
            "[Agent] That approval does not belong to this session's last run; nothing changed."
        )
        return
    command = (
        approvals_command.run_approve if head in {"approve", "승인"} else approvals_command.run_deny
    )
    resolve_code = await command(
        argparse.Namespace(
            approval_id=pending,
            resolved_by=None,
            approval_dir="data/approvals",
            audit_dir="data/audit",
        )
    )
    if resolve_code != EXIT_OK:
        return
    await resume_command.run(
        argparse.Namespace(
            run_id=str(session.last_run_id),
            engagement_path=str(session.engagement_path),
            max_steps=None,
            log_level=None,
            json=False,
        ),
        on_finished=session.record_run,
    )


async def _session_loop(
    console: Console,
    session: InteractiveSession,
    *,
    llm_config: LLMConfig,
    orchestration_config: OrchestrationConfig,
    capability_config: CapabilityConfig,
    logging_config: LoggingConfig,
    verbose: bool,
    conversation: ConversationAssistant | None = None,
) -> int:
    """Compatibility entrypoint; all subsequent input uses the session controller."""
    from redteam.app.cli.session_runtime import run_session

    conversation = conversation or ConversationAssistant(build_llm_client(llm_config))
    result = await run_session(
        console,
        AppConfig(
            engagement=session.engagement_config,
            llm=llm_config,
            orchestration=orchestration_config,
            capability=capability_config,
            logging=logging_config,
        ),
        session=session,
        conversation=conversation,
        verbose=verbose,
    )
    return result.exit_code
