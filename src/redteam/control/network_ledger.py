"""One engagement's durable network reservations and conservative admissions.

This ledger owns capacity, not destination policy or kernel inspection.
Only the trusted Control/controller composition supplies verification.
Unknown windows keep their complete reservations across process restarts.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from redteam.core.contracts.network import (
    AdmissionDecision,
    GrantRefused,
    MeterReading,
    NetworkBudget,
    NetworkGrant,
    WindowCaps,
    WindowSettlement,
    WindowState,
)
from redteam.sqlite_support import run_transaction

_MAX_SQLITE_INTEGER = 2**63 - 1


class SQLiteNetworkGrantLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        engagement_id: str,
        budget: NetworkBudget,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not engagement_id:
            raise ValueError("network ledger requires engagement identity")
        self._path = Path(path)
        self._engagement = engagement_id
        self._budget = budget
        self._now = now

    def _clock(self) -> float:
        value = self._now()
        if not math.isfinite(value):
            raise ValueError("network ledger clock must be finite")
        return value

    async def setup(self) -> None:
        for capacity in (self._budget.global_capacity, self._budget.target_capacity,
                         self._budget.install_capacity):
            if max(capacity.max_flow_init_deliveries, capacity.max_output_bytes) > (
                _MAX_SQLITE_INTEGER
            ):
                raise ValueError("network capacity exceeds SQLite integer storage")
        self._path.parent.mkdir(parents=True, exist_ok=True)

        def initialize(db: sqlite3.Connection) -> None:
            # execute individually: executescript would implicitly commit
            # the BEGIN IMMEDIATE that guards setup and budget identity.
            db.execute("""CREATE TABLE IF NOT EXISTS network_pools (
                engagement TEXT NOT NULL, purpose TEXT NOT NULL,
                max_deliveries INTEGER NOT NULL, max_bytes INTEGER NOT NULL,
                available_deliveries INTEGER NOT NULL, available_bytes INTEGER NOT NULL,
                last_delivery REAL, budget_json TEXT,
                PRIMARY KEY(engagement,purpose))""")
            db.execute("""CREATE TABLE IF NOT EXISTS network_windows (
                window_id TEXT PRIMARY KEY, engagement TEXT NOT NULL,
                run_id TEXT NOT NULL, instance_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                purpose TEXT NOT NULL, state TEXT NOT NULL, grant_json TEXT NOT NULL,
                caps_json TEXT NOT NULL, committed_deliveries INTEGER NOT NULL DEFAULT 0,
                daemon_session TEXT, last_delivery REAL, settlement_json TEXT,
                UNIQUE(engagement,run_id,instance_id,epoch))""")
            db.execute("""CREATE TABLE IF NOT EXISTS network_seq_log (
                window_id TEXT NOT NULL REFERENCES network_windows(window_id),
                seq INTEGER NOT NULL, daemon_session TEXT NOT NULL,
                flow_key TEXT NOT NULL, decision_json TEXT NOT NULL,
                PRIMARY KEY(window_id,seq))""")
            capacities = {
                "global": self._budget.global_capacity,
                "target": self._budget.target_capacity,
                "install": self._budget.install_capacity,
            }
            for purpose, capacity in capacities.items():
                existing = db.execute(
                    "SELECT max_deliveries,max_bytes,budget_json FROM network_pools "
                    "WHERE engagement=? AND purpose=?", (self._engagement, purpose)
                ).fetchone()
                limits = (capacity.max_flow_init_deliveries, capacity.max_output_bytes)
                if existing is not None:
                    if tuple(existing[:2]) != limits or (
                        purpose == "global" and existing[2] != self._budget.model_dump_json()
                    ):
                        raise ValueError("persisted network budget differs from configuration")
                    continue
                db.execute("INSERT INTO network_pools(engagement,purpose,max_deliveries,max_bytes,"
                           "available_deliveries,available_bytes,budget_json) "
                           "VALUES (?,?,?,?,?,?,?)",
                           (self._engagement, purpose, *limits, *limits,
                            self._budget.model_dump_json() if purpose == "global" else None))

        await run_transaction(self._path, initialize, write=True)

    async def request_window(
        self, *, run_id: str, instance_id: str, epoch: int, caps: WindowCaps
    ) -> NetworkGrant | GrantRefused:
        if not run_id or not instance_id or type(epoch) is not int or not 0 < epoch < 2**63:
            return GrantRefused(code="window_identity_invalid")
        if max(caps.max_flow_init_deliveries, caps.max_output_bytes) > _MAX_SQLITE_INTEGER:
            return GrantRefused(code="caps_invalid")
        moment = self._clock()
        identity = json.dumps([self._engagement, run_id, instance_id, epoch])
        grant = NetworkGrant(
            window_id="nwin_" + hashlib.sha256(identity.encode()).hexdigest(),
            run_id=run_id, instance_id=instance_id, epoch=epoch, purpose=caps.purpose,
            reserved_deliveries=caps.max_flow_init_deliveries,
            reserved_bytes=caps.max_output_bytes,
            deadline_epoch_seconds=moment + caps.duration_seconds,
        )

        def reserve(db: sqlite3.Connection) -> NetworkGrant | GrantRefused:
            active = db.execute(
                "SELECT state FROM network_windows WHERE engagement=? AND run_id=? "
                "AND instance_id=? AND state!=? LIMIT 1",
                (self._engagement, run_id, instance_id, WindowState.RELEASED.value),
            ).fetchone()
            if active:
                return GrantRefused(code="window_busy" if active[0] == "open" else "quarantined")
            latest = db.execute(
                "SELECT MAX(epoch) FROM network_windows WHERE engagement=? AND run_id=? "
                "AND instance_id=?", (self._engagement, run_id, instance_id),
            ).fetchone()[0]
            if latest is not None and epoch <= latest:
                return GrantRefused(code="epoch_stale")
            for purpose in ("global", caps.purpose.value):
                available = db.execute(
                    "SELECT available_deliveries,available_bytes FROM network_pools "
                    "WHERE engagement=? AND purpose=?", (self._engagement, purpose),
                ).fetchone()
                if available is None:
                    return GrantRefused(code="network_budget_unconfigured")
                if available[0] < grant.reserved_deliveries:
                    return GrantRefused(code=f"{purpose}_deliveries_exhausted")
                if available[1] < grant.reserved_bytes:
                    return GrantRefused(code=f"{purpose}_bytes_exhausted")
            for purpose in ("global", caps.purpose.value):
                db.execute(
                    "UPDATE network_pools SET available_deliveries=available_deliveries-?, "
                    "available_bytes=available_bytes-? WHERE engagement=? AND purpose=?",
                    (grant.reserved_deliveries, grant.reserved_bytes, self._engagement, purpose),
                )
            db.execute(
                "INSERT INTO network_windows(window_id,engagement,run_id,instance_id,epoch,"
                "purpose,state,grant_json,caps_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (grant.window_id, self._engagement, run_id, instance_id, epoch,
                 caps.purpose.value, "open", grant.model_dump_json(), caps.model_dump_json()),
            )
            return grant

        return await run_transaction(self._path, reserve, write=True)

    async def admit_delivery(
        self, *, window_id: str, daemon_session_id: str, seq: int, flow_key: str
    ) -> AdmissionDecision:
        if type(seq) is not int or not 0 <= seq < 2**63:
            raise ValueError("network sequence must fit a nonnegative SQLite integer")
        moment = self._clock()

        def admit(db: sqlite3.Connection) -> AdmissionDecision:
            def refused(reason: str) -> AdmissionDecision:
                return AdmissionDecision(accepted=False, seq=seq, reason=reason)

            row = db.execute(
                "SELECT state,grant_json,caps_json,committed_deliveries,daemon_session,"
                "last_delivery FROM network_windows WHERE engagement=? AND window_id=?",
                (self._engagement, window_id),
            ).fetchone()
            if row is None or row[0] != "open":
                return refused("window_not_open")
            grant = NetworkGrant.model_validate_json(row[1])
            caps = WindowCaps.model_validate_json(row[2])
            if moment >= grant.deadline_epoch_seconds:
                db.execute("UPDATE network_windows SET state=? WHERE window_id=?",
                           ("quarantined", window_id))
                return refused("window_expired")
            if not daemon_session_id or not flow_key or len(flow_key) > 256:
                return refused("admission_identity_invalid")
            previous = db.execute(
                "SELECT daemon_session,flow_key,decision_json FROM network_seq_log "
                "WHERE window_id=? AND seq=?", (window_id, seq),
            ).fetchone()
            if previous is not None:
                if previous[0] != daemon_session_id or previous[1] != flow_key:
                    db.execute("UPDATE network_windows SET state=? WHERE window_id=?",
                               ("quarantined", window_id))
                    return refused("seq_reuse_forbidden")
                previous_decision = AdmissionDecision.model_validate_json(previous[2])
                return previous_decision.model_copy(update={
                    "charged_delivery": False,
                    "reason": (
                        "duplicate_replay"
                        if previous_decision.accepted else previous_decision.reason
                    ),
                })
            if row[4] is not None and row[4] != daemon_session_id:
                db.execute("UPDATE network_windows SET state=? WHERE window_id=?",
                           ("quarantined", window_id))
                return refused("daemon_restart_requires_reconciliation")
            if row[4] is None:
                db.execute("UPDATE network_windows SET daemon_session=? WHERE window_id=?",
                           (daemon_session_id, window_id))
            if row[3] >= grant.reserved_deliveries:
                decision = refused("deliveries_exhausted")
            elif self._global_rate_exhausted(db, moment):
                decision = refused("global_delivery_rate_exhausted")
            elif (caps.delivery_rate_per_second is not None and row[5] is not None
                  and moment - row[5] < 1 / caps.delivery_rate_per_second):
                decision = refused("delivery_rate_exhausted")
            else:
                decision = AdmissionDecision(
                    accepted=True, seq=seq, reason="committed", charged_delivery=True,
                )
                db.execute(
                    "UPDATE network_windows SET committed_deliveries=committed_deliveries+1,"
                    "daemon_session=?,last_delivery=? WHERE window_id=?",
                    (daemon_session_id, moment, window_id),
                )
                db.execute(
                    "UPDATE network_pools SET last_delivery=? WHERE engagement=? AND purpose=?",
                    (moment, self._engagement, "global"),
                )
            db.execute("INSERT INTO network_seq_log VALUES(?,?,?,?,?)",
                       (window_id, seq, daemon_session_id, flow_key, decision.model_dump_json()))
            return decision

        return await run_transaction(self._path, admit, write=True)

    def _global_rate_exhausted(self, db: sqlite3.Connection, moment: float) -> bool:
        rate = self._budget.delivery_rate_per_second
        if rate is None:
            return False
        last = db.execute(
            "SELECT last_delivery FROM network_pools WHERE engagement=? AND purpose=?",
            (self._engagement, "global"),
        ).fetchone()
        return last is not None and last[0] is not None and moment - last[0] < 1 / rate

    async def settle_window(
        self, *, window_id: str, meter: MeterReading | None, verified_closed: bool
    ) -> WindowSettlement:
        def settle(db: sqlite3.Connection) -> WindowSettlement:
            row = db.execute(
                "SELECT grant_json,committed_deliveries,settlement_json,state "
                "FROM network_windows "
                "WHERE engagement=? AND window_id=?", (self._engagement, window_id),
            ).fetchone()
            if row is None:
                raise ValueError("unknown network window")
            if row[2] is not None:
                return WindowSettlement.model_validate_json(row[2])
            grant = NetworkGrant.model_validate_json(row[0])
            integrity_failure = meter is not None and meter.metered_bytes > grant.reserved_bytes
            proven = (
                row[3] == "open" and verified_closed and meter is not None and not integrity_failure
            )
            settlement = WindowSettlement(
                window_id=window_id,
                state=WindowState.RELEASED if proven else WindowState.QUARANTINED,
                charged_deliveries=row[1] if proven else grant.reserved_deliveries,
                charged_bytes=meter.metered_bytes if proven and meter else grant.reserved_bytes,
                released_deliveries=grant.reserved_deliveries - row[1] if proven else 0,
                released_bytes=(
                    grant.reserved_bytes - meter.metered_bytes if proven and meter else 0
                ),
                committed_deliveries=row[1],
                metered_bytes=meter.metered_bytes if meter else None,
                metered_packets=meter.metered_packets if meter else None,
                outcome=("integrity_quarantined" if integrity_failure else
                         "closed_verified" if proven else "unknown_fully_charged"),
            )
            for purpose in ("global", grant.purpose.value):
                db.execute(
                    "UPDATE network_pools SET available_deliveries=available_deliveries+?,"
                    "available_bytes=available_bytes+? WHERE engagement=? AND purpose=?",
                    (settlement.released_deliveries, settlement.released_bytes,
                     self._engagement, purpose),
                )
            db.execute(
                "UPDATE network_windows SET state=?,settlement_json=? WHERE window_id=?",
                (settlement.state.value, settlement.model_dump_json(), window_id),
            )
            return settlement

        return await run_transaction(self._path, settle, write=True)

    async def window_grant(self, *, window_id: str) -> NetworkGrant | None:
        """Original grant for one window id, or None when unknown."""
        def read(db: sqlite3.Connection) -> NetworkGrant | None:
            row = db.execute(
                "SELECT grant_json FROM network_windows "
                "WHERE engagement=? AND window_id=?",
                (self._engagement, window_id),
            ).fetchone()
            if row is None:
                return None
            return NetworkGrant.model_validate_json(row[0])

        return await run_transaction(self._path, read, write=False)

    async def release_quarantine(self, *, window_id: str, verified_closed: bool) -> bool:
        if not verified_closed:
            return False

        def release(db: sqlite3.Connection) -> bool:
            row = db.execute(
                "SELECT state,settlement_json FROM network_windows WHERE engagement=? "
                "AND window_id=?", (self._engagement, window_id),
            ).fetchone()
            # A quarantine must first durably retain its full reservation.
            # Releasing ownership cannot refund that previous settlement.
            if row is None or row[0] != "quarantined" or row[1] is None:
                return False
            db.execute("UPDATE network_windows SET state=? WHERE window_id=?",
                       ("released", window_id))
            return True

        return await run_transaction(self._path, release, write=True)
