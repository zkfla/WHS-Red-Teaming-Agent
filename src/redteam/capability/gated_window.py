"""Capability-side gated-window extension. Owned by Capability (TASK-005).

Wraps a ``ShellWorkroomManager`` with per-instance exclusive transmit
windows backed by the Control-owned ``NetworkGrantLedger`` authority
(``core.ports.network_grants`` Protocol only -- this module never imports
control code; the live ledger object is injected by Control composition).

Ordering per window: durable ``request_window`` reservation FIRST (before
any transmission-capable step), then the kernel attach (none -> DROP via a
NET_ADMIN-only sidecar sharing the room netns -> ``disconnect none`` ->
``connect`` the owned bridge, same ns inode throughout), provider observed
closed verification, and only then the daemon stdio RPC loop. Any failure
settles UNKNOWN (full reservation retained) via the ledger.

External IPv6 egress is UNSUPPORTED and fail-closed: the builder emits no
IPv6 accept element, ever, and explicit v6 drops carry counters. Loopback
proofs are never presented as external support.

Units, honestly: the meter reads the post-accept OUTPUT-chain named counter
(packets/bytes as charged at the IP hook, i.e. SKBs), never ``quota.used``
(a blocker, not a meter). ``MeterReading`` is outbound-only.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import uuid
from dataclasses import dataclass, field

from redteam.capability.gate_observer import (
    ClosedReport,
    RunDocker,
    WindowEgressAdmission,
    check_barrier_closed,
    check_default_drop,
    check_interfaces_closed,
    check_open_posture,
)
from redteam.capability.gate_wire import (
    MAX_LINE_BYTES,
    AdmissionReply,
    WireError,
    decode_request,
    encode_reply,
)
from redteam.capability.gated_network_policy import GatedNetworkPolicy
from redteam.capability.shell_workroom import (
    ExecutionAdmission,
    ShellWorkroomManager,
    workroom_key_for,
)
from redteam.core.contracts.network import (
    WINDOW_EGRESS_SOURCE_ID,
    GrantRefused,
    MeterReading,
    NetworkGrant,
    NetworkPurpose,
    WindowCaps,
    WindowSettlement,
)
from redteam.core.ports.network_grants import NetworkGrantLedger

TABLE_NAME = "gated"
GATED_QUEUE_NUM = 7
GATED_LABEL = "io.whs-redteam.gated-window"
GATED_CONTEXT_LABEL = "io.whs-redteam.gated-window-id"
OPAQUE_PREFIX = "gwin-"

# Provider-owned source string stamped on every emitted companion.
# The 004A side reads it; it never authorizes anything.
# A manager-level source_id property for cards remains 007-owned.
GATE_SOURCE_ID = WINDOW_EGRESS_SOURCE_ID

# Constraint 4: external IPv6 is unsupported/fail-closed. This reason string
# is surfaced wherever an IPv6 allow is requested.
IPV6_EXTERNAL_UNSUPPORTED = "external-ipv6-unsupported-fail-closed"


class GatedNetworkError(ValueError):
    """Gated-window refusal. The message is the machine-readable code."""


@dataclass(frozen=True)
class AllowedFlow:
    ip: str
    port: int
    proto: str  # "tcp" | "udp"


@dataclass
class OpenWindow:
    key: str
    room: str
    grant: NetworkGrant
    daemon_session_id: str
    container_tag: str
    network_name: str
    holder_action_id: str
    holder_invocation_id: str
    holder_work_id: str | None = None
    allowed: tuple[AllowedFlow, ...] = ()
    admissions: list[WindowEgressAdmission] = field(default_factory=list)
    settled: bool = field(default=False)
    rpc_task: asyncio.Task[None] | None = field(default=None)
    daemon_proc: asyncio.subprocess.Process | None = field(default=None)
    rpc_error: str | None = field(default=None)
    network_created: bool = field(default=False)
    target_route_identity: dict | None = None


def opaque_window_tag(run_id: str, instance_id: str, epoch: int) -> str:
    """Opaque window tag: no registry, no listable identity beyond the hash."""
    digest = hashlib.sha256(f"{run_id}.{instance_id}.{epoch}".encode()).hexdigest()[:16]
    return OPAQUE_PREFIX + digest


def validate_allowed_flows(allowed: tuple[AllowedFlow, ...]) -> None:
    """Canonical-IPv4 execution-boundary validation (no DNS, no names)."""
    for flow in allowed:
        if ":" in flow.ip:
            raise GatedNetworkError(IPV6_EXTERNAL_UNSUPPORTED)
        try:
            canonical = str(ipaddress.IPv4Address(flow.ip))
        except ValueError:
            raise GatedNetworkError("flow_ip_invalid") from None
        if canonical != flow.ip:
            raise GatedNetworkError("flow_ip_not_canonical")
        if flow.proto not in ("tcp", "udp"):
            raise GatedNetworkError("flow_proto_unsupported")
        if not 0 < flow.port <= 65535:
            raise GatedNetworkError("flow_port_out_of_range")


def build_ruleset(
    *,
    queue_num: int,
    allowed: tuple[AllowedFlow, ...],
    quota_bytes: int,
    met_counter: str = "met_out",
) -> str:
    """Render the per-window nftables ruleset (applied with ``nft -f``).

    Default-deny on output/input/forward (IPv4+IPv6 via the inet family).
    One output base chain holds every decision in order: explicit DNS and
    IPv6 drops first, then ``ct state new`` diversion to NFQUEUE, then the
    live-set jump into the allowlisted flows, then the metered quota accept.
    A single chain is deliberate: daemon-accepted flow-inits and established
    packets (ACKs, data, retransmits) alike continue into the same allow,
    quota, and meter decisions, so no direction bypasses accounting. The
    input chain accepts only established/related return traffic -- never new
    inbound initiation. Raises ``GatedNetworkError`` for non-IPv4 flows.

    Expiry: every accept path additionally requires membership in the live_v4 timeout set.
    """
    if quota_bytes <= 0:
        raise GatedNetworkError("quota_bytes_invalid")
    validate_allowed_flows(allowed)
    lines = [
        f"table inet {TABLE_NAME} {{",
        f"  quota qb {{ over {quota_bytes} bytes }}",
        "  set live_v4 { type ipv4_addr; flags timeout; }",
        "  counter denied_dns_local { }",
        "  counter denied_dns_ext { }",
        "  counter denied_v6 { }",
        "  counter denied_other { }",
        "  counter denied_gated { }",
        f"  counter {met_counter} {{ }}",
        "  chain gated_egress {",
        "    type filter hook output priority filter; policy drop;",
        "    ip daddr 127.0.0.11 udp dport 53 counter name denied_dns_local drop",
        "    ip daddr 127.0.0.11 tcp dport 53 counter name denied_dns_local drop",
        "    udp dport 53 counter name denied_dns_ext drop",
        "    tcp dport 53 counter name denied_dns_ext drop",
        "    ip6 daddr ::/0 counter name denied_v6 drop",
        f"    ct state new queue num {queue_num}",
        "    ip daddr @live_v4 jump gated_flows",
        "    counter name denied_other drop",
        "  }",
        "  chain gated_flows {",
    ]
    for flow in allowed:
        lines.append(
            f"    ip daddr {flow.ip} {flow.proto} dport {flow.port} jump meter_accept"
        )
    lines += [
        "    counter name denied_gated drop",
        "  }",
        "  chain meter_accept {",
        "    quota name qb drop",
        f"    counter name {met_counter} accept",
        "  }",
        "  chain input {",
        "    type filter hook input priority filter; policy drop;",
        "    ct state established,related accept",
        "  }",
        "  chain forward {",
        "    type filter hook forward priority filter; policy drop;",
        "  }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def build_closed_ruleset() -> str:
    """Pure default-drop ruleset (window close / reset state).

    Total silence: no queue, no accepts of any kind.
    """
    rules = [
        "table inet " + TABLE_NAME + " {",
        "  chain closed_out {",
        "    type filter hook output priority filter; policy drop;",
        "  }",
        "  chain closed_in {",
        "    type filter hook input priority filter; policy drop;",
        "  }",
        "  chain closed_fwd {",
        "    type filter hook forward priority filter; policy drop;",
        "  }",
        "}",
    ]
    return "\n".join(rules) + "\n"

@dataclass
class GatedWindowExtension(ExecutionAdmission):
    """Per-instance exclusive transmit windows over one stateful room."""

    manager: ShellWorkroomManager
    ledger: NetworkGrantLedger
    run_docker: RunDocker
    policy: GatedNetworkPolicy | None = None
    queue_num: int = GATED_QUEUE_NUM
    target_route_identity: dict | None = None
    docker_endpoint: dict | None = None

    def __post_init__(self) -> None:
        self._open: dict[str, OpenWindow] = {}
        self._opening: set[str] = set()
        self._companions: dict[str, dict[str, object]] = {}

    def program_execution_routes(self):
        from redteam.capability.gated_manifest import OPERATION_PURPOSE, build_gated_descriptors
        from redteam.core.ports.program_discovery import ProgramExecutionRoute

        policy = self.policy
        if policy is None:
            return super().program_execution_routes()
        descriptors = build_gated_descriptors(self.manager.policy)
        routes = []
        for purpose in NetworkPurpose:
            registry = (policy.registry.targets if purpose is NetworkPurpose.TARGET
                        else policy.registry.mirrors)
            ready = bool(self.target_route_identity and self.docker_endpoint)
            reason = "prepared_route_rechecked_at_execution" if ready else "route_not_prepared"
            status = ("target_ready" if purpose is NetworkPurpose.TARGET else "install_ready"
                      ) if ready else "configured_unverified"
            capacity = (policy.budget.target_capacity if purpose is NetworkPurpose.TARGET
                        else policy.budget.install_capacity)
            if not registry or not policy.sidecar_image:
                status, reason = "unavailable", "gated_registry_or_sidecar_missing"
            elif min(capacity.max_flow_init_deliveries,
                     policy.budget.global_capacity.max_flow_init_deliveries,
                     policy.caps_for(purpose).max_flow_init_deliveries) == 0 or min(
                         capacity.max_output_bytes, policy.budget.global_capacity.max_output_bytes,
                         policy.caps_for(purpose).max_output_bytes) == 0:
                status, reason = "unavailable", "network_purpose_budget_disabled"
            elif purpose is NetworkPurpose.INSTALL and policy.install_proxy is None:
                status, reason = "unavailable", "install_proxy_unconfigured"
            routes.append(ProgramExecutionRoute(
                purpose=purpose.value, status=status, reason_code=reason,
                tool_ids=[str(tool.tool_id) for tool in descriptors
                          if OPERATION_PURPOSE[tool.operations[0]] is purpose],
                selectors={name: {"ips": list(ips),
                                  "ports": list(policy.target_ports.get(name, ()))
                                  if purpose is NetworkPurpose.TARGET else None}
                           for name, ips in registry.items()},
                limits=policy.caps_for(purpose).model_dump(mode="json"),
            ))
        return routes

    def _held(self, key: str) -> bool:
        return key in self._open or key in self._opening

    def is_transmit_open(self, key: str) -> bool:
        """Read-only admission input for the manager busy-deny hook."""
        return self._held(key)

    def inventory_mode(self, key: str) -> str:
        """'static' while a window is held (no pip join), else 'full'."""
        return "static" if self._held(key) else "full"

    def expected_networks(self, key: str) -> frozenset[str]:
        """Only the active owned window may replace the closed attachment."""
        window = self._open.get(key)
        if window is not None and not window.settled:
            return frozenset({window.network_name})
        return super().expected_networks(key)

    def expected_network_ids(self, key: str) -> dict[str, str]:
        window = self._open.get(key)
        if window is not None and not window.settled and window.target_route_identity:
            identity = window.target_route_identity
            if identity.get("network_id"):
                return {window.network_name: identity["network_id"]}
        return {}

    def check_exec_allowed(self, key: str, *, invocation_id: str | None = None) -> None:
        """Only the trusted window holder may enter its execution delegate."""
        window = self._open.get(key)
        if (window is not None and not window.settled and invocation_id is not None
                and invocation_id == window.holder_invocation_id):
            return
        if self._held(key):
            raise GatedNetworkError("window_busy")

    async def _docker(
        self, argv: tuple[str, ...], stdin: bytes | None = None,
        timeout: float = 30.0, limit: int | None = None,
    ) -> tuple[int | None, bytes, bytes]:
        code, out, err, _t, _c = await self.run_docker(argv, stdin, timeout, limit)
        return code, out, err

    async def _remove_sidecar(self, tag: str) -> None:
        """Remove our sidecar; refuse a foreign name collision."""
        code, out, _err = await self._docker((
            "inspect", "--format", "{{json .Config.Labels}}", tag
        ), None, 30.0, 1 << 16)
        if code == 0:
            try:
                labels = json.loads(out.decode(errors="replace") or "null")
            except ValueError:
                labels = None
            if not isinstance(labels, dict) or labels.get(GATED_CONTEXT_LABEL) != tag:
                raise GatedNetworkError("foreign_sidecar_collision")
        await self.run_docker(("rm", "-f", tag), None, 30.0, None)

    async def open_window(
        self,
        *,
        run_id: str,
        instance_id: str,
        epoch: int,
        caps: WindowCaps | None = None,
        allowed: tuple[AllowedFlow, ...] = (),
        holder_action_id: str,
        holder_invocation_id: str,
        holder_work_id: str | None = None,
        internal: bool = True,
    ) -> NetworkGrant:
        """Reserve durably, attach the owned net under DROP, verify, start RPC."""
        if self.policy is None:
            raise GatedNetworkError("gated_network_unconfigured")
        if caps is None:
            raise GatedNetworkError("window_caps_required")
        if not holder_action_id or not holder_invocation_id:
            raise GatedNetworkError("holder_identity_required")
        validate_allowed_flows(allowed)
        self._check_scope_membership(allowed, caps.purpose)
        route_identity = None
        if caps.purpose is NetworkPurpose.TARGET:
            from redteam.capability.gated_routing import TargetRouteError, validate_target_route
            try:
                route_identity = await validate_target_route(
                    self.policy, self.run_docker, expected=self.target_route_identity,
                )
            except TargetRouteError as exc:
                raise GatedNetworkError(str(exc)) from None
        if self.docker_endpoint is not None:
            code, out, _, timed_out, cut = await self.run_docker(
                ("context", "inspect", self.docker_endpoint["context"], "--format",
                 "{{.Endpoints.docker.Host}}"), None, 30, 8192,
            )
            if (code or timed_out or cut
                    or out.decode().strip() != self.docker_endpoint["host"]):
                raise GatedNetworkError("gated_docker_endpoint_changed")

        # Operator image selection/availability is checked before reserving
        # capacity or creating a room. Never pull a mutable default image.
        try:
            sidecar_image = self.policy.require_sidecar_image()
        except ValueError as exc:
            raise GatedNetworkError(str(exc)) from None
        code, out, _err, timed_out, cut = await self.run_docker(
            ("image", "inspect", "--format", "{{.Id}}", sidecar_image), None, 30.0, 1024,
        )
        image_id = out.decode(errors="replace").strip()
        if (code != 0 or timed_out or cut or not image_id.startswith("sha256:")
                or len(image_id) != 71
                or any(char not in "0123456789abcdef" for char in image_id[7:])
                or (sidecar_image.startswith("sha256:") and image_id != sidecar_image)):
            raise GatedNetworkError("gated_sidecar_image_unavailable")

        key = workroom_key_for(run_id, instance_id)
        if key in self._opening:
            raise GatedNetworkError("window_open_in_progress")
        tag = opaque_window_tag(run_id, instance_id, epoch)
        network_name = tag.replace(OPAQUE_PREFIX, "gwn-", 1)
        selected_network = (self.policy.target_network_id
                            if caps.purpose is NetworkPurpose.TARGET else None)
        if selected_network is not None:
            assert route_identity is not None
            network_name = route_identity["network_name"]
        session = uuid.uuid4().hex
        self._opening.add(key)
        grant: NetworkGrant | None = None
        created = False
        room: str | None = None
        try:
            refused_or_grant = await self.ledger.request_window(
                run_id=run_id, instance_id=instance_id, epoch=epoch, caps=caps
            )
            if isinstance(refused_or_grant, GrantRefused):
                raise GatedNetworkError(f"grant_refused:{refused_or_grant.code}")
            grant = refused_or_grant
            room = await self.manager.ensure_workroom(key)
            await self._create_sidecar(tag, room)
            await self._apply_ruleset(
                tag, build_ruleset(
                    queue_num=self.queue_num, allowed=allowed,
                    quota_bytes=grant.reserved_bytes,
                ),
            )
            await self._add_live_elements(tag, allowed, caps.duration_seconds)
            if selected_network is not None:
                await self._connect_network(room, selected_network)
            else:
                created = await self._transition_network(room, network_name, internal)
            await self._verify_open_posture_or_raise(tag, allowed=allowed)
        except BaseException:
            # Cancellation during reserve/attach is also an unknown outcome.
            # Keep the key held until the shielded rollback settles the original grant.
            async def rollback():
                try:
                    if room is not None:
                        try:
                            await self._apply_ruleset(
                                tag, build_closed_ruleset(), atomic_reset=True,
                            )
                        except Exception:
                            pass
                        try:
                            await self._docker(("network", "disconnect",
                                                selected_network or network_name, room),
                                               None, 30.0, None)
                        except Exception:
                            pass
                    await self._remove_sidecar(tag)
                    if selected_network is None:
                        await self._remove_owned_network(network_name)
                finally:
                    try:
                        if grant is not None:
                            await self.ledger.settle_window(
                                window_id=grant.window_id, meter=None, verified_closed=False
                            )
                    finally:
                        self._opening.discard(key)
            try:
                await asyncio.shield(asyncio.create_task(rollback()))
            except Exception:
                # The ledger finally path still holds the unknown reservation.
                # Preserve the original stop/failure instead of replacing its cause.
                pass
            raise
        window = OpenWindow(
            key=key, room=room, grant=grant, daemon_session_id=session,
            container_tag=tag, network_name=network_name,
            network_created=created,
            holder_action_id=holder_action_id,
            holder_invocation_id=holder_invocation_id,
            holder_work_id=holder_work_id,
            allowed=allowed,
            target_route_identity=route_identity,
        )
        window.rpc_task = asyncio.ensure_future(self._rpc_loop(window))
        self._open[key] = window
        self._opening.discard(key)
        return grant

    async def _create_sidecar(self, tag: str, room: str) -> None:
        if self.policy is None:
            raise GatedNetworkError("gated_network_unconfigured")
        code, _out, err = await self._docker(
            ("create", "--pull=never", "--name", tag, "--network", f"container:{room}",
             "--cap-add", "NET_ADMIN", "--cap-drop", "ALL",
             "--label", GATED_LABEL, "--label", f"{GATED_CONTEXT_LABEL}={tag}",
             self.policy.require_sidecar_image()), None, 60.0, None,
        )
        if code != 0:
            detail = _short(err)
            if "already in use" in detail or "Conflict" in detail:
                raise GatedNetworkError("sidecar_collision")
            raise GatedNetworkError(f"sidecar_create_failed: {detail}")
        code, _out, err = await self._docker(("start", tag), None, 30.0, None)
        if code != 0:
            raise GatedNetworkError(f"sidecar_start_failed: {_short(err)}")

    async def _apply_ruleset(self, tag: str, text: str, *, atomic_reset: bool = False) -> None:
        """Replace our table; a missing table is fresh, not an error.

        Opening in network-none tolerates a missing table. Closing replaces
        an existing table in one nft transaction so the barrier has no gap.
        """
        if atomic_reset:
            # The barrier must remain until its replacement commits. Two
            # separate delete/define commands would briefly remove filtering.
            text = f"delete table inet {TABLE_NAME}\n" + text
        else:
            await self._docker(
                ("exec", tag, "nft", "delete", "table", "inet", TABLE_NAME),
                None, 30.0, None,
            )
        code, _out, err = await self._docker(
            ("exec", "-i", tag, "nft", "-f", "-"), text.encode(), 30.0, None
        )
        if code != 0:
            raise GatedNetworkError(f"ruleset_apply_failed: {_short(err, 300)}")

    async def _ruleset_text(self, tag: str) -> str:
        code, out, err = await self._docker(
            ("exec", tag, "nft", "list", "ruleset"), None, 30.0, 1 << 20
        )
        if code != 0:
            raise GatedNetworkError(f"ruleset_read_failed: {_short(err)}")
        return out.decode("utf-8", errors="replace")

    def _check_scope_membership(
        self, allowed: tuple[AllowedFlow, ...], purpose: NetworkPurpose
    ) -> None:
        """Subset check against operator pins (no scope language here).

        Control assembles destinations via the existing core.scope_rules
        check_scope before enabling a window; this stays a pure
        IP-membership subset test so no allow/deny semantics are duplicated.
        """
        if self.policy is None:
            raise GatedNetworkError("gated_network_unconfigured")
        pins = (
            self.policy.registry.target_ips()
            if purpose is NetworkPurpose.TARGET
            else self.policy.registry.mirror_ips()
        )
        normalized: set[str] = set()
        for pin in pins:
            try:
                normalized.add(str(ipaddress.IPv4Address(pin)))
            except ValueError:
                continue
        for flow in allowed:
            if str(ipaddress.IPv4Address(flow.ip)) not in normalized:
                raise GatedNetworkError("scope_unpinned")
            if purpose is NetworkPurpose.TARGET:
                for name, pins in self.policy.registry.targets.items():
                    ports = self.policy.target_ports.get(name)
                    if flow.ip in pins and ports is not None and flow.port not in ports:
                        raise GatedNetworkError("gated_target_port_unselected")


    async def _network_owned(self, network_name: str) -> bool | None:
        """True when our label owns the name; None when absent.

        A present-but-foreign name raises and is never adopted or removed.
        """
        code, out, _err = await self._docker(
            ("network", "inspect", "--format", "{{json .Labels}}", network_name),
            None,
            30.0,
            1 << 16,
        )
        if code != 0:
            return None
        try:
            labels = json.loads(out.decode(errors="replace") or "null")
        except ValueError:
            return None
        if not isinstance(labels, dict):
            return None
        if labels.get(GATED_LABEL) is None:
            raise GatedNetworkError("foreign_network_collision")
        return True

    async def _add_live_elements(
        self, tag: str, allowed: tuple[AllowedFlow, ...], duration_seconds: float
    ) -> None:
        """Install per-open expiry elements (never refreshed)."""
        ips = sorted({flow.ip for flow in allowed})
        if not ips:
            return
        seconds = max(1, int(duration_seconds))
        elements = ", ".join(f"{ip} timeout {seconds}s" for ip in ips)
        code, _o, err = await self._docker(
            ("exec", tag, "nft", "add", "element", "inet", TABLE_NAME,
             "live_v4", "{", elements, "}"),
            None, 30.0, None,
        )
        if code != 0:
            raise GatedNetworkError(f"live_elements_failed: {_short(err)}")

    async def _remove_owned_network(self, network_name: str) -> None:
        """Remove a network only when our owner label proves it is ours."""
        if await self._network_owned(network_name):
            await self.run_docker(("network", "rm", network_name), None, 30.0, None)

    async def _transition_network(self, room: str, network_name: str, internal: bool) -> bool:
        """disconnect none, then connect the owned bridge (same ns throughout).

        internal=True keeps the bridge unable to route outside, for mocks;
        internal=False is the product path for approved outside scope and
        needs its positive-mock proof. Returns True when this call created
        the network (only then may teardown remove it).
        """
        owned = await self._network_owned(network_name)
        created = False
        if owned is None:
            args: tuple[str, ...] = ("network", "create", "--driver", "bridge")
            if internal:
                args = args + ("--internal",)
            args = args + ("--label", GATED_LABEL, network_name)
            code, _o, err = await self._docker(args, None, 60.0, None)
            if code != 0:
                raise GatedNetworkError(f"network_create_failed: {_short(err)}")
            created = True
        await self._connect_network(room, network_name)
        return created

    async def _connect_network(self, room: str, network: str) -> None:
        code, _o, err = await self._docker(
            ("network", "disconnect", "none", room), None, 30.0, None
        )
        if code != 0:
            # A settled room has no attachments; prove absence instead of
            # interpreting an arbitrary Docker error as a safe disconnect.
            check, out, _, timed_out, cut = await self.run_docker(
                ("inspect", "--format", "{{json .NetworkSettings.Networks}}", room),
                None, 30.0, 65536,
            )
            try:
                empty = json.loads(out) == {}
            except ValueError:
                empty = False
            if check or timed_out or cut or not empty:
                raise GatedNetworkError("none_disconnect_failed")
        code, _o, err = await self._docker(
            ("network", "connect", network, room), None, 30.0, None
        )
        if code != 0:
            raise GatedNetworkError(f"network_connect_failed: {_short(err)}")

    async def _verify_open_posture_or_raise(
        self, tag: str, *, allowed: tuple[AllowedFlow, ...]
    ) -> ClosedReport:
        """Prove the deployed netns matches exactly the authorized window.

        Default-deny on both output chains, the NFQUEUE diversion, the quota
        gate, precisely the admitted allowlist jumps, and no accept other
        than the named meter. This is an authorized-posture proof for an
        OPEN window, never a closure claim.
        """
        ruleset = await self._ruleset_text(tag)
        posture = check_open_posture(
            ruleset,
            table=TABLE_NAME,
            allowed=tuple((flow.ip, flow.port, flow.proto) for flow in allowed),
            queue_num=self.queue_num,
            meter_name="met_out",
        )
        if not posture.closed:
            raise GatedNetworkError(f"posture_verify_failed: {posture.detail}")
        code, out, err = await self._docker(
            ("exec", tag, "sh", "-c", "ip -o addr; echo ---; ip route"),
            None, 30.0, 1 << 16,
        )
        if code != 0:
            raise GatedNetworkError(f"iface_read_failed: {_short(err)}")
        addr_text, _, route_text = out.decode(errors="replace").partition("---")
        confined = check_interfaces_closed(addr_text, route_text)
        if not confined.closed:
            raise GatedNetworkError(f"posture_verify_failed: {confined.detail}")
        return posture

    async def _rpc_loop(self, window: OpenWindow) -> None:
        """Control-side admission bridge with pre-ledger scope gate.

        Scope is verified BEFORE the ledger: an out-of-scope attempt never
        commits a delivery. Reads are time-bounded; a partial-line hang
        drops the packet and repeated bad frames end the loop, so a daemon
        failure is always reported truthfully as refusal. The daemon session
        travels via docker exec -e (client env never crosses containers).
        """
        proc = await asyncio.create_subprocess_exec(
            *self.manager.policy.docker_command("exec", "-i"),
            "-e", "GATE_DAEMON_SESSION=" + window.daemon_session_id,
            window.container_tag,
            "gate-daemon", "--window", window.grant.window_id,
            "--epoch", str(window.grant.epoch), "--queue", str(self.queue_num),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_LINE_BYTES + 256,
        )
        window.daemon_proc = proc
        assert proc.stdin is not None and proc.stdout is not None
        allowed_triples = {(flow.ip, flow.port, flow.proto) for flow in window.allowed}
        bad_frames = 0
        end_reason = "rpc_loop_exited"
        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    end_reason = "verdict_pipe_broken"
                    break
                if not line:
                    end_reason = "verdict_pipe_eof_daemon_gone"
                    break
                if len(line) > MAX_LINE_BYTES:
                    bad_frames += 1
                    if bad_frames > 10:
                        break
                    continue
                try:
                    request = decode_request(line)
                except WireError:
                    bad_frames += 1
                    if bad_frames > 10:
                        break
                    continue
                bad_frames = 0
                scope_ok = (request.dst_ip, request.dst_port, request.proto) in allowed_triples
                same_window = request.window_id == window.grant.window_id
                same_session = request.daemon_session_id == window.daemon_session_id
                if not same_window or not same_session:
                    reply = AdmissionReply(
                        seq=request.seq, accepted=False, reason="identity_mismatch"
                    )
                elif not scope_ok:
                    reply = AdmissionReply(seq=request.seq, accepted=False, reason="out_of_scope")
                else:
                    try:
                        decision = await self.ledger.admit_delivery(
                            window_id=request.window_id,
                            daemon_session_id=request.daemon_session_id,
                            seq=request.seq,
                            flow_key=request.flow_key,
                        )
                        reply = AdmissionReply(
                            seq=decision.seq,
                            accepted=decision.accepted,
                            reason=decision.reason,
                        )
                    except (ValueError, OSError):
                        reply = AdmissionReply(seq=request.seq, accepted=False, reason="rpc_failed")
                window.admissions.append(
                    WindowEgressAdmission(
                        seq=request.seq,
                        flow_key=request.flow_key,
                        dst_ip=request.dst_ip,
                        dst_port=request.dst_port,
                        verdict=reply.reason,
                        observed_hook="nf-output",
                    )
                )
                try:
                    proc.stdin.write(encode_reply(reply))
                    await proc.stdin.drain()
                except (OSError, ValueError):
                    end_reason = "verdict_reply_unwritable"
                    break
        except asyncio.CancelledError:
            end_reason = "cancelled"
            raise
        finally:
            window.rpc_error = end_reason
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    async def close_window(self, key: str) -> WindowSettlement:
        """Settle through the ledger; UNKNOWN holds the full reservation.

        The original receipt is preserved: settle replays the stored
        settlement on retry and never fabricates. Expiry, drop, or daemon
        failure all settle with ``meter=None`` / ``verified_closed=False``
        (full charge, quarantine). Quarantined windows stay tracked so
        ``release_quarantine`` can reconcile them; only RELEASED pops.
        """
        window = self._open.get(key)
        if window is None:
            raise GatedNetworkError("window_not_open")
        if window.rpc_task is not None:
            window.rpc_task.cancel()
        await self._stop_daemon(window)
        meter: MeterReading | None = None
        verified = False
        try:
            await self._apply_barrier(window.container_tag)
            barrier_ruleset = await self._ruleset_text(window.container_tag)
            barrier = check_barrier_closed(barrier_ruleset, table=TABLE_NAME)
            if barrier.closed:
                try:
                    from redteam.capability.gate_observer import build_meter as _meter
                    meter = _meter(
                        barrier_ruleset, byte_counter="met_out", packet_counter="met_out"
                    )
                except ValueError:
                    meter = None
            verified = barrier.closed and await self._no_descendants(window)
        except GatedNetworkError:
            meter, verified = None, False
        settlement = await self.ledger.settle_window(
            window_id=window.grant.window_id, meter=meter, verified_closed=verified
        )
        self._emit_companion(window, settlement)
        window.settled = True
        if await self._container_present(window.container_tag):
            await self._apply_ruleset(
                window.container_tag, build_closed_ruleset(), atomic_reset=True,
            )
            await self._remove_sidecar(window.container_tag)
        # A removed sidecar skips the reset: nobody privileged remains in
        # that netns, the queue listener died with it, and settlement above
        # already holds the full reservation UNKNOWN.
        await self._teardown_network(window)
        from redteam.core.contracts.network import WindowState

        if settlement.state is WindowState.RELEASED:
            self._open.pop(key, None)
        return settlement

    async def _container_present(self, name: str) -> bool:
        """True when docker can see the named container (any state)."""
        code, _out, _err = await self._docker(
            ("inspect", name), None, 30.0, 1 << 16
        )
        return code == 0

    async def _teardown_network(self, window: OpenWindow) -> None:
        """Remove an extension-created window network; adopted nets stay.

        Runs after settlement and the closed reset, so accounting never
        depends on it. The stateful room is detached first and left without
        a data network until its next window; only networks this extension
        created (owner label) may be removed. A failed detach raises loudly;
        the final label-checked removal is best-effort.
        """
        selected_network = (window.target_route_identity or {}).get("network_id")
        if await self._container_present(window.room):
            code, _out, err = await self._docker(
                ("network", "disconnect", selected_network or window.network_name, window.room),
                None,
                30.0,
                None,
            )
            if code != 0:
                raise GatedNetworkError(f"network_disconnect_failed: {_short(err)}")
        if window.network_created:
            await self._remove_owned_network(window.network_name)
        window.network_created = False

    async def _apply_barrier(self, tag: str) -> None:
        """Freeze output ahead of every rule; counters stay readable.

        The barrier chain drops terminally at priority -10, so no verdict
        below it can run while the meter is read. Named counter objects
        survive: the later closed reset only replaces table rules.
        """
        code, _out, err = await self._docker(
            (
                "exec",
                tag,
                "nft",
                "add",
                "chain",
                "inet",
                TABLE_NAME,
                "barrier",
                "{ type filter hook output priority -10; policy drop; }",
            ),
            None,
            30.0,
            None,
        )
        if code != 0:
            raise GatedNetworkError(f"barrier_apply_failed: {_short(err, 300)}")

    async def _stop_daemon(self, window: OpenWindow) -> None:
        """Terminate the verdict child; a dead pipe never decides flows.

        The sidecar removal later reaps the container side; this reaps the
        host-side exec child so no stale verdicts can land afterwards.
        """
        proc = window.daemon_proc
        window.daemon_proc = None
        if proc is None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def _no_descendants(self, window: OpenWindow) -> bool:
        """Best-effort untrusted-process check; doubt means False (hold)."""
        code, out, _err = await self._docker(
            ("exec", window.room,
             "sh", "-c",
             "for d in /proc/[0-9]*/cmdline; do tr '\\0' ' ' < \"$d\"; echo; done"),
            None, 30.0, 1 << 20,
        )
        if code != 0:
            return False
        text = out.decode(errors="replace")
        allowed_markers = ("sleep infinity", "gate-daemon", "sh -c")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if not any(marker in line for marker in allowed_markers):
                return False
        return True

    async def release_quarantine(self, key: str) -> bool:
        """Control-controller-only quarantine release (no model path).

        Takes no boolean: the observer runs here and its attestation feeds
        the ledger, so the model can never set ``verified_closed``.
        """
        window = self._open.get(key)
        if window is None or not window.settled:
            return False
        try:
            ruleset = await self._ruleset_text(window.container_tag)
        except GatedNetworkError:
            return False
        drop = check_default_drop(ruleset, table=TABLE_NAME)
        if not drop.closed or not await self._no_descendants(window):
            return False
        released = await self.ledger.release_quarantine(
            window_id=window.grant.window_id, verified_closed=True
        )
        if released:
            self._open.pop(key, None)
        return released

    def admission_records(self, key: str) -> tuple[dict[str, object], ...]:
        """Copy of one window admission linkages: attempt-level, never contact."""
        window = self._open.get(key)
        if window is None:
            return ()
        return tuple(entry.as_dict() for entry in window.admissions)

    def _emit_companion(
        self, window: OpenWindow, settlement: WindowSettlement
    ) -> dict[str, object]:
        """Build (once) the immutable SYSTEM companion for a settled window.

        Shape matches the 004A-consumed companion: window linkage, REQUIRED
        holder identity stamped at open, known-scope allowlist scope,
        per-attempt metering linkages. Emitted once at close/settle, never
        as interim partials. A conflicting second companion for the same
        window is rejected immutably.
        """
        window_id = window.grant.window_id
        scope = [
            {"ip": flow.ip, "port": flow.port, "proto": flow.proto}
            for flow in window.allowed
        ]
        companion: dict[str, object] = {
            "source_id": GATE_SOURCE_ID,
            "window_id": window_id,
            "run_id": window.grant.run_id,
            "instance_id": window.grant.instance_id,
            "epoch": window.grant.epoch,
            "workroom_key": window.key,
            "holder": {
                "action_id": window.holder_action_id,
                "invocation_id": window.holder_invocation_id,
                "work_id": window.holder_work_id,
            },
            "scope": scope,
            "admissions": [entry.as_dict() for entry in window.admissions],
            "outcome": settlement.outcome,
            "committed_deliveries": settlement.committed_deliveries,
            "metered_bytes": settlement.metered_bytes,
            "charged_bytes": settlement.charged_bytes,
            "charged_deliveries": settlement.charged_deliveries,
            "observed_hook": "nf-output",
            "note": (
                "observed at nf-output; an accepted admission is metering"
                "plus destination-attempt linkage only, not evidence of a"
                "reached peer, application response, remote content, or"
                "session provenance"
            ),
        }
        if window.target_route_identity is not None:
            companion["target_route_identity"] = window.target_route_identity
        if self.docker_endpoint is not None:
            companion["docker_endpoint"] = self.docker_endpoint
        prior = self._companions.get(window_id)
        if prior is not None:
            for name in ("committed_deliveries", "metered_bytes", "charged_bytes"):
                if prior.get(name) != companion.get(name):
                    raise GatedNetworkError("companion_conflict")
            return prior
        self._companions[window_id] = companion
        return companion

    def companion_for(self, window_id: str) -> dict[str, object] | None:
        """The emitted companion for a settled window, if any."""
        return self._companions.get(window_id)

    @staticmethod
    def settlement_summary(settlement: WindowSettlement) -> dict[str, object]:
        return json.loads(settlement.model_dump_json())


def _short(data: bytes, limit: int = 200) -> str:
    return data.decode(errors="replace")[:limit]
