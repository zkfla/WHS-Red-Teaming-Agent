"""Command-line interface for configuration and policy checks."""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from whs_redteam.audit import AuditLogger
from whs_redteam.config import load_config
from whs_redteam.exceptions import ConfigurationError
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import ActionProposal, ApprovalClaims, Capability, QueueStatus
from whs_redteam.orchestration import AgentOrchestrator
from whs_redteam.orchestration.approvals import ApprovalVerifier, sign_approval
from whs_redteam.orchestration.queue import ActionQueue
from whs_redteam.planning import RulesPlanner
from whs_redteam.policy.budget import RequestBudget
from whs_redteam.policy.scope import ScopeGuard
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


def _agent_result(
    run, queue_counts: dict[str, int] | None = None
) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "engagement_name": run.engagement_name,
        "status": run.status.value,
        "resume_status": run.resume_status.value if run.resume_status else None,
        "exploration_run_id": run.exploration_run_id,
        "validation_id": run.validation_id,
        "findings_path": run.findings_path,
        "last_error": run.last_error,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "queue": queue_counts or {},
    }


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


@app.command("run")
def run_agent(config_path: Path) -> None:
    """Run the checkpointed discovery-to-report orchestration pipeline."""

    try:
        engagement = load_config(config_path).engagement
        logger = AuditLogger(engagement.audit_log_path)
        with InventoryStore(engagement.inventory_db_path) as inventory:
            orchestrator = AgentOrchestrator(engagement, inventory, logger)
            run = asyncio.run(orchestrator.run())
            queue_counts = inventory.queue_counts(run.run_id)
    except Exception as exc:
        typer.echo(f"Agent run failed: {exc}", err=True)
        raise typer.Exit(code=7) from exc
    typer.echo(json.dumps(_agent_result(run, queue_counts), ensure_ascii=False, indent=2))


@app.command("resume")
def resume_agent(config_path: Path, run_id: str) -> None:
    """Resume an interrupted agent run from its persisted checkpoint."""

    try:
        engagement = load_config(config_path).engagement
        logger = AuditLogger(engagement.audit_log_path)
        with InventoryStore(engagement.inventory_db_path) as inventory:
            orchestrator = AgentOrchestrator(engagement, inventory, logger)
            run = asyncio.run(orchestrator.run(run_id))
            queue_counts = inventory.queue_counts(run.run_id)
    except Exception as exc:
        typer.echo(f"Agent resume failed: {exc}", err=True)
        raise typer.Exit(code=8) from exc
    typer.echo(json.dumps(_agent_result(run, queue_counts), ensure_ascii=False, indent=2))


@app.command("status")
def agent_status(config_path: Path, run_id: str) -> None:
    """Show a persisted agent run without sending network requests."""

    try:
        engagement = load_config(config_path).engagement
        logger = AuditLogger(engagement.audit_log_path)
        with InventoryStore(engagement.inventory_db_path) as inventory:
            run = AgentOrchestrator(engagement, inventory, logger).get(run_id)
            queue_counts = inventory.queue_counts(run.run_id)
    except Exception as exc:
        typer.echo(f"Cannot read agent status: {exc}", err=True)
        raise typer.Exit(code=9) from exc
    typer.echo(json.dumps(_agent_result(run, queue_counts), ensure_ascii=False, indent=2))


@app.command("pending-approvals")
def pending_approvals(config_path: Path, run_id: str) -> None:
    """List safe metadata for actions currently waiting for signed approval."""

    try:
        engagement = load_config(config_path).engagement
        logger = AuditLogger(engagement.audit_log_path)
        with InventoryStore(engagement.inventory_db_path) as inventory:
            AgentOrchestrator(engagement, inventory, logger).get(run_id)
            rows = inventory.connection.execute(
                """
                SELECT queue_id, proposal_json FROM action_queue
                WHERE run_id = ? AND status = ? ORDER BY priority DESC, created_at
                """,
                (run_id, QueueStatus.WAITING_APPROVAL.value),
            ).fetchall()
            actions = []
            for row in rows:
                proposal = ActionProposal.model_validate_json(row["proposal_json"])
                actions.append(
                    {
                        "queue_id": row["queue_id"],
                        "capability": proposal.capability.value,
                        "target_url": proposal.target_url,
                        "method": proposal.method,
                        "fixture_id": proposal.fixture_id,
                        "actor_id": proposal.actor_id,
                    }
                )
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
            if token_file.stat().st_mode & 0o077:
                raise ValueError("approval token file permissions must be 0600 or stricter")
            token = token_file.read_text(encoding="utf-8").strip()
            if not token:
                raise ValueError("approval token file is empty")
        engagement = load_config(config_path).engagement
        logger = AuditLogger(engagement.audit_log_path)
        with InventoryStore(engagement.inventory_db_path) as inventory:
            AgentOrchestrator(engagement, inventory, logger).get(run_id)
            queue = ActionQueue(inventory)
            item = queue.get(queue_id)
            if item is None or item.run_id != run_id:
                raise ValueError("queued action does not belong to this agent run")
            verifier = ApprovalVerifier.from_environment(engagement, inventory)
            approved = queue.approve_waiting(queue_id, token, verifier)
            logger.emit(
                "queued_action_approved",
                run_id=run_id,
                queue_id=queue_id,
                approval_id=approved.proposal.approval_id,
            )
    except Exception as exc:
        typer.echo(f"Approval failed: {exc}", err=True)
        raise typer.Exit(code=10) from exc
    typer.echo(
        json.dumps(
            {
                "run_id": run_id,
                "queue_id": approved.queue_id,
                "status": approved.status.value,
                "approval_id": approved.proposal.approval_id,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    app()
