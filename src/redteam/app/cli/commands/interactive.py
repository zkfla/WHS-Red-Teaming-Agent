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
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path

import yaml
from rich.console import Console

from redteam.app.bootstrap import build_llm_client
from redteam.app.cli import renderer
from redteam.app.cli._shared import (
    DEFAULT_APP_CONFIG_PATH,
    DEFAULT_ENGAGEMENT_PATH,
    DEFAULT_ENV_PATH,
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_OK,
    env_file_is_gitignored,
    prompt_secret,
    write_env_var,
)
from redteam.app.cli.commands import run as run_command
from redteam.app.cli.commands.validate import check_capability, check_roe, check_scope
from redteam.capability.settings import MCPConfigurationError
from redteam.core.config import (
    AppConfig,
    CapabilityConfig,
    EngagementConfig,
    LLMConfig,
    LoggingConfig,
    OrchestrationConfig,
    load_engagement_config,
)
from redteam.core.ports.llm import LLMClient
from redteam.intake.assistant import IntakeAssistant, IntakeAssistantError, IntakeSession
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
        # command is `redteam run <engagement> --config <app>`.
        console.print(
            "\n[dim]No input available -- this is the interactive setup. "
            "Non-interactive environments should use "
            "`redteam run <engagement.yaml> --config <app.yaml> --non-interactive` "
            "instead.[/dim]"
        )
        return EXIT_CONFIG_ERROR
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/dim]")
        raise


async def _run(console: Console, *, verbose: bool) -> int:
    llm_config, orchestration_config, capability_config, logging_config = (
        await _ensure_runtime_config(console, DEFAULT_APP_CONFIG_PATH)
    )
    llm_client = build_llm_client(llm_config)

    if not DEFAULT_ENGAGEMENT_PATH.exists():
        console.print("\nNo engagement configuration found.\n")
        console.print("Starting engagement setup.\n")
        engagement_config = await _new_engagement(console, llm_client)
        if engagement_config is None:
            return EXIT_OK
        _write_engagement(DEFAULT_ENGAGEMENT_PATH, engagement_config)
    else:
        engagement_config = load_engagement_config(DEFAULT_ENGAGEMENT_PATH)

    engagement_config = await _existing_config_menu(console, engagement_config, llm_client)
    if engagement_config is None:
        return EXIT_OK

    engagement_config = await _preflight_loop(
        console, engagement_config, llm_client, llm_config, capability_config
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
    return await _execute(console, full_config, verbose=verbose)


# --- runtime (LLM/capability) config: a short one-time prompt, never an ----
# --- Intake Assistant conversation (section 3/9 -- see the design note ----
# --- in the completion report on why this is not itself LLM-driven). ------


async def _ensure_runtime_config(
    console: Console, path: Path
) -> tuple[LLMConfig, OrchestrationConfig, CapabilityConfig, LoggingConfig]:
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return (
            LLMConfig.model_validate(raw.get("llm", {})),
            OrchestrationConfig.model_validate(raw.get("orchestration", {})),
            CapabilityConfig.model_validate(raw.get("capability", {})),
            LoggingConfig.model_validate(raw.get("logging", {})),
        )

    console.print(f"\n[dim]No {path} found -- one-time runtime setup.[/dim]")
    model = console.input(
        "LLM model (e.g. chatgpt/gpt-5.5, gpt-4o-mini) [blank = offline/fake]: "
    ).strip()
    llm_config = LLMConfig(provider="fake") if not model else LLMConfig(
        provider="litellm", default_model=model
    )
    if llm_config.provider == "litellm" and not llm_config.resolve_api_key():
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

    orchestration_config = OrchestrationConfig()
    capability_config = CapabilityConfig()
    logging_config = LoggingConfig()
    path.write_text(
        yaml.safe_dump(
            {
                "llm": llm_config.model_dump(),
                "orchestration": orchestration_config.model_dump(),
                "capability": capability_config.model_dump(),
                "logging": logging_config.model_dump(),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    console.print(f"[dim]Wrote {path}.[/dim]")
    return llm_config, orchestration_config, capability_config, logging_config


# --- Engagement Intake (sections 9, 12) ------------------------------------


async def _new_engagement(console: Console, llm_client: LLMClient) -> EngagementConfig | None:
    assistant = IntakeAssistant(llm_client)
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

    assistant = IntakeAssistant(llm_client)
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
    console: Console, assistant: IntakeAssistant, session: IntakeSession, first_message: str
) -> EngagementConfig | None:
    message = first_message
    while True:
        try:
            turn = await assistant.step(session, message)
        except IntakeAssistantError as exc:
            console.print(f"[red]{renderer.theme.ICON_FAILURE} {exc}[/red]")
            message = console.input("[cyan]>[/cyan] ")
            if not message.strip():
                return None
            continue

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
        for index, question in enumerate(questions, start=1):
            console.print(f"  {index}. {question}")
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
) -> EngagementConfig | None:
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
            console, "", "[ Enter ] Start   [ E ] Edit   [ N ] New   [ Q ] Quit"
        )
        if choice in ("", "start", "s"):
            return engagement_config
        if choice in ("e", "edit"):
            updated = await _edit_engagement(console, llm_client, engagement_config)
            if updated is not None:
                engagement_config = updated
                _write_engagement(DEFAULT_ENGAGEMENT_PATH, engagement_config)
            continue
        if choice in ("n", "new"):
            fresh = await _new_engagement(console, llm_client)
            if fresh is not None:
                engagement_config = fresh
                _write_engagement(DEFAULT_ENGAGEMENT_PATH, engagement_config)
            continue
        return None


# --- Preflight (section 10, 21) --------------------------------------------


async def _preflight_loop(
    console: Console,
    engagement_config: EngagementConfig,
    llm_client: LLMClient,
    llm_config: LLMConfig,
    capability_config: CapabilityConfig,
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
        runtime_checks = [
            ("LLM", llm_client is not None, llm_config.default_model),
            ("Config", not problems, "Valid" if not problems else f"{len(problems)} problem(s)"),
            ("Scope", not scope_problems, "Valid" if not scope_problems else "Invalid"),
        ]
        if capability_config.mcp_servers:
            runtime_checks.append(
                ("MCP", True, f"{len(capability_config.mcp_servers)} server(s) configured")
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
                "or edit engagement.yaml/app.yaml directly.",
            )
            choice = renderer.prompt_choice(console, "", "[ E ] Edit   [ Q ] Quit")
            if choice in ("e", "edit"):
                updated = await _edit_engagement(console, llm_client, engagement_config)
                if updated is not None:
                    engagement_config = updated
                continue
            return None

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


async def _execute(console: Console, full_config: AppConfig, *, verbose: bool) -> int:
    async with AsyncExitStack() as stack:
        return await _execute_scoped(console, full_config, verbose=verbose, mcp_stack=stack)


async def _execute_scoped(
    console: Console, full_config: AppConfig, *, verbose: bool, mcp_stack: AsyncExitStack
) -> int:
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
            return EXIT_CONFIG_ERROR
        except Exception as exc:  # noqa: BLE001 - fail closed with a readable message
            if logging.getLogger().isEnabledFor(logging.DEBUG):
                logging.exception("Application bootstrap failed")
            renderer.render_config_error(
                console,
                target=full_config.engagement.name,
                problem=f"failed to start application: {exc}",
                suggested_fix=None,
            )
            return EXIT_FAILURE

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
        )
    except Exception as exc:  # noqa: BLE001 - same fail-closed contract as commands.run
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.exception("Run failed")
        console.print(f"\n[red]{renderer.theme.ICON_FAILURE} run failed: {exc}[/red]")
        return EXIT_FAILURE

    console.print()
    renderer.render_run_summary(
        console,
        actions_executed=final.actions_executed,
        findings=event_renderer.findings,
        hypotheses=event_renderer.hypotheses,
        tool_calls=event_renderer.tool_calls,
        violations=event_renderer.violations,
    )
    console.print(f"\n[dim]RunId: {final.run_id}[/dim]")
    console.print(f"[dim]Report: redteam report generate {final.run_id}[/dim]")
    return EXIT_FAILURE if final.phase.value == "failed" else EXIT_OK
