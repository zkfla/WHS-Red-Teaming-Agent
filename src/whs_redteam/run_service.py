"""Shared application service for CLI and future web entry points."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import yaml

from whs_redteam.api_schema import canonical_endpoint_url, infer_query_schema
from whs_redteam.audit import AuditLogger
from whs_redteam.config import AIConfig, EngagementConfig, load_config
from whs_redteam.injection import discover_injection_fixtures
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ActionProposal,
    AgentRun,
    ApiRequestSchemaRecord,
    QueueStatus,
)
from whs_redteam.orchestration.approvals import ApprovalVerifier
from whs_redteam.orchestration.orchestrator import AgentOrchestrator
from whs_redteam.orchestration.queue import ActionQueue

DiscoveryMode = Literal["http", "browser"]


@dataclass(frozen=True, slots=True)
class RunResult:
    """Serializable run result shared by CLI, HTTP API, and tests."""

    run: AgentRun
    queue_counts: dict[str, int]
    config_path: Path | None = None

    def to_dict(self) -> dict[str, object]:
        run = self.run
        result: dict[str, object] = {
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
            "queue": dict(self.queue_counts),
        }
        if run.findings_path:
            result["markdown_report_path"] = str(
                Path(run.findings_path).with_suffix(".md")
            )
        if self.config_path is not None:
            result["config_path"] = str(self.config_path)
        return result


class RunService:
    """Application boundary that owns config, persistence, and orchestration setup.

    The service deliberately exposes no raw HTTP client.  CLI and web callers use
    the same methods, while network requests remain behind AgentOrchestrator's
    scope, budget, approval, and dispatcher gates.
    """

    def __init__(
        self,
        engagement: EngagementConfig,
        *,
        config_path: Path | None = None,
    ) -> None:
        self.engagement = engagement
        self.config_path = config_path

    @classmethod
    def from_config_path(cls, config_path: str | Path) -> RunService:
        path = Path(config_path)
        return cls(load_config(path).engagement, config_path=path)

    @classmethod
    def from_sqli_url(
        cls,
        target_url: str,
        *,
        output_dir: str | Path = Path("outputs/sqli-scan"),
        discovery_mode: DiscoveryMode = "http",
        follow_discovered_links: bool = False,
        approval_secret_env: str = "WHS_APPROVAL_SECRET",
        allow_private_dns: bool = False,
        planner_mode: Literal["rules", "hybrid"] = "rules",
        ai_base_url: str = "http://127.0.0.1:11434/v1",
        ai_model: str = "",
        report_writer_enabled: bool = False,
        max_requests: int = 100,
        max_pages: int = 20,
        max_depth: int = 2,
    ) -> RunService:
        """Create a persisted SQLi-only engagement from one URL.

        Query values are used only to infer value types; they are never written to
        the generated config.  The deterministic profile directory is keyed by a
        SHA-256 digest so repeated CLI invocations can resume the same run without
        placing target values in filenames.
        """

        parsed = urlsplit(target_url)
        if parsed.fragment:
            raise ValueError("target URL must not contain a fragment")
        if parsed.username or parsed.password:
            raise ValueError("credentials must not be embedded in target URLs")
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("target URL must be an absolute HTTP(S) URL")
        target_without_query = urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc,
                parsed.path or "/",
                "",
                "",
            )
        )
        digest = hashlib.sha256(target_url.encode()).hexdigest()[:16]
        profile_dir = Path(output_dir) / digest
        config_path = profile_dir / "engagement.yaml"
        ai_provider = (
            "openai_compatible"
            if planner_mode == "hybrid" or report_writer_enabled
            else "none"
        )
        enabled_capabilities = {
            "discovery",
            "api_schema",
            "auth_session",
            "resource_model",
            "finding_validation",
            "sqli_safe",
        }
        if planner_mode == "hybrid":
            enabled_capabilities.add("llm_planning")
        base_config = EngagementConfig(
            name=f"sqli-scan-{digest}",
            targets=(target_without_query,),
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            max_requests=max_requests,
            requests_per_second=20,
            max_runtime_minutes=15,
            max_pages=max_pages,
            max_depth=max_depth,
            test_level="safe_active",
            allow_state_change=False,
            audit_log_path=profile_dir / "events.jsonl",
            inventory_db_path=profile_dir / "inventory.db",
            screenshots_dir=profile_dir / "screenshots",
            capture_screenshots=False,
            browser_headless=True,
            discovery_mode=discovery_mode,
            follow_discovered_links=follow_discovered_links,
            allow_private_dns=allow_private_dns,
            injection_fixtures=(),
            auto_injection_discovery=True,
            auto_injection_checks=frozenset({"sqli"}),
            max_auto_injection_fixtures=20,
            planner_mode=planner_mode,
            ai=AIConfig(
                provider=ai_provider,
                base_url=ai_base_url,
                model=ai_model,
                report_writer_enabled=report_writer_enabled,
            ),
            tool_backend="local",
            validation_repetitions=2,
            enabled_capabilities=frozenset(enabled_capabilities),
            approval_hmac_secret_env=approval_secret_env,
        )
        direct_schema = ApiRequestSchemaRecord(
            endpoint_url=canonical_endpoint_url(target_url),
            method="GET",
            resource_type="document",
            query_schema=infer_query_schema(target_url),
        )
        fixtures = discover_injection_fixtures(base_config, (direct_schema,))
        service = cls(
            base_config.model_copy(update={"injection_fixtures": fixtures}),
            config_path=config_path,
        )
        service.write_config(config_path)
        return service

    def write_config(self, path: str | Path | None = None) -> Path:
        """Persist a value-free YAML profile for approval and later resumption."""

        target = Path(path or self.config_path or "outputs/engagement.yaml")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"engagement": self.engagement.model_dump(mode="json")}
        target.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return target

    async def run(self, run_id: str | None = None) -> RunResult:
        """Create or resume an orchestration run using one shared lifecycle."""

        logger = AuditLogger(self.engagement.audit_log_path)
        with InventoryStore(self.engagement.inventory_db_path) as inventory:
            orchestrator = AgentOrchestrator(self.engagement, inventory, logger)
            run = await orchestrator.run(run_id)
            counts = inventory.queue_counts(run.run_id)
        return RunResult(run, counts, self.config_path)

    def status(self, run_id: str) -> RunResult:
        logger = AuditLogger(self.engagement.audit_log_path)
        with InventoryStore(self.engagement.inventory_db_path) as inventory:
            run = AgentOrchestrator(self.engagement, inventory, logger).get(run_id)
            counts = inventory.queue_counts(run.run_id)
        return RunResult(run, counts, self.config_path)

    def pending_approvals(self, run_id: str) -> list[dict[str, object]]:
        logger = AuditLogger(self.engagement.audit_log_path)
        with InventoryStore(self.engagement.inventory_db_path) as inventory:
            AgentOrchestrator(self.engagement, inventory, logger).get(run_id)
            rows = inventory.connection.execute(
                """
                SELECT queue_id, proposal_json FROM action_queue
                WHERE run_id = ? AND status = ? ORDER BY priority DESC, created_at
                """,
                (run_id, QueueStatus.WAITING_APPROVAL.value),
            ).fetchall()
            actions: list[dict[str, object]] = []
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
        return actions

    def approve(self, run_id: str, queue_id: str, token: str) -> dict[str, object]:
        """Verify and release one action; used by CLI and the future web API."""

        logger = AuditLogger(self.engagement.audit_log_path)
        with InventoryStore(self.engagement.inventory_db_path) as inventory:
            AgentOrchestrator(self.engagement, inventory, logger).get(run_id)
            queue = ActionQueue(inventory)
            item = queue.get(queue_id)
            if item is None or item.run_id != run_id:
                raise ValueError("queued action does not belong to this agent run")
            verifier = ApprovalVerifier.from_environment(self.engagement, inventory)
            approved = queue.approve_waiting(queue_id, token, verifier)
            logger.emit(
                "queued_action_approved",
                run_id=run_id,
                queue_id=queue_id,
                approval_id=approved.proposal.approval_id,
            )
            return {
                "run_id": run_id,
                "queue_id": approved.queue_id,
                "status": approved.status.value,
                "approval_id": approved.proposal.approval_id,
            }
