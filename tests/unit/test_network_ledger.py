"""Network budget failure/restart scenarios; no Docker or external traffic."""

import asyncio
import sqlite3

import pytest

from redteam.control.network_ledger import SQLiteNetworkGrantLedger
from redteam.core.contracts.network import (
    GrantRefused,
    MeterReading,
    NetworkBudget,
    NetworkCapacity,
    NetworkGrant,
    NetworkPurpose,
    WindowCaps,
    WindowState,
)
from redteam.core.ports.network_grants import (
    NetworkGrantLedgerReadError,
    OutstandingNetworkWindow,
)


def budget(deliveries=6, byte_cap=1000, rate=None):
    capacity = NetworkCapacity(max_flow_init_deliveries=deliveries, max_output_bytes=byte_cap)
    return NetworkBudget(global_capacity=capacity, target_capacity=capacity,
                         install_capacity=capacity, delivery_rate_per_second=rate)


def caps(deliveries=3, byte_cap=600, purpose=NetworkPurpose.TARGET):
    return WindowCaps(max_flow_init_deliveries=deliveries, max_output_bytes=byte_cap,
                      duration_seconds=30, purpose=purpose)


async def ledger(path, configured=None, clock=lambda: 100.0):
    result = SQLiteNetworkGrantLedger(path, engagement_id="eng", budget=configured or budget(),
                                     now=clock)
    await result.setup()
    return result


@pytest.mark.asyncio
async def test_two_independent_gate_reservations_cannot_overshoot_shared_bytes(tmp_path):
    first = await ledger(tmp_path / "state.db")
    second = await ledger(tmp_path / "state.db")
    results = await asyncio.gather(
        first.request_window(run_id="run", instance_id="a", epoch=1, caps=caps()),
        second.request_window(run_id="run", instance_id="b", epoch=1, caps=caps()),
    )
    assert sum(isinstance(result, NetworkGrant) for result in results) == 1
    refused = next(result for result in results if isinstance(result, GrantRefused))
    assert refused.code == "global_bytes_exhausted"


@pytest.mark.asyncio
async def test_lost_admission_reply_is_charged_once_and_seq_reuse_quarantines(tmp_path):
    path = tmp_path / "state.db"
    first = await ledger(path)
    window = await first.request_window(run_id="run", instance_id="a", epoch=1, caps=caps())
    assert isinstance(window, NetworkGrant)
    args = dict(window_id=window.window_id, daemon_session_id="daemon", seq=1, flow_key="flow")
    assert (await first.admit_delivery(**args)).charged_delivery
    resumed = await ledger(path)
    duplicate = await resumed.admit_delivery(**args)
    assert duplicate.accepted and not duplicate.charged_delivery
    changed = await resumed.admit_delivery(**{**args, "flow_key": "another-packet"})
    assert not changed.accepted and changed.reason == "seq_reuse_forbidden"
    blocked = await resumed.request_window(run_id="run", instance_id="a", epoch=2, caps=caps())
    assert isinstance(blocked, GrantRefused) and blocked.code == "quarantined"
    settled = await resumed.settle_window(window_id=window.window_id,
        meter=MeterReading(metered_bytes=0, metered_packets=0), verified_closed=True)
    assert settled.released_bytes == settled.released_deliveries == 0


@pytest.mark.asyncio
async def test_unknown_reservation_never_refunds_on_restart_or_verified_later_release(tmp_path):
    path = tmp_path / "state.db"
    first = await ledger(path)
    window = await first.request_window(run_id="run", instance_id="a", epoch=1, caps=caps())
    assert isinstance(window, NetworkGrant)
    settled = await first.settle_window(window_id=window.window_id, meter=None,
                                        verified_closed=False)
    assert settled.state is WindowState.QUARANTINED
    assert settled.charged_bytes == 600 and settled.released_bytes == 0
    resumed = await ledger(path)
    assert not await resumed.release_quarantine(window_id=window.window_id, verified_closed=False)
    assert await resumed.release_quarantine(window_id=window.window_id, verified_closed=True)
    refused = await resumed.request_window(run_id="run", instance_id="a", epoch=2, caps=caps())
    assert isinstance(refused, GrantRefused) and refused.code == "global_bytes_exhausted"
    repeated = await resumed.settle_window(window_id=window.window_id,
        meter=MeterReading(metered_bytes=0, metered_packets=0), verified_closed=True)
    assert repeated == settled


@pytest.mark.asyncio
async def test_verified_unused_capacity_returns_once_and_downstream_drop_stays_charged(tmp_path):
    subject = await ledger(tmp_path / "state.db")
    window = await subject.request_window(run_id="run", instance_id="a", epoch=1, caps=caps())
    assert isinstance(window, NetworkGrant)
    await subject.admit_delivery(window_id=window.window_id, daemon_session_id="daemon",
                                 seq=1, flow_key="post-verdict-drop")
    args = dict(window_id=window.window_id,
                meter=MeterReading(metered_bytes=100, metered_packets=2), verified_closed=True)
    settled = await subject.settle_window(**args)
    assert settled.charged_deliveries == settled.committed_deliveries == 1
    assert settled.charged_bytes == 100 and settled.released_bytes == 500
    assert await subject.settle_window(**args) == settled
    # A duplicate settlement must not mint another delivery or byte reservation.
    next_window = await subject.request_window(run_id="run", instance_id="a", epoch=2,
                                               caps=caps(deliveries=5, byte_cap=900))
    assert isinstance(next_window, NetworkGrant)
    blocked = await subject.request_window(run_id="run", instance_id="b", epoch=1,
                                          caps=caps(deliveries=1, byte_cap=1))
    assert isinstance(blocked, GrantRefused)


@pytest.mark.asyncio
async def test_meter_overshoot_is_visible_and_quarantined_instead_of_clamped(tmp_path):
    subject = await ledger(tmp_path / "state.db")
    window = await subject.request_window(run_id="run", instance_id="a", epoch=1, caps=caps())
    assert isinstance(window, NetworkGrant)
    settled = await subject.settle_window(window_id=window.window_id,
        meter=MeterReading(metered_bytes=601, metered_packets=7), verified_closed=True)
    assert settled.state is WindowState.QUARANTINED
    assert settled.outcome == "integrity_quarantined"
    assert settled.metered_bytes == 601 and settled.released_bytes == 0


@pytest.mark.asyncio
async def test_global_rate_and_expiry_survive_independent_instance_restart(tmp_path):
    moment = [100.0]
    configured = budget(deliveries=10, byte_cap=2000, rate=1)
    path = tmp_path / "state.db"
    first = await ledger(path, configured, lambda: moment[0])
    windows = [await first.request_window(run_id="run", instance_id=name, epoch=1, caps=caps())
               for name in ("a", "b")]
    assert all(isinstance(window, NetworkGrant) for window in windows)
    a, b = windows
    assert isinstance(a, NetworkGrant) and isinstance(b, NetworkGrant)
    assert (await first.admit_delivery(window_id=a.window_id, daemon_session_id="a",
                                      seq=1, flow_key="a")).accepted
    resumed = await ledger(path, configured, lambda: moment[0])
    denied = await resumed.admit_delivery(window_id=b.window_id, daemon_session_id="b",
                                         seq=1, flow_key="b")
    assert denied.reason == "global_delivery_rate_exhausted"
    moment[0] = 131
    expired = await resumed.admit_delivery(window_id=a.window_id, daemon_session_id="a",
                                          seq=2, flow_key="late")
    assert not expired.accepted and expired.reason == "window_expired"
    refused = await resumed.request_window(run_id="run", instance_id="a", epoch=2, caps=caps())
    assert isinstance(refused, GrantRefused) and refused.code == "quarantined"
    with pytest.raises(ValueError, match="differs"):
        await ledger(path, budget(deliveries=10, byte_cap=2000, rate=None))


@pytest.mark.asyncio
async def test_outstanding_windows_are_engagement_run_instance_and_state_scoped(tmp_path):
    path = tmp_path / "state.db"
    subject = await ledger(path, budget(deliveries=20, byte_cap=8000))
    same_identity_other_engagement = SQLiteNetworkGrantLedger(
        path, engagement_id="other-eng", budget=budget(deliveries=20, byte_cap=8000),
        now=lambda: 100.0,
    )
    await same_identity_other_engagement.setup()

    open_grant = await subject.request_window(
        run_id="run-a", instance_id="instance-a", epoch=1, caps=caps(),
    )
    quarantine_grant = await subject.request_window(
        run_id="run-a", instance_id="instance-b", epoch=1, caps=caps(),
    )
    released_grant = await subject.request_window(
        run_id="run-b", instance_id="instance-a", epoch=1, caps=caps(),
    )
    foreign_grant = await same_identity_other_engagement.request_window(
        run_id="run-a", instance_id="instance-a", epoch=1, caps=caps(),
    )
    assert all(isinstance(item, NetworkGrant) for item in (
        open_grant, quarantine_grant, released_grant, foreign_grant,
    ))
    assert isinstance(open_grant, NetworkGrant)
    assert isinstance(quarantine_grant, NetworkGrant)
    assert isinstance(released_grant, NetworkGrant)
    assert isinstance(foreign_grant, NetworkGrant)

    quarantine_settlement = await subject.settle_window(
        window_id=quarantine_grant.window_id, meter=None, verified_closed=False,
    )
    released_settlement = await subject.settle_window(
        window_id=released_grant.window_id,
        meter=MeterReading(metered_bytes=0, metered_packets=0),
        verified_closed=True,
    )

    rows = await subject.outstanding_windows(run_id="run-a", instance_id="instance-a")
    assert rows == (OutstandingNetworkWindow(
        grant=open_grant, state=WindowState.OPEN, settlement=None,
    ),)
    quarantined = await subject.outstanding_windows(run_id="run-a", instance_id="instance-b")
    assert quarantined == (OutstandingNetworkWindow(
        grant=quarantine_grant,
        state=WindowState.QUARANTINED,
        settlement=quarantine_settlement,
    ),)
    assert quarantine_settlement.state is WindowState.QUARANTINED
    assert released_settlement.state is WindowState.RELEASED
    assert await subject.outstanding_windows(run_id="run-b", instance_id="instance-a") == ()
    assert await same_identity_other_engagement.outstanding_windows(
        run_id="run-a", instance_id="instance-a",
    ) == (OutstandingNetworkWindow(
        grant=foreign_grant, state=WindowState.OPEN, settlement=None,
    ),)


@pytest.mark.asyncio
async def test_outstanding_windows_empty_is_distinct_from_read_failures(tmp_path):
    subject = await ledger(tmp_path / "state.db")
    assert await subject.outstanding_windows(run_id="run", instance_id="instance") == ()

    missing_path = tmp_path / "missing.db"
    missing = SQLiteNetworkGrantLedger(
        missing_path, engagement_id="eng", budget=budget(),
    )
    with pytest.raises(NetworkGrantLedgerReadError) as exc:
        await missing.outstanding_windows(run_id="run", instance_id="instance")
    assert exc.value.reason_code == "network_window_ledger_unavailable"
    assert not missing_path.exists()

    schema_path = tmp_path / "no-network-schema.db"
    sqlite3.connect(schema_path).close()
    no_schema = SQLiteNetworkGrantLedger(
        schema_path, engagement_id="eng", budget=budget(),
    )
    with pytest.raises(NetworkGrantLedgerReadError) as exc:
        await no_schema.outstanding_windows(run_id="run", instance_id="instance")
    assert exc.value.reason_code == "network_window_schema_unavailable"


@pytest.mark.asyncio
async def test_outstanding_windows_rejects_blank_identity_with_stable_error(tmp_path):
    subject = await ledger(tmp_path / "state.db")
    with pytest.raises(NetworkGrantLedgerReadError) as exc:
        await subject.outstanding_windows(run_id=" ", instance_id="instance")
    assert exc.value.reason_code == "network_window_query_identity_invalid"


@pytest.mark.asyncio
async def test_outstanding_windows_rejects_corrupt_grant_identity(tmp_path):
    path = tmp_path / "state.db"
    subject = await ledger(path)
    grant = await subject.request_window(
        run_id="run", instance_id="instance", epoch=1, caps=caps(),
    )
    assert isinstance(grant, NetworkGrant)
    corrupt = grant.model_copy(update={"instance_id": "other-instance"})
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE network_windows SET grant_json=? WHERE engagement=? AND window_id=?",
            (corrupt.model_dump_json(), "eng", grant.window_id),
        )

    with pytest.raises(NetworkGrantLedgerReadError) as exc:
        await subject.outstanding_windows(run_id="run", instance_id="instance")
    assert exc.value.reason_code == "network_window_record_invalid"


@pytest.mark.asyncio
async def test_outstanding_windows_rejects_corrupt_settlement_identity(tmp_path):
    path = tmp_path / "state.db"
    subject = await ledger(path)
    grant = await subject.request_window(
        run_id="run", instance_id="instance", epoch=1, caps=caps(),
    )
    assert isinstance(grant, NetworkGrant)
    settlement = await subject.settle_window(
        window_id=grant.window_id, meter=None, verified_closed=False,
    )
    corrupt = settlement.model_copy(update={"window_id": "another-window"})
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE network_windows SET settlement_json=? WHERE engagement=? AND window_id=?",
            (corrupt.model_dump_json(), "eng", grant.window_id),
        )

    with pytest.raises(NetworkGrantLedgerReadError) as exc:
        await subject.outstanding_windows(run_id="run", instance_id="instance")
    assert exc.value.reason_code == "network_window_record_invalid"


@pytest.mark.asyncio
async def test_outstanding_windows_rejects_caps_mismatch(tmp_path):
    path = tmp_path / "state.db"
    subject = await ledger(path)
    grant = await subject.request_window(
        run_id="run", instance_id="instance", epoch=1, caps=caps(),
    )
    assert isinstance(grant, NetworkGrant)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE network_windows SET caps_json=? WHERE engagement=? AND window_id=?",
            ("{}", "eng", grant.window_id),
        )

    with pytest.raises(NetworkGrantLedgerReadError) as exc:
        await subject.outstanding_windows(run_id="run", instance_id="instance")
    assert exc.value.reason_code == "network_window_record_invalid"


@pytest.mark.asyncio
async def test_outstanding_windows_rejects_settlement_committed_count_mismatch(tmp_path):
    path = tmp_path / "state.db"
    subject = await ledger(path)
    grant = await subject.request_window(
        run_id="run", instance_id="instance", epoch=1, caps=caps(),
    )
    assert isinstance(grant, NetworkGrant)
    settlement = await subject.settle_window(
        window_id=grant.window_id, meter=None, verified_closed=False,
    )
    corrupt = settlement.model_copy(update={"committed_deliveries": 1})
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE network_windows SET settlement_json=? WHERE engagement=? AND window_id=?",
            (corrupt.model_dump_json(), "eng", grant.window_id),
        )

    with pytest.raises(NetworkGrantLedgerReadError) as exc:
        await subject.outstanding_windows(run_id="run", instance_id="instance")
    assert exc.value.reason_code == "network_window_record_invalid"


@pytest.mark.asyncio
async def test_outstanding_windows_rejects_reservation_accounting_mismatch(tmp_path):
    path = tmp_path / "state.db"
    subject = await ledger(path)
    grant = await subject.request_window(
        run_id="run", instance_id="instance", epoch=1, caps=caps(),
    )
    assert isinstance(grant, NetworkGrant)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE network_pools SET available_bytes=available_bytes+1 "
            "WHERE engagement=? AND purpose='global'",
            ("eng",),
        )

    with pytest.raises(NetworkGrantLedgerReadError) as exc:
        await subject.outstanding_windows(run_id="run", instance_id="instance")
    assert exc.value.reason_code == "network_window_record_invalid"


@pytest.mark.asyncio
async def test_outstanding_windows_preserves_quarantine_without_settlement(tmp_path):
    subject = await ledger(tmp_path / "state.db")
    grant = await subject.request_window(
        run_id="run", instance_id="instance", epoch=1, caps=caps(),
    )
    assert isinstance(grant, NetworkGrant)
    accepted = await subject.admit_delivery(
        window_id=grant.window_id,
        daemon_session_id="daemon-one",
        seq=1,
        flow_key="flow-one",
    )
    assert accepted.accepted
    refused = await subject.admit_delivery(
        window_id=grant.window_id,
        daemon_session_id="daemon-two",
        seq=2,
        flow_key="flow-two",
    )
    assert not refused.accepted
    assert refused.reason == "daemon_restart_requires_reconciliation"

    rows = await subject.outstanding_windows(run_id="run", instance_id="instance")
    assert rows == (OutstandingNetworkWindow(
        grant=grant, state=WindowState.QUARANTINED, settlement=None,
    ),)


@pytest.mark.asyncio
async def test_outstanding_windows_rejects_refunded_quarantine_receipt(tmp_path):
    path = tmp_path / "state.db"
    subject = await ledger(path)
    grant = await subject.request_window(
        run_id="run", instance_id="instance", epoch=1, caps=caps(),
    )
    assert isinstance(grant, NetworkGrant)
    settlement = await subject.settle_window(
        window_id=grant.window_id, meter=None, verified_closed=False,
    )
    refunded = settlement.model_copy(update={
        "charged_deliveries": 0,
        "charged_bytes": 0,
        "released_deliveries": grant.reserved_deliveries,
        "released_bytes": grant.reserved_bytes,
    })
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE network_windows SET settlement_json=? WHERE engagement=? AND window_id=?",
            (refunded.model_dump_json(), "eng", grant.window_id),
        )
        db.execute(
            "UPDATE network_pools SET available_deliveries=available_deliveries+?, "
            "available_bytes=available_bytes+? WHERE engagement=? "
            "AND purpose IN ('global','target')",
            (grant.reserved_deliveries, grant.reserved_bytes, "eng"),
        )

    with pytest.raises(NetworkGrantLedgerReadError) as exc:
        await subject.outstanding_windows(run_id="run", instance_id="instance")
    assert exc.value.reason_code == "network_window_record_invalid"
