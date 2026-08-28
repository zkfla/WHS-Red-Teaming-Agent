import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from whs_redteam.audit import AuditLogger
from whs_redteam.config import (
    ActorConfig,
    AuthorizationFixture,
    EngagementConfig,
)
from whs_redteam.exceptions import RequestLimitExceeded
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ActionProposal,
    AgentRunStatus,
    ApprovalClaims,
    Capability,
    EndpointRecord,
    Finding,
    FindingStatus,
    Severity,
    ToolBackend,
    utc_now,
)
from whs_redteam.models import TestPlan as Plan
from whs_redteam.orchestration import AgentOrchestrator
from whs_redteam.orchestration.approvals import ApprovalVerifier, sign_approval
from whs_redteam.orchestration.cleanup import CleanupQueue
from whs_redteam.orchestration.queue import ActionQueue
from whs_redteam.orchestration.retry import RetryPolicy
from whs_redteam.policy.budget import PersistentRequestBudget, RequestBudget
from whs_redteam.tools.actor_sessions import ActorSessionPool
from whs_redteam.tools.network_gate import NetworkGate


class StubOrchestrator(AgentOrchestrator):
    def __init__(
        self,
        *args,
        fail_once: bool = False,
        transient_explore_failures: int = 0,
        validation_failures: int = 0,
        consume_budget: bool = False,
        actor_transport: httpx.AsyncBaseTransport | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.fail_once = fail_once
        self.transient_explore_failures = transient_explore_failures
        self.validation_failures = validation_failures
        self.consume_budget = consume_budget
        self.actor_transport = actor_transport
        self.explore_calls = 0
        self.validate_calls = 0

    async def _explore(self, exploration_id: str, budget=None) -> None:
        self.explore_calls += 1
        if self.consume_budget:
            assert budget is not None
            budget.consume()
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic exploration failure")
        if self.transient_explore_failures > 0:
            self.transient_explore_failures -= 1
            raise TimeoutError("synthetic exploration timeout")

    async def _validate(self, plan: Plan, budget=None) -> str:
        self.validate_calls += 1
        assert plan.items
        if self.consume_budget:
            assert budget is not None
            budget.consume()
        if self.validation_failures > 0:
            self.validation_failures -= 1
            raise TimeoutError("synthetic validation timeout")
        return "validation-test"

    def _actor_session_pool(
        self,
        budget: RequestBudget | PersistentRequestBudget,
    ) -> ActorSessionPool:
        return ActorSessionPool(
            self.config,
            NetworkGate(self.scope_guard, budget, self.audit_logger),
            self.audit_logger,
            transport=self.actor_transport,
        )


def make_orchestrator(
    tmp_path: Path,
    *,
    fail_once: bool = False,
    transient_explore_failures: int = 0,
    validation_failures: int = 0,
    consume_budget: bool = False,
    max_requests: int = 50,
    enabled_capabilities: frozenset[str] | None = None,
    tool_backend: str = "local",
) -> tuple[InventoryStore, StubOrchestrator]:
    config = EngagementConfig(
        name="orchestrator-test",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
        max_requests=max_requests,
        enabled_capabilities=(
            enabled_capabilities
            if enabled_capabilities is not None
            else frozenset(
                {
                    "discovery",
                    "api_schema",
                    "auth_session",
                    "resource_model",
                    "finding_validation",
                }
            )
        ),
        tool_backend=tool_backend,
    )
    store = InventoryStore(config.inventory_db_path)
    return store, StubOrchestrator(
        config,
        store,
        AuditLogger(config.audit_log_path),
        fail_once=fail_once,
        transient_explore_failures=transient_explore_failures,
        validation_failures=validation_failures,
        consume_budget=consume_budget,
        retry_policy=RetryPolicy(base_delay_seconds=0, max_delay_seconds=0),
    )


def test_orchestrator_completes_and_persists_checkpoint(tmp_path: Path) -> None:
    store, orchestrator = make_orchestrator(tmp_path)
    try:
        run = asyncio.run(orchestrator.run())
        persisted = store.get_agent_run(run.run_id)
        actions = store.list_action_results(run.run_id)
        queued = store.connection.execute(
            "SELECT status, action_id FROM action_queue WHERE run_id = ?",
            (run.run_id,),
        ).fetchall()
        dependency = store.connection.execute(
            "SELECT * FROM action_dependencies"
        ).fetchone()
    finally:
        store.close()

    assert run.status is AgentRunStatus.COMPLETED
    assert persisted is not None
    assert persisted.status is AgentRunStatus.COMPLETED
    assert run.exploration_run_id == f"{run.run_id}-explore"
    assert json.loads(run.attack_surface_json or "{}")["schema_version"] == (
        "attack-surface-v3"
    )
    assert run.validation_id == "validation-test"
    assert json.loads(run.exploitability_decision_json or "{}")["verdict"] == (
        "NOT_DEMONSTRATED"
    )
    assert len(actions) == 5
    assert {action["capability"] for action in actions} == {
        "discovery",
        "api_schema",
        "auth_session",
        "resource_model",
        "finding_validation",
    }
    assert len(queued) == 5
    assert {row["status"] for row in queued} == {"SUCCEEDED"}
    assert {row["action_id"] for row in queued} == {
        action["action_id"] for action in actions
    }
    assert dependency is not None
    assert Path(run.findings_path or "").exists()
    report = json.loads(Path(run.findings_path or "").read_text())
    assert report["report_version"] == "orchestrator-report-v3"
    assert report["run_id"] == run.run_id
    assert report["exploitability_decision"]["exploit_chain_demonstrated"] is False
    assert report["api_request_schemas"] == []
    assert report["resources"] == []
    assert report["actor_sessions"]["configured"] == 0
    assert report["findings"] == []


def test_resource_model_stage_populates_attack_surface_and_report(
    tmp_path: Path,
) -> None:
    store, orchestrator = make_orchestrator(tmp_path)
    try:
        created = orchestrator.create("resource-run")
        store.save_endpoint(
            "resource-run-explore",
            EndpointRecord(
                url="https://example.test/api/orders/123",
                method="GET",
                resource_type="fetch",
            ),
        )
        run = asyncio.run(orchestrator.run(created.run_id))
        report = json.loads(Path(run.findings_path or "").read_text())
    finally:
        store.close()

    surface = json.loads(run.attack_surface_json or "{}")
    assert surface["resources"] == 1
    assert report["resources"][0]["endpoint_template"].endswith(
        "/api/orders/{resource_id}"
    )


def test_actor_credential_value_never_reaches_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "Bearer actor-secret-must-not-persist"
    monkeypatch.setenv("WHS_TEST_ACTOR_AUTH", secret)
    config = EngagementConfig(
        name="actor-persistence-test",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
        actors=(
            ActorConfig(
                actor_id="user_a",
                header_env={"Authorization": "WHS_TEST_ACTOR_AUTH"},
            ),
        ),
    )
    store = InventoryStore(config.inventory_db_path)
    orchestrator = StubOrchestrator(
        config,
        store,
        AuditLogger(config.audit_log_path),
        retry_policy=RetryPolicy(base_delay_seconds=0, max_delay_seconds=0),
    )
    try:
        run = asyncio.run(orchestrator.run())
        report_path = Path(run.findings_path or "")
        report = json.loads(report_path.read_text())
        assert report["actor_sessions"]["credentials_available"] == 1
        assert report["actor_sessions"]["authenticated"] == 0
    finally:
        store.close()

    persisted = (
        config.inventory_db_path.read_bytes()
        + config.audit_log_path.read_bytes()
        + report_path.read_bytes()
    )
    assert secret.encode() not in persisted


def test_explicit_authorization_fixture_waits_for_signed_approval(
    tmp_path: Path,
) -> None:
    config = EngagementConfig(
        name="idor-waiting-test",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
        actors=(
            ActorConfig(
                actor_id="owner",
                header_env={"Authorization": "OWNER_TEST_AUTH"},
            ),
            ActorConfig(
                actor_id="other",
                header_env={"Authorization": "OTHER_TEST_AUTH"},
            ),
        ),
        authorization_fixtures=(
            AuthorizationFixture(
                fixture_id="order-idor",
                approval_scope_url="https://example.test/api",
                target_url="https://example.test/api/orders/123",
                negative_control_url="https://example.test/api/controls/denied",
                owner_actor="owner",
                non_owner_actors=("other",),
            ),
        ),
        enabled_capabilities=frozenset(
            {
                "discovery",
                "api_schema",
                "auth_session",
                "resource_model",
                "finding_validation",
                "idor_safe",
            }
        ),
        approval_hmac_secret_env="APPROVAL_SECRET",
    )
    store = InventoryStore(config.inventory_db_path)
    orchestrator = StubOrchestrator(
        config,
        store,
        AuditLogger(config.audit_log_path),
        retry_policy=RetryPolicy(base_delay_seconds=0, max_delay_seconds=0),
    )
    try:
        run = asyncio.run(orchestrator.run())
        idor = store.connection.execute(
            """
            SELECT queue_id, status FROM action_queue
            WHERE proposal_json LIKE '%idor_safe%'
            """
        ).fetchone()
        dependency_capabilities = {
            row["capability"]
            for row in store.connection.execute(
                """
                SELECT q.proposal_json, a.capability
                FROM action_dependencies d
                JOIN action_queue q ON q.queue_id = d.queue_id
                JOIN agent_actions a
                  ON a.queue_id = d.depends_on_queue_id
                WHERE q.queue_id = ?
                """,
                (idor["queue_id"],),
            ).fetchall()
        }
        ownership = store.list_resource_ownership(
            run.exploration_run_id or ""
        )
    finally:
        store.close()

    assert run.status is AgentRunStatus.WAITING_APPROVAL
    assert idor["status"] == "WAITING_APPROVAL"
    assert dependency_capabilities == {"finding_validation", "resource_model"}
    assert len(ownership) == 1
    assert ownership[0].endpoint_template.endswith(
        "/api/orders/{resource_id}"
    )


def test_exploitability_distinguishes_hardening_from_bounded_idor(
    tmp_path: Path,
) -> None:
    store, orchestrator = make_orchestrator(tmp_path)
    now = utc_now()
    hardening = Finding(
        finding_id="F-hardening",
        dedup_key="hardening-key",
        category="security_headers",
        test_id="WEB-HEADERS-001",
        target_url="https://example.test",
        status=FindingStatus.CONFIRMED,
        severity=Severity.LOW,
        title="header gap",
        summary="header gap",
        reproduction_count=2,
        required_reproductions=2,
        negative_control_passed=True,
        experiment_ids=("a", "b"),
        evidence_hashes=("c", "d"),
        first_observed_at=now,
    )
    idor = hardening.model_copy(
        update={
            "finding_id": "F-idor",
            "dedup_key": "idor-key",
            "category": "idor",
            "test_id": "WEB-IDOR-001",
            "severity": Severity.HIGH,
            "title": "bounded IDOR",
            "summary": "bounded IDOR",
        }
    )
    try:
        store.save_finding("hardening-validation", hardening)
        hardening_decision = orchestrator._decide_exploitability(
            "hardening-validation"
        )
        store.save_finding("idor-validation", idor)
        idor_decision = orchestrator._decide_exploitability(
            "idor-validation"
        )
    finally:
        store.close()

    assert hardening_decision.verdict.value == "HARDENING_GAPS"
    assert hardening_decision.bounded_exploit_demonstrated is False
    assert idor_decision.verdict.value == "CONFIRMED"
    assert idor_decision.bounded_exploit_demonstrated is True


def test_signed_approval_resumes_and_reports_bounded_idor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OWNER_TEST_AUTH", "Bearer owner-token")
    monkeypatch.setenv("OTHER_TEST_AUTH", "Bearer other-token")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/controls/denied"):
            return httpx.Response(403)
        return httpx.Response(200, json={"order_id": 123, "private": "evidence"})

    config = EngagementConfig(
        name="idor-end-to-end",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
        actors=(
            ActorConfig(
                actor_id="owner",
                header_env={"Authorization": "OWNER_TEST_AUTH"},
            ),
            ActorConfig(
                actor_id="other",
                header_env={"Authorization": "OTHER_TEST_AUTH"},
            ),
        ),
        authorization_fixtures=(
            AuthorizationFixture(
                fixture_id="order-idor",
                approval_scope_url="https://example.test/api",
                target_url="https://example.test/api/orders/123",
                negative_control_url="https://example.test/api/controls/denied",
                owner_actor="owner",
                non_owner_actors=("other",),
            ),
        ),
        enabled_capabilities=frozenset(
            {
                "discovery",
                "api_schema",
                "auth_session",
                "resource_model",
                "finding_validation",
                "idor_safe",
            }
        ),
        approval_hmac_secret_env="APPROVAL_SECRET",
    )
    store = InventoryStore(config.inventory_db_path)
    orchestrator = StubOrchestrator(
        config,
        store,
        AuditLogger(config.audit_log_path),
        actor_transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(base_delay_seconds=0, max_delay_seconds=0),
    )
    secret = b"0123456789abcdef0123456789abcdef"
    try:
        waiting = asyncio.run(orchestrator.run())
        queued = ActionQueue(store).find_latest(
            waiting.run_id, Capability.IDOR_SAFE
        )
        assert queued is not None
        now = datetime.now(UTC)
        token = sign_approval(
            ApprovalClaims(
                approval_id="approval-idor-e2e",
                issuer="security-lead",
                engagement_name=config.name,
                capabilities=frozenset({Capability.IDOR_SAFE}),
                targets=("https://example.test/api",),
                methods=frozenset({"GET"}),
                issued_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(minutes=10),
                max_uses=1,
            ),
            secret,
        )
        verifier = ApprovalVerifier(config, store, secret)
        ActionQueue(store).approve_waiting(queued.queue_id, token, verifier)
        completed = asyncio.run(orchestrator.run(waiting.run_id))
        report = json.loads(Path(completed.findings_path or "").read_text())
    finally:
        store.close()

    assert waiting.status is AgentRunStatus.WAITING_APPROVAL
    assert completed.status is AgentRunStatus.COMPLETED
    assert report["exploitability_decision"]["verdict"] == "CONFIRMED"
    assert report["exploitability_decision"]["bounded_exploit_demonstrated"] is True
    assert report["authorization_tests"][0]["actors_confirmed"] == ["other"]
    assert report["findings"][0]["category"] == "idor"
    assert report["findings"][0]["target_url"].endswith(
        "/api/orders/{resource_id}"
    )


def test_unknown_terminal_failure_is_checkpointed_and_not_retried(
    tmp_path: Path,
) -> None:
    store, orchestrator = make_orchestrator(tmp_path, fail_once=True)
    try:
        with pytest.raises(RuntimeError, match="synthetic"):
            asyncio.run(orchestrator.run())
        rows = store.connection.execute("SELECT run_id FROM agent_runs").fetchall()
        run_id = str(rows[0]["run_id"])
        failed = store.get_agent_run(run_id)
        with pytest.raises(RuntimeError, match="not executable: FAILED"):
            asyncio.run(orchestrator.run(run_id))
    finally:
        store.close()

    assert failed is not None
    assert failed.status is AgentRunStatus.FAILED
    assert failed.resume_status is AgentRunStatus.DISCOVERING
    assert failed.last_error == "RuntimeError: synthetic exploration failure"
    assert "\n" not in failed.last_error
    assert orchestrator.explore_calls == 1


def test_transient_exploration_failure_uses_retry_policy(tmp_path: Path) -> None:
    store, orchestrator = make_orchestrator(
        tmp_path, transient_explore_failures=1
    )
    try:
        completed = asyncio.run(orchestrator.run())
        action = store.connection.execute(
            """
            SELECT status, attempt_count FROM action_queue
            WHERE proposal_json LIKE '%discovery%'
            """
        ).fetchone()
    finally:
        store.close()

    assert completed.status is AgentRunStatus.COMPLETED
    assert orchestrator.explore_calls == 2
    assert action["status"] == "SUCCEEDED"
    assert action["attempt_count"] == 2


def test_changed_config_cannot_resume(tmp_path: Path) -> None:
    store, orchestrator = make_orchestrator(tmp_path)
    try:
        run = orchestrator.create("fixed-run")
        changed = EngagementConfig(
            name="changed",
            targets=("https://example.test",),
            inventory_db_path=tmp_path / "inventory.db",
            audit_log_path=tmp_path / "events.jsonl",
        )
        other = AgentOrchestrator(
            changed, store, AuditLogger(changed.audit_log_path)
        )
        with pytest.raises(ValueError, match="configuration changed"):
            other.get(run.run_id)
    finally:
        store.close()


def test_run_waits_until_all_approval_actions_are_released(tmp_path: Path) -> None:
    store, orchestrator = make_orchestrator(tmp_path)
    try:
        created = orchestrator.create("approval-run")
        queue = ActionQueue(store)
        waiting, _ = queue.enqueue(
            created.run_id,
            ActionProposal(
                capability=Capability.IDOR_SAFE,
                target_url="https://example.test/orders/1",
                reason="requires signed approval",
            ),
        )
        paused = orchestrator.set_waiting_for_approval(
            created.run_id, resume_status=AgentRunStatus.EXECUTING
        )
        still_waiting = asyncio.run(orchestrator.run(created.run_id))
        store.connection.execute(
            """
            UPDATE action_queue SET status = 'PENDING', approval_verified = 1
            WHERE queue_id = ?
            """,
            (waiting.queue_id,),
        )
        store.connection.commit()
        resumed = asyncio.run(orchestrator.run(created.run_id))
        persisted = store.get_agent_run(created.run_id)
    finally:
        store.close()

    assert paused.status is AgentRunStatus.WAITING_APPROVAL
    assert still_waiting.status is AgentRunStatus.WAITING_APPROVAL
    assert resumed.status is AgentRunStatus.COMPLETED
    assert persisted is not None
    assert persisted.status is AgentRunStatus.COMPLETED


def test_validation_probe_retries_bounded_transient_failure(tmp_path: Path) -> None:
    store, orchestrator = make_orchestrator(tmp_path, validation_failures=1)
    try:
        run = asyncio.run(orchestrator.run())
        rows = store.connection.execute(
            """
            SELECT status, attempt_count FROM action_queue
            WHERE run_id = ? AND proposal_json LIKE '%finding_validation%'
            """,
            (run.run_id,),
        ).fetchall()
    finally:
        store.close()

    assert run.status is AgentRunStatus.COMPLETED
    assert orchestrator.validate_calls == 2
    assert len(rows) == 1
    assert rows[0]["status"] == "SUCCEEDED"
    assert rows[0]["attempt_count"] == 2


def test_disabled_validation_capability_fails_closed(tmp_path: Path) -> None:
    store, orchestrator = make_orchestrator(
        tmp_path,
        enabled_capabilities=frozenset(
            {"discovery", "api_schema", "auth_session", "resource_model"}
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="did not complete"):
            asyncio.run(orchestrator.run())
        row = store.connection.execute(
            """
            SELECT status FROM action_queue
            WHERE proposal_json LIKE '%finding_validation%'
            """
        ).fetchone()
        persisted = store.connection.execute(
            "SELECT status, resume_status FROM agent_runs"
        ).fetchone()
    finally:
        store.close()

    assert orchestrator.validate_calls == 0
    assert row["status"] == "REJECTED"
    assert persisted["status"] == "FAILED"
    assert persisted["resume_status"] == "EXECUTING"


def test_legacy_validating_checkpoint_is_migrated_to_queue(tmp_path: Path) -> None:
    store, orchestrator = make_orchestrator(tmp_path)
    try:
        run = orchestrator.create("legacy-validation")
        plan = orchestrator._plan()
        legacy = run.model_copy(
            update={
                "status": AgentRunStatus.VALIDATING,
                "plan_json": plan.model_dump_json(),
            }
        )
        store.save_agent_run(legacy)
        completed = asyncio.run(orchestrator.run(run.run_id))
        queued = store.connection.execute(
            "SELECT status FROM action_queue WHERE run_id = ?",
            (run.run_id,),
        ).fetchall()
    finally:
        store.close()

    assert completed.status is AgentRunStatus.COMPLETED
    assert orchestrator.validate_calls == 1
    assert [row["status"] for row in queued] == ["SUCCEEDED"]


def test_discovery_and_validation_share_one_persistent_budget(tmp_path: Path) -> None:
    store, orchestrator = make_orchestrator(
        tmp_path, consume_budget=True, max_requests=1
    )
    try:
        with pytest.raises(RequestLimitExceeded, match="request count exceeded"):
            asyncio.run(orchestrator.run())
        budget = store.connection.execute(
            "SELECT used_requests FROM run_budgets"
        ).fetchone()
        validation = store.connection.execute(
            """
            SELECT status, attempt_count FROM action_queue
            WHERE proposal_json LIKE '%finding_validation%'
            """
        ).fetchone()
    finally:
        store.close()

    assert budget["used_requests"] == 1
    assert validation["status"] == "FAILED"
    assert validation["attempt_count"] == 1


def test_full_run_can_route_workers_through_in_memory_mcp(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    store, orchestrator = make_orchestrator(
        tmp_path, tool_backend="mcp_in_memory"
    )
    try:
        run = asyncio.run(orchestrator.run())
        actions = store.list_action_results(run.run_id)
    finally:
        store.close()

    assert run.status is AgentRunStatus.COMPLETED
    assert orchestrator.explore_calls == 1
    assert orchestrator.validate_calls == 1
    assert [action["capability"] for action in actions] == [
        "discovery",
        "api_schema",
        "auth_session",
        "resource_model",
        "finding_validation",
    ]


def test_run_blocks_until_persistent_cleanup_receipt_succeeds(
    tmp_path: Path,
) -> None:
    store, orchestrator = make_orchestrator(tmp_path)
    cleanup_queue = CleanupQueue(store)
    try:
        created = orchestrator.create("cleanup-blocked-run")
        checkpoint = created.model_copy(
            update={
                "status": AgentRunStatus.CLEANING_UP,
                "validation_id": "validation-cleanup",
            }
        )
        store.save_agent_run(checkpoint)
        task, _ = cleanup_queue.enqueue(
            run_id=created.run_id,
            parent_action_id="state-action",
            parent_queue_id=None,
            capability=Capability.CSRF_SAFE,
            backend=ToolBackend.LOCAL,
            tool_name="local.csrf_safe.cleanup",
            arguments={"resource_id": "synthetic-cleanup"},
        )
        blocked = asyncio.run(orchestrator.run(created.run_id))
        claimed = cleanup_queue.claim_next(created.run_id, "cleanup-worker")
        assert claimed is not None
        cleanup_queue.mark_succeeded(task.cleanup_id)
        completed = asyncio.run(orchestrator.run(created.run_id))
    finally:
        store.close()

    assert blocked.status is AgentRunStatus.BLOCKED
    assert blocked.resume_status is AgentRunStatus.CLEANING_UP
    assert blocked.last_error is not None
    assert completed.status is AgentRunStatus.COMPLETED
