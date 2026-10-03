"""Scope Check — the first pipeline stage in ``DefaultControlPlane.evaluate``.

Resolves each target to a ``(host, port)`` identity and matches it against
the engagement's allow/deny lists:

  - ``allowed_hosts`` / ``denied_hosts``   — exact host or IP-literal match,
    with optional ``host:port`` scoping (see "Port semantics" below)
  - ``allowed_domains`` / ``denied_domains`` — domain-suffix match
    (``api.example.com`` is within ``example.com``; ``notexample.com`` is
    NOT — a plain ``str.endswith`` would wrongly allow it)
  - ``allowed_networks``                    — CIDR membership for
    IP-literal targets (``ipaddress``)
  - ``allowed_assets`` / ``denied_assets``  — exact-match on
    ``AssetTarget.asset_id`` (mobile artifacts have no host identity, so
    they cannot use the rules above; see ``_check_asset_scope``)

Deny lists win over allow lists; with no allow-list configured the
default is DENY (safer for a red-team gate).

Port semantics (host entries only — the model is host-centric):
  - A bare host entry (``127.0.0.1``) matches that host on *any* port.
    Domain- and network-level allow/deny are likewise port-agnostic; any
    ``:port`` written on a domain entry is ignored.
  - A ``host:port`` entry (``127.0.0.1:8080``) matches only that port.
  - If an *allow* entry pins a port but the request target carries no
    port, we do NOT assume the port matches — that is an explicit DENY
    with a reason, not a silent pass. A port must come from the target
    (``EndpointTarget.port``, a URL, or a ``host:port`` string), never be
    inferred by Control.
  - A port-pinned *deny* entry blocks that port; a bare-host deny blocks
    every port. When a target matches a port-pinned deny host but carries
    no port, we cannot prove it is a *different* port, so we fail closed.

The ``host:port`` expression lives inside the existing ``list[str]``
fields — this is a string-content convention, NOT a contract change, so
no ``core/contracts`` edit / PM Issue is required. This string convention
is the *final, chosen* design for port scoping (decided 2026-10-02);
promoting ports to a dedicated ``EngagementScope`` field was considered
and declined — it would be a PM-owned contract change, revisited only if
a real need to leave the host-centric model arises (see
docs/control/PLAN.md §5).

Notes / limitations:
  - No DNS resolution: ``allowed_networks`` only matches targets that are
    already IP literals, not hostnames that would resolve into the range.
  - ``principal`` / ``graph_node`` targets have no host identity and
    therefore fall through to DENY (scope is host-based). ``asset`` is
    handled separately, by ``allowed_assets``/``denied_assets``.
  - Address parsing is IPv6-aware (bracketed ``[::1]:8080`` and bare
    ``::1`` literals) — a naive ``split(":")`` would corrupt them.
  - A malformed *deny* entry (host or domain) is a fatal config error
    (``ABORT_RUN``), never silently skipped — that would fail open. A
    malformed *allow* entry is dropped (it would only ever grant access)
    but surfaced in the denial reason so a typo is not mistaken for a
    mysterious "everything denied". Port ``0`` and an empty port
    (``host:``) are rejected as malformed.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import (
    AnyTarget,
    AssetTarget,
    DeviceTarget,
    DomainTarget,
    EndpointTarget,
    HostTarget,
    ServiceTarget,
    UrlTarget,
)
from redteam.core.enums import Severity

# A resolved host identity: hostname plus an optional port. ``None`` port
# means "no port information" — not "any port".
HostPort = tuple[str, int | None]

_IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Well-known default ports, so a scheme-only URL ("http://host/") carries
# the port a host:port scope rule expects (fixes port-scoped deny bypass).
_DEFAULT_SCHEME_PORTS: dict[str, int] = {
    "http": 80,
    "https": 443,
    "ws": 80,
    "wss": 443,
    "ftp": 21,
    "ssh": 22,
}


def _as_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _split_host_port(raw: str) -> tuple[str | None, int | None, bool]:
    """Split an address string into ``(host, port, ok)``.

    ``ok=False`` means the input is malformed / ambiguous and the caller
    should treat it as a scope DENY (for a target) or a config error (for a
    deny entry). IPv6 is handled explicitly: bracketed forms go through
    ``urlsplit``; a bare ``::1`` literal is recognised before any
    colon-splitting so it is never mistaken for ``host:port``.
    """
    s = raw.strip()
    if not s:
        return None, None, False

    has_scheme = "://" in s

    # Bare (unbracketed) IPv6 literal, e.g. "::1" / "fe80::1". It has many
    # colons but is not host:port — recognise it before urlsplit. A URL
    # (scheme present) is never a bare literal, so skip this branch for it.
    if not has_scheme and "[" not in s and s.count(":") >= 2:
        ip = _as_ip(s)
        if ip is not None:
            return str(ip), None, True
        # Many colons, not a valid IPv6 literal, not bracketed: ambiguous.
        return None, None, False

    target = s if has_scheme else "//" + s
    try:
        parts = urlsplit(target)
        host = parts.hostname
        port = parts.port  # raises ValueError on non-numeric / out-of-range
    except ValueError:
        return None, None, False
    if not host:
        return None, None, False
    # An explicit-but-empty port ("host:", "[::1]:", "http://host:/") is a
    # typo — reject it rather than silently treating it as a bare host, which
    # would mean "all ports".
    if port is None and parts.netloc.endswith(":"):
        return None, None, False
    # Port 0 is the "unspecified" port — never a real target; reject it.
    if port == 0:
        return None, None, False
    # Infer the well-known default port from the URL scheme so a scheme-only
    # URL carries the port a host:port rule expects.
    if port is None and parts.scheme in _DEFAULT_SCHEME_PORTS:
        port = _DEFAULT_SCHEME_PORTS[parts.scheme]
    return host, port, True


def _valid_port(port: int | None) -> bool:
    # Port 0 is the "unspecified" port, never a connectable target.
    return port is None or 1 <= port <= 65535


def _resolve_target(target: AnyTarget) -> tuple[list[HostPort], str | None]:
    """Resolve a target to ``(host, port)`` identities.

    Returns ``(resolved, error)``. A non-empty ``error`` means the target
    is malformed/ambiguous and must be DENY'd with that reason. An empty
    ``resolved`` with no error means the target kind has no host identity
    (principal/asset/graph_node, or a service with no host).
    """
    if isinstance(target, HostTarget):
        host, port, ok = _split_host_port(target.host)
        if not ok or host is None:
            return [], f"malformed host target '{target.host}'"
        return [(host, port)], None

    if isinstance(target, DomainTarget):
        host, port, ok = _split_host_port(target.domain)
        if not ok or host is None:
            return [], f"malformed domain target '{target.domain}'"
        return [(host, port)], None

    if isinstance(target, UrlTarget):
        host, port, ok = _split_host_port(target.url)
        if not ok or host is None:
            return [], f"malformed url target '{target.url}'"
        return [(host, port)], None

    if isinstance(target, EndpointTarget):
        host, parsed_port, ok = _split_host_port(target.host)
        if not ok or host is None:
            return [], f"malformed endpoint host '{target.host}'"
        if not _valid_port(target.port):
            return [], f"invalid endpoint port {target.port}"
        if target.port is not None and parsed_port is not None and target.port != parsed_port:
            return [], (
                f"endpoint port conflict: host string '{target.host}' vs port field {target.port}"
            )
        port = target.port if target.port is not None else parsed_port
        return [(host, port)], None

    if isinstance(target, ServiceTarget):
        if not target.host:
            return [], None  # no host identity — falls through to DENY
        host, port, ok = _split_host_port(target.host)
        if not ok or host is None:
            return [], f"malformed service host '{target.host}'"
        return [(host, port)], None

    return [], None


def _host_eq(a: str, b: str) -> bool:
    if a.lower() == b.lower():
        return True
    a_ip, b_ip = _as_ip(a), _as_ip(b)
    return a_ip is not None and b_ip is not None and a_ip == b_ip


def _host_entries(entries: list[str]) -> tuple[list[HostPort], list[str]]:
    """Parse ``host`` / ``host:port`` config entries into ``(parsed,
    malformed)``. ``malformed`` holds raw strings that failed to parse.

    A malformed *allow* entry is safe to drop (it just grants nothing), but a
    malformed *deny* entry must NOT be silently dropped: that would fail open,
    leaving a deny rule with zero effect. ``check_scope`` fails closed when any
    deny entry is malformed."""
    parsed: list[HostPort] = []
    malformed: list[str] = []
    for entry in entries:
        host, port, ok = _split_host_port(entry)
        if ok and host is not None:
            parsed.append((host, port))
        else:
            malformed.append(entry)
    return parsed, malformed


def _domain_entries(entries: list[str]) -> tuple[list[str], list[str]]:
    """Parse domain allow/deny entries into ``(bases, malformed)``.

    Domains are port-agnostic: any ``:port`` written on a domain entry is
    ignored (a domain rule covers every port). Entries that fail to parse are
    reported in ``malformed`` so a malformed *deny* domain is never silently
    skipped — that would fail open, exactly like a malformed deny host."""
    bases: list[str] = []
    malformed: list[str] = []
    for entry in entries:
        host, _, ok = _split_host_port(entry)
        if ok and host is not None:
            bases.append(host.lower().rstrip("."))
        else:
            malformed.append(entry)
    return bases, malformed


def _network_entries(cidrs: list[str]) -> tuple[list[_IpNetwork], list[str]]:
    """Parse CIDR allow entries into ``(networks, malformed)``. Networks are
    allow-only (there is no ``denied_networks``), so a malformed entry is safe
    to drop, but it is still surfaced so an operator sees the typo."""
    nets: list[_IpNetwork] = []
    malformed: list[str] = []
    for cidr in cidrs:
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            malformed.append(cidr)
    return nets, malformed


def _matches_host_entry(cand: HostPort, entries: list[HostPort]) -> tuple[bool, bool]:
    """Match a resolved target against host entries.

    Returns ``(matched, port_required)``. ``port_required`` is True when an
    entry matched the host and pinned a port, but the target carried no
    port — an ambiguity the caller surfaces as an explicit DENY (fail
    closed) rather than guessing.
    """
    cand_host, cand_port = cand
    port_required = False
    for ehost, eport in entries:
        if not _host_eq(cand_host, ehost):
            continue
        if eport is None or eport == cand_port:
            return True, False
        if cand_port is None:
            port_required = True  # host matches but port can't be confirmed
    return False, port_required


def _matches_domain(candidate: str, bases: list[str]) -> bool:
    if _as_ip(candidate) is not None:
        return False  # an IP literal is never a domain-suffix match
    cand = candidate.lower().rstrip(".")
    for base in bases:
        if cand == base or cand.endswith("." + base):
            return True
    return False


def _matches_network(candidate: str, nets: list[_IpNetwork]) -> bool:
    ip = _as_ip(candidate)
    if ip is None:
        return False
    for net in nets:
        if ip.version == net.version and ip in net:
            return True
    return False


def _check_asset_scope(
    target: AssetTarget | DeviceTarget, scope: EngagementScope
) -> tuple[bool, list[str], Severity | None]:
    """Exact-match scope check for mobile artifacts.

    Deliberately not a substring/path check: an ``asset_id`` is a
    canonical identifier (see ``AssetTarget`` docstring), not a
    filesystem path, so there is nothing to normalize beyond exact,
    case-sensitive comparison — unlike a hostname, there is no DNS-style
    convention that makes two differently-cased identifiers the same
    artifact.
    """
    asset_id = str(
        target.asset_id
        if isinstance(target, AssetTarget)
        else target.asset_id or target.package_name
    )

    if asset_id in scope.denied_assets:
        return (
            False,
            [f"asset '{asset_id}' is explicitly denied (denied_assets)"],
            Severity.BLOCK_ACTION,
        )
    if not scope.allowed_assets:
        return (
            False,
            ["engagement scope has no allowed_assets configured"],
            Severity.BLOCK_ACTION,
        )
    if asset_id in scope.allowed_assets:
        return True, [], None
    return False, [f"asset '{asset_id}' is not within engagement scope"], Severity.BLOCK_ACTION


def _label(cand: HostPort) -> str:
    host, port = cand
    return f"{host}:{port}" if port is not None else host


def check_scope(
    action: ActionProposal, scope: EngagementScope
) -> tuple[bool, list[str], Severity | None]:
    """Returns ``(allowed, reasons, severity)``.

    A scope denial is normally ``BLOCK_ACTION`` (try a different in-scope
    target). The exception is a malformed deny-list config, which is
    non-recoverable — every action hits the same error — so it is
    ``ABORT_RUN``."""
    if isinstance(action.target, AssetTarget):
        # asset_ref is the schema-declared identity used by workspace tools.
        # Authorizing one asset must never allow arguments to select another.
        if "asset_ref" in action.arguments and action.arguments["asset_ref"] != str(
            action.target.asset_id
        ):
            return (
                False,
                ["asset_ref does not match the action's asset target"],
                Severity.BLOCK_ACTION,
            )
        return _check_asset_scope(action.target, scope)

    if isinstance(action.target, DeviceTarget):
        return _check_asset_scope(action.target, scope)

    resolved, error = _resolve_target(action.target)
    if error is not None:
        return False, [f"target rejected: {error}"], Severity.BLOCK_ACTION
    if not resolved:
        return (
            False,
            [f"target kind '{action.target.kind}' has no scope-checkable host identity"],
            Severity.BLOCK_ACTION,
        )

    denied_hosts, denied_host_bad = _host_entries(scope.denied_hosts)
    allowed_hosts, allowed_host_bad = _host_entries(scope.allowed_hosts)
    denied_domains, denied_domain_bad = _domain_entries(scope.denied_domains)
    allowed_domains, allowed_domain_bad = _domain_entries(scope.allowed_domains)
    allowed_nets, allowed_net_bad = _network_entries(scope.allowed_networks)

    # Malformed DENY entries (host or domain) must never be silently skipped:
    # that leaves a deny rule with zero effect (fail-open). A broken scope
    # config is also non-recoverable — every action hits the same error — so
    # ABORT_RUN rather than BLOCK_ACTION (which would retry every target).
    deny_malformed = denied_host_bad + denied_domain_bad
    if deny_malformed:
        return (
            False,
            [
                f"engagement scope has malformed deny entries {deny_malformed} "
                "— refusing to evaluate (fix config)"
            ],
            Severity.ABORT_RUN,
        )

    # Deny lists win outright. A bare-host deny blocks every port; a
    # port-pinned deny blocks its port, and when the target carries no port we
    # cannot prove it is a *different* port, so we fail closed.
    for cand in resolved:
        matched, deny_port_ambiguous = _matches_host_entry(cand, denied_hosts)
        if matched:
            return (
                False,
                [f"target '{_label(cand)}' is explicitly denied (denied_hosts)"],
                Severity.BLOCK_ACTION,
            )
        if deny_port_ambiguous:
            return (
                False,
                [
                    f"target '{_label(cand)}' may fall under a port-scoped deny "
                    "(denied_hosts) but carries no port — refusing"
                ],
                Severity.BLOCK_ACTION,
            )
        if _matches_domain(cand[0], denied_domains):
            return (
                False,
                [f"target '{_label(cand)}' is explicitly denied (denied_domains)"],
                Severity.BLOCK_ACTION,
            )

    if not (scope.allowed_hosts or scope.allowed_domains or scope.allowed_networks):
        # No explicit allow-list configured: default-deny is safer for a
        # red-team framework than default-allow.
        return (
            False,
            ["engagement scope has no allowed_hosts/domains/networks configured"],
            Severity.BLOCK_ACTION,
        )

    port_required = False
    for cand in resolved:
        matched, needs_port = _matches_host_entry(cand, allowed_hosts)
        if matched:
            return True, [], None
        port_required = port_required or needs_port
        if _matches_domain(cand[0], allowed_domains):
            return True, [], None
        if _matches_network(cand[0], allowed_nets):
            return True, [], None

    labels = [_label(c) for c in resolved]
    if port_required:
        return (
            False,
            [
                f"target {labels} matches an allowed host but carries no port, "
                "while the allow-list pins a specific port — port cannot be inferred"
            ],
            Severity.BLOCK_ACTION,
        )
    # Surface ignored malformed ALLOW entries: safe to drop, but a silent typo
    # here otherwise looks like a mysterious "everything denied".
    allow_malformed = allowed_host_bad + allowed_domain_bad + allowed_net_bad
    reason = f"target {labels} is not within engagement scope"
    if allow_malformed:
        reason += f" (ignored malformed allow entries: {allow_malformed})"
    return False, [reason], Severity.BLOCK_ACTION
