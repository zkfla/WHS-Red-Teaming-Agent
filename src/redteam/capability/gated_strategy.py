"""Provider-owned contextual gated executor. Owned by Capability (TASK-005).

Resolves the approved target and operator policy into a kernel-enforced
window, then runs one delegated execution inside it. Purpose comes only
from provider descriptor metadata (OPERATION_PURPOSE); the model never
supplies purpose, grants, epochs, or holder identity. Scope authority
comes only from the injected Control-bound scope validator; destination
authority never comes from model text. The window opens after the generic
gateway before_start admission, with the exact holder threaded from the
trusted ToolInvocation, and closes in the delegate finally path. The
immutable SYSTEM evidence companion is persisted through the injected
EvidenceStore after close settlement; the in-memory copy is only a
duplicate guard, never the record of provenance.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol, TypedDict

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.gated_manifest import OPERATION_PURPOSE, TARGET_JOB_START_OPERATION
from redteam.capability.gated_window import (
    GATE_SOURCE_ID,
    AllowedFlow,
    GatedNetworkError,
    GatedWindowExtension,
)
from redteam.capability.install_proxy import InstallProxyRoute, proxy_env, route_for
from redteam.capability.shell_workroom import ShellWorkroomError, workroom_key_for
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.network import (
    GrantRefused,
    NetworkGrant,
    NetworkPurpose,
    WindowSettlement,
)
from redteam.core.contracts.target import AnyTarget, EndpointTarget
from redteam.core.contracts.tool import (
    EXECUTION_STARTED_METADATA_KEY,
    EXECUTION_UNCERTAIN_METADATA_KEY,
    ToolError,
    ToolInvocation,
)
from redteam.core.convert import digest_payload
from redteam.core.enums import EvidenceSourceType, Severity
from redteam.core.ids import (
    ActionId,
    AgentInstanceId,
    EngagementId,
    EvidenceId,
    InvocationId,
    RunId,
)
from redteam.core.ports.evidence_store import EvidenceStore


class ParsedScript(TypedDict, total=False):
    """Adapter-parsed script fields shared with the delegate lifecycle."""

    language: str
    script: str
    timeout_seconds: float | None


Delegate = Callable[[], Awaitable[MCPCallResult]]
"""The owning execution (one-shot run or job start) wrapped by the window."""


def project_parsed_script(arguments: Mapping[str, Any]) -> ParsedScript:
    """Public dict projection of the script fields for policy.execute.

    Picks exactly language, script, and timeout_seconds from the bound
    arguments. The invocation itself is never mutated, so the bound input
    digest and the approved_target survive exactly as the gateway bound
    them. Selectors (target_ref, port) and identity (workroom_key) stay
    with the invocation for resolve and the delegate to consume.
    """
    projected: ParsedScript = {}
    language = arguments.get("language")
    if isinstance(language, str):
        projected["language"] = language
    script = arguments.get("script")
    if isinstance(script, str):
        projected["script"] = script
    timeout = arguments.get("timeout_seconds")
    if isinstance(timeout, bool):
        pass
    elif isinstance(timeout, (int, float)):
        projected["timeout_seconds"] = float(timeout)
    elif timeout is None:
        projected["timeout_seconds"] = None
    return projected


class ScopeValidator(Protocol):
    """Control-bound scope check with the engagement scope already applied."""

    def __call__(
        self, target: AnyTarget, *, denials_only: bool = False
    ) -> tuple[bool, list[str], Severity | None]: ...


def _ack_running(result: MCPCallResult) -> bool:
    """True when a job-start delegate ACK reports a running job."""
    output = result.output
    return isinstance(output, dict) and output.get("status") == "running"


@dataclass(frozen=True)
class RetainedSettlement:
    """Outcome of closing a retained job window at job terminal."""

    settlement: WindowSettlement | None
    evidence_id: str | None = None
    evidence_error: str | None = None


@dataclass(frozen=True)
class RetainedWindow:
    """Durable handle for one retained job window (job-manifest cargo).

    The adapter embeds ``to_record()`` in the protected job manifest at
    the retained ACK; after a host restart a new policy instance recovers
    it via ``recover_retained`` from this record plus the durable ledger
    grant. No new store: the ledger owns the reservation, the job manifest
    owns this handle.
    """

    key: str
    window_id: str
    engagement_id: EngagementId
    run_id: RunId
    instance_id: AgentInstanceId
    epoch: int
    purpose: NetworkPurpose
    allowed: tuple[AllowedFlow, ...]
    scope_host: str
    scope_port: int
    holder_action_id: ActionId
    holder_invocation_id: InvocationId
    holder_work_id: str | None = None

    def to_record(self) -> dict[str, object]:
        """JSON-safe dict for the protected job manifest."""
        return {
            "key": self.key,
            "window_id": self.window_id,
            "engagement_id": self.engagement_id,
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            "epoch": self.epoch,
            "purpose": self.purpose.value,
            "allowed": [
                {"ip": flow.ip, "port": flow.port, "proto": flow.proto}
                for flow in self.allowed
            ],
            "scope_host": self.scope_host,
            "scope_port": self.scope_port,
            "holder_action_id": self.holder_action_id,
            "holder_invocation_id": self.holder_invocation_id,
            "holder_work_id": self.holder_work_id,
        }

    @staticmethod
    def from_record(record: dict[str, object]) -> RetainedWindow:
        """Rebuild a handle from job-manifest cargo; strict or refused."""
        try:
            purpose = NetworkPurpose(str(record["purpose"]))
            allowed_raw = record["allowed"]
            assert isinstance(allowed_raw, list)
            allowed = tuple(
                AllowedFlow(
                    ip=str(entry["ip"]), port=int(entry["port"]), proto=str(entry["proto"])
                )
                for entry in allowed_raw
            )
            work_id = record["holder_work_id"]
            return RetainedWindow(
                key=str(record["key"]),
                window_id=str(record["window_id"]),
                engagement_id=EngagementId(str(record["engagement_id"])),
                run_id=RunId(str(record["run_id"])),
                instance_id=AgentInstanceId(str(record["instance_id"])),
                epoch=_record_int(record["epoch"], "epoch"),
                purpose=purpose,
                allowed=allowed,
                scope_host=str(record["scope_host"]),
                scope_port=_record_int(record["scope_port"], "scope_port"),
                holder_action_id=ActionId(str(record["holder_action_id"])),
                holder_invocation_id=InvocationId(str(record["holder_invocation_id"])),
                holder_work_id=str(work_id) if work_id is not None else None,
            )
        except (KeyError, TypeError, ValueError, AssertionError) as exc:
            raise GatedNetworkError("gated_retained_record_invalid") from exc


def _record_int(value: object, field: str) -> int:
    """Strict JSON integer for retained cargo (bools are never integers)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GatedNetworkError(f"gated_retained_record_invalid:{field}")
    return value


def _retained_identity_mismatch(
    record: RetainedWindow, grant: NetworkGrant
) -> str | None:
    """Mismatch code when the durable grant is not this record window.

    The ledger owns the reservation, so the original grant determines the
    run, instance, epoch and purpose bound to this window. Any disagreement
    is a stale or foreign retained record and must refuse, never settle.
    """
    if record.run_id != grant.run_id:
        return "gated_retained_grant_mismatch:run_id"
    if record.instance_id != grant.instance_id:
        return "gated_retained_grant_mismatch:instance_id"
    if record.epoch != grant.epoch:
        return "gated_retained_grant_mismatch:epoch"
    if record.purpose != grant.purpose:
        return "gated_retained_grant_mismatch:purpose"
    return None


@dataclass(frozen=True)
class GatedRoute:
    """Resolved window destinations plus the exact trusted holder."""

    purpose: NetworkPurpose
    scope_target: EndpointTarget
    allowed: tuple[AllowedFlow, ...]
    holder_action_id: str
    holder_invocation_id: str
    holder_work_id: str | None = None


def _canonical_ipv4(value: object) -> str | None:
    """Canonical IPv4 literal, else None (never DNS, never names)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parsed = ipaddress.IPv4Address(text)
    except ValueError:
        return None
    canonical = str(parsed)
    return canonical if canonical == text else None


class GatedExecutionPolicy:
    """Provider-owned contextual executor for the three gated descriptors."""

    def __init__(
        self,
        extension: GatedWindowExtension | None,
        *,
        scope_validator: ScopeValidator | None = None,
        evidence_store: EvidenceStore | None = None,
    ) -> None:
        self._extension = extension
        self._scope_validator = scope_validator
        self._evidence = evidence_store
        self._retained: dict[str, tuple[ToolInvocation, RetainedWindow]] = {}

    @property
    def extension(self) -> GatedWindowExtension | None:
        return self._extension

    def _operation_for(self, invocation: ToolInvocation) -> str:
        """Tool identity first: the gated name is authoritative in both modes."""
        name = str(invocation.tool_id).split(".")[-1]
        if name in OPERATION_PURPOSE:
            return name
        if invocation.operation:
            return invocation.operation
        return name

    def _refusal(self, code: str, retryable: bool = False) -> GrantRefused:
        return GrantRefused(code=code, retryable=retryable)

    def _failure(self, *, tool_name: str, code: str) -> MCPCallResult:
        now = datetime.now(timezone.utc)
        return MCPCallResult(
            success=False,
            started_at=now,
            finished_at=now,
            output=None,
            error=ToolError(
                code=code,
                message="gated window refused: " + code,
                retryable=False,
            ),
            metadata={"tool_name": tool_name, "gated_refused": code},
        )

    def _check_allowed(self, target: EndpointTarget) -> GrantRefused | None:
        validator = self._scope_validator
        if validator is None:
            return self._refusal("gated_scope_validator_missing")
        try:
            ok, _reasons, _severity = validator(target)
        except Exception:
            return self._refusal("gated_scope_error")
        if not ok:
            return self._refusal("gated_scope_denied")
        return None

    def _check_denials(self, ips: tuple[str, ...], port: int | None) -> GrantRefused | None:
        validator = self._scope_validator
        if validator is None:
            return self._refusal("gated_scope_validator_missing")
        for ip in ips:
            probe = EndpointTarget(host=ip, port=port)
            try:
                ok, _reasons, _severity = validator(probe, denials_only=True)
            except Exception:
                return self._refusal("gated_scope_error")
            if not ok:
                return self._refusal("gated_pinned_denied")
        return None

    def resolve(self, invocation: ToolInvocation) -> GatedRoute | GrantRefused:
        """Resolve destinations and holder; never opens a window."""
        op = self._operation_for(invocation)
        purpose = OPERATION_PURPOSE.get(op)
        if purpose is None:
            return self._refusal("gated_unknown_operation")
        if invocation.execution_context.approved_target is None:
            return self._refusal("gated_target_missing")
        args = invocation.arguments
        if not isinstance(args, dict):
            return self._refusal("gated_arguments_invalid")
        ref = args.get("target_ref")
        if not isinstance(ref, str) or not ref.strip():
            return self._refusal("gated_target_ref_missing")
        name = ref.strip()
        ext = self._extension
        if ext is None or ext.policy is None:
            return self._refusal("gated_network_unconfigured")
        holder_action = str(invocation.action_id)
        holder_invocation = str(invocation.invocation_id)
        if not holder_action or not holder_invocation:
            return self._refusal("gated_holder_missing")
        if purpose is NetworkPurpose.INSTALL:
            return self._resolve_install(
                name=name,
                holder_action=holder_action,
                holder_invocation=holder_invocation,
            )
        return self._resolve_target(
            name=name,
            args=args,
            holder_action=holder_action,
            holder_invocation=holder_invocation,
        )

    def _resolve_target(
        self,
        *,
        name: str,
        args: dict[str, Any],
        holder_action: str,
        holder_invocation: str,
    ) -> GatedRoute | GrantRefused:
        ext = self._extension
        if ext is None or ext.policy is None:
            return self._refusal("gated_network_unconfigured")
        port = args.get("port")
        if isinstance(port, bool) or not isinstance(port, int):
            return self._refusal("gated_port_required")
        if not 0 < port <= 65535:
            return self._refusal("gated_port_required")
        pins = ext.policy.registry.targets.get(name)
        if not pins:
            return self._refusal("gated_unknown_registry_name")
        canonical: list[str] = []
        for pin in pins:
            item = _canonical_ipv4(pin)
            if item is None:
                if isinstance(pin, str) and ":" in pin:
                    return self._refusal("gated_ipv6_unsupported")
                return self._refusal("gated_pin_invalid")
            canonical.append(item)
        scope_target = EndpointTarget(host=name, port=port)
        refused = self._check_allowed(scope_target)
        if refused is not None:
            return refused
        denied = self._check_denials(tuple(canonical), port)
        if denied is not None:
            return denied
        allowed = tuple(
            AllowedFlow(ip=ip, port=port, proto=proto)
            for ip in canonical
            for proto in ("tcp", "udp")
        )
        return GatedRoute(
            purpose=NetworkPurpose.TARGET,
            scope_target=scope_target,
            allowed=allowed,
            holder_action_id=holder_action,
            holder_invocation_id=holder_invocation,
        )

    def _resolve_install(
        self,
        *,
        name: str,
        holder_action: str,
        holder_invocation: str,
    ) -> GatedRoute | GrantRefused:
        ext = self._extension
        if ext is None or ext.policy is None:
            return self._refusal("gated_network_unconfigured")
        registry = ext.policy.registry
        mirror_pins = registry.mirrors.get(name)
        if not mirror_pins:
            return self._refusal("gated_unknown_mirror")
        proxy = ext.policy.install_proxy
        if proxy is None:
            return self._refusal("gated_install_proxy_missing")
        try:
            route = route_for(proxy, mirror_name=name, mirror_ips=tuple(mirror_pins))
        except ValueError:
            return self._refusal("gated_install_route_invalid")
        canonical_mirrors: list[str] = []
        for pin in mirror_pins:
            item = _canonical_ipv4(pin)
            if item is None:
                if isinstance(pin, str) and ":" in pin:
                    return self._refusal("gated_ipv6_unsupported")
                return self._refusal("gated_pin_invalid")
            canonical_mirrors.append(item)
        canonical_proxy: list[str] = []
        for host in proxy.pinned_hosts:
            item = _canonical_ipv4(host)
            if item is None:
                if isinstance(host, str) and ":" in host:
                    return self._refusal("gated_ipv6_unsupported")
                return self._refusal("gated_pin_invalid")
            canonical_proxy.append(item)
        target_ips = set()
        for ips in registry.targets.values():
            for ip in ips:
                item = _canonical_ipv4(ip)
                if item is not None:
                    target_ips.add(item)
        if set(canonical_mirrors) & target_ips:
            return self._refusal("gated_pin_overlap")
        if set(canonical_proxy) & target_ips:
            return self._refusal("gated_pin_overlap")
        scope_target = EndpointTarget(host=name, port=route.upstream_port)
        refused = self._check_allowed(scope_target)
        if refused is not None:
            return refused
        if route.upstream_host.lower().rstrip(".") != name.strip().lower():
            upstream_target = EndpointTarget(
                host=route.upstream_host, port=route.upstream_port
            )
            refused = self._check_allowed(upstream_target)
            if refused is not None:
                return refused
        denied = self._check_denials(tuple(canonical_mirrors), route.upstream_port)
        if denied is not None:
            return denied
        denied = self._check_denials(tuple(canonical_proxy), route.proxy_port)
        if denied is not None:
            return denied
        allowed = tuple(
            AllowedFlow(ip=ip, port=route.proxy_port, proto="tcp")
            for ip in canonical_proxy
        )
        return GatedRoute(
            purpose=NetworkPurpose.INSTALL,
            scope_target=scope_target,
            allowed=allowed,
            holder_action_id=holder_action,
            holder_invocation_id=holder_invocation,
        )

    def install_route_for(self, mirror_name: str) -> InstallProxyRoute:
        """Provider-owned install route for one mirror name.

        Pins and both ports come from the operator config; the model never
        chooses the upstream. Raises GatedNetworkError when unconfigured.
        """
        ext = self._extension
        if ext is None or ext.policy is None:
            raise GatedNetworkError("gated_network_unconfigured")
        proxy = ext.policy.install_proxy
        if proxy is None:
            raise GatedNetworkError("gated_install_proxy_missing")
        pins = ext.policy.registry.mirrors.get(mirror_name)
        if not pins:
            raise GatedNetworkError("gated_unknown_mirror")
        try:
            return route_for(proxy, mirror_name=mirror_name, mirror_ips=tuple(pins))
        except ValueError as exc:
            raise GatedNetworkError("gated_install_route_invalid") from exc

    def install_forward_env(self, route: InstallProxyRoute) -> dict[str, str]:
        """Provider-seeded installer env for one install route.

        Forward-proxy variables only, never an index URL. The adapter must
        seed the install exec env from this and never let model-supplied
        proxy or index variables override it; the package index itself
        comes from the provider-owned install recipe.
        """
        return proxy_env(route)

    async def execute(
        self,
        invocation: ToolInvocation,
        parsed: ParsedScript,
        delegate: Delegate,
    ) -> MCPCallResult:
        """Open the window, run one delegate inside it, close and persist.

        ``parsed`` must be the ``project_parsed_script`` DICT projection of
        the bound arguments (never the adapter-private args model); the
        delegate re-parses the full trusted invocation for selectors.
        """
        op = self._operation_for(invocation)
        if not isinstance(parsed, dict):
            return self._failure(tool_name=op, code="gated_script_missing")
        if not parsed.get("language") or not parsed.get("script"):
            return self._failure(tool_name=op, code="gated_script_missing")
        route = self.resolve(invocation)
        if isinstance(route, GrantRefused):
            return self._failure(tool_name=op, code=route.code)
        ext = self._extension
        if ext is None or ext.policy is None:
            return self._failure(tool_name=op, code="gated_network_unconfigured")
        ctx = invocation.execution_context
        run_id = str(ctx.run_id)
        instance_id = str(ctx.instance_id) if ctx.instance_id is not None else ""
        if not run_id or not instance_id:
            return self._failure(tool_name=op, code="gated_instance_missing")
        try:
            key = workroom_key_for(run_id, instance_id)
        except ValueError:
            return self._failure(tool_name=op, code="gated_identity_invalid")
        epoch = time.time_ns()
        if epoch < 1:
            epoch = 1
        caps = ext.policy.caps_for(route.purpose)
        try:
            grant = await ext.open_window(
                run_id=run_id,
                instance_id=instance_id,
                epoch=epoch,
                caps=caps,
                allowed=route.allowed,
                holder_action_id=route.holder_action_id,
                holder_invocation_id=route.holder_invocation_id,
                holder_work_id=route.holder_work_id,
                internal=False,
            )
        except GatedNetworkError as exc:
            return self._failure(tool_name=op, code=str(exc) or "gated_open_failed")
        except Exception:
            return self._failure(tool_name=op, code="gated_open_failed")
        # Pre-spawn cargo: the retained handle is recorded BEFORE the delegate
        # runs, so the adapter can embed retained_for(key).to_record() into the
        # protected job manifest BEFORE create_task launches. A crash after spawn
        # but before the post-ACK annotate still leaves the manifest cargo plus
        # the durable ledger grant, which recover_retained settles UNKNOWN.
        if op == TARGET_JOB_START_OPERATION:
            pre_record = RetainedWindow(
                key=key,
                window_id=grant.window_id,
                engagement_id=ctx.engagement_id,
                run_id=RunId(run_id),
                instance_id=AgentInstanceId(instance_id),
                epoch=epoch,
                purpose=route.purpose,
                allowed=route.allowed,
                scope_host=route.scope_target.host,
                scope_port=route.scope_target.port or 0,
                holder_action_id=ActionId(route.holder_action_id),
                holder_invocation_id=InvocationId(route.holder_invocation_id),
                holder_work_id=route.holder_work_id,
            )
            self._retained[key] = (invocation, pre_record)
        try:
            result = await delegate()
        except ShellWorkroomError as exc:
            self._discard_provisional(key, grant.window_id)
            if not exc.uncertainty:
                await self._close_quiet(key)
                evidence_id, evidence_error = await self._persist_settled(
                    invocation, grant_window_id=grant.window_id
                )
                refused = self._failure(
                    tool_name=op, code=exc.reason_code or "gated_delegate_refused"
                )
                meta = dict(refused.metadata or {})
                meta["gated_window_id"] = grant.window_id
                self._merge_evidence_meta(meta, evidence_id, evidence_error)
                return replace(refused, metadata=meta)
            await self._close_quiet(key)
            evidence_id, evidence_error = await self._persist_settled(
                invocation, grant_window_id=grant.window_id
            )
            return self._uncertain_failure(
                tool_name=op,
                code=exc.reason_code or "gated_delegate_failed",
                window_id=grant.window_id,
                evidence_id=evidence_id,
                evidence_error=evidence_error,
            )
        except Exception:
            self._discard_provisional(key, grant.window_id)
            await self._close_quiet(key)
            evidence_id, evidence_error = await self._persist_settled(
                invocation, grant_window_id=grant.window_id
            )
            return self._uncertain_failure(
                tool_name=op,
                code="gated_delegate_failed",
                window_id=grant.window_id,
                evidence_id=evidence_id,
                evidence_error=evidence_error,
            )
        except BaseException:
            self._discard_provisional(key, grant.window_id)
            await self._close_quiet(key)
            await self._persist_settled(invocation, grant_window_id=grant.window_id)
            raise
        if op == TARGET_JOB_START_OPERATION and result.success and _ack_running(result):
            # The pre-spawn cargo above is already the retained handle. A job
            # that finished during the delegate may have popped it home and
            # closed its window already, so an absent entry means this window is
            # SETTLED, not missing: report that truthfully instead of rebuilding
            # a retained handle for a closed window. A foreign entry under this
            # key belongs to another holder and is never overwritten here.
            stored = self._retained.get(key)
            meta = dict(result.metadata or {})
            meta["gated_window_id"] = grant.window_id
            if stored is None:
                meta["gated_window_retained"] = False
                meta["gated_window_settled"] = "closed_during_delegate"
            elif stored[1].window_id != grant.window_id:
                meta["gated_window_retained"] = False
                meta["gated_window_settled"] = "foreign_window_held_by_key"
            else:
                meta["gated_window_retained"] = True
            return replace(result, metadata=meta)
        self._discard_provisional(key, grant.window_id)
        try:
            settlement = await ext.close_window(key)
        except GatedNetworkError as exc:
            evidence_id, evidence_error = await self._persist_settled(
                invocation, grant_window_id=grant.window_id
            )
            meta = dict(result.metadata or {})
            meta["gated_settlement"] = str(exc) or "gated_settle_failed"
            meta["gated_window_id"] = grant.window_id
            self._merge_evidence_meta(meta, evidence_id, evidence_error)
            return replace(result, metadata=meta)
        except Exception:
            evidence_id, evidence_error = await self._persist_settled(
                invocation, grant_window_id=grant.window_id
            )
            meta = dict(result.metadata or {})
            meta["gated_settlement"] = "gated_settle_failed"
            meta["gated_window_id"] = grant.window_id
            self._merge_evidence_meta(meta, evidence_id, evidence_error)
            return replace(result, metadata=meta)
        except BaseException:
            await self._close_quiet(key)
            await self._persist_settled(invocation, grant_window_id=grant.window_id)
            raise
        evidence_id, evidence_error = await self._persist_settled(
            invocation, grant_window_id=grant.window_id
        )
        meta = dict(result.metadata or {})
        meta["gated_window_id"] = grant.window_id
        meta["gated_settlement_state"] = settlement.state.value
        self._merge_evidence_meta(meta, evidence_id, evidence_error)
        return replace(result, metadata=meta)

    def _uncertain_failure(
        self,
        *,
        tool_name: str,
        code: str,
        window_id: str,
        evidence_id: str | None,
        evidence_error: str | None,
    ) -> MCPCallResult:
        """Delegate-entry failure: entry happened, so the outcome is unknown.

        Carries the common STARTED plus UNCERTAIN markers so the gateway
        never mistakes this for a proved pre-entry refusal.
        """
        now = datetime.now(timezone.utc)
        metadata: dict[str, object] = {
            "tool_name": tool_name,
            "gated_error": code,
            "gated_window_id": window_id,
            EXECUTION_STARTED_METADATA_KEY: True,
            EXECUTION_UNCERTAIN_METADATA_KEY: True,
        }
        self._merge_evidence_meta(metadata, evidence_id, evidence_error)
        return MCPCallResult(
            success=False,
            started_at=now,
            finished_at=now,
            output=None,
            error=ToolError(
                code=code,
                message="gated execution uncertain: " + code,
                retryable=False,
            ),
            metadata=metadata,
        )

    async def _close_quiet(self, key: str) -> None:
        """Best-effort close for failure paths; ordinary errors are dropped.

        Cancellation and other BaseExceptions are never swallowed: they
        propagate so the gateway observes the true signal.
        """
        ext = self._extension
        if ext is None:
            return
        try:
            await ext.close_window(key)
        except Exception:
            pass

    @staticmethod
    def _merge_evidence_meta(
        meta: dict[str, object], evidence_id: str | None, evidence_error: str | None
    ) -> None:
        if evidence_id is not None:
            meta["gated_evidence_id"] = evidence_id
        if evidence_error is not None:
            meta["gated_evidence"] = evidence_error

    async def _persist_settled(
        self, invocation: ToolInvocation, *, grant_window_id: str
    ) -> tuple[str | None, str | None]:
        """Append the settled companion; never raises.

        Returns (evidence_id, error). The digest covers exactly the stored
        companion payload (record_kind plus companion), so the digest means
        the stored bytes, not the envelope. A missing store, a missing
        companion, or a sink failure all return a machine-readable error
        instead of raising, so callers surface them truthfully.
        """
        store = self._evidence
        if store is None:
            return None, "gated_evidence_unconfigured"
        ext = self._extension
        companion: dict[str, object] | None = None
        if ext is not None:
            companion = ext.companion_for(grant_window_id)
        if companion is None:
            return None, "gated_companion_missing"
        payload: dict[str, object] = {
            "record_kind": "window_egress_admission",
            "companion": companion,
        }
        ctx = invocation.execution_context
        record = EvidenceRecord(
            evidence_id=EvidenceId("ev_gwin_" + grant_window_id),
            run_id=ctx.run_id,
            engagement_id=ctx.engagement_id,
            source_type=EvidenceSourceType.SYSTEM,
            source_id=GATE_SOURCE_ID,
            action_id=invocation.action_id,
            invocation_id=invocation.invocation_id,
            hypothesis_id=None,
            content_type="application/json",
            artifact_ref=None,
            digest=digest_payload(payload),
            summary="gated window egress admission for " + grant_window_id,
            created_at=datetime.now(timezone.utc),
            metadata=payload,
        )
        try:
            await store.append(record)
        except Exception:
            return None, "gated_evidence_failed"
        return str(record.evidence_id), None

    async def close_retained(
        self, key: str, *, window_id: str | None = None
    ) -> RetainedSettlement:
        """Close a retained job window at job terminal; always returns.

        The adapter calls this when a retained gated job-start reaches
        terminal or verified cancel. Unknown keys yield an empty settlement
        so status polls stay safe. Durability note: the retained handle is
        process memory plus the durable ledger grant; a restart before this
        call leaves the grant reserved until the ledger expiry and
        quarantine reconcile charges it UNKNOWN. Restart re-attach of
        retained windows is not implemented.
        For restart recovery use recover_retained with the job-manifest
        record (see RetainedWindow); this path only serves the live process.
        """
        if window_id is not None:
            stored = self._retained.get(key)
            if stored is None or stored[1].window_id != window_id:
                # A stale job must not settle a sibling window opened on the
                # same key; that holder keeps both its window and its entry.
                return RetainedSettlement(
                    settlement=None, evidence_error="gated_window_identity_mismatch"
                )
        stored = self._retained.pop(key, None)
        invocation = stored[0] if stored is not None else None
        ext = self._extension
        if ext is None:
            return RetainedSettlement(settlement=None)
        if invocation is None and not ext.is_transmit_open(key):
            return RetainedSettlement(settlement=None)
        try:
            settlement = await ext.close_window(key)
        except GatedNetworkError as exc:
            if str(exc) == "window_not_open":
                return RetainedSettlement(settlement=None)
            raise
        if invocation is None:
            return RetainedSettlement(
                settlement=settlement, evidence_error="gated_invocation_unknown"
            )
        evidence_id, evidence_error = await self._persist_settled(
            invocation, grant_window_id=settlement.window_id
        )
        return RetainedSettlement(
            settlement=settlement,
            evidence_id=evidence_id,
            evidence_error=evidence_error,
        )

    def retained_for(self, key: str) -> RetainedWindow | None:
        """Durable handle for one retained window, for the job manifest.

        Available from AFTER open_window until the delegate returns, so the
        adapter embeds to_record() into the protected job manifest BEFORE
        create_task launches. A manifest-write failure BEFORE spawn must be a
        plain refusal (ShellWorkroomError uncertainty=False, never swallowed);
        a failure AFTER spawn is common uncertainty plus a bounded diagnostic.
        The post-ACK annotate path only re-attaches the same cargo."""
        stored = self._retained.get(key)
        return stored[1] if stored is not None else None

    def _discard_provisional(self, key: str, window_id: str) -> None:
        """Drop a pre-spawn provisional whose grant never reached a running ACK."""
        stored = self._retained.get(key)
        if stored is not None and stored[1].window_id == window_id:
            self._retained.pop(key, None)

    async def recover_retained(self, record: RetainedWindow) -> RetainedSettlement:
        """Settle a retained window after a host restart; never refunds.

        The kernel TTL already closed transmission; the actual byte counter
        is unknowable here, so the durable ledger charges the FULL
        reservation UNKNOWN (quarantine) instead of refunding anything.
        Settlement replays idempotently when the ledger already settled.
        A recovery evidence record carries the settlement numbers with an
        explicit restart note; per-attempt admissions are gone with the old
        process and are reported as such, never fabricated.

        Identity is verified BEFORE any settlement. The ORIGINAL grant is read
        back through the existing window_grant ledger port, and its run_id,
        instance_id, epoch and purpose must match this record, because the
        ledger owns the reservation by that identity. A stale or foreign
        retained record therefore never settles a window it does not own: a
        mismatch, or a grant the ledger does not know, refuses with a distinct
        code and leaves the settlement None, so nothing is charged or released.
        The durable ledger is bound to one engagement, so an unknown window id
        here is also a foreign-engagement reference, refused for the same reason.
        """
        ext = self._extension
        if ext is None:
            return RetainedSettlement(
                settlement=None, evidence_error="gated_network_unconfigured"
            )
        try:
            grant = await ext.ledger.window_grant(window_id=record.window_id)
            if grant is None:
                return RetainedSettlement(
                    settlement=None, evidence_error="gated_window_unknown"
                )
            mismatch = _retained_identity_mismatch(record, grant)
            if mismatch is not None:
                return RetainedSettlement(settlement=None, evidence_error=mismatch)
            settlement = await ext.ledger.settle_window(
                window_id=record.window_id, meter=None, verified_closed=False
            )
        except ValueError:
            return RetainedSettlement(
                settlement=None, evidence_error="gated_window_unknown"
            )
        evidence_id, evidence_error = await self._persist_recovery(record, settlement)
        return RetainedSettlement(
            settlement=settlement,
            evidence_id=evidence_id,
            evidence_error=evidence_error,
        )

    async def _persist_recovery(
        self, record: RetainedWindow, settlement: WindowSettlement
    ) -> tuple[str | None, str | None]:
        """Append the post-restart recovery record; never raises."""
        store = self._evidence
        if store is None:
            return None, "gated_evidence_unconfigured"
        companion: dict[str, object] = {
            "source_id": GATE_SOURCE_ID,
            "window_id": record.window_id,
            "run_id": record.run_id,
            "instance_id": record.instance_id,
            "epoch": record.epoch,
            "workroom_key": record.key,
            "holder": {
                "action_id": record.holder_action_id,
                "invocation_id": record.holder_invocation_id,
                "work_id": record.holder_work_id,
            },
            "scope": [
                {"ip": flow.ip, "port": flow.port, "proto": flow.proto}
                for flow in record.allowed
            ],
            "admissions": [],
            "outcome": settlement.outcome,
            "committed_deliveries": settlement.committed_deliveries,
            "metered_bytes": settlement.metered_bytes,
            "charged_bytes": settlement.charged_bytes,
            "charged_deliveries": settlement.charged_deliveries,
            "observed_hook": "nf-output",
            "note": (
                "recovered after host restart; per-attempt admissions died "
                "with the old process and are not reconstructed; settlement "
                "numbers are authoritative; an accepted admission is metering "
                "plus destination-attempt linkage only, not evidence of a "
                "reached peer or application response"
            ),
        }
        payload: dict[str, object] = {
            "record_kind": "window_egress_admission",
            "companion": companion,
            "recovered_after_restart": True,
        }
        evidence = EvidenceRecord(
            evidence_id=EvidenceId("ev_gwin_" + record.window_id),
            run_id=record.run_id,
            engagement_id=record.engagement_id,
            source_type=EvidenceSourceType.SYSTEM,
            source_id=GATE_SOURCE_ID,
            action_id=record.holder_action_id,
            invocation_id=record.holder_invocation_id,
            hypothesis_id=None,
            content_type="application/json",
            artifact_ref=None,
            digest=digest_payload(payload),
            summary="gated window egress admission recovered for " + record.window_id,
            created_at=datetime.now(timezone.utc),
            metadata=payload,
        )
        try:
            await store.append(evidence)
        except Exception:
            return None, "gated_evidence_failed"
        return str(evidence.evidence_id), None
