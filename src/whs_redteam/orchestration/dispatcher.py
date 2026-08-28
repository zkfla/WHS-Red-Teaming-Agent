"""Central fail-closed dispatcher for all capability probes."""

from __future__ import annotations

from uuid import uuid4

from whs_redteam.audit import AuditLogger
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ActionProposal,
    ActionResult,
    ActionStatus,
    ToolCall,
    utc_now,
)
from whs_redteam.orchestration.adapters import (
    LocalProbeAdapter,
    ProbeContext,
    ProbeRegistry,
    ToolAdapter,
)
from whs_redteam.orchestration.cleanup import CleanupQueue
from whs_redteam.policy.capabilities import CapabilityPolicy
from whs_redteam.policy.capability_catalog import CAPABILITY_CATALOG
from whs_redteam.sanitizer import sanitize_data, sanitize_url


class ActionDispatcher:
    def __init__(
        self,
        policy: CapabilityPolicy,
        registry: ProbeRegistry,
        inventory: InventoryStore,
        audit_logger: AuditLogger,
        *,
        tool_adapter: ToolAdapter | None = None,
    ) -> None:
        self.policy = policy
        self.registry = registry
        self.inventory = inventory
        self.audit_logger = audit_logger
        self.tool_adapter = tool_adapter or LocalProbeAdapter(registry)

    async def dispatch(
        self,
        proposal: ActionProposal,
        *,
        run_id: str,
        queue_id: str | None = None,
        approval_verified: bool = False,
    ) -> ActionResult:
        action_id = uuid4().hex
        started_at = utc_now()
        try:
            approved = self.policy.approve(
                proposal, approval_verified=approval_verified
            )
            descriptor = self.tool_adapter.resolve(approved.capability)
        except Exception as exc:
            self.inventory.save_action_result(
                self._error_result(
                    action_id, queue_id, run_id, proposal, ActionStatus.REJECTED,
                    started_at, exc
                )
            )
            self.audit_logger.emit(
                "action_rejected",
                action_id=action_id,
                run_id=run_id,
                capability=proposal.capability.value,
                target_url=proposal.target_url,
                error_type=type(exc).__name__,
            )
            raise

        self.audit_logger.emit(
            "action_started",
            action_id=action_id,
            run_id=run_id,
            capability=approved.capability.value,
            target_url=approved.target_url,
            tool_backend=descriptor.backend.value,
            tool_name=descriptor.tool_name,
        )
        spec = CAPABILITY_CATALOG[approved.capability]
        cleanup = None
        try:
            if spec.cleanup_required:
                if descriptor.cleanup_tool_name is None:
                    raise RuntimeError(
                        "cleanup-required tool has no predeclared cleanup binding"
                    )
                cleanup, _ = CleanupQueue(self.inventory).reserve(
                    run_id=run_id,
                    parent_action_id=action_id,
                    parent_queue_id=queue_id,
                    capability=approved.capability,
                    backend=descriptor.backend,
                    tool_name=descriptor.cleanup_tool_name,
                )
                self.audit_logger.emit(
                    "cleanup_task_reserved",
                    cleanup_id=cleanup.cleanup_id,
                    action_id=action_id,
                    run_id=run_id,
                    capability=approved.capability.value,
                )
            execution = await self.tool_adapter.execute(
                ToolCall(
                    action_id=action_id,
                    queue_id=queue_id,
                    run_id=run_id,
                    engagement_name=self.policy.config.name,
                    capability=approved.capability,
                    target_url=approved.target_url,
                    method=approved.method,
                    risk_level=approved.risk_level,
                    reason=approved.reason,
                    actor_id=approved.actor_id,
                    fixture_id=approved.fixture_id,
                    destructive=approved.destructive,
                    approval_verified=approval_verified,
                ),
                ProbeContext(
                    run_id,
                    self.inventory,
                    self.audit_logger,
                    self.policy.config,
                ),
            )
            if spec.cleanup_required:
                if (
                    execution.cleanup_tool_name is None
                    or execution.cleanup_arguments is None
                ):
                    raise RuntimeError(
                        "cleanup-required tool did not return a cleanup contract"
                    )
                if cleanup is None:
                    raise RuntimeError("cleanup reservation disappeared")
                if execution.cleanup_tool_name != cleanup.tool_name:
                    raise RuntimeError("cleanup tool changed after action execution")
                cleanup = CleanupQueue(self.inventory).arm(
                    cleanup.cleanup_id, execution.cleanup_arguments
                )
                self.audit_logger.emit(
                    "cleanup_task_armed",
                    cleanup_id=cleanup.cleanup_id,
                    action_id=action_id,
                    run_id=run_id,
                    capability=approved.capability.value,
                )
        except Exception as exc:
            self.inventory.save_action_result(
                self._error_result(
                    action_id, queue_id, run_id, approved, ActionStatus.FAILED,
                    started_at, exc
                )
            )
            self.audit_logger.emit(
                "action_failed",
                action_id=action_id,
                run_id=run_id,
                capability=approved.capability.value,
                error_type=type(exc).__name__,
            )
            raise
        result = ActionResult(
            action_id=action_id,
            queue_id=queue_id,
            run_id=run_id,
            capability=approved.capability,
            target_url=sanitize_url(
                approved.target_url, redact_all_query_values=True
            ),
            status=ActionStatus.SUCCEEDED,
            output=sanitize_data(execution.output),
            started_at=started_at,
            finished_at=utc_now(),
        )
        self.inventory.save_action_result(result)
        self.audit_logger.emit(
            "action_succeeded",
            action_id=action_id,
            run_id=run_id,
            capability=approved.capability.value,
            tool_backend=execution.backend.value,
            tool_name=execution.tool_name,
        )
        return result

    @staticmethod
    def _error_result(
        action_id: str,
        queue_id: str | None,
        run_id: str,
        proposal: ActionProposal,
        status: ActionStatus,
        started_at,
        exc: Exception,
    ) -> ActionResult:
        message = str(exc).splitlines()[0][:400]
        return ActionResult(
            action_id=action_id,
            queue_id=queue_id,
            run_id=run_id,
            capability=proposal.capability,
            target_url=sanitize_url(
                proposal.target_url, redact_all_query_values=True
            ),
            status=status,
            error_type=type(exc).__name__,
            error_message=message,
            started_at=started_at,
            finished_at=utc_now(),
        )
