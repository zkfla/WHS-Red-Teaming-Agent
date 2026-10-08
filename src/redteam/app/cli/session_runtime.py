"""CLI composition: existing application APIs, one persistent frontend."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import AsyncExitStack, redirect_stderr, redirect_stdout
from typing import Literal, cast

from redteam.app.cli import interactive_commands
from redteam.app.cli.commands.validate import check_roe, check_scope
from redteam.app.cli.conversation import ConversationAssistant
from redteam.app.cli.display import safe_text
from redteam.app.cli.session_controller import SessionController
from redteam.app.cli.session_types import RunResult
from redteam.app.cli.terminal_frontend import ConsoleFrontend, PlainFrontend, TerminalFrontend
from redteam.app.cli.view_models import SessionProjection
from redteam.core.config import load_engagement_config


def scope_values(config) -> list[str]:
    scope = config.engagement.scope
    return [
        *scope.allowed_hosts,
        *scope.allowed_domains,
        *scope.allowed_networks,
        *scope.allowed_assets,
    ]


async def run_session(
    console,
    config,
    *,
    conversation: ConversationAssistant,
    session,
    objective: str | None = None,
    verbose: bool = False,
    frontend=None,
    built=None,
    single_run: bool = False,
    component_registry=None,
    component_profile=None,
) -> RunResult:
    from redteam.app.cli.commands import run as run_command

    if config.specialist_profile is not None:
        from redteam.app.agent_profile import load_specialist_profile

        loaded = load_specialist_profile(config.specialist_profile)
        console.print(safe_text(
            f"Specialist profile: {loaded.profile.profile_id} · version {loaded.profile.version}"
        ))
        prompts = built[1].agent_prompts if built is not None else loaded.prompts
        conversation.bind_prompts(prompts)

    if frontend is None:
        if not sys.stdin.isatty():
            frontend = ConsoleFrontend(console)
        elif os.environ.get("REDTEAM_CLI_PLAIN") == "1" or os.environ.get("TERM") == "dumb":
            frontend = PlainFrontend(console)
        else:
            frontend = TerminalFrontend()

    async def runner(request, controller):
        current = config
        if (built is None or single_run) and not session.engagement_path.exists():
            return RunResult(exit_code=2, error="Engagement 설정 파일이 없어 실행하지 않았습니다.")
        if session.engagement_path.exists():
            refreshed = load_engagement_config(session.engagement_path)
            current = config.model_copy(update={"engagement": refreshed})
        if built is not None and current.engagement != config.engagement:
            return RunResult(
                exit_code=2, error="초기화 후 scope/설정이 바뀌어 실행하지 않았습니다."
            )
        problems = [*check_scope(current.engagement), *check_roe(current.engagement)]
        classified_config = controller.active_receipt.context_revision
        if classified_config is not None and classified_config != current.engagement:
            return RunResult(
                exit_code=2,
                error="분류 후 설정이 변경되어 실행하지 않았습니다. 새 요청으로 확인하세요.",
            )
        if problems:
            return RunResult(
                exit_code=2, error="Invalid engagement configuration: " + "; ".join(problems)
            )
        session.engagement_config = current.engagement
        controller.scope = scope_values(current)
        controller.engagement_name = current.engagement.name
        async with AsyncExitStack() as stack:
            # Optional profile wiring only: unset means the builtin default,
            # and existing build() test doubles keep working unpatched.
            build_options: dict = {}
            if component_profile is not None:
                build_options["component_profile"] = component_profile
            if component_registry is not None:
                build_options["component_registry"] = component_registry
            engagement, deps = built or await run_command.build(
                current, max_steps=None, mcp_stack=stack, **build_options
            )
            if config.specialist_profile is not None and (
                deps.component_binding is None
                or deps.component_binding.specialist_profile_fingerprint != loaded.fingerprint
            ):
                return RunResult(
                    exit_code=2,
                    error="세션 시작 후 전문 역할/프롬프트가 변경됐습니다. 새 세션으로 확인하세요.",
                )
            # Do not dispatch using a configuration changed during bootstrap.
            if (
                session.engagement_path.exists()
                and load_engagement_config(session.engagement_path) != current.engagement
            ):
                return RunResult(
                    exit_code=2,
                    error="설정이 변경되어 시작하지 않았습니다. 요청을 다시 확인하세요.",
                )
            projection = SessionProjection(controller, verbose=verbose)
            try:
                final = await run_command.execute(
                    engagement,
                    deps,
                    config=current,
                    non_interactive=False,
                    on_event=projection,
                    echo=controller.output.emit,
                    objective=request,
                    session_channel=controller,
                )
                return RunResult(final, 1 if final.phase.value == "failed" else 0)
            finally:
                projection.closed = True
                # execute() closes dispatch before this scope releases MCP/deps.
                await controller.detach()

    class QueuedConsole:
        def print(self, *items, **kwargs):
            from rich.text import Text

            for item in items:
                controller.output.emit(Text(safe_text(item)) if isinstance(item, str) else item)

        def clear(self):
            frontend.invalidate()

    async def slash(name, rest, controller):
        if name == "approvals" and controller.pending_approval:
            controller.output.emit(
                f"승인 대기: {controller.pending_approval} · approve <ID> / deny <ID>"
            )
            return
        await interactive_commands.dispatch(QueuedConsole(), session, f"/{name} {rest}")

    async def resume_runner(kind, approval_id, run_id, controller):
        from redteam.app.cli.commands import approvals, resume
        from redteam.control.approval import ApprovalService
        from redteam.control.audit import AuditLog
        from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
        from redteam.core.enums import ApprovalStatus

        previous = controller.final_result.final
        if kind in {"approve", "deny"}:
            record = await ApprovalService(FilesystemApprovalStore("data/approvals")).get(
                approval_id
            )
            audit = AuditLog(FilesystemAuditStore("data/audit"))
            request = (
                await audit.find_approval_request(record.action_id, record.approval_id)
                if record
                else None
            )
            if (
                record is None
                or record.status != ApprovalStatus.PENDING
                or request is None
                or str(request.run_id) != run_id
            ):
                return RunResult(
                    previous, 1, "승인 기록과 원래 Run을 확인할 수 없습니다. 변경하지 않았습니다."
                )
            resolver = approvals.run_approve if kind == "approve" else approvals.run_deny
            code = await resolver(
                argparse.Namespace(
                    approval_id=approval_id,
                    resolved_by=None,
                    approval_dir="data/approvals",
                    audit_dir="data/audit",
                )
            )
            if code:
                return RunResult(previous, code, "승인 응답 확인 실패 — 기록을 확인하세요.")
        finals = []
        projection = SessionProjection(controller, verbose=verbose)
        try:
            # Same-registry resume: only forwarded when set, so the
            # builtin-default path stays compatible with existing callers.
            resume_options: dict = {}
            if component_registry is not None:
                resume_options["component_registry"] = component_registry
            code = await resume.run(
                argparse.Namespace(
                    run_id=run_id,
                    engagement_path=str(session.engagement_path),
                    max_steps=None,
                    log_level=None,
                    json=False,
                ),
                on_finished=finals.append,
                session_channel=controller,
                on_event=projection,
                **resume_options,
            )
            return RunResult(
                finals[-1] if finals else previous,
                code,
                None if finals else "Run을 재개하지 못했습니다. checkpoint/승인 상태를 확인하세요.",
            )
        finally:
            projection.closed = True

    def refresh_context():
        if (built is None or single_run) and not session.engagement_path.exists():
            raise ValueError("Engagement 설정 파일이 없어 실행하지 않았습니다.")
        refreshed = (
            load_engagement_config(session.engagement_path)
            if session.engagement_path.exists()
            else config.engagement
        )
        problems = [*check_scope(refreshed), *check_roe(refreshed)]
        if problems:
            raise ValueError("Invalid engagement configuration: " + "; ".join(problems))
        session.engagement_config = refreshed
        controller.engagement_name = refreshed.name
        controller.scope = scope_values(config.model_copy(update={"engagement": refreshed}))
        return refreshed

    async def verify_approval(approval_id, run_id):
        from redteam.control.approval import ApprovalService
        from redteam.control.audit import AuditLog
        from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
        from redteam.core.enums import ApprovalStatus

        record = await ApprovalService(FilesystemApprovalStore("data/approvals")).get(approval_id)
        if record is None or record.status != ApprovalStatus.PENDING:
            return False
        request = await AuditLog(FilesystemAuditStore("data/audit")).find_approval_request(
            record.action_id, record.approval_id
        )
        return request is not None and str(request.run_id) == run_id

    controller = SessionController(
        console=console,
        frontend=frontend,
        conversation=conversation,
        runner=runner,
        engagement_name=config.engagement.name,
        scope=scope_values(config),
        on_finished=session.record_run,
        slash_handler=slash,
        resume_runner=resume_runner,
        context_loader=refresh_context,
        approval_verifier=verify_approval,
        commands=[c.name for c in interactive_commands.COMMANDS],
        command_help={c.name: c.description for c in interactive_commands.COMMANDS},
        model_label=config.llm.default_model,
        single_run=single_run,
        configured_servers=tuple(config.capability.mcp_servers),
    )
    # Legacy CLI commands and third-party logs still print. Redirect them once
    # at the session boundary, never around an individual concurrent command.
    # Freeze Rich's destination before replacing sys.stdout to avoid feedback.
    from rich.console import Console

    if isinstance(console, Console):
        controller.output.console = Console(
            file=console.file,
            width=console.width,
            color_system=cast(
                Literal["auto", "standard", "256", "truecolor", "windows"] | None,
                console.color_system,
            ),
        )

    class SessionStream:
        def __init__(self):
            self.pending = ""

        def write(self, text):
            self.pending += text
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                if line:
                    controller.output.emit(safe_text(line))
            if len(self.pending) > 12000:
                self.flush()
            return len(text)

        def flush(self):
            if self.pending:
                controller.output.emit(safe_text(self.pending))
                self.pending = ""

        def isatty(self):
            return False

    stream = SessionStream()

    class SessionLogHandler(logging.Handler):
        def emit(self, record):
            controller.output.emit(safe_text(self.format(record)))

    root_logger = logging.getLogger()
    old_handlers = root_logger.handlers[:]
    root_logger.handlers = [SessionLogHandler()]
    try:
        with redirect_stdout(stream), redirect_stderr(stream):
            return await controller.run(
                ("/new " + objective) if single_run and objective else objective
            )
    finally:
        root_logger.handlers = old_handlers
