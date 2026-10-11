"""Gated strategy execute lifecycle: uncertainty, cancel, job retention.

Owned by Capability (TASK-005). No docker, no network: fakes only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.gated_manifest import SHELL_TARGET_JOB_START, SHELL_TARGET_RUN
from redteam.capability.gated_network_policy import GatedNetworkPolicy, PinnedRegistry
from redteam.capability.gated_strategy import GatedExecutionPolicy, RetainedWindow
from redteam.capability.gated_window import AllowedFlow
from redteam.capability.shell_workroom import ShellWorkroomError
from redteam.control.network_ledger import SQLiteNetworkGrantLedger
from redteam.core.contracts.network import (
    NetworkBudget,
    NetworkCapacity,
    NetworkGrant,
    NetworkPurpose,
    WindowCaps,
    WindowSettlement,
    WindowState,
)
from redteam.core.contracts.target import DomainTarget
from redteam.core.contracts.tool import (
    EXECUTION_STARTED_METADATA_KEY,
    EXECUTION_UNCERTAIN_METADATA_KEY,
    ExecutionContext,
    ToolInvocation,
)
from redteam.core.ids import (
    ActionId,
    AgentInstanceId,
    EngagementId,
    InvocationId,
    RunId,
    ToolId,
)


def _policy() -> GatedNetworkPolicy:
    cap = NetworkCapacity(max_flow_init_deliveries=4, max_output_bytes=4096)
    budget = NetworkBudget(
        global_capacity=cap, target_capacity=cap, install_capacity=cap
    )
    target = WindowCaps(
        max_flow_init_deliveries=4,
        max_output_bytes=4096,
        duration_seconds=60.0,
        purpose=NetworkPurpose.TARGET,
    )
    install = WindowCaps(
        max_flow_init_deliveries=1,
        max_output_bytes=1024,
        duration_seconds=60.0,
        purpose=NetworkPurpose.INSTALL,
    )
    return GatedNetworkPolicy(
        budget=budget,
        default_target_caps=target,
        default_install_caps=install,
        registry=PinnedRegistry(targets={"t": ("10.0.0.9",)}, mirrors={}),
    )


def _grant() -> NetworkGrant:
    return NetworkGrant(
        window_id="gwin_abc123",
        run_id="run_001",
        instance_id="inst_001",
        epoch=1,
        purpose=NetworkPurpose.TARGET,
        reserved_deliveries=4,
        reserved_bytes=4096,
        deadline_epoch_seconds=9999999999.0,
    )


def _settlement() -> WindowSettlement:
    return WindowSettlement(
        window_id="gwin_abc123",
        state=WindowState.RELEASED,
        charged_deliveries=1,
        charged_bytes=100,
        released_deliveries=3,
        released_bytes=3996,
        committed_deliveries=1,
        metered_bytes=100,
        metered_packets=2,
        outcome="released",
    )


class _FakeExtension:
    def __init__(self, policy: GatedNetworkPolicy) -> None:
        self.policy = policy
        self.opened = 0
        self.closed = 0
        self.companions: dict[str, dict[str, object]] = {
            "gwin_abc123": {"window_id": "gwin_abc123"}
        }

    async def open_window(self, **kwargs: object) -> NetworkGrant:
        self.opened += 1
        return _grant()

    async def close_window(self, key: str) -> WindowSettlement:
        self.closed += 1
        return _settlement()

    def companion_for(self, window_id: str) -> dict[str, object] | None:
        return self.companions.get(window_id)

    def is_transmit_open(self, key: str) -> bool:
        return False


class _FakeEvidence:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.records: list[object] = []

    async def append(self, evidence: object) -> None:
        if self.fail:
            raise OSError("sink down")
        self.records.append(evidence)


def _validator(target: object, *, denials_only: bool = False):
    return True, [], None


def _invocation(tool_name: str) -> ToolInvocation:
    now = datetime.now(timezone.utc)
    context = ExecutionContext(
        engagement_id=EngagementId("eng_001"),
        run_id=RunId("run_001"),
        instance_id=AgentInstanceId("inst_001"),
        approved_target=DomainTarget(domain="t"),
    )
    return ToolInvocation(
        invocation_id=InvocationId("inv_001"),
        action_id=ActionId("act_001"),
        run_id=RunId("run_001"),
        tool_id=ToolId("analysis." + tool_name),
        arguments={"language": "bash", "script": "true", "target_ref": "t", "port": 8080},
        execution_context=context,
        created_at=now,
    )


def _parsed() -> dict:
    return {"language": "bash", "script": "true", "timeout_seconds": 5.0}


def _ok_result(status: str = "completed"):
    from redteam.capability.adapters.mcp.base import MCPCallResult

    now = datetime.now(timezone.utc)
    return MCPCallResult(
        success=True,
        started_at=now,
        finished_at=now,
        output={"status": status},
        error=None,
        metadata={},
    )


def test_delegate_failure_carries_uncertain_markers() -> None:
    async def go():
        ext = _FakeExtension(_policy())
        policy = GatedExecutionPolicy(
            ext, scope_validator=_validator, evidence_store=_FakeEvidence()
        )

        async def boom():
            raise ValueError("job blew up after entry")

        result = await policy.execute(_invocation(SHELL_TARGET_RUN), _parsed(), boom)
        assert result.success is False
        assert result.error is not None and result.error.code == "gated_delegate_failed"
        assert result.metadata["execution_started"] is True
        assert result.metadata["execution_uncertain"] is True
        assert ext.closed == 1

    asyncio.run(go())


def test_cancel_closes_and_reraises() -> None:
    async def go():
        ext = _FakeExtension(_policy())
        policy = GatedExecutionPolicy(
            ext, scope_validator=_validator, evidence_store=_FakeEvidence()
        )

        async def cancelled():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await policy.execute(_invocation(SHELL_TARGET_RUN), _parsed(), cancelled)
        assert ext.closed == 1

    asyncio.run(go())


def test_job_start_retains_until_close_retained() -> None:
    async def go():
        ext = _FakeExtension(_policy())
        evidence = _FakeEvidence()
        policy = GatedExecutionPolicy(
            ext, scope_validator=_validator, evidence_store=evidence
        )
        invocation = _invocation(SHELL_TARGET_JOB_START)

        async def ack():
            return _ok_result(status="running")

        result = await policy.execute(invocation, _parsed(), ack)
        assert result.success is True
        assert result.metadata["gated_window_retained"] is True
        assert ext.closed == 0
        retained = await policy.close_retained("run_001.inst_001")
        assert retained.settlement is not None
        assert retained.settlement.window_id == "gwin_abc123"
        assert retained.evidence_error is None
        assert ext.closed == 1
        assert len(evidence.records) == 1

    asyncio.run(go())


def test_close_retained_unknown_key_is_empty() -> None:
    async def go():
        policy = GatedExecutionPolicy(
            _FakeExtension(_policy()), scope_validator=_validator
        )
        retained = await policy.close_retained("run_001.nope")
        assert retained.settlement is None

    asyncio.run(go())


def test_evidence_failure_surfaced_not_hidden() -> None:
    async def go():
        ext = _FakeExtension(_policy())
        policy = GatedExecutionPolicy(
            ext, scope_validator=_validator, evidence_store=_FakeEvidence(fail=True)
        )

        async def ok():
            return _ok_result()

        result = await policy.execute(_invocation(SHELL_TARGET_RUN), _parsed(), ok)
        assert result.success is True
        assert result.metadata["gated_evidence"] == "gated_evidence_failed"
        assert ext.closed == 1

    asyncio.run(go())


def test_sync_success_closes_and_persists() -> None:
    async def go():
        ext = _FakeExtension(_policy())
        evidence = _FakeEvidence()
        policy = GatedExecutionPolicy(
            ext, scope_validator=_validator, evidence_store=evidence
        )

        async def ok():
            return _ok_result()

        result = await policy.execute(_invocation(SHELL_TARGET_RUN), _parsed(), ok)
        assert result.success is True
        assert result.metadata["gated_window_id"] == "gwin_abc123"
        assert result.metadata["gated_settlement_state"] == "released"
        assert result.metadata["gated_evidence_id"] == "ev_gwin_gwin_abc123"
        assert ext.closed == 1
        assert len(evidence.records) == 1

    asyncio.run(go())


def test_allowed_flows_bounded() -> None:
    flow = AllowedFlow(ip="10.0.0.9", port=8080, proto="tcp")
    assert (flow.ip, flow.port, flow.proto) == ("10.0.0.9", 8080, "tcp")


def _ledger_policy():
    cap = NetworkCapacity(max_flow_init_deliveries=4, max_output_bytes=4096)
    budget = NetworkBudget(
        global_capacity=cap, target_capacity=cap, install_capacity=cap
    )
    target = WindowCaps(
        max_flow_init_deliveries=4,
        max_output_bytes=4096,
        duration_seconds=60.0,
        purpose=NetworkPurpose.TARGET,
    )
    install = WindowCaps(
        max_flow_init_deliveries=1,
        max_output_bytes=1024,
        duration_seconds=60.0,
        purpose=NetworkPurpose.INSTALL,
    )
    return GatedNetworkPolicy(
        budget=budget,
        default_target_caps=target,
        default_install_caps=install,
        registry=PinnedRegistry(targets={"t": ("10.0.0.9",)}, mirrors={}),
    )


class _LedgerWindow:
    """Extension stub whose authority is the REAL durable ledger."""

    def __init__(self, policy, ledger):
        self.policy = policy
        self.ledger = ledger

    async def open_window(self, *, run_id, instance_id, epoch, caps, allowed,
                          holder_action_id, holder_invocation_id,
                          holder_work_id=None, internal=False):
        return await self.ledger.request_window(
            run_id=run_id, instance_id=instance_id, epoch=epoch, caps=caps
        )

    async def close_window(self, key):
        raise AssertionError("retained windows never close pre-restart")

    def companion_for(self, window_id):
        return None

    def is_transmit_open(self, key):
        return False


def _allow_all(target, *, denials_only=False):
    return True, [], None


def _job_invocation():
    context = ExecutionContext(
        engagement_id=EngagementId("eng_recover_001"),
        run_id=RunId("run_001"),
        instance_id=AgentInstanceId("inst_001"),
        approved_target=DomainTarget(domain="t"),
    )
    return ToolInvocation(
        invocation_id=InvocationId("inv_001"),
        action_id=ActionId("act_001"),
        run_id=RunId("run_001"),
        tool_id=ToolId("analysis." + SHELL_TARGET_JOB_START),
        arguments={"language": "bash", "script": "true", "target_ref": "t", "port": 8080},
        execution_context=context,
        created_at=datetime.now(timezone.utc),
    )


def _running_ack():
    now = datetime.now(timezone.utc)
    return MCPCallResult(
        success=True,
        started_at=now,
        finished_at=now,
        output={"job_id": "shjob_0123456789abcdef", "status": "running"},
        error=None,
        metadata={},
    )


def test_retained_window_recovers_after_restart_without_refund(tmp_path) -> None:
    """A NEW policy instance settles the retained grant UNKNOWN, never refunded."""
    async def go():
        cap = NetworkCapacity(max_flow_init_deliveries=4, max_output_bytes=4096)
        budget = NetworkBudget(
            global_capacity=cap, target_capacity=cap, install_capacity=cap
        )
        ledger1 = SQLiteNetworkGrantLedger(
            str(tmp_path / "net.db"), engagement_id="eng_recover_001", budget=budget
        )
        await ledger1.setup()
        policy1 = GatedExecutionPolicy(
            _LedgerWindow(_ledger_policy(), ledger1),
            scope_validator=_allow_all,
            evidence_store=None,
        )
        invocation = _job_invocation()

        async def ack():
            return _running_ack()

        result = await policy1.execute(
            invocation, {"language": "bash", "script": "true"}, ack
        )
        assert result.metadata.get("gated_window_retained") is True
        window_id = result.metadata["gated_window_id"]
        record = policy1.retained_for("run_001.inst_001")
        assert record is not None and record.window_id == window_id
        cargo = record.to_record()
        assert RetainedWindow.from_record(cargo).window_id == window_id
        del policy1
        ledger2 = SQLiteNetworkGrantLedger(
            str(tmp_path / "net.db"), engagement_id="eng_recover_001", budget=budget
        )
        await ledger2.setup()
        evidence = _FakeEvidence()
        policy2 = GatedExecutionPolicy(
            _LedgerWindow(_ledger_policy(), ledger2),
            scope_validator=_allow_all,
            evidence_store=evidence,
        )
        recovered = await policy2.recover_retained(RetainedWindow.from_record(cargo))
        settlement = recovered.settlement
        assert settlement is not None
        assert settlement.state.value == "quarantined"
        assert settlement.charged_bytes == 4096
        assert settlement.charged_deliveries == 4
        assert settlement.released_bytes == 0 and settlement.released_deliveries == 0
        assert recovered.evidence_error is None
        assert recovered.evidence_id == "ev_gwin_" + window_id
        assert len(evidence.records) == 1
        again = await policy2.recover_retained(RetainedWindow.from_record(cargo))
        assert again.settlement is not None
        assert again.settlement.charged_bytes == 4096
        assert again.evidence_id == recovered.evidence_id

    asyncio.run(go())


def test_prespawn_cargo_covers_post_spawn_pre_ack_crash(tmp_path) -> None:
    """Cargo captured DURING the delegate recovers with a NEW instance."""
    async def go():
        cap = NetworkCapacity(max_flow_init_deliveries=4, max_output_bytes=4096)
        budget = NetworkBudget(
            global_capacity=cap, target_capacity=cap, install_capacity=cap
        )
        ledger1 = SQLiteNetworkGrantLedger(
            str(tmp_path / "net.db"), engagement_id="eng_pre_001", budget=budget
        )
        await ledger1.setup()
        policy1 = GatedExecutionPolicy(
            _LedgerWindow(_ledger_policy(), ledger1),
            scope_validator=_allow_all,
            evidence_store=None,
        )
        invocation = _job_invocation()
        seen: dict[str, object] = {}

        async def delegate(_policy: GatedExecutionPolicy = policy1):
            # Adapter pre-spawn manifest write reads this BEFORE create_task.
            handle = _policy.retained_for("run_001.inst_001")
            assert handle is not None
            seen["cargo"] = handle.to_record()
            return _running_ack()

        result = await policy1.execute(
            invocation, {"language": "bash", "script": "true"}, delegate
        )
        assert result.metadata.get("gated_window_retained") is True
        assert "cargo" in seen
        # The pre-spawn cargo must equal the post-ACK retained cargo.
        post = policy1.retained_for("run_001.inst_001")
        assert post is not None
        assert seen["cargo"] == post.to_record()
        # Crash before any post-ACK annotate: only the DURING cargo survives.
        during = RetainedWindow.from_record(seen["cargo"])  # type: ignore[arg-type]
        del policy1
        ledger2 = SQLiteNetworkGrantLedger(
            str(tmp_path / "net.db"), engagement_id="eng_pre_001", budget=budget
        )
        await ledger2.setup()
        evidence = _FakeEvidence()
        policy2 = GatedExecutionPolicy(
            _LedgerWindow(_ledger_policy(), ledger2),
            scope_validator=_allow_all,
            evidence_store=evidence,
        )
        recovered = await policy2.recover_retained(during)
        assert recovered.settlement is not None
        assert recovered.settlement.state.value == "quarantined"
        assert recovered.settlement.charged_bytes == 4096
        assert recovered.evidence_error is None

    asyncio.run(go())


def test_prespawn_manifest_failure_is_plain_refusal() -> None:
    """Pre-spawn manifest failure: refusal, never STARTED/UNCERTAIN."""
    async def go():
        ext = _FakeExtension(_policy())
        evidence = _FakeEvidence()
        policy = GatedExecutionPolicy(
            ext, scope_validator=_validator, evidence_store=evidence
        )

        async def delegate():
            raise ShellWorkroomError("shell_manifest_write_failed")

        result = await policy.execute(
            _invocation(SHELL_TARGET_JOB_START), _parsed(), delegate
        )
        assert result.success is False
        assert EXECUTION_STARTED_METADATA_KEY not in (result.metadata or {})
        assert EXECUTION_UNCERTAIN_METADATA_KEY not in (result.metadata or {})
        assert ext.closed == 1
        assert policy.retained_for("run_001.inst_001") is None

    asyncio.run(go())


def test_prespawn_refusal_surfaces_evidence_sink_failure() -> None:
    """Pre-spawn refusal still reports a failing evidence sink truthfully."""
    async def go():
        ext = _FakeExtension(_policy())
        evidence = _FakeEvidence(fail=True)
        policy = GatedExecutionPolicy(
            ext, scope_validator=_validator, evidence_store=evidence
        )

        async def delegate():
            raise ShellWorkroomError("shell_manifest_write_failed")

        result = await policy.execute(
            _invocation(SHELL_TARGET_JOB_START), _parsed(), delegate
        )
        assert result.success is False
        assert EXECUTION_STARTED_METADATA_KEY not in (result.metadata or {})
        assert EXECUTION_UNCERTAIN_METADATA_KEY not in (result.metadata or {})
        assert (result.metadata or {}).get("gated_window_id") == "gwin_abc123"
        assert (result.metadata or {}).get("gated_evidence") == "gated_evidence_failed"
        assert ext.closed == 1
        assert policy.retained_for("run_001.inst_001") is None

    asyncio.run(go())


def test_postspawn_loss_is_uncertain() -> None:
    """Post-spawn loss: STARTED plus UNCERTAIN, never a plain refusal."""
    async def go():
        ext = _FakeExtension(_policy())
        evidence = _FakeEvidence()
        policy = GatedExecutionPolicy(
            ext, scope_validator=_validator, evidence_store=evidence
        )

        async def delegate():
            raise ShellWorkroomError("shell_spawn_lost", uncertainty=True)

        result = await policy.execute(
            _invocation(SHELL_TARGET_JOB_START), _parsed(), delegate
        )
        assert result.success is False
        assert (result.metadata or {}).get(EXECUTION_STARTED_METADATA_KEY) is True
        assert (result.metadata or {}).get(EXECUTION_UNCERTAIN_METADATA_KEY) is True
        assert ext.closed == 1
        assert policy.retained_for("run_001.inst_001") is None

    asyncio.run(go())
