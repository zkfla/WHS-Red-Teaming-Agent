"""Durable network capacity, independent of action/tool-call accounting.

Flow-init deliveries are conservative durable admissions before a queue
verdict, including admissions whose packet is subsequently dropped. They
are neither unique connections nor application requests. Bytes and packets
are the protected boundary's post-accept counters; quota.used is not a meter.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# Storage/display identity of the protected provider's existing companion.
# This is metering and destination-attempt linkage, never remote content proof.
WINDOW_EGRESS_SOURCE_ID = "whs-gated-window-v1"
WINDOW_EGRESS_RECORD_KIND = "window_egress_admission"


class NetworkPurpose(str, Enum):
    TARGET = "target"
    INSTALL = "install"


class WindowState(str, Enum):
    OPEN = "open"
    QUARANTINED = "quarantined"
    RELEASED = "released"


class _NetworkValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class NetworkCapacity(_NetworkValue):
    max_flow_init_deliveries: int = Field(ge=0, strict=True)
    max_output_bytes: int = Field(ge=0, strict=True)


class NetworkBudget(_NetworkValue):
    """One engagement's shared ceiling and separate purpose purses.

    Configuring a budget alone does not authorize any destination or open
    a gate. Both purpose capacities must be explicitly configured, including
    a zero capacity for a disabled purpose.
    """

    global_capacity: NetworkCapacity
    target_capacity: NetworkCapacity
    install_capacity: NetworkCapacity
    delivery_rate_per_second: float | None = Field(default=None, gt=0)

    def for_purpose(self, purpose: NetworkPurpose) -> NetworkCapacity:
        return self.target_capacity if purpose is NetworkPurpose.TARGET else self.install_capacity


class WindowCaps(_NetworkValue):
    max_flow_init_deliveries: int = Field(gt=0, strict=True)
    max_output_bytes: int = Field(gt=0, strict=True)
    delivery_rate_per_second: float | None = Field(default=None, gt=0)
    duration_seconds: float = Field(gt=0)
    purpose: NetworkPurpose


class NetworkGrant(_NetworkValue):
    """Capacity reservation; destination authority stays with Control policy."""

    window_id: str
    run_id: str
    instance_id: str
    epoch: int = Field(ge=1, strict=True)
    purpose: NetworkPurpose
    reserved_deliveries: int = Field(gt=0, strict=True)
    reserved_bytes: int = Field(gt=0, strict=True)
    deadline_epoch_seconds: float


class GrantRefused(_NetworkValue):
    code: str
    retryable: bool = False


class AdmissionDecision(_NetworkValue):
    accepted: bool
    seq: int = Field(ge=0, strict=True)
    reason: str
    charged_delivery: bool = False


class MeterReading(_NetworkValue):
    """Protected post-accept counters, never model-supplied usage claims.

    Delivery consumption is read from the durable ledger, not this meter.
    Packets are informational; no cumulative packet cap is promised.
    """

    metered_bytes: int = Field(ge=0, strict=True)
    metered_packets: int = Field(ge=0, strict=True)


class WindowSettlement(_NetworkValue):
    window_id: str
    state: WindowState
    charged_deliveries: int = Field(ge=0, strict=True)
    charged_bytes: int = Field(ge=0, strict=True)
    released_deliveries: int = Field(ge=0, strict=True)
    released_bytes: int = Field(ge=0, strict=True)
    committed_deliveries: int = Field(ge=0, strict=True)
    metered_bytes: int | None = Field(default=None, ge=0, strict=True)
    metered_packets: int | None = Field(default=None, ge=0, strict=True)
    outcome: str
