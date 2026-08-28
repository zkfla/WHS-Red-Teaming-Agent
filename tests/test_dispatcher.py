import asyncio
import json
from pathlib import Path

import pytest

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import (
    CapabilityPolicyViolation,
    ProbeRegistrationError,
    ScopeViolation,
)
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import ActionProposal, ActionStatus, Capability, RiskLevel
from whs_redteam.orchestration.dispatcher import (
    ActionDispatcher,
    ProbeContext,
    ProbeRegistry,
)
from whs_redteam.policy.capabilities import CapabilityPolicy
from whs_redteam.policy.scope import ScopeGuard


class RecordingProbe:
    capability = Capability.DISCOVERY

    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self, proposal: ActionProposal, context: ProbeContext
    ) -> dict[str, object]:
        self.calls += 1
        return {
            "target": proposal.target_url,
            "run": context.run_id,
            "authorization": "must-not-persist",
        }


class FailingProbe(RecordingProbe):
    async def execute(
        self, proposal: ActionProposal, context: ProbeContext
    ) -> dict[str, object]:
        raise RuntimeError("synthetic probe failure\ninternal details")


class IdorRecordingProbe(RecordingProbe):
    capability = Capability.IDOR_SAFE


def make_dispatcher(
    tmp_path: Path, registry: ProbeRegistry
) -> tuple[InventoryStore, ActionDispatcher, Path]:
    config = EngagementConfig(
        name="dispatcher-test",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
    )
    store = InventoryStore(config.inventory_db_path)
    logger = AuditLogger(config.audit_log_path)
    dispatcher = ActionDispatcher(
        CapabilityPolicy(config, ScopeGuard(config, logger)),
        registry,
        store,
        logger,
    )
    return store, dispatcher, config.audit_log_path


def proposal(url: str = "https://example.test/catalog") -> ActionProposal:
    return ActionProposal(
        capability=Capability.DISCOVERY,
        target_url=url,
        reason="bounded inventory lookup",
        risk_level=RiskLevel.PASSIVE,
    )


def test_dispatcher_executes_registered_policy_approved_probe(tmp_path: Path) -> None:
    registry = ProbeRegistry()
    probe = RecordingProbe()
    registry.register(probe)
    store, dispatcher, audit_path = make_dispatcher(tmp_path, registry)
    try:
        result = asyncio.run(dispatcher.dispatch(proposal(), run_id="run-1"))
        stored = store.list_action_results("run-1")
    finally:
        store.close()

    assert result.status is ActionStatus.SUCCEEDED
    assert result.output["authorization"] == "[REDACTED]"
    assert len(stored) == 1
    assert stored[0]["output"]["authorization"] == "[REDACTED]"
    assert probe.calls == 1
    events = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert [event["type"] for event in events] == [
        "policy_decision",
        "action_started",
        "action_succeeded",
    ]


def test_registry_rejects_duplicate_and_missing_probe() -> None:
    registry = ProbeRegistry()
    registry.register(RecordingProbe())
    with pytest.raises(ProbeRegistrationError, match="already registered"):
        registry.register(RecordingProbe())
    with pytest.raises(ProbeRegistrationError, match="no probe"):
        registry.resolve(Capability.API_SCHEMA)


def test_unregistered_capability_never_executes(tmp_path: Path) -> None:
    store, dispatcher, _ = make_dispatcher(tmp_path, ProbeRegistry())
    try:
        with pytest.raises(ProbeRegistrationError, match="no probe"):
            asyncio.run(dispatcher.dispatch(proposal(), run_id="run-2"))
        stored = store.list_action_results("run-2")
    finally:
        store.close()
    assert len(stored) == 1
    assert stored[0]["status"] == "REJECTED"
    assert stored[0]["error_type"] == "ProbeRegistrationError"


def test_scope_rejection_happens_before_probe_execution(tmp_path: Path) -> None:
    registry = ProbeRegistry()
    probe = RecordingProbe()
    registry.register(probe)
    store, dispatcher, _ = make_dispatcher(tmp_path, registry)
    try:
        with pytest.raises(ScopeViolation):
            asyncio.run(
                dispatcher.dispatch(proposal("https://outside.test"), run_id="run-3")
            )
    finally:
        store.close()
    assert probe.calls == 0


def test_disabled_capability_rejected_before_registry_lookup(tmp_path: Path) -> None:
    registry = ProbeRegistry()
    store, dispatcher, _ = make_dispatcher(tmp_path, registry)
    unsafe = ActionProposal(
        capability=Capability.SQLI_SAFE,
        target_url="https://example.test/search",
        reason="must be denied",
        approval_id="ticket-123",
    )
    try:
        with pytest.raises(CapabilityPolicyViolation, match="not enabled"):
            asyncio.run(dispatcher.dispatch(unsafe, run_id="run-4"))
    finally:
        store.close()


def test_probe_failure_is_persisted_without_multiline_details(tmp_path: Path) -> None:
    registry = ProbeRegistry()
    registry.register(FailingProbe())
    store, dispatcher, _ = make_dispatcher(tmp_path, registry)
    try:
        with pytest.raises(RuntimeError, match="synthetic probe failure"):
            asyncio.run(dispatcher.dispatch(proposal(), run_id="run-5"))
        stored = store.list_action_results("run-5")
    finally:
        store.close()

    assert len(stored) == 1
    assert stored[0]["status"] == "FAILED"
    assert stored[0]["error_type"] == "RuntimeError"
    assert stored[0]["error_message"] == "synthetic probe failure"


def test_direct_dispatch_cannot_trust_fabricated_approval_id(tmp_path: Path) -> None:
    config = EngagementConfig(
        name="dispatcher-approval-test",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
        enabled_capabilities=frozenset({"idor_safe"}),
    )
    store = InventoryStore(config.inventory_db_path)
    logger = AuditLogger(config.audit_log_path)
    registry = ProbeRegistry()
    probe = IdorRecordingProbe()
    registry.register(probe)
    dispatcher = ActionDispatcher(
        CapabilityPolicy(config, ScopeGuard(config, logger)),
        registry,
        store,
        logger,
    )
    forged = ActionProposal(
        capability=Capability.IDOR_SAFE,
        target_url="https://example.test/orders/1",
        reason="fabricated approval must fail",
        approval_id="forged-approval",
    )
    try:
        with pytest.raises(CapabilityPolicyViolation, match="verified approval"):
            asyncio.run(dispatcher.dispatch(forged, run_id="run-forged"))
        stored = store.list_action_results("run-forged")
    finally:
        store.close()

    assert probe.calls == 0
    assert stored[0]["status"] == "REJECTED"
