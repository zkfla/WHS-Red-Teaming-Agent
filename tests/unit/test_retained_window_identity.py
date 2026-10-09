"""Retained window identity refuses settlement of another reservation."""

from dataclasses import replace

import pytest

from redteam.capability.gated_strategy import GatedExecutionPolicy
from redteam.control.network_ledger import SQLiteNetworkGrantLedger
from redteam.core.contracts.network import GrantRefused, NetworkPurpose
from tests.unit.test_gated_strategy_execute import (
    _allow_all,
    _FakeEvidence,
    _job_invocation,
    _ledger_policy,
    _LedgerWindow,
    _running_ack,
)


@pytest.mark.parametrize("field", ["run_id", "instance_id", "epoch", "purpose", "window_id"])
async def test_foreign_retained_record_leaves_original_window_open(tmp_path, field):
    config = _ledger_policy()
    ledger = SQLiteNetworkGrantLedger(
        str(tmp_path / "network.db"), engagement_id="eng_recover_001", budget=config.budget
    )
    await ledger.setup()
    evidence = _FakeEvidence()
    policy = GatedExecutionPolicy(
        _LedgerWindow(config, ledger), scope_validator=_allow_all, evidence_store=evidence
    )

    async def delegate():
        return _running_ack()

    result = await policy.execute(_job_invocation(), {"language": "bash", "script": "true"},
                                  delegate)
    record = policy.retained_for("run_001.inst_001")
    assert record is not None
    changed = {
        "run_id": "other-run", "instance_id": "other-instance", "epoch": record.epoch + 1,
        "purpose": NetworkPurpose.INSTALL, "window_id": "unknown-window",
    }
    refused = await policy.recover_retained(replace(record, **{field: changed[field]}))
    assert refused.settlement is None
    assert refused.evidence_error is not None
    assert evidence.records == []
    # A settlement would quarantine the original. It must still be OPEN.
    next_grant = await ledger.request_window(
        run_id="run_001", instance_id="inst_001", epoch=record.epoch + 2,
        caps=config.default_target_caps,
    )
    assert isinstance(next_grant, GrantRefused) and next_grant.code == "window_busy"
    assert result.metadata["gated_window_id"] == record.window_id


async def test_stale_close_preserves_current_retained_holder():
    from tests.unit.test_gated_strategy_execute import _FakeExtension, _invocation, _parsed, _policy
    extension = _FakeExtension(_policy())
    policy = GatedExecutionPolicy(extension, scope_validator=_allow_all)

    async def delegate():
        return _running_ack()

    await policy.execute(_invocation("shell_target_job_start"), _parsed(), delegate)
    record = policy.retained_for("run_001.inst_001")
    assert record is not None
    refused = await policy.close_retained(record.key, window_id="previous-window")
    assert refused.settlement is None
    assert refused.evidence_error == "gated_window_identity_mismatch"
    assert policy.retained_for(record.key) is record
    assert extension.closed == 0
