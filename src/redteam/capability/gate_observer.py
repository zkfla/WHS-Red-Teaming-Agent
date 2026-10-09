"""Provider-owned kernel observation and closed verification. Owned by Capability.

The observer reads the room netns through a host-side docker runner owned by
the gated-window extension. It never touches the ledger (that is Control's
authority) and never trusts model input: every verified boolean it returns
is computed from kernel output. Its attestations feed the ledger's
``verified_closed`` input; the ledger still holds UNKNOWN on any doubt.

Units, stated honestly: packets/bytes come from named nftables counters on
the post-accept OUTPUT chain, i.e. packets as charged at the IP hook
(SKBs), not a total wire byte cap. ``quota.used`` is a blocker, never a
meter, and is never read here. ``MeterReading`` is outbound-only; there are
no inbound fields (a separate informational proposal goes to root first if
inbound observation is ever needed).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from redteam.core.contracts.network import MeterReading

# Same shape as ``ShellWorkroomManager._run_docker``: argv, stdin,
# timeout, output limit -> (code, stdout, stderr, outer_timeout, cut).
RunDocker = Callable[
    [tuple[str, ...], bytes | None, float, int | None],
    Awaitable[tuple[int | None, bytes, bytes, bool, bool]],
]

_COUNTER_RE = re.compile(
    r"counter\s+(\S+)\s*\{[^}]*?packets\s+(\d+)\s+bytes\s+(\d+)", re.DOTALL
)
_POLICY_DROP_RE = re.compile(r"type\s+filter\s+hook\s+(\w+)\s+[^;]*;\s*policy\s+drop\s*")


@dataclass(frozen=True)
class CounterSample:
    packets: int
    bytes: int


@dataclass(frozen=True)
class ClosedReport:
    closed: bool
    detail: str


def parse_named_counters(ruleset: str) -> dict[str, CounterSample]:
    """Parse ``nft list`` named-counter packets/bytes. Raises on no counters."""
    found = {
        name: CounterSample(packets=int(packets), bytes=int(nbytes))
        for name, packets, nbytes in _COUNTER_RE.findall(ruleset)
    }
    if not found:
        raise ValueError("no named counters in ruleset")
    return found


def build_meter(
    ruleset: str, *, byte_counter: str, packet_counter: str
) -> MeterReading:
    """Build the outbound-only meter from post-accept chain counters."""
    counters = parse_named_counters(ruleset)
    try:
        byte_sample = counters[byte_counter]
        packet_sample = counters[packet_counter]
    except KeyError as exc:
        raise ValueError(f"meter counter missing: {exc}") from exc
    return MeterReading(
        metered_bytes=byte_sample.bytes, metered_packets=packet_sample.packets
    )


def check_default_drop(ruleset: str, *, table: str) -> ClosedReport:
    """Pure check: our table drops on output+input+forward with no accepts.

    Any ``accept`` verdict inside our table, or a missing drop hook, means
    NOT closed. Docker-stock tables in the same dump are ignored (read-only
    observation; they are never evidence for or against host changes).
    """
    start = ruleset.find(f"table inet {table} {{")
    if start < 0:
        return ClosedReport(closed=False, detail=f"table inet {table} absent")
    end = ruleset.find("\ntable ", start + 1)
    _ = end
    body = ruleset[start:] if end < 0 else ruleset[start:end]
    if re.search(r"(?<![\w_])accept(?![\w_])", body):
        return ClosedReport(closed=False, detail=f"table inet {table} holds accept")
    hooks = set(_POLICY_DROP_RE.findall(body))
    missing = {"output", "input", "forward"} - hooks
    if missing:
        return ClosedReport(
            closed=False, detail=f"table inet {table} missing drop: {sorted(missing)}"
        )
    return ClosedReport(closed=True, detail=f"table inet {table} default-drop on all hooks")


def _table_body(ruleset: str, *, table: str) -> str | None:
    start = ruleset.find("table inet " + table + " {")
    if start < 0:
        return None
    end = ruleset.find("\ntable ", start + 1)
    return ruleset[start:] if end < 0 else ruleset[start:end]


def check_open_posture(
    ruleset: str,
    *,
    table: str,
    allowed: tuple[tuple[str, int, str], ...],
    queue_num: int,
    meter_name: str,
) -> ClosedReport:
    """Verify a DEPLOYED open window matches exactly the authorized posture.

    This is not a closure claim: an open window legitimately holds one
    counting accept. It proves default-deny on both output chains, the
    NFQUEUE diversion for flow-inits, the quota gate, exactly the admitted
    allowlist jumps, and no accept other than the named meter. Anything
    else fails closed.
    """
    body = _table_body(ruleset, table=table)
    if body is None:
        return ClosedReport(closed=False, detail="table inet " + table + " absent")
    egress = re.search(r"chain\s+gated_egress\s*\{(.*?)\}", body, re.DOTALL)
    if egress is None:
        return ClosedReport(closed=False, detail="gated_egress chain is missing")
    egress_body = egress.group(1)
    if "policy drop" not in egress_body or "hook output" not in egress_body:
        return ClosedReport(closed=False, detail="gated_egress chain is not default-drop")
    queue_hit = re.search(r"queue\s+(num|to)\s+" + str(queue_num) + r"\b", body)
    if queue_hit is None:
        return ClosedReport(closed=False, detail="nfqueue diversion is missing")
    denies = ("denied_dns_local", "denied_dns_ext", "denied_v6", "denied_other", "denied_gated")
    for marker in denies:
        if marker not in body:
            return ClosedReport(closed=False, detail="explicit deny missing: " + marker)
    quota_hit = re.search(r"quota\s+name\s+\"?qb\"?\s+drop", body)
    if quota_hit is None:
        return ClosedReport(closed=False, detail="byte quota gate is missing")
    for ip, port, proto in allowed:
        wanted = [
            r"ip daddr\s+" + re.escape(ip) + r"(?![\d.])",
            re.escape(proto) + r"\s+dport\s+" + re.escape(str(port)) + r"(?![\d])",
            r"jump\s+meter_accept",
        ]
        if not any(
            all(re.search(part, line) is not None for part in wanted)
            for line in body.splitlines()
        ):
            return ClosedReport(closed=False, detail="allowlist jump missing for " + ip)
    meter_lines = [
        line for line in body.splitlines()
        if "counter name" in line and meter_name in line and "accept" in line
    ]
    if len(meter_lines) != 1:
        return ClosedReport(closed=False, detail="meter accept is not unique")
    if len(meter_lines) != 1:
        return ClosedReport(closed=False, detail="meter accept is not unique")
    chains: dict[str, str] = {}
    for match in re.finditer(r"chain\s+(\w+)\s*\{(.*?)\}", body, re.DOTALL):
        chains[match.group(1)] = match.group(2)
    output_side = "".join(
        chains.get(name, "") for name in ("gated_egress", "gated_flows", "meter_accept")
    )
    output_accepts = re.findall(r"(?<![\w_])accept(?![\w_])", output_side)
    if len(output_accepts) != 1:
        return ClosedReport(closed=False, detail="more than the meter accept is present")
    inbound = chains.get("input", "")
    if "policy drop" not in inbound or "established,related accept" not in inbound:
        return ClosedReport(closed=False, detail="input chain is not return-only")
    if "policy drop" not in chains.get("forward", ""):
        return ClosedReport(closed=False, detail="forward chain is not default-drop")
    return ClosedReport(closed=True, detail="open posture matches the authorized window")


def check_barrier_closed(ruleset: str, *, table: str) -> ClosedReport:
    """Verify the close barrier: a terminal drop ahead of every output rule.

    The barrier chain freezes counters (no verdict below it can run) before
    the meter is read. Only the barrier presence with default-drop plus the
    intact meter object counts; policy text elsewhere is not re-audited here.
    """
    body = _table_body(ruleset, table=table)
    if body is None:
        return ClosedReport(closed=False, detail="table inet " + table + " absent")
    match = re.search(r"chain\s+barrier\s*\{(.*?)\}", body, re.DOTALL)
    if match is None:
        return ClosedReport(closed=False, detail="barrier chain is absent")
    inner = match.group(1)
    priority_hit = re.search(r"priority\s+(filter\s+-\s*10|-10)", inner)
    if "hook output" not in inner or priority_hit is None:
        return ClosedReport(closed=False, detail="barrier is not ahead of output")
    if "policy drop" not in inner:
        return ClosedReport(closed=False, detail="barrier is not default-drop")
    return ClosedReport(closed=True, detail="barrier holds terminal output drop")


def check_route_closed(route_text: str, *, owned_cidrs: tuple[str, ...]) -> ClosedReport:
    """Pure check: no default route except via the owned net; loopback only."""
    lines = [line.strip() for line in route_text.splitlines() if line.strip()]
    if not lines:
        return ClosedReport(closed=True, detail="no routes at all")
    for line in lines:
        if line.startswith("default "):
            if not any(line.endswith(f"dev {dev}") for dev in owned_cidrs):
                return ClosedReport(closed=False, detail=f"foreign default route: {line}")
    return ClosedReport(closed=True, detail="routes confined to owned net")


@dataclass(frozen=True)
class WindowEgressAdmission:
    """One NFQUEUE admission linkage: metering plus destination attempt.

    A committed or accepted delivery can still hit quota, drop, or expiry
    before outbound, so this is never a contact guarantee. Contact is
    reserved for later-hook or response-proven delivery. observed_hook
    names the kernel hook that observed the packet; quantity carries the
    observed size when known, else None. Never remote content or session
    provenance.
    """

    seq: int
    flow_key: str
    dst_ip: str
    dst_port: int
    verdict: str
    observed_hook: str
    quantity: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "flow_key": self.flow_key,
            "dst_ip": self.dst_ip,
            "dst_port": self.dst_port,
            "verdict": self.verdict,
            "observed_hook": self.observed_hook,
            "quantity": self.quantity,
        }

def check_interfaces_closed(addr_text: str, route_text: str) -> ClosedReport:
    """Pure check: at most one non-loopback interface, defaults only via it.

    addr_text is ip -o addr output, route_text is ip route output, both
    read from inside the room netns. Pre-attach only lo may exist;
    post-attach exactly one data interface may exist. Any default route
    must leave via that same interface. A second attached network
    (rogue eth1) fails closed. Only the count and the route binding
    are asserted.
    """
    data_ifaces: set[str] = set()
    for line in addr_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[1]
        if name != "lo":
            data_ifaces.add(name)
    if len(data_ifaces) > 1:
        return ClosedReport(
            closed=False, detail=f"extra data interfaces: {sorted(data_ifaces)}")
    for line in route_text.splitlines():
        line = line.strip()
        if not line.startswith("default "):
            continue
        if not data_ifaces:
            return ClosedReport(closed=False, detail=f"default route with no data iface: {line}")
        if not any(line.endswith(f"dev {iface}") for iface in data_ifaces):
            return ClosedReport(closed=False, detail=f"foreign default route: {line}")
    return ClosedReport(
        closed=True, detail="interfaces confined: " + str(sorted(data_ifaces) or ["lo-only"]))
