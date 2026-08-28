"""Command-line interface for configuration and policy checks."""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

import typer

from whs_redteam.audit import AuditLogger
from whs_redteam.config import load_config
from whs_redteam.exceptions import ConfigurationError
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import AgentRunStatus, ApprovalClaims, Capability
from whs_redteam.orchestration.approvals import sign_approval
from whs_redteam.planning import RulesPlanner
from whs_redteam.policy.budget import RequestBudget
from whs_redteam.policy.scope import ScopeGuard
from whs_redteam.run_service import RunService
from whs_redteam.tests_catalog import AssessmentRunner
from whs_redteam.tools.browser import WebExplorer
from whs_redteam.tools.http_explorer import HttpExplorer
from whs_redteam.tools.network_gate import NetworkGate
from whs_redteam.validation import FindingValidator

app = typer.Typer(
    name="whs-redteam",
    help="Safety-first web red-teaming prototype.",
    no_args_is_help=True,
)


def _read_token_file(token_file: Path) -> str:
    if token_file.stat().st_mode & 0o077:
        raise ValueError("approval token file permissions must be 0600 or stricter")
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("approval token file is empty")
    return token


@app.command("validate-config")
def validate_config(config_path: Path) -> None:
    """Validate an engagement YAML file without sending network requests."""

    try:
        project = load_config(config_path)
    except ConfigurationError as exc:
        typer.echo(f"Invalid configuration: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    engagement = project.engagement
    typer.echo(
        json.dumps(
            {
                "valid": True,
                "name": engagement.name,
                "targets": engagement.targets,
                "allowed_methods": sorted(engagement.allowed_methods),
                "max_requests": engagement.max_requests,
                "max_runtime_minutes": engagement.max_runtime_minutes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("check-request")
def check_request(
    config_path: Path,
    url: str,
    method: str = typer.Option("GET", "--method", "-X"),
) -> None:
    """Evaluate a URL against ScopeGuard without making a request."""

    try:
        project = load_config(config_path)
    except ConfigurationError as exc:
        typer.echo(f"Invalid configuration: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    engagement = project.engagement
    logger = AuditLogger(engagement.audit_log_path)
    decision = ScopeGuard(engagement, logger).evaluate(url, method)
    typer.echo(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    if not decision.allowed:
        raise typer.Exit(code=3)


@app.command("explore")
def explore(config_path: Path) -> None:
    """Explore approved pages and save a passive web inventory."""

    try:
        project = load_config(config_path)
    except ConfigurationError as exc:
        typer.echo(f"Invalid configuration: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    engagement = project.engagement
    logger = AuditLogger(engagement.audit_log_path)
    guard = ScopeGuard(engagement, logger)
    budget = RequestBudget.from_config(engagement, logger)
    gate = NetworkGate(guard, budget, logger)
    try:
        with InventoryStore(engagement.inventory_db_path) as inventory:
            explorer = (
                HttpExplorer(engagement, inventory, gate, logger)
                if engagement.discovery_mode == "http"
                else WebExplorer(engagement, inventory, gate, logger)
            )
            summary = asyncio.run(explorer.explore())
    except Exception as exc:
        typer.echo(f"Exploration failed: {exc}", err=True)
        raise typer.Exit(code=4) from exc

    typer.echo(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2))


@app.command("assess")
def assess(
    config_path: Path,
    include_inventory: bool = typer.Option(
        False,
        "--include-inventory",
        help="Also assess safe pages from the latest exploration run.",
    ),
) -> None:
    """Run deterministic Day 3 checks against approved seed targets."""

    try:
        project = load_config(config_path)
    except ConfigurationError as exc:
        typer.echo(f"Invalid configuration: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    engagement = project.engagement
    logger = AuditLogger(engagement.audit_log_path)
    guard = ScopeGuard(engagement, logger)
    budget = RequestBudget.from_config(engagement, logger)
    gate = NetworkGate(guard, budget, logger)
    try:
        with InventoryStore(engagement.inventory_db_path) as inventory:
            targets = list(engagement.targets)
            if include_inventory:
                for candidate in inventory.latest_page_urls(engagement.max_pages):
                    if guard.evaluate(candidate, "GET").allowed:
                        targets.append(candidate)
            summary = asyncio.run(
                AssessmentRunner(engagement, inventory, gate, logger).run(targets)
            )
            observations = inventory.list_observations(summary.run_id)
    except Exception as exc:
        typer.echo(f"Assessment failed: {exc}", err=True)
        raise typer.Exit(code=5) from exc

    output_path = engagement.inventory_db_path.parent / f"observations-{summary.run_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(observations, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result = {
        **summary.model_dump(mode="json"),
        "observations_path": str(output_path),
    }
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("validate-findings")
def validate_findings(
    config_path: Path,
    include_inventory: bool = typer.Option(
        False,
        "--include-inventory",
        help="Also validate safe pages from the latest exploration run.",
    ),
) -> None:
    """Plan, reproduce, control-check, deduplicate, and promote findings."""

    try:
        project = load_config(config_path)
    except ConfigurationError as exc:
        typer.echo(f"Invalid configuration: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    engagement = project.engagement
    logger = AuditLogger(engagement.audit_log_path)
    guard = ScopeGuard(engagement, logger)
    try:
        with InventoryStore(engagement.inventory_db_path) as inventory:
            targets = list(engagement.targets)
            if include_inventory:
                for candidate in inventory.latest_page_urls(engagement.max_pages):
                    if guard.evaluate(candidate, "GET").allowed:
                        targets.append(candidate)
            plan = RulesPlanner(engagement).plan(targets)
            validator = FindingValidator(
                engagement,
                inventory,
                guard,
                logger,
            )
            summary, findings = asyncio.run(validator.validate(plan))
    except Exception as exc:
        typer.echo(f"Finding validation failed: {exc}", err=True)
        raise typer.Exit(code=6) from exc

    output_path = engagement.inventory_db_path.parent / f"findings-{summary.validation_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            [finding.model_dump(mode="json") for finding in findings],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    summary = summary.model_copy(update={"findings_path": str(output_path)})
    typer.echo(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2))


@app.command("sqli-scan")
def sqli_scan(
    target_url: str,
    discovery_mode: Annotated[
        Literal["http", "browser"],
        typer.Option(
            "--discovery-mode",
            help="Use dependency-light HTTP discovery or Playwright browser discovery.",
        ),
    ] = "http",
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="Directory for the generated value-free profile and run artifacts.",
        ),
    ] = Path("outputs/sqli-scan"),
    follow_links: Annotated[
        bool,
        typer.Option(
            "--follow-links/--no-follow-links",
            help="Follow same-scope HTML links during discovery.",
        ),
    ] = False,
    approval_secret_env: Annotated[
        str,
        typer.Option(
            "--approval-secret-env",
            help="Environment variable name used to verify signed approvals.",
        ),
    ] = "WHS_APPROVAL_SECRET",
    run_id: Annotated[
        str | None,
        typer.Option(
            "--run-id",
            help="Resume the run created by an earlier invocation for this URL.",
        ),
    ] = None,
    token_file: Annotated[
        Path | None,
        typer.Option(
            "--token-file",
            help="Optional 0600 signed token; releases all waiting SQLi actions and resumes.",
        ),
    ] = None,
) -> None:
    """Run a SQLi-only scan from one URL and persist a reusable approval profile."""

    try:
        service = RunService.from_sqli_url(
            target_url,
            output_dir=output_dir,
            discovery_mode=discovery_mode,
            follow_discovered_links=follow_links,
            approval_secret_env=approval_secret_env,
        )
        result = asyncio.run(service.run(run_id))
        released = 0
        if token_file is not None and result.run.status is AgentRunStatus.WAITING_APPROVAL:
            token = _read_token_file(token_file)
            for action in service.pending_approvals(result.run.run_id):
                service.approve(result.run.run_id, str(action["queue_id"]), token)
                released += 1
            result = asyncio.run(service.run(result.run.run_id))
        payload = result.to_dict()
        payload["released_approvals"] = released
        if result.run.status is AgentRunStatus.WAITING_APPROVAL:
            payload["next_steps"] = [
                "Issue a signed token for sqli_safe using config_path.",
                "Approve the listed queue actions, then run this command again with --run-id.",
            ]
    except Exception as exc:
        typer.echo(f"SQLi scan failed: {exc}", err=True)
        raise typer.Exit(code=13) from exc
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("run")
def run_agent(config_path: Path) -> None:
    """Run the checkpointed discovery-to-report orchestration pipeline."""

    try:
        result = asyncio.run(RunService.from_config_path(config_path).run())
    except Exception as exc:
        typer.echo(f"Agent run failed: {exc}", err=True)
        raise typer.Exit(code=7) from exc
    typer.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


@app.command("resume")
def resume_agent(config_path: Path, run_id: str) -> None:
    """Resume an interrupted agent run from its persisted checkpoint."""

    try:
        result = asyncio.run(RunService.from_config_path(config_path).run(run_id))
    except Exception as exc:
        typer.echo(f"Agent resume failed: {exc}", err=True)
        raise typer.Exit(code=8) from exc
    typer.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


@app.command("status")
def agent_status(config_path: Path, run_id: str) -> None:
    """Show a persisted agent run without sending network requests."""

    try:
        result = RunService.from_config_path(config_path).status(run_id)
    except Exception as exc:
        typer.echo(f"Cannot read agent status: {exc}", err=True)
        raise typer.Exit(code=9) from exc
    typer.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


@app.command("pending-approvals")
def pending_approvals(config_path: Path, run_id: str) -> None:
    """List safe metadata for actions currently waiting for signed approval."""

    try:
        actions = RunService.from_config_path(config_path).pending_approvals(run_id)
    except Exception as exc:
        typer.echo(f"Cannot list approvals: {exc}", err=True)
        raise typer.Exit(code=11) from exc
    typer.echo(json.dumps({"run_id": run_id, "actions": actions}, indent=2))


@app.command("issue-approval")
def issue_approval(
    config_path: Path,
    output_path: Annotated[Path, typer.Option("--output")],
    capability: Annotated[list[Capability], typer.Option("--capability")],
    target: Annotated[list[str] | None, typer.Option("--target")] = None,
    issuer: str = typer.Option("local-prototype-operator", "--issuer"),
    valid_minutes: int = typer.Option(15, "--valid-minutes", min=1, max=120),
    max_uses: int = typer.Option(1, "--max-uses", min=1, max=100),
) -> None:
    """Issue a local prototype approval token into a mode-0600 file."""

    try:
        engagement = load_config(config_path).engagement
        secret_variable = engagement.approval_hmac_secret_env
        if secret_variable is None:
            raise ValueError("approval_hmac_secret_env is not configured")
        secret = os.environ.get(secret_variable)
        if secret is None:
            raise ValueError(
                f"approval secret environment variable is missing: {secret_variable}"
            )
        unsupported = {
            item.value for item in capability if item.value not in engagement.enabled_capabilities
        }
        if unsupported:
            raise ValueError(
                "capabilities are not enabled: " + ", ".join(sorted(unsupported))
            )
        now = datetime.now(UTC)
        claims = ApprovalClaims(
            approval_id=f"local-{now.strftime('%Y%m%d%H%M%S')}",
            issuer=issuer,
            engagement_name=engagement.name,
            capabilities=frozenset(capability),
            targets=tuple(target or engagement.targets),
            methods=frozenset({"GET"}),
            issued_at=now - timedelta(seconds=5),
            expires_at=now + timedelta(minutes=valid_minutes),
            max_uses=max_uses,
        )
        token = sign_approval(claims, secret.encode())
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(token, encoding="utf-8")
        output_path.chmod(0o600)
    except Exception as exc:
        typer.echo(f"Approval issue failed: {exc}", err=True)
        raise typer.Exit(code=12) from exc
    typer.echo(
        json.dumps(
            {
                "token_path": str(output_path),
                "capabilities": sorted(item.value for item in capability),
                "targets": list(claims.targets),
                "expires_at": claims.expires_at.isoformat(),
                "max_uses": claims.max_uses,
            },
            indent=2,
        )
    )


@app.command("approve")
def approve_action(
    config_path: Path,
    run_id: str,
    queue_id: str,
    token_env: Annotated[
        str | None,
        typer.Option(
            "--token-env",
            help="Environment variable containing the signed approval token.",
        ),
    ] = None,
    token_file: Annotated[
        Path | None,
        typer.Option(
            "--token-file",
            help="Mode-0600 file containing the signed approval token.",
        ),
    ] = None,
) -> None:
    """Verify a signed approval and release one waiting queued action."""

    try:
        if (token_env is None) == (token_file is None):
            raise ValueError("provide exactly one of --token-env or --token-file")
        if token_env is not None:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", token_env):
                raise ValueError("token environment variable name is invalid")
            token = os.environ.get(token_env)
            if token is None:
                raise ValueError(
                    f"approval token environment variable is missing: {token_env}"
                )
        else:
            assert token_file is not None
            token = _read_token_file(token_file)
        result = RunService.from_config_path(config_path).approve(
            run_id, queue_id, token
        )
    except Exception as exc:
        typer.echo(f"Approval failed: {exc}", err=True)
        raise typer.Exit(code=10) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
