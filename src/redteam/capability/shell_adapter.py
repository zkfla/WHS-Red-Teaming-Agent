"""In-process builtin shell adapter. Owned by Capability Team.

TASK-003: implements the internal ``MCPAdapter`` protocol with no MCP
transport, in the same structural position as ``ExampleMCPAdapter``:
``discover_tools`` returns the pinned shell manifest and ``invoke`` routes
to the warm workroom manager. This is the only shell execution entry
point; orchestration and control keep talking to the public
``ToolCatalog`` / ``ToolGateway`` contracts, so approval, work-ledger
binding, evidence, and resume keep working without knowing this class.

Job semantics reuse the existing vocabulary (running / completed /
failed / cancelled) plus two uncertainty-holding states
(``cleanup-unknown`` for teardown-unverified cancels, ``uncertain`` for
supervisor loss after execution may have started) and bind to the
trusted workroom key derived inside ``bind_context`` from the execution
identity (never a caller-built or model-trusted key).
Binding a shell ``job_id`` to a ``WorkItem`` stays with the future
orchestration change (TASK-006); this adapter only guarantees that a
job_id is meaningful inside its own workroom and never leaks across keys.

Cancelling a job stops the waiter, reaps
the host ``docker exec`` child, and terminates through the root
supervisor as the single deadline and termination authority (TERM, grace,
KILL, reap with subreaper tracking, immune to script env scrubbing and
session separation); protected supervisor state lives outside the model
workspace and mechanism commands resolve via trusted absolute paths with
a system-only mechanism PATH plus isolated Python, never the
workroom-first model PATH. The warm container itself is never killed by
a cancel. A verified teardown reclaims its ``max_jobs`` slot immediately;
a teardown-unverified cancel holds ``cleanup-unknown`` (slot held) and
supervisor loss holds ``uncertain`` (slot held) until a
supervisor-verified outcome or an authoritative container teardown
settles them; TTL and deadlines alone never release a possibly-live
budget. Uncertainty survives on the job record and in result metadata
through the common uncertain-execution marker. After restart, protected job
manifests and supervisor spools restore existing job ids. Submit recovery
returns the original acknowledgement; background results remain on job status.
Missing or unverified originals stay held and never authorize resubmission.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from redteam.capability.adapters.mcp.base import (
    ContextualInvocationAdapter,
    MCPCallResult,
)
from redteam.capability.gated_manifest import OPERATION_PURPOSE, gated_body_operation
from redteam.capability.gated_strategy import RetainedWindow, project_parsed_script
from redteam.capability.shell_manifest import (
    JOB_CANCEL_TOOL_NAME,
    JOB_START_OPERATION,
    JOB_START_TOOL_NAME,
    JOB_STATUS_TOOL_NAME,
    RUN_OPERATION,
    RUN_TOOL_NAME,
    SHELL_LANGUAGES,
    SHELL_SERVER_ID,
    SHELL_TOOLS,
    build_shell_descriptors,
)
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import (
    _JOB_TAG,
    ShellWorkroomError,
    ShellWorkroomManager,
    check_workroom_key,
    workroom_key_for,
)
from redteam.core.contracts.execution import (
    ExecutionRecoveryAnswer,
    ExecutionReference,
    ExecutionState,
)
from redteam.core.contracts.network import NetworkPurpose
from redteam.core.contracts.tool import (
    EXECUTION_STARTED_METADATA_KEY,
    EXECUTION_UNCERTAIN_METADATA_KEY,
    ToolDescriptor,
    ToolError,
    ToolInvocation,
    ToolResult,
)
from redteam.core.convert import execution_input_digest
from redteam.core.enums import ToolHealthStatus
from redteam.core.ports.execution_recovery import ExecutionRecoveryProvider

_JOB_ID_PREFIX = "shjob_"
_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
# Executions holding an active budget: running, cancel-requested but
# teardown-unverified, and supervisor-loss uncertain. Only
# verified-terminal records free the slot; TTL/deadlines never do.
_ACTIVE_STATUSES = ("running", "cleanup-unknown", "uncertain")
# Settle margin past the supervisor deadline before a cleanup-unknown job
# is eligible for a deadline-confirmed lookup.
_SETTLE_MARGIN_SECONDS = 10.0
# Bounded wait for a cancelled job teardown (host-child reap plus the
# container-side tag kill) before the cancel call reports its status.
_CANCEL_REAP_TIMEOUT_SECONDS = 20.0


class GatedExecutionDelegate(Protocol):
    # One-shot delegate into the existing closed-provider bodies.
    async def __call__(self) -> MCPCallResult: ...


class GatedExecutionPolicy(Protocol):
    # Provider-side structural view of the TASK-005 gated policy.
    # The concrete class lives with 005; it must structurally conform
    # so this seam binds without importing an unpublished module.
    # execute() routes through policy enforcement, then the delegate.
    async def execute(
        self,
        invocation: ToolInvocation,
        parsed: Any,
        delegate: GatedExecutionDelegate,
    ) -> MCPCallResult: ...
    # Install-route seam (TASK-005/007): the provider asks the policy
    # for the operator-pinned route of one mirror name, then seeds the
    # install exec env from the forward-proxy variables. Structural so
    # no provider-metal import is needed here.
    def install_route_for(self, mirror_name: str) -> Any: ...
    def install_forward_env(self, route: Any) -> dict[str, str]: ...
    # Retained-window seam (TASK-005/007): pre-spawn cargo lookup,
    # live close at terminal/cancel, post-restart recovery settle.
    # Structural; seam-less executors simply lack them and the
    # adapter guards every call.
    def retained_for(self, key: str) -> Any: ...
    async def close_retained(self, key: str, *, window_id: str | None = None) -> Any: ...
    async def recover_retained(self, record: Any) -> Any: ...


class _ScriptArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workroom_key: str
    language: str
    script: str
    timeout_seconds: float | None = None


class _JobArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workroom_key: str
    job_id: str


@dataclass
class _ShellJob:
    workroom_key: str
    language: str
    script: str
    script_sha256: str
    timeout_seconds: float
    created_at: float = 0.0
    status: str = "running"
    task: asyncio.Task | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    updated_at: float = 0.0
    cancel_verified: bool = False
    cancel_outcome: str | None = None
    uncertain: bool = False
    # Invocation-derived supervisor identity (TASK-007). The supervisor
    # state files and spool are keyed by this, while job_id stays the
    # model-facing handle. Legacy memory-only jobs default it to job_id.
    supervisor_id: str = ""
    invocation_id: str = ""
    input_digest: str = ""
    # Durable cancel seal state: True once the host-observed verified
    # teardown was preserved in the protected cancel mark before the
    # ACK. A verified cancel without a seal still ACKs, but carries an
    # uncertainty diagnostic so post-crash restore holds honestly.
    cancel_mark_sealed: bool = False
    # Retained gated window cargo in the protected manifest. Set at
    # submit when the pre-spawn merge lands and at restore when the
    # manifest carries it. Gates window close/recover calls so native
    # jobs never touch the gate seam.
    retained_window: bool = False
    manifest: dict[str, Any] = field(default_factory=dict)
    window_settlement_reason: str | None = None
    window_evidence_id: str | None = None
    window_settled: bool = False
    restored: bool = False


class BuiltinShellAdapter(ContextualInvocationAdapter, ExecutionRecoveryProvider):
    """Shell execution entry point: gateway-bound tools to warm workrooms.

    Nominal conformance to the contextual execution and read-only
    recovery ports is declared on the class so signature drift fails
    type-checking instead of silently degrading to the legacy path.
    """

    server_id = SHELL_SERVER_ID

    # Trusted-identity contract for the gateway binder seam: this prefix
    # names shell-raised context errors, and ``bind_context`` is the only
    # place a model-supplied workroom key is reconciled with the trusted
    # execution identity.
    error_prefix = "shell"

    def __init__(
        self, policy: ShellWorkroomPolicy, *, workroom: ShellWorkroomManager | None = None
    ) -> None:
        # Single construction boundary for the image pin: it applies even
        # when the caller injects a workroom (e.g. tests). The manager
        # validates again on its own construction; the duplicate is harmless.
        policy.validate_image()
        self._policy = policy
        self._workroom = workroom if workroom is not None else ShellWorkroomManager(policy)
        self._jobs: dict[str, _ShellJob] = {}
        self._lock = asyncio.Lock()
        self._gated_policy: GatedExecutionPolicy | None = None
        self._gated_descriptors: tuple[ToolDescriptor, ...] = ()
        self._gated_by_name: dict[str, ToolDescriptor] = {}

    @property
    def policy(self) -> ShellWorkroomPolicy:
        return self._policy

    def set_gated_policy(
        self,
        executor: GatedExecutionPolicy,
        descriptors: list[ToolDescriptor],
    ) -> None:
        # Public configure hook for the TASK-005 gated provider
        # (composition root calls this when gated execution is wired).
        # Stores the policy plus its descriptors. Gated names resolve
        # from the configured set, never from a central name list, and
        # discover_tools advertises them only while configured.
        if not callable(getattr(executor, "execute", None)):
            raise ValueError("gated executor must expose execute()")
        stored = tuple(descriptors)
        for descriptor in stored:
            if not isinstance(descriptor, ToolDescriptor):
                raise ValueError("gated descriptors must be ToolDescriptor")
            if not descriptor.tool_name:
                raise ValueError("gated descriptor needs a tool name")
        self._gated_policy = executor
        self._gated_descriptors = stored
        self._gated_by_name = {item.tool_name: item for item in stored}

    @property
    def program_source(self) -> ShellWorkroomManager:
        """Discovery object for the shell_programs injection (no new factory)."""
        return self._workroom

    def bind_context(self, invocation: ToolInvocation) -> dict[str, Any]:
        """Bind the trusted workroom key from the execution identity.

        The gateway calls this before schema validation and admission:
        the returned arguments (not the raw model arguments) are what
        gets validated and executed. Rules, owned here:
        the invocation run must match the execution-context run;
        instance identity is required (no silent run-only fallback);
        the trusted key is derived inside via ``workroom_key_for``;
        an omitted model key is injected; a supplied key must equal the
        trusted one or the call is rejected (never silently overwritten).
        Applies uniformly to all four shell tools, so job ownership
        cannot cross instances. Raises ``ValueError`` on mismatch, which
        the gateway reports without executing.
        """
        execution_context = invocation.execution_context
        if invocation.run_id != execution_context.run_id:
            raise ValueError("shell run identity mismatch")
        instance_id = execution_context.instance_id
        if instance_id is None:
            raise ValueError("shell invocation is missing instance identity")
        trusted = workroom_key_for(str(invocation.run_id), str(instance_id))
        bound = dict(invocation.arguments)
        supplied = bound.get("workroom_key")
        if supplied is None:
            bound["workroom_key"] = trusted
        elif supplied != trusted:
            raise ValueError("shell workroom key mismatch")
        return bound

    @staticmethod
    def script_digest(language: str, script: str) -> str:
        return hashlib.sha256((language + "\x00" + script).encode("utf-8")).hexdigest()

    async def discover_tools(self) -> list[ToolDescriptor]:
        # Policy-derived bounds so the schema never advertises values the
        # runtime would reject.
        tools = build_shell_descriptors(self._policy)
        if self._gated_policy is not None:
            tools = [*tools, *self._gated_descriptors]
        return tools

    async def health_check(self) -> ToolHealthStatus:
        try:
            alive = await self._workroom.probe()
        except Exception:
            return ToolHealthStatus.UNKNOWN
        return ToolHealthStatus.HEALTHY if alive else ToolHealthStatus.UNHEALTHY

    def _failure(
        self,
        tool: ToolDescriptor,
        started: datetime,
        code: str,
        message: str,
        *,
        uncertain: bool = False,
    ) -> MCPCallResult:
        metadata: dict[str, Any] = {"tool_name": tool.tool_name}
        if uncertain:
            # Producer side of the root-owned uncertain-execution
            # contract: execution may already have started (supervisor
            # loss), so the identical call must not be auto-repeated.
            # Before-start rejections never set this.
            metadata[EXECUTION_UNCERTAIN_METADATA_KEY] = True
        return MCPCallResult(
            success=False,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            output=None,
            error=ToolError(code=code, message=message, retryable=False),
            metadata=metadata,
        )

    def _valid_job_id(self, job_id: str) -> bool:
        if not job_id.startswith(_JOB_ID_PREFIX) or len(job_id) != len(_JOB_ID_PREFIX) + 16:
            return False
        return all(c in "0123456789abcdef" for c in job_id[len(_JOB_ID_PREFIX):])

    async def invoke(self, tool: ToolDescriptor, arguments: dict[str, Any]) -> MCPCallResult:
        started = datetime.now(timezone.utc)
        if tool.server_id != self.server_id or tool.tool_name not in SHELL_TOOLS:
            return self._failure(tool, started, "shell_unknown_tool", "unknown shell tool")
        try:
            if tool.tool_name == RUN_TOOL_NAME:
                return await self._invoke_run(tool, arguments, started)
            if tool.tool_name == JOB_START_TOOL_NAME:
                return await self._invoke_job_start(tool, arguments, started)
            if tool.tool_name == JOB_STATUS_TOOL_NAME:
                return await self._invoke_job_status(tool, arguments, started)
            return await self._invoke_job_cancel(tool, arguments, started)
        except ShellWorkroomError as exc:
            return self._failure(
                tool,
                started,
                str(exc),
                "shell workroom rejected the call",
                uncertain=exc.uncertainty,
            )
        except Exception:
            return self._failure(tool, started, "shell_internal_error", "shell call failed")

    @staticmethod
    def _tool_name_for(tool_id: object) -> str:
        return str(tool_id).split(".")[-1]

    def _reference_kind(self, invocation: ToolInvocation) -> str | None:
        # Execution kind for a recovery handle: closed names first,
        # then configured gated descriptors resolved through the
        # retained body map (existing RUN/JOB_START tags only). The
        # handle keeps the ORIGINAL tool name; kind drives dispatch.
        # Pure: no I/O, no execution.
        name = self._tool_name_for(invocation.tool_id)
        if name == JOB_START_TOOL_NAME:
            return "job_start"
        if name == RUN_TOOL_NAME:
            return "run"
        descriptor = self._gated_by_name.get(name)
        if descriptor is None:
            return None
        body = gated_body_operation(descriptor)
        if body == JOB_START_OPERATION:
            return "job_start"
        if body == RUN_OPERATION:
            return "run"
        return None

    @staticmethod
    def _reference_is_start(handle: dict[str, Any]) -> bool:
        kind = handle.get("kind")
        if kind in ("job_start", "run"):
            return kind == "job_start"
        return handle.get("tool") == JOB_START_TOOL_NAME

    def _contextual_descriptor(self, invocation: ToolInvocation) -> ToolDescriptor:
        return ToolDescriptor(
            tool_id=invocation.tool_id,
            server_id=self.server_id,
            tool_name=self._tool_name_for(invocation.tool_id),
        )

    def _trusted_supervisor(self, invocation: ToolInvocation) -> tuple[str, str]:
        # Pure derivation of (workroom_key, supervisor_id) from the trusted
        # bound invocation. Mirrors bind_context but never touches model
        # arguments beyond compare: a mismatch yields no handle, never a
        # silently overwritten identity. No I/O, no execution, no cache.
        context = invocation.execution_context
        if invocation.run_id != context.run_id:
            raise ValueError("shell run identity mismatch")
        if context.instance_id is None:
            raise ValueError("shell invocation is missing instance identity")
        trusted = workroom_key_for(str(invocation.run_id), str(context.instance_id))
        supplied = invocation.arguments.get("workroom_key")
        if supplied is not None and supplied != trusted:
            raise ValueError("shell workroom key mismatch")
        supervisor_id = str(invocation.invocation_id)
        if not _JOB_TAG.fullmatch(supervisor_id):
            raise ValueError("shell supervisor identity out of range")
        return trusted, supervisor_id

    _PROJECTION_FIELDS = ("workroom_key", "language", "script", "timeout_seconds")

    def _execution_arguments(self, invocation: ToolInvocation) -> ToolInvocation:
        # Provider-internal copy carrying only the closed-provider
        # fields, so the existing strict bodies never see foreign
        # selectors. Identity (ids, digest source) stays identical;
        # only the argument surface narrows.
        args = invocation.arguments
        narrowed = {
            k: args[k] for k in self._PROJECTION_FIELDS if isinstance(args, dict) and k in args
        }
        return invocation.model_copy(update={"arguments": narrowed})

    @staticmethod
    def _install_mirror(descriptor: ToolDescriptor, invocation: ToolInvocation) -> str | None:
        # Data-driven install detection through 005's published purpose
        # table: the descriptor purpose op decides, never a tool-name
        # branch here. Returns the mirror selector (target_ref) or None
        # when this is not an install execution. Pure: no I/O.
        try:
            ops = descriptor.operations or []
        except Exception:
            return None
        is_install = any(
            OPERATION_PURPOSE.get(op) is NetworkPurpose.INSTALL
            for op in ops
            if isinstance(op, str)
        )
        if not is_install:
            return None
        args = invocation.arguments
        ref = args.get("target_ref") if isinstance(args, dict) else None
        if not isinstance(ref, str) or not ref.strip():
            return None
        return ref.strip()

    @staticmethod
    def _install_env(policy: GatedExecutionPolicy, mirror: str) -> dict[str, str]:
        # Provider-owned installer env from the policy route. The proxy
        # variables come from operator pins (never model args); the
        # supervisor applies them after scrubbing model proxy/index
        # variables so a model override can never win. Fail closed.
        try:
            route = policy.install_route_for(mirror)
            env = policy.install_forward_env(route)
        except Exception as exc:
            raise ShellWorkroomError("shell_install_route_unavailable") from exc
        if not isinstance(env, dict) or not env:
            raise ShellWorkroomError("shell_install_route_unavailable")
        cleaned: dict[str, str] = {}
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ShellWorkroomError("shell_install_route_unavailable")
            cleaned[key] = value
        return cleaned

    async def _retained_window_record(
        self, invocation: ToolInvocation
    ) -> dict[str, Any] | None:
        # Pre-launch read of the policy retained-window cargo for this
        # key. Must run INSIDE the start delegate (after the policy
        # opened the window, before the manifest write and spawn).
        # None when unconfigured, absent, or unreadable. Never raises;
        # callers decide refusal from the seam below.
        try:
            policy = self._gated_policy
            if policy is None:
                return None
            retained_for = getattr(policy, "retained_for", None)
            if not callable(retained_for):
                return None
            context = invocation.execution_context
            if context.instance_id is None:
                return None
            workroom_key = workroom_key_for(str(invocation.run_id), str(context.instance_id))
            retained = retained_for(workroom_key)
            if retained is None:
                return None
            to_record = getattr(retained, "to_record", None)
            if not callable(to_record):
                return None
            record = to_record()
            return record if isinstance(record, dict) else None
        except Exception:
            return None

    def _policy_retains_windows(self) -> bool:
        # True when the configured policy exposes the retained-window
        # seam (retained_for). Only then is a missing pre-spawn record
        # a refusal; seam-less executors keep the tolerant path.
        policy = self._gated_policy
        return policy is not None and callable(getattr(policy, "retained_for", None))

    async def _invoke_gated(
        self, descriptor: ToolDescriptor, invocation: ToolInvocation, started: datetime
    ) -> MCPCallResult:
        # Gated execution: policy enforcement wraps one of the existing
        # closed-provider bodies. The body is picked by the descriptor
        # existing shell operation tags, so gated tool NAMES never appear
        # here. Anything unmappable fails closed instead of executing
        # the wrong body.
        policy = self._gated_policy
        assert policy is not None
        # Body resolution consumes the retained strategy map through
        # gated_body_operation: existing RUN/JOB_START tags only, so
        # purpose-specific gated operations resolve without any gated
        # tool NAME appearing here. Unmapped descriptors fail closed.
        body = gated_body_operation(descriptor)
        if body is None:
            raise ShellWorkroomError("shell_unknown_tool")
        # Identity gate first: equality against the trusted key (raises
        # on mismatch before policy sees anything). The delegate runs
        # the strict bodies on a narrowed copy so foreign selectors
        # never hit the closed validators, while the digest still
        # covers the original unstripped invocation.
        self._trusted_supervisor(invocation)
        narrowed = self._execution_arguments(invocation)
        spool_digest = execution_input_digest(invocation)
        projected = project_parsed_script(invocation.arguments)
        # Install executions seed the provider-owned installer env into
        # the delegate BEFORE the window opens, so an unroutable mirror
        # fails closed without holding a window. Install is sync-only:
        # a job body with an install purpose is refused outright.
        mirror = self._install_mirror(descriptor, invocation)
        extra_env: dict[str, str] | None = None
        if mirror is not None:
            if body != RUN_OPERATION:
                raise ShellWorkroomError("shell_install_route_unavailable")
            extra_env = self._install_env(policy, mirror)
        if body == JOB_START_OPERATION:
            async def delegate_start() -> MCPCallResult:
                # Inside the delegate the window is open: the retained
                # cargo must be embedded in the manifest BEFORE spawn.
                # A policy with the seam but no record refuses here
                # (never spawn without cargo); seam-less executors
                # stay tolerant with post-ACK backfill.
                retained = await self._retained_window_record(invocation)
                if retained is None and self._policy_retains_windows():
                    raise ShellWorkroomError("gated_window_unretained")
                return await self._invoke_job_start_contextual(
                    descriptor, narrowed, started, input_digest=spool_digest,
                    retained_window=retained,
                )
            result = await policy.execute(invocation, projected, delegate_start)
            annotated = await self._annotate_retained_window(invocation, descriptor, result)
            if not annotated and self._is_running_ack(result):
                # Post-spawn backfill lost: the submit stands, but the
                # window owner is unconfirmed — common uncertainty plus
                # a bounded diagnostic, never a silent drop.
                meta = dict(result.metadata or {})
                meta[EXECUTION_UNCERTAIN_METADATA_KEY] = True
                meta["gated_window_unconfirmed"] = (
                    "retained-window cargo unavailable after ACK; "
                    "holding for ledger settle"
                )
                result = replace(result, metadata=meta)
            return result
        async def delegate_run() -> MCPCallResult:
            return await self._invoke_run_contextual(
                descriptor, narrowed, started, input_digest=spool_digest,
                extra_env=extra_env,
            )
        return await policy.execute(invocation, projected, delegate_run)

    async def _annotate_retained_window(
        self, invocation: ToolInvocation, descriptor: ToolDescriptor, result: MCPCallResult
    ) -> bool:
        # Best-effort merge of the policy retained-window cargo into
        # the protected job manifest after a running ACK. Only runs for
        # start-kind delegates that actually acknowledged; anything
        # else is a no-op success. The pre-spawn merge is the primary
        # contract; this only backfills cargo that appeared after
        # launch. Returns True when the manifest carries the cargo (or
        # no backfill applied); False on a lost backfill so the caller
        # surfaces uncertainty. Never raises.
        try:
            if not result.success or not isinstance(result.output, dict):
                return True
            if result.output.get("status") != "running":
                return True
            job_id = result.output.get("job_id")
            if not isinstance(job_id, str) or not self._valid_job_id(job_id):
                return True
            policy = self._gated_policy
            if policy is None:
                return True
            retained_for = getattr(policy, "retained_for", None)
            if not callable(retained_for):
                return True
            context = invocation.execution_context
            if context.instance_id is None:
                return True
            workroom_key = workroom_key_for(str(invocation.run_id), str(context.instance_id))
            current = await self._workroom.read_job_manifest(workroom_key, job_id)
            if isinstance(current, dict) and "retained_window" in current:
                return True
            retained = retained_for(workroom_key)
            if retained is None:
                return False
            to_record = getattr(retained, "to_record", None)
            if not callable(to_record):
                return False
            record = to_record()
            if not isinstance(record, dict):
                return False
            return await self._workroom.annotate_job_manifest(
                workroom_key, job_id, {"retained_window": record}
            )
        except Exception:
            return False

    @staticmethod
    def _is_running_ack(result: MCPCallResult) -> bool:
        return (
            result.success
            and isinstance(result.output, dict)
            and result.output.get("status") == "running"
        )

    async def invoke_contextual(self, invocation: ToolInvocation) -> MCPCallResult:
        # Structural ContextualInvocationAdapter entry: the gateway supplies
        # the same bound, validated invocation after admission. Supervisor
        # identity derives from the trusted invocation id, never model args.
        # No contextual cache is kept: every call derives afresh.
        try:
            return await self._invoke_contextual_inner(invocation)
        except asyncio.CancelledError:
            await self._seal_contextual_cancel(invocation)
            raise

    async def _seal_contextual_cancel(self, invocation: ToolInvocation) -> None:
        # Best-effort durable cancel seal for a cancelled run-kind call.
        # Only run-kind references name a supervisor execution worth
        # sealing (job submits settle through their own manifest path).
        # Re-verifies teardown — the exec path already tore down under
        # shield — writes the mark when verified, then returns for the
        # re-raise. Never raises.
        try:
            if self._reference_kind(invocation) != "run":
                return
            workroom_key, supervisor_id = self._trusted_supervisor(invocation)
            report = await self._workroom.kill_tagged_exec(workroom_key, supervisor_id)
            if not bool(getattr(report, "verified", False)):
                return
            input_digest = execution_input_digest(invocation)
            mark = {
                "supervisor_id": supervisor_id,
                "outcome": "cancelled",
                "code": 142,
                "verified_teardown": True,
                "invocation_id": str(invocation.invocation_id),
                "input_digest": input_digest,
                "observed_epoch": time.time(),
            }
            await self._workroom.write_cancel_mark(workroom_key, supervisor_id, mark)
        except Exception:
            pass

    async def _invoke_contextual_inner(self, invocation: ToolInvocation) -> MCPCallResult:
        started = datetime.now(timezone.utc)
        name = self._tool_name_for(invocation.tool_id)
        tool = self._contextual_descriptor(invocation)
        gated = self._gated_by_name.get(name) if self._gated_policy is not None else None
        if gated is not None:
            try:
                return await self._invoke_gated(gated, invocation, started)
            except ShellWorkroomError as exc:
                return self._failure(
                    gated, started, str(exc), "shell workroom rejected the call",
                    uncertain=exc.uncertainty,
                )
            except Exception:
                return self._failure(gated, started, "shell_internal_error", "shell call failed")
        if name not in SHELL_TOOLS:
            return self._failure(tool, started, "shell_unknown_tool", "unknown shell tool")
        try:
            if name == RUN_TOOL_NAME:
                return await self._invoke_run_contextual(tool, invocation, started)
            if name == JOB_START_TOOL_NAME:
                return await self._invoke_job_start_contextual(tool, invocation, started)
            if name == JOB_STATUS_TOOL_NAME:
                return await self._invoke_job_status(tool, invocation.arguments, started)
            if name == JOB_CANCEL_TOOL_NAME:
                return await self._invoke_job_cancel(tool, invocation.arguments, started)
            return self._failure(tool, started, "shell_unknown_tool", "unknown shell tool")
        except ShellWorkroomError as exc:
            return self._failure(
                tool, started, str(exc), "shell workroom rejected the call",
                uncertain=exc.uncertainty,
            )
        except Exception:
            return self._failure(tool, started, "shell_internal_error", "shell call failed")

    async def _invoke_run_contextual(
        self,
        tool: ToolDescriptor,
        invocation: ToolInvocation,
        started: datetime,
        *,
        input_digest: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> MCPCallResult:
        workroom_key, supervisor_id = self._trusted_supervisor(invocation)
        if input_digest is None:
            input_digest = execution_input_digest(invocation)
        spool_digest = input_digest
        parsed = self._parse_script(invocation.arguments)
        if parsed.workroom_key != workroom_key:
            raise ShellWorkroomError("shell_workroom_key_mismatch")
        digest = self.script_digest(parsed.language, parsed.script)
        result = await self._workroom.exec_script(
            workroom_key,
            parsed.language,
            parsed.script,
            parsed.timeout_seconds,
            exec_tag=supervisor_id,
            invocation_id=supervisor_id,
            input_digest=spool_digest,
            retain_record=True,
            extra_env=extra_env,
        )
        return MCPCallResult(
            success=True,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            output=result.to_output_dict(digest),
            error=None,
            metadata={
                "tool_name": tool.tool_name,
                "workroom_key": workroom_key,
                "script_sha256": digest,
                EXECUTION_STARTED_METADATA_KEY: True,
            },
        )

    async def _await_spawn_verified(
        self, workroom_key: str, supervisor_id: str, *, timeout_seconds: float = 15.0
    ) -> bool:
        # Bounded wait for supervisor registration (pgid/record/meta
        # trace). True only on a verified trace. Never raises. The
        # job_start ACK is only returned after this; a host crash
        # before it leaves the submit ambiguous, and query then proves
        # registration from the manifest plus this same trace check,
        # else holds without resubmitting.
        loop = asyncio.get_running_loop()
        start = loop.time()
        while loop.time() - start < timeout_seconds:
            if await self._workroom.supervisor_trace_present(workroom_key, supervisor_id):
                return True
            await asyncio.sleep(0.5)
        return False

    @staticmethod
    def _unverified_submit(
        tool: ToolDescriptor, started: datetime, workroom_key: str,
        digest: str, supervisor_id: str,
    ) -> MCPCallResult:
        # Submit reached the provider but spawn is unverified: the
        # caller must collect via status, never resubmit blindly. A
        # later retry of the same invocation converges through the
        # deterministic id plus the manifest restore path.
        now = datetime.now(timezone.utc)
        return MCPCallResult(
            success=False,
            started_at=started,
            finished_at=now,
            output=None,
            error=ToolError(
                code="shell_submit_unverified",
                    message="submit reached provider but spawn unverified; collect via status",
                retryable=False,
            ),
            metadata={
                "tool_name": tool.tool_name,
                "workroom_key": workroom_key,
                "script_sha256": digest,
                "supervisor_id": supervisor_id,
                EXECUTION_STARTED_METADATA_KEY: True,
                EXECUTION_UNCERTAIN_METADATA_KEY: True,
            },
        )

    async def _invoke_job_start_contextual(
        self,
        tool: ToolDescriptor,
        invocation: ToolInvocation,
        started: datetime,
        *,
        input_digest: str | None = None,
        retained_window: dict[str, Any] | None = None,
    ) -> MCPCallResult:
        workroom_key, supervisor_id = self._trusted_supervisor(invocation)
        if input_digest is None:
            input_digest = execution_input_digest(invocation)
        parsed = self._parse_script(invocation.arguments)
        if parsed.workroom_key != workroom_key:
            raise ShellWorkroomError("shell_workroom_key_mismatch")
        digest = self.script_digest(parsed.language, parsed.script)
        async with self._lock:
            await self._sweep_locked()
            effective_timeout = (
                self._policy.default_timeout_seconds
                if parsed.timeout_seconds is None
                else float(parsed.timeout_seconds)
            )
            now = time.monotonic()
            job_id = self._deterministic_job_id(str(invocation.invocation_id), input_digest)
            existing = self._jobs.get(job_id)
            if existing is not None and existing.workroom_key == workroom_key:
                # Idempotent resubmit of the same invocation: active jobs
                # re-acknowledge without spawning again. A terminal
                # duplicate is refused outright; the ACK schema admits
                # only a running acknowledgement.
                if existing.status in _TERMINAL_STATUSES:
                    raise ShellWorkroomError("shell_duplicate_job")
                if not await self._await_spawn_verified(workroom_key, supervisor_id):
                    return self._unverified_submit(
                        tool, started, workroom_key, digest, supervisor_id
                    )
                return MCPCallResult(
                    success=True,
                    started_at=started,
                    finished_at=datetime.now(timezone.utc),
                    output={"job_id": job_id, "status": "running",
                            "workroom_key": workroom_key},
                    error=None,
                    metadata={
                        "tool_name": tool.tool_name,
                        "workroom_key": workroom_key,
                        "script_sha256": digest,
                        "supervisor_id": supervisor_id,
                        EXECUTION_STARTED_METADATA_KEY: True,
                    },
                )
            if existing is not None:
                raise ShellWorkroomError("shell_duplicate_job")
            if self._active_count() >= self._policy.max_jobs:
                raise ShellWorkroomError("shell_job_limit")
            manifest = {
                "job_id": job_id,
                "supervisor_id": supervisor_id,
                "workroom_key": workroom_key,
                "invocation_id": str(invocation.invocation_id),
                "input_digest": input_digest,
                "script_sha256": digest,
                "tool": self._tool_name_for(invocation.tool_id),
                "language": parsed.language,
                "timeout_seconds": effective_timeout,
                "created_epoch": time.time(),
                "holder": {
                    "run_id": str(invocation.run_id),
                    "instance_id": str(invocation.execution_context.instance_id),
                    "action_id": str(invocation.action_id),
                },
                "route": {
                    "tool": self._tool_name_for(invocation.tool_id),
                    "operation": invocation.operation,
                    "workroom_key": workroom_key,
                },
            }
            if retained_window is not None:
                # Pre-spawn retained-window cargo: the window owner is
                # durable before the job exists. Strictly validated;
                # a malformed record refuses the submit outright.
                if not isinstance(retained_window, dict):
                    raise ShellWorkroomError("shell_invalid_arguments")
                try:
                    cargo = json.dumps(retained_window, sort_keys=True).encode("utf-8")
                except (TypeError, ValueError) as exc:
                    raise ShellWorkroomError("shell_invalid_arguments") from exc
                if len(cargo) > 8192:
                    raise ShellWorkroomError("shell_invalid_arguments")
                manifest["retained_window"] = json.loads(cargo.decode("utf-8"))
                manifest["window_annotated"] = True
            await self._workroom.ensure_workroom(workroom_key)
            await self._workroom.write_job_manifest(workroom_key, job_id, manifest)
            job = _ShellJob(
                workroom_key=workroom_key,
                language=parsed.language,
                script=parsed.script,
                script_sha256=digest,
                timeout_seconds=effective_timeout,
                created_at=now,
                updated_at=now,
                supervisor_id=supervisor_id,
                invocation_id=str(invocation.invocation_id),
                input_digest=input_digest,
                retained_window=isinstance(manifest.get("retained_window"), dict),
                manifest=manifest,
            )
            job.task = asyncio.create_task(self._run_job(job_id))
            self._jobs[job_id] = job
        if not await self._await_spawn_verified(workroom_key, supervisor_id):
            return self._unverified_submit(tool, started, workroom_key, digest, supervisor_id)
        return MCPCallResult(
            success=True,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            output={"job_id": job_id, "status": "running", "workroom_key": workroom_key},
            error=None,
            metadata={
                "tool_name": tool.tool_name,
                "workroom_key": workroom_key,
                "script_sha256": digest,
                "supervisor_id": supervisor_id,
                EXECUTION_STARTED_METADATA_KEY: True,
            },
        )

    def prepare_reference(self, invocation: ToolInvocation) -> ExecutionReference | None:
        # Pure derivation from the same bound, validated invocation the
        # gateway supplies to invoke_contextual. No I/O, no execution, no
        # shared mutation, no clock: the same invocation always yields the
        # same reference. None means this operation has no durable
        # provider handle (status/cancel probes); ordinary invocation
        # remains available. The gateway verifies the returned identity
        # fields, including input_digest over the bound arguments.
        try:
            name = self._tool_name_for(invocation.tool_id)
            workroom_key, supervisor_id = self._trusted_supervisor(invocation)
            kind = self._reference_kind(invocation)
            if kind is None:
                return None
            narrowed = {
                k: invocation.arguments[k]
                for k in self._PROJECTION_FIELDS
                if isinstance(invocation.arguments, dict) and k in invocation.arguments
            }
            parsed = self._parse_script(narrowed)
            if parsed.workroom_key != workroom_key:
                return None
            context = invocation.execution_context
            input_digest = execution_input_digest(invocation)
            handle: dict[str, object] = {
                "workroom_key": workroom_key,
                "supervisor_id": supervisor_id,
                "script_sha256": self.script_digest(parsed.language, parsed.script),
                "tool": name,
            }
            handle["kind"] = kind
            if kind == "job_start":
                handle["job_id"] = self._deterministic_job_id(
                    str(invocation.invocation_id), input_digest
                )
            return ExecutionReference(
                engagement_id=context.engagement_id,
                run_id=invocation.run_id,
                instance_id=context.instance_id,
                action_id=invocation.action_id,
                invocation_id=invocation.invocation_id,
                tool_id=invocation.tool_id,
                input_digest=input_digest,
                provider_handle=handle,
            )
        except Exception:
            return None

    def _parse_script(self, arguments: dict[str, Any]) -> _ScriptArgs:
        try:
            parsed = _ScriptArgs.model_validate(arguments)
        except ValidationError:
            raise ShellWorkroomError("shell_invalid_arguments") from None
        check_workroom_key(parsed.workroom_key)
        if parsed.language not in SHELL_LANGUAGES:
            raise ShellWorkroomError("shell_unsupported_language")
        if not parsed.script:
            raise ShellWorkroomError("shell_empty_script")
        return parsed

    def _parse_job(self, arguments: dict[str, Any]) -> _JobArgs:
        try:
            parsed = _JobArgs.model_validate(arguments)
        except ValidationError:
            raise ShellWorkroomError("shell_invalid_arguments") from None
        check_workroom_key(parsed.workroom_key)
        if not self._valid_job_id(parsed.job_id):
            raise ShellWorkroomError("shell_unknown_job")
        return parsed

    def _lookup(self, parsed: _JobArgs) -> _ShellJob | None:
        # Jobs live only in this process memory: after a restart every old
        # job_id misses here and surfaces as shell_unknown_job. Recovery
        # across restarts belongs to TASK-007; this adapter only guarantees
        # the miss is explicit, never a stale success.
        job = self._jobs.get(parsed.job_id)
        if job is None or job.workroom_key != parsed.workroom_key:
            return None
        if job.status in _TERMINAL_STATUSES:
            ttl = self._policy.completed_job_ttl_seconds
            if time.monotonic() - job.updated_at > ttl:
                del self._jobs[parsed.job_id]
                return None
        return job

    async def _spooled_output(
        self, key: str, selector: str, script_sha: str
    ) -> dict[str, object] | None:
        # Assemble the RUN-shaped output from sealed spool streams.
        # Full gates re-checked here so restore and fetch share one
        # trusted builder: seal, correlation-free bounds, byte hashes
        # and sizes, policy caps. None on anything unverified. The
        # caller supplies the invocation/input correlation it owns.
        try:
            record = await self._workroom.supervisor_record(key, selector)
            meta = await self._workroom.spool_record(key, selector)
            if not isinstance(record, dict) or not isinstance(meta, dict):
                return None
            if not ShellWorkroomManager.record_trusted(record):
                return None
            if record.get("outcome") not in ("completed", "timeout"):
                return None
            if not self._spool_complete(meta):
                return None
            if not self._spool_bounds_ok(meta):
                return None
            if not self._spool_digest_ok(record, meta):
                return None
            if not self._record_code_ok(record):
                return None
            raw_out = await self._workroom.read_spool_stream(key, selector, stream="out")
            raw_err = await self._workroom.read_spool_stream(key, selector, stream="err")
            if raw_out is None or raw_err is None:
                return None
            out_entry = meta.get("out")
            err_entry = meta.get("err")
            if not isinstance(out_entry, dict) or not isinstance(err_entry, dict):
                return None
            if hashlib.sha256(raw_out).hexdigest() != out_entry.get("sha256"):
                return None
            if hashlib.sha256(raw_err).hexdigest() != err_entry.get("sha256"):
                return None
            if len(raw_out) != out_entry.get("size") or len(raw_err) != err_entry.get("size"):
                return None
            cap = self._policy.max_output_bytes
            if len(raw_out) > cap or len(raw_err) > cap:
                return None
            if not self._meta_times_ok(meta):
                return None
            started_epoch = meta.get("started_epoch")
            finished_epoch = meta.get("finished_epoch")
            assert isinstance(started_epoch, (int, float))
            assert isinstance(finished_epoch, (int, float))
            duration_ms = max(0, int((float(finished_epoch) - float(started_epoch)) * 1000))
            timed_out = record.get("outcome") == "timeout"
            code = record.get("code")
            return {
                "exit_code": code if record.get("outcome") == "completed" else None,
                "stdout": raw_out.decode("utf-8", errors="replace"),
                "stderr": raw_err.decode("utf-8", errors="replace"),
                "timed_out": timed_out,
                "truncated": bool(out_entry.get("truncated")) or bool(err_entry.get("truncated")),
                "duration_ms": duration_ms,
                "script_sha256": script_sha,
                "workroom_key": key,
            }
        except Exception:
            return None

    async def _restore_from_manifest(self, parsed: _JobArgs) -> _ShellJob | None:
        # Read-only lazy restore of a durable job id from the protected
        # submit manifest. No spawn, no supervisor launch, no manifest
        # rewrite: the restored entry only lets status/cancel operate on
        # the durable identity. Returns a settled-or-held entry, or None
        # when nothing durable exists. Never raises.
        try:
            manifest = await self._workroom.read_job_manifest(parsed.workroom_key, parsed.job_id)
            if not isinstance(manifest, dict):
                return None
            if manifest.get("job_id") != parsed.job_id:
                return None
            if manifest.get("workroom_key") != parsed.workroom_key:
                return None
            supervisor = manifest.get("supervisor_id")
            if not isinstance(supervisor, str) or not _JOB_TAG.fullmatch(supervisor):
                return None
            try:
                timeout = float(manifest.get("timeout_seconds") or 0.0)
            except (TypeError, ValueError):
                timeout = 0.0
            if not timeout > 0:
                timeout = self._policy.default_timeout_seconds
            job = _ShellJob(
                workroom_key=parsed.workroom_key,
                language=str(manifest.get("language") or ""),
                script="",
                script_sha256=str(manifest.get("script_sha256") or ""),
                timeout_seconds=timeout,
                created_at=0.0,
                updated_at=time.monotonic(),
                supervisor_id=supervisor,
                invocation_id=str(manifest.get("invocation_id") or ""),
                input_digest=str(manifest.get("input_digest") or ""),
                retained_window=isinstance(manifest.get("retained_window"), dict),
                manifest=manifest,
                restored=True,
            )
            self._jobs[parsed.job_id] = job
            await self._settle_restored(job, manifest)
            return job
        except Exception:
            return None

    async def _settle_restored(self, job: _ShellJob, manifest: dict[str, Any]) -> None:
        # One-shot read-only settlement for a restored entry. Order:
        # verified terminal record first, then sealed-spool retention
        # (record cleaned post-success), then liveness hold, then the
        # never-spawned conclusion only for a present-but-dead room
        # past the submit age. Gone/unknown rooms always hold. Never
        # raises; every inconclusive branch leaves the entry running.
        try:
            key = job.workroom_key
            sid = job.supervisor_id
            record = await self._workroom.supervisor_record(key, sid)
            meta = await self._workroom.spool_record(key, sid)
            trusted_outcome: str | None = None
            if (
                isinstance(record, dict)
                and record.get("outcome") in ("completed", "timeout", "cancelled")
                and ShellWorkroomManager.record_trusted(record)
            ):
                raw_outcome = record.get("outcome")
                trusted_outcome = raw_outcome if isinstance(raw_outcome, str) else None
            bound = (
                trusted_outcome is not None
                and self._spool_complete(meta)
                and self._meta_times_ok(meta)
                and self._spool_bounds_ok(meta)
                and self._spool_digest_ok(record, meta)
                and self._record_code_ok(record)
            )
            if trusted_outcome is not None and bound:
                if trusted_outcome == "completed":
                    output = await self._spooled_output(key, sid, job.script_sha256)
                    if output is not None:
                        job.result = output
                        job.status = "completed"
                        job.uncertain = False
                        job.updated_at = time.monotonic()
                        return
                elif trusted_outcome == "timeout":
                    # Same original contract as the completed arm: the
                    # bounded actual stdout/stderr, with the timeout
                    # signaled in the envelope plus the status error.
                    output = await self._spooled_output(key, sid, job.script_sha256)
                    job.status = "completed"
                    job.result = output
                    if output is None:
                        job.error = "shell_execution_timeout:output-unavailable"
                    else:
                        job.error = "shell_execution_timeout"
                    job.uncertain = output is None
                    job.updated_at = time.monotonic()
                    return
                else:
                    job.status = "cancelled"
                    job.result = None
                    job.error = None
                    job.uncertain = False
                    job.updated_at = time.monotonic()
                    return
            mark = await self._workroom.read_cancel_mark(key, sid)
            if self._cancel_mark_ok(
                mark,
                supervisor=sid,
                invocation_id=job.invocation_id,
                input_digest=job.input_digest,
            ):
                # Host-verified cancel seal with no terminal commit
                # (teardown consumed it): exact cancelled settlement.
                job.status = "cancelled"
                job.result = None
                job.error = None
                job.uncertain = False
                job.updated_at = time.monotonic()
                return
            if (
                self._spool_complete(meta)
                and self._meta_times_ok(meta)
                and isinstance(meta, dict)
                and meta.get("invocation_id") == job.invocation_id
                and meta.get("input_digest") == job.input_digest
            ):
                _, alive = await self._workroom.supervisor_outcome(key, sid)
                if not alive:
                    output = await self._spooled_output(key, sid, job.script_sha256)
                    if output is not None:
                        job.result = output
                        job.status = "completed"
                        job.uncertain = False
                        job.updated_at = time.monotonic()
                        return
            _, alive = await self._workroom.supervisor_outcome(key, sid)
            if alive:
                return
            container = await self._workroom.workroom_container_state(key)
            if container in ("gone", "unknown"):
                return
            created = manifest.get("created_epoch")
            if (
                isinstance(created, (int, float))
                and not isinstance(created, bool)
                and time.time() - float(created)
                > job.timeout_seconds + self._policy.exec_grace_seconds + _SETTLE_MARGIN_SECONDS
            ):
                job.status = "failed"
                job.result = None
                job.error = "shell_submit_unconfirmed"
                job.uncertain = False
                job.updated_at = time.monotonic()
        except Exception:
            pass

        finally:
            if job.status in _TERMINAL_STATUSES and not job.uncertain:
                await self._recover_gated_window_for(job, manifest)

    async def _invoke_run(
        self, tool: ToolDescriptor, arguments: dict[str, Any], started: datetime
    ) -> MCPCallResult:
        parsed = self._parse_script(arguments)
        digest = self.script_digest(parsed.language, parsed.script)
        result = await self._workroom.exec_script(
            parsed.workroom_key, parsed.language, parsed.script, parsed.timeout_seconds,
            retain_record=True,
        )
        return MCPCallResult(
            success=True,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            output=result.to_output_dict(digest),
            error=None,
            metadata={
                "tool_name": tool.tool_name,
                "workroom_key": parsed.workroom_key,
                "script_sha256": digest,
            },
        )

    def _active_count(self) -> int:
        """Jobs holding an active budget: running plus cleanup-unknown."""
        return sum(1 for job in self._jobs.values() if job.status in _ACTIVE_STATUSES)

    def _evict_terminal_pressure(self) -> None:
        """Drop oldest terminal records past the max_jobs scale. Defensive."""
        terminal = sorted(
            (job.updated_at, job_id)
            for job_id, job in self._jobs.items()
            if job.status in _TERMINAL_STATUSES
        )
        while terminal and sum(
            1 for job in self._jobs.values() if job.status in _TERMINAL_STATUSES
        ) > self._policy.max_jobs:
            _updated_at, evict_id = terminal.pop(0)
            self._jobs.pop(evict_id, None)

    async def _sweep_locked(self) -> None:
        """Drop expired terminal jobs, settle cleanup-unknown, bound scale.

        A verified cancel frees its active slot immediately and its record
        sits in terminal like any completed job. A cleanup-unknown job
        holds its slot until a deadline-confirmed supervisor lookup
        settles it; TTL alone never releases a possibly-live budget.
        """
        now = time.monotonic()
        ttl = self._policy.completed_job_ttl_seconds
        for job_id, job in list(self._jobs.items()):
            if job.status in _TERMINAL_STATUSES and now - job.updated_at >= ttl:
                self._jobs.pop(job_id, None)
        await self._settle_unknown_locked(now)
        self._evict_terminal_pressure()

    async def _settle_unknown_locked(self, now: float) -> None:
        """Settle teardown-unverified or uncertain jobs on verified evidence only.

        Only a supervisor-VERIFIED terminal outcome or an authoritative
        container state (gone, or present-but-stopped) ends uncertainty;
        a missing outcome with a running or unreadable container holds the
        slot indefinitely. TTL and deadlines alone never free a
        possibly-live budget.
        """
        for job_id, job in list(self._jobs.items()):
            if job.status not in ("cleanup-unknown", "uncertain"):
                continue
            supervisor_tag = job.supervisor_id or job_id
            outcome, _alive = await self._workroom.supervisor_outcome(
                job.workroom_key, supervisor_tag
            )
            if outcome is not None:
                record = await self._workroom.supervisor_record(
                    job.workroom_key, supervisor_tag
                )
                if (
                    record is not None
                    and record.get("outcome") == outcome
                    and ShellWorkroomManager.record_trusted(record)
                ):
                    self._apply_verified_outcome(job, outcome)
                    continue
                # A distrusted outcome file (forged, unprotected, or with
                # survivors) falls through to container-state evidence;
                # a live writer means the slot stays held.
            if not self._settle_eligible(now, job):
                continue
            state = await self._workroom.workroom_container_state(job.workroom_key)
            if state == "gone":
                job.status = "cancelled"
                job.result = None
                job.error = "shell_container_torn_down"
                job.uncertain = False
                job.updated_at = time.monotonic()
            elif state == "stopped":
                job.status = "cancelled"
                job.result = None
                job.error = "shell_container_stopped"
                job.uncertain = False
                job.updated_at = time.monotonic()
            # running or unknown: hold the slot.

    def _settle_eligible(self, now: float, job: _ShellJob) -> bool:
        """Deadline past which a missing outcome may consult container state."""
        return (now - job.created_at) > (
            job.timeout_seconds
            + self._policy.exec_grace_seconds
            + _SETTLE_MARGIN_SECONDS
        )

    def _apply_verified_outcome(self, job: _ShellJob, outcome: str) -> None:
        """Terminal transition on the supervisor own verified word.

        Cancel-originated jobs settle as cancelled whatever the supervisor
        reports. Supervisor-loss jobs keep their uncertainty except on a
        verified teardown (cancelled), since completed and timeout outcomes
        confirm the end but not the effects.
        """
        if job.status == "cleanup-unknown":
            job.status = "cancelled"
            job.result = None
            job.error = "shell_cancel_settled:" + outcome
            job.uncertain = False
        elif outcome == "cancelled":
            job.status = "cancelled"
            job.result = None
            job.error = "shell_cancel_settled:cancelled"
            job.uncertain = False
        elif outcome == "timeout":
            job.status = "completed"
            job.result = None
            job.error = "shell_execution_timeout:output-unavailable"
            job.uncertain = True
        else:
            job.status = "completed"
            job.result = None
            job.error = "shell_completion_unverified:output-unavailable"
            job.uncertain = True
        job.updated_at = time.monotonic()

    async def _invoke_job_start(
        self, tool: ToolDescriptor, arguments: dict[str, Any], started: datetime
    ) -> MCPCallResult:
        parsed = self._parse_script(arguments)
        digest = self.script_digest(parsed.language, parsed.script)
        async with self._lock:
            # max_jobs caps ACTIVE executions: running plus
            # teardown-unverified cancels. Verified-terminal records sit
            # under the same scale with oldest-first eviction on pressure.
            await self._sweep_locked()
            if self._active_count() >= self._policy.max_jobs:
                raise ShellWorkroomError("shell_job_limit")
            job_id = _JOB_ID_PREFIX + uuid.uuid4().hex[:16]
            while job_id in self._jobs:
                job_id = _JOB_ID_PREFIX + uuid.uuid4().hex[:16]
            effective_timeout = (
                self._policy.default_timeout_seconds
                if parsed.timeout_seconds is None
                else float(parsed.timeout_seconds)
            )
            now = time.monotonic()
            job = _ShellJob(
                workroom_key=parsed.workroom_key,
                language=parsed.language,
                script=parsed.script,
                script_sha256=digest,
                timeout_seconds=effective_timeout,
                created_at=now,
                updated_at=now,
            )
            job.supervisor_id = job_id
            job.task = asyncio.create_task(self._run_job(job_id))
            self._jobs[job_id] = job
        return MCPCallResult(
            success=True,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            output={"job_id": job_id, "status": "running", "workroom_key": parsed.workroom_key},
            error=None,
            metadata={
                "tool_name": tool.tool_name,
                "workroom_key": parsed.workroom_key,
                "script_sha256": digest,
            },
        )

    async def _run_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        supervisor_tag = job.supervisor_id or job_id
        try:
            result = await self._workroom.exec_script(
                job.workroom_key,
                job.language,
                job.script,
                job.timeout_seconds,
                exec_tag=supervisor_tag,
                invocation_id=job.supervisor_id or None,
                input_digest=job.input_digest or None,
                retain_record=True,
            )
            job.result = result.to_output_dict(job.script_sha256)
            job.status = "completed"
            self._evict_terminal_pressure()
        except asyncio.CancelledError:
            # The host docker-exec child is already reaped by the workroom;
            # terminate through the supervisor and record whether the
            # teardown verified. Verified frees the active slot at once;
            # unverified holds it as cleanup-unknown until a
            # deadline-confirmed lookup settles it. Never fails the
            # cancellation itself.
            kill_tagged = getattr(self._workroom, "kill_tagged_exec", None)
            report = None
            if kill_tagged is not None:
                try:
                    report = await kill_tagged(job.workroom_key, supervisor_tag)
                except Exception:
                    report = None
            verified = bool(getattr(report, "verified", False))
            job.cancel_verified = verified
            job.cancel_outcome = getattr(report, "outcome", None)
            job.status = "cancelled" if verified else "cleanup-unknown"
            job.result = None
            if not verified:
                job.error = "shell_cancel_unverified"
            else:
                await self._write_cancel_mark_for(job)
                self._evict_terminal_pressure()
            raise
        except ShellWorkroomError as exc:
            if exc.uncertainty:
                # Supervisor loss after execution may have started: stay
                # uncertainty-active (slot held) until a verified outcome
                # or an authoritative teardown settles the job.
                job.status = "uncertain"
                job.uncertain = True
                job.result = None
                job.error = str(exc)
            else:
                job.status = "failed"
                job.error = str(exc)
                self._evict_terminal_pressure()
        except Exception:
            job.status = "failed"
            job.error = "shell_job_failed"
            self._evict_terminal_pressure()
        finally:
            if job.status in _TERMINAL_STATUSES and not job.uncertain:
                await self._close_gated_window_for(job, job.manifest)
            job.updated_at = time.monotonic()
            job.task = None

    async def _invoke_job_status(
        self, tool: ToolDescriptor, arguments: dict[str, Any], started: datetime
    ) -> MCPCallResult:
        parsed = self._parse_job(arguments)
        job = self._lookup(parsed)
        if job is None:
            job = await self._restore_from_manifest(parsed)
        if job is None:
            return self._failure(tool, started, "shell_unknown_job", "unknown shell job")
        metadata: dict[str, Any] = {
            "tool_name": tool.tool_name,
            "workroom_key": parsed.workroom_key,
        }
        if job.uncertain:
            # The lookup itself succeeded; the marker describes the
            # underlying job execution, which must not be assumed clean.
            metadata[EXECUTION_UNCERTAIN_METADATA_KEY] = True
        self._window_metadata(job, metadata)
        return MCPCallResult(
            success=True,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            output={
                "job_id": parsed.job_id,
                "status": job.status,
                "result": job.result,
                "error": job.error,
                "workroom_key": parsed.workroom_key,
            },
            error=None,
            metadata=metadata,
        )

    async def _invoke_job_cancel(
        self, tool: ToolDescriptor, arguments: dict[str, Any], started: datetime
    ) -> MCPCallResult:
        parsed = self._parse_job(arguments)
        job = self._lookup(parsed)
        if job is None:
            job = await self._restore_from_manifest(parsed)
        if job is None:
            return self._failure(tool, started, "shell_unknown_job", "unknown shell job")
        was_active = job.status in _ACTIVE_STATUSES
        if job.status == "running":
            task = job.task
            if task is not None:
                task.cancel()
                # Wait for the teardown (host-child reap plus the
                # supervisor termination in _run_job) so the reported
                # status is measured, not a guess. Bounded: a slow
                # daemon cannot hang cancel. The awaited task ends
                # cancelled, which surfaces here as CancelledError and
                # is expected.
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), _CANCEL_REAP_TIMEOUT_SECONDS
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
                refreshed = self._jobs.get(parsed.job_id)
                if refreshed is not None:
                    job = refreshed
            if job.status == "running":
                if task is not None and not task.done():
                    # Teardown still in flight past the bound: the worker
                    # will settle itself, but the slot stays held as
                    # cleanup-unknown until it does.
                    job.status = "cleanup-unknown"
                    job.result = None
                    job.error = "shell_cancel_unverified"
                    job.updated_at = time.monotonic()
                else:
                    # Dead worker that never settled: verify directly
                    # instead of declaring success blindly.
                    await self._verify_cancel_locked(parsed.job_id)
                    refreshed = self._jobs.get(parsed.job_id)
                    if refreshed is not None:
                        job = refreshed
        elif job.status in ("cleanup-unknown", "uncertain") and (
            job.task is None or job.task.done()
        ):
            # Retry verification for a previous unverified cancel. A done
            # task (including one cancelled before its first step, which
            # never runs any handler) means no live worker remains.
            await self._verify_cancel_locked(parsed.job_id)
            refreshed = self._jobs.get(parsed.job_id)
            if refreshed is not None:
                job = refreshed
        if job.status in _TERMINAL_STATUSES and not job.uncertain:
            if job.restored:
                await self._recover_gated_window_for(job, job.manifest)
            else:
                await self._close_gated_window_for(job, job.manifest)
        return self._cancel_result(tool, started, parsed, job, was_active)

    async def _verify_cancel_locked(self, job_id: str) -> None:
        """Directly verify one cancellable job via the supervisor.

        The direct kill is bounded like any teardown wait: on timeout the
        job stays cleanup-unknown instead of hanging the cancel call.
        """
        job = self._jobs.get(job_id)
        if job is None or job.status not in ("running", "cleanup-unknown"):
            return
        try:
            report = await asyncio.wait_for(
                self._workroom.kill_tagged_exec(
                    job.workroom_key, job.supervisor_id or job_id
                ),
                _CANCEL_REAP_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            report = None
        verified = bool(getattr(report, "verified", False))
        job.cancel_verified = verified
        job.cancel_outcome = getattr(report, "outcome", None)
        job.result = None
        job.updated_at = time.monotonic()
        if verified:
            job.status = "cancelled"
            job.error = None
            await self._write_cancel_mark_for(job)
            self._evict_terminal_pressure()
        else:
            job.status = "cleanup-unknown"
            job.error = "shell_cancel_unverified"

    def _cancel_result(
        self,
        tool: ToolDescriptor,
        started: datetime,
        parsed: _JobArgs,
        job: _ShellJob,
        was_active: bool,
    ) -> MCPCallResult:
        """Build the cancel call result with honest teardown reporting.

        A verified teardown is success with the slot freed at once. An
        unverified teardown is failure carrying the common
        uncertain-execution marker: the job may still run, so the
        identical call must not be auto-repeated and the model must
        collect the job first. Terminal jobs report their state.
        ``cancelled`` is True only when this call performed the cancel.
        """
        finished = datetime.now(timezone.utc)
        metadata: dict[str, Any] = {
            "tool_name": tool.tool_name,
            "workroom_key": parsed.workroom_key,
            "container_teardown_verified": job.cancel_verified,
            "cancel_outcome": job.cancel_outcome,
        }
        metadata["cancel_sealed"] = job.cancel_mark_sealed
        self._window_metadata(job, metadata)
        if (
            job.status == "cancelled"
            and job.cancel_verified
            and not job.cancel_mark_sealed
            and job.invocation_id
            and job.input_digest
        ):
            # Verified teardown, but the durable seal is unconfirmed:
            # a host crash before a re-seal leaves restore holding.
            # Bounded diagnostic, common uncertainty marker.
            metadata[EXECUTION_UNCERTAIN_METADATA_KEY] = True
            metadata["cancel_seal_diagnostic"] = (
                "cancel verified but the durable seal is unconfirmed; "
                "restore after a host crash will hold as uncertain"
            )
        if job.status == "cancelled" and job.cancel_verified:
            return MCPCallResult(
                success=True,
                started_at=started,
                finished_at=finished,
                output={
                    "job_id": parsed.job_id,
                    "status": job.status,
                    "cancelled": was_active,
                    "workroom_key": parsed.workroom_key,
                },
                error=None,
                metadata=metadata,
            )
        if job.status == "cleanup-unknown":
            metadata[EXECUTION_UNCERTAIN_METADATA_KEY] = True
            return MCPCallResult(
                success=False,
                started_at=started,
                finished_at=finished,
                output={
                    "job_id": parsed.job_id,
                    "status": job.status,
                    "cancelled": False,
                    "workroom_key": parsed.workroom_key,
                },
                error=ToolError(
                    code="shell_cancel_unverified",
                    message="cancel requested but container teardown is unverified",
                    retryable=False,
                ),
                metadata=metadata,
            )
        if job.status == "uncertain":
            metadata[EXECUTION_UNCERTAIN_METADATA_KEY] = True
            return MCPCallResult(
                success=False,
                started_at=started,
                finished_at=finished,
                output={
                    "job_id": parsed.job_id,
                    "status": job.status,
                    "cancelled": False,
                    "workroom_key": parsed.workroom_key,
                },
                error=ToolError(
                    code=job.error or "shell_execution_uncertain",
                    message="execution outcome is uncertain; collect the job result first",
                    retryable=False,
                ),
                metadata=metadata,
            )
        if job.uncertain:
            # Terminal record with preserved uncertainty: report the state,
            # still marked so effects are never assumed clean.
            metadata[EXECUTION_UNCERTAIN_METADATA_KEY] = True
        return MCPCallResult(
            success=True,
            started_at=started,
            finished_at=finished,
            output={
                "job_id": parsed.job_id,
                "status": job.status,
                "cancelled": False,
                "workroom_key": parsed.workroom_key,
            },
            error=None,
            metadata=metadata,
        )

    async def aclose(self) -> None:
        """Cancel running jobs without touching warm containers."""
        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _reference_selector(reference: ExecutionReference) -> tuple[str, str] | None:
        # The only provider-handle read in this file. Central code never
        # opens the handle; only (workroom_key, supervisor_id) leaves it,
        # and only for container reads owned by this provider.
        try:
            handle = reference.provider_handle
            key = handle.get("workroom_key")
            selector = handle.get("supervisor_id")
        except AttributeError:
            return None
        if not isinstance(key, str) or not isinstance(selector, str):
            return None
        return key, selector

    @staticmethod
    def _spool_complete(meta: object) -> bool:
        if not isinstance(meta, dict):
            return False
        if meta.get("sealed") is not True:
            return False
        for stream in ("out", "err"):
            entry = meta.get(stream)
            if not isinstance(entry, dict) or entry.get("complete") is not True:
                return False
        return True

    @staticmethod
    def _spool_correlated(
        meta: object, reference: ExecutionReference
    ) -> bool:
        if not isinstance(meta, dict):
            return False
        return (
            meta.get("invocation_id") == str(reference.invocation_id)
            and meta.get("input_digest") == reference.input_digest
        )

    @staticmethod
    def _deterministic_job_id(invocation_id: str, input_digest: str) -> str:
        # Pure: the same trusted invocation always yields the same
        # model-facing job id, so a journal reference prepared before
        # submit already names the job, and a repeated submit of the
        # same invocation resolves instead of spawning twice.
        raw = ("job:" + invocation_id + ":" + input_digest).encode("utf-8")
        return _JOB_ID_PREFIX + hashlib.sha256(raw).hexdigest()[:16]

    async def _write_cancel_mark_for(self, job: _ShellJob) -> None:
        # Durable host-observed cancel seal, awaited BEFORE the cancel
        # ACK is built. Records a verified teardown the supervisor
        # already sealed (its record was consumed by the teardown),
        # correlated to the invocation that owns it. A failed write
        # never breaks the verified cancel itself; the caller surfaces
        # an uncertainty diagnostic instead. Never raises.
        try:
            if not job.invocation_id or not job.input_digest:
                return
            supervisor = job.supervisor_id
            if not isinstance(supervisor, str) or not _JOB_TAG.fullmatch(supervisor):
                return
            mark = {
                "supervisor_id": supervisor,
                "outcome": "cancelled",
                "code": 142,
                "verified_teardown": True,
                "invocation_id": job.invocation_id,
                "input_digest": job.input_digest,
                "observed_epoch": time.time(),
            }
            await self._workroom.write_cancel_mark(job.workroom_key, supervisor, mark)
            job.cancel_mark_sealed = True
        except Exception:
            pass

    @staticmethod
    def _cancel_mark_ok(
        mark: object, *, supervisor: str, invocation_id: str, input_digest: str
    ) -> bool:
        # A cancel seal counts only when it names this supervisor and
        # correlates to this invocation/input with a finite observed
        # epoch. Anything else is ignored, never trusted.
        if not isinstance(mark, dict):
            return False
        if mark.get("outcome") != "cancelled":
            return False
        code = mark.get("code")
        if isinstance(code, bool) or code != 142:
            return False
        if mark.get("verified_teardown") is not True:
            return False
        if mark.get("supervisor_id") != supervisor:
            return False
        if mark.get("invocation_id") != invocation_id:
            return False
        if mark.get("input_digest") != input_digest:
            return False
        epoch = mark.get("observed_epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
            return False
        return math.isfinite(float(epoch))

    async def _spool_bytes(
        self, key: str, selector: str, meta: dict[str, Any]
    ) -> tuple[bytes, bytes] | None:
        # Byte-verified sealed spool streams: reads both streams and
        # checks hashes, sizes, and policy caps against the sealed meta.
        # Shared by the record and cancel-seal fetch paths. None on
        # anything unverified. Never raises.
        try:
            raw_out = await self._workroom.read_spool_stream(key, selector, stream="out")
            raw_err = await self._workroom.read_spool_stream(key, selector, stream="err")
            if raw_out is None or raw_err is None:
                return None
            out_entry = meta.get("out")
            err_entry = meta.get("err")
            if not isinstance(out_entry, dict) or not isinstance(err_entry, dict):
                return None
            if hashlib.sha256(raw_out).hexdigest() != out_entry.get("sha256"):
                return None
            if hashlib.sha256(raw_err).hexdigest() != err_entry.get("sha256"):
                return None
            if len(raw_out) != out_entry.get("size") or len(raw_err) != err_entry.get("size"):
                return None
            cap = self._policy.max_output_bytes
            if len(raw_out) > cap or len(raw_err) > cap:
                return None
            return (raw_out, raw_err)
        except Exception:
            return None

    @staticmethod
    def _spool_digest_ok(record: object, meta: object) -> bool:
        # The terminal commit binds the meta: exact record spool flag
        # plus a recompute of sha256 over the canonical meta JSON.
        # Metadata/cap/time corruption therefore breaks the binding
        # instead of passing on a hash that only covered raw bytes.
        if not isinstance(record, dict) or not isinstance(meta, dict):
            return False
        if record.get("spool") != "complete":
            return False
        expected = record.get("spool_digest")
        if not isinstance(expected, str) or not expected:
            return False
        try:
            canonical = json.dumps(meta, sort_keys=True).encode("utf-8")
        except (TypeError, ValueError):
            return False
        return hashlib.sha256(canonical).hexdigest() == expected

    @staticmethod
    def _note_window_settlement_failure(job: _ShellJob, reason: str) -> None:
        # Bounded surface for a window settlement that did not
        # complete: the common uncertainty marker plus one short
        # diagnostic on the job record. The window is never assumed
        # closed, and no caller path re-raises.
        job.window_settlement_reason = reason

    @staticmethod
    def _window_metadata(job: _ShellJob, metadata: dict[str, Any]) -> None:
        if job.window_settlement_reason:
            metadata["gated_settlement"] = job.window_settlement_reason
        if job.window_evidence_id:
            metadata["gated_evidence_id"] = job.window_evidence_id

    def _consume_window_settlement(self, job: _ShellJob, outcome: Any) -> None:
        error = getattr(outcome, "evidence_error", None)
        settlement = getattr(outcome, "settlement", None)
        job.window_evidence_id = getattr(outcome, "evidence_id", None)
        if error:
            self._note_window_settlement_failure(job, str(error)[:160])
        elif settlement is None:
            self._note_window_settlement_failure(job, "gated_settlement_unconfirmed")
        else:
            job.window_settlement_reason = None
        job.window_settled = settlement is not None

    @staticmethod
    def _retained_record_for(
        cargo: object, *, key: str, run_id: str, instance_id: str,
        action_id: str, invocation_id: str,
    ) -> RetainedWindow | None:
        # Strict rebuild of the retained handle plus exact holder
        # correlation against the reference/manifest identity. The
        # ledger grant itself carries no holder, so the adapter
        # verifies it here: key, run, instance, action, invocation.
        # None on anything else. Never raises.
        try:
            if not isinstance(cargo, dict):
                return None
            record = RetainedWindow.from_record(cargo)
        except Exception:
            return None
        try:
            if record.key != key:
                return None
            if str(record.run_id) != run_id:
                return None
            if str(record.instance_id) != instance_id:
                return None
            if str(record.holder_action_id) != action_id:
                return None
            if str(record.holder_invocation_id) != invocation_id:
                return None
        except Exception:
            return None
        return record

    async def _recover_gated_window_for(
        self, job: _ShellJob, manifest: dict[str, Any]
    ) -> None:
        # Fresh-adapter terminal settlement: settle this job's
        # retained window UNKNOWN from the durable manifest cargo
        # with a verified holder. The ledger keeps the full
        # reservation (quarantine, never refund). Never re-raises into
        # a status/cancel query; an unverifiable holder or a failed
        # recovery surfaces as bounded job-level uncertainty.
        policy = self._gated_policy
        if policy is None or not job.retained_window or job.window_settled:
            return
        cargo = manifest.get("retained_window")
        holder = manifest.get("holder")
        if not isinstance(holder, dict):
            self._note_window_settlement_failure(
                job, "gated_window_holder_unknown"
            )
            return
        record = self._retained_record_for(
            cargo, key=job.workroom_key,
            run_id=str(holder.get("run_id")),
            instance_id=str(holder.get("instance_id")),
            action_id=str(holder.get("action_id")),
            invocation_id=job.invocation_id,
        )
        if record is None:
            # Strict holder/cargo correlation failed: this job does
            # not own the cargo, so settle nothing.
            self._note_window_settlement_failure(
                job, "gated_window_holder_unverified"
            )
            return
        try:
            outcome = await policy.recover_retained(record)
            self._consume_window_settlement(job, outcome)
        except Exception as exc:
            self._note_window_settlement_failure(
                job, "gated_window_recovery_failed:" + type(exc).__name__,
            )

    async def _close_gated_window_for(
        self, job: _ShellJob, manifest: dict[str, Any] | None = None
    ) -> None:
        # Live-process terminal settlement: close exactly this job's
        # retained window. The exact window id is rebuilt from the
        # durable protected cargo this adapter embedded at submit
        # time, so a stale job poll can never close a sibling's
        # freshly opened window on the same key. Unknown keys settle
        # empty and seam-less policies lack the call, so this is safe
        # for every job. Never re-raises into a status/cancel query;
        # a failure surfaces as bounded job-level uncertainty.
        policy = self._gated_policy
        if policy is None or not job.retained_window or job.window_settled:
            return
        cargo = manifest.get("retained_window") if manifest is not None else None
        holder = manifest.get("holder") if manifest is not None else None
        record: RetainedWindow | None = None
        if isinstance(cargo, dict) and isinstance(holder, dict):
            record = self._retained_record_for(
                cargo, key=job.workroom_key,
                run_id=str(holder.get("run_id")),
                instance_id=str(holder.get("instance_id")),
                action_id=str(holder.get("action_id")),
                invocation_id=job.invocation_id,
            )
        window_id = str(record.window_id) if record is not None else None
        if window_id is None:
            # No verifiable window id: close nothing rather than
            # risk a sibling window, and say so on the job.
            self._note_window_settlement_failure(
                job, "gated_window_identity_unverified"
            )
            return
        try:
            outcome = await policy.close_retained(job.workroom_key, window_id=window_id)
            self._consume_window_settlement(job, outcome)
        except Exception as exc:
            self._note_window_settlement_failure(
                job, "gated_window_close_failed:" + type(exc).__name__,
            )


    @staticmethod
    def _meta_times_ok(meta: object) -> bool:
        # Meta timestamps must be finite and ordered. There is no
        # fallback clock: a bad timestamp voids the seal instead of
        # fabricating a zero duration original.
        if not isinstance(meta, dict):
            return False
        started = meta.get("started_epoch")
        finished = meta.get("finished_epoch")
        if isinstance(started, bool) or not isinstance(started, (int, float)):
            return False
        if isinstance(finished, bool) or not isinstance(finished, (int, float)):
            return False
        started_f = float(started)
        finished_f = float(finished)
        if not math.isfinite(started_f) or not math.isfinite(finished_f):
            return False
        return finished_f >= started_f

    @staticmethod
    def _record_code_ok(record: object) -> bool:
        # Strict int (bool excluded) inside the supervisor code
        # vocabulary: completed carries a command status 0-255,
        # timeout is 124, cancelled is 142.
        if not isinstance(record, dict):
            return False
        code = record.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            return False
        outcome = record.get("outcome")
        if outcome == "completed":
            return 0 <= code <= 255
        if outcome == "timeout":
            return code == 124
        if outcome == "cancelled":
            return code == 142
        return False

    @staticmethod
    def _spool_bounds_ok(meta: object) -> bool:
        # Truncation flags must be bools and sizes must fit the sealed
        # cap the supervisor ran with.
        if not isinstance(meta, dict):
            return False
        cap = meta.get("cap_bytes")
        if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
            return False
        for stream in ("out", "err"):
            entry = meta.get(stream)
            if not isinstance(entry, dict):
                return False
            if not isinstance(entry.get("truncated"), bool):
                return False
            size = entry.get("size")
            if isinstance(size, bool) or not isinstance(size, int):
                return False
            if size < 0 or size > cap:
                return False
        return True

    async def _query_submit(
        self, reference: ExecutionReference, key: str
    ) -> ExecutionRecoveryAnswer:
        # Submit-operation state for a job_start reference. The manifest
        # (or live memory) proves the submit; the supervisor trace proves
        # registration. Background progress never blocks this answer, and
        # the background result stays on the separate status path under
        # the job instance owner. Never resubmits. Never raises.
        try:
            handle = reference.provider_handle
            job_id = handle.get("job_id")
            supervisor = handle.get("supervisor_id")
            if not isinstance(job_id, str) or not self._valid_job_id(job_id):
                return ExecutionRecoveryAnswer(
                    state=ExecutionState.UNKNOWN,
                    uncertain=True,
                    result_available=False,
                    error=ToolError(
                        code="shell_unknown_job",
                        message="unknown shell execution reference",
                    ),
                )
            if not isinstance(supervisor, str):
                return ExecutionRecoveryAnswer(
                    state=ExecutionState.UNKNOWN,
                    uncertain=True,
                    result_available=False,
                    error=ToolError(
                        code="shell_unknown_job",
                        message="unknown shell execution reference",
                    ),
                )
            manifest = await self._workroom.read_job_manifest(key, job_id)
            manifest_ok = (
                isinstance(manifest, dict)
                and manifest.get("job_id") == job_id
                and manifest.get("workroom_key") == key
                and manifest.get("supervisor_id") == supervisor
                and manifest.get("invocation_id") == str(reference.invocation_id)
                and manifest.get("input_digest") == reference.input_digest
            )
            memory = self._jobs.get(job_id)
            if memory is not None and memory.workroom_key != key:
                memory = None
            if not manifest_ok and memory is None:
                return ExecutionRecoveryAnswer(
                    state=ExecutionState.UNKNOWN,
                    uncertain=True,
                    result_available=False,
                    error=ToolError(
                        code="shell_unknown_job",
                        message="no durable submit for this job",
                    ),
                )
            if await self._workroom.supervisor_trace_present(key, supervisor):
                return ExecutionRecoveryAnswer(
                    state=ExecutionState.SUCCEEDED,
                    verified=True,
                    uncertain=False,
                    result_available=True,
                )
            return ExecutionRecoveryAnswer(
                state=ExecutionState.PENDING,
                uncertain=True,
                result_available=False,
                error=ToolError(
                    code="shell_submit_unconfirmed",
                    message="submit recorded but spawn unverified; holding",
                ),
            )
        except Exception:
            return ExecutionRecoveryAnswer(
                state=ExecutionState.UNKNOWN,
                uncertain=True,
                result_available=False,
                error=ToolError(
                    code="shell_recovery_query_failed",
                    message="provider execution state could not be verified",
                ),
            )

    async def _fetch_submit_ack(
        self, reference: ExecutionReference, handle: dict[str, Any], key: str
    ) -> ToolResult | None:
        # Preserved submit acknowledgement for a job_start reference:
        # JOB_START_SCHEMA shape, stable identity and timestamp,
        # returned immediately without waiting for the background.
        # Never RUN output. None unless the manifest proves the submit.
        try:
            job_id = handle.get("job_id")
            if not isinstance(job_id, str) or not self._valid_job_id(job_id):
                return None
            manifest = await self._workroom.read_job_manifest(key, job_id)
            if not isinstance(manifest, dict):
                return None
            if manifest.get("job_id") != job_id:
                return None
            if manifest.get("workroom_key") != key:
                return None
            if manifest.get("supervisor_id") != handle.get("supervisor_id"):
                return None
            if manifest.get("invocation_id") != str(reference.invocation_id):
                return None
            if manifest.get("input_digest") != reference.input_digest:
                return None
            created = manifest.get("created_epoch")
            if isinstance(created, bool) or not isinstance(created, (int, float)):
                return None
            if not math.isfinite(float(created)):
                return None
            stamp = datetime.fromtimestamp(float(created), tz=timezone.utc)
            return ToolResult(
                invocation_id=reference.invocation_id,
                action_id=reference.action_id,
                tool_id=reference.tool_id,
                success=True,
                output={"job_id": job_id, "status": "running", "workroom_key": key},
                error=None,
                started_at=stamp,
                finished_at=stamp,
                metadata={
                    "tool_name": JOB_START_TOOL_NAME,
                    "workroom_key": key,
                    "supervisor_id": handle.get("supervisor_id"),
                    "job_id": job_id,
                    EXECUTION_STARTED_METADATA_KEY: True,
                },
            )
        except Exception:
            return None

    async def query_execution(
        self, reference: ExecutionReference
    ) -> ExecutionRecoveryAnswer:
        # Read-only collection of an existing execution. Never resubmits,
        # never re-executes. Unknown failures are bounded into an UNKNOWN
        # answer, never raised. A teardown with UNKNOWN effect stays
        # UNCERTAIN: it is never settled as cancelled here.
        try:
            selected = self._reference_selector(reference)
            if selected is None:
                return ExecutionRecoveryAnswer(
                    state=ExecutionState.UNKNOWN,
                    error=ToolError(
                        code="shell_unknown_job",
                        message="unknown shell execution reference",
                    ),
                )
            key, selector = selected
            if not await self._workroom.verify_workroom_ownership(key):
                # A foreign same-name container (or a gone room) must never
                # supply a protected result through recovery: hold without
                # reading further. A gone room with an inaccessible spool
                # holds here too.
                return ExecutionRecoveryAnswer(
                    state=ExecutionState.UNKNOWN,
                    uncertain=True,
                    result_available=False,
                    error=ToolError(
                        code="shell_workroom_unverified",
                        message="workroom ownership unverified; holding",
                    ),
                )
            if self._reference_is_start(reference.provider_handle):
                # The reference names the SUBMIT operation, not the
                # background: submit state below, background result via
                # the separate status path.
                return await self._query_submit(reference, key)
            record = await self._workroom.supervisor_record(key, selector)
            meta = await self._workroom.spool_record(key, selector)
            trusted_outcome: str | None = None
            if (
                isinstance(record, dict)
                and record.get("outcome") in ("completed", "timeout", "cancelled")
                and ShellWorkroomManager.record_trusted(record)
            ):
                outcome_raw = record.get("outcome")
                trusted_outcome = outcome_raw if isinstance(outcome_raw, str) else None
            # The terminal commit binds the sealed meta: completeness plus
            # correlation, finite ordered times, cap-consistent truncation,
            # an exact spool_digest recompute over the canonical meta, and
            # a strict command-status code. Anything less is uncertain.
            bound = (
                self._spool_complete(meta)
                and self._spool_correlated(meta, reference)
                and self._meta_times_ok(meta)
                and self._spool_bounds_ok(meta)
                and self._spool_digest_ok(record, meta)
                and self._record_code_ok(record)
            )
            if trusted_outcome is not None and bound:
                if trusted_outcome == "completed":
                    return ExecutionRecoveryAnswer(
                        state=ExecutionState.SUCCEEDED,
                        verified=True,
                        uncertain=False,
                        result_available=True,
                    )
                if trusted_outcome == "timeout":
                    # A trusted timeout sealed the reaped tree: known
                    # terminal with preserved output, not a hold. The
                    # timed_out output flag carries the meaning downstream.
                    return ExecutionRecoveryAnswer(
                        state=ExecutionState.SUCCEEDED,
                        verified=True,
                        uncertain=False,
                        result_available=True,
                    )
                return ExecutionRecoveryAnswer(
                    state=ExecutionState.CANCELLED,
                    verified=True,
                    uncertain=False,
                    result_available=True,
                )
            if trusted_outcome is not None:
                # The end is proven but the raw is not: uncertain, never
                # success, and no result to fetch.
                state = (
                    ExecutionState.CANCELLED
                    if trusted_outcome == "cancelled"
                    else ExecutionState.SUCCEEDED
                )
                return ExecutionRecoveryAnswer(
                    state=state,
                    verified=False,
                    uncertain=True,
                    result_available=False,
                )
            if not isinstance(record, dict):
                # No terminal commit at all: a verified host cancel seal
                # still authorizes the preserved cancelled original.
                # A present-but-untrusted record keeps the uncertain
                # paths below; the seal never overrides it.
                mark = await self._workroom.read_cancel_mark(key, selector)
                if self._cancel_mark_ok(
                    mark,
                    supervisor=selector,
                    invocation_id=str(reference.invocation_id),
                    input_digest=reference.input_digest,
                ):
                    return ExecutionRecoveryAnswer(
                        state=ExecutionState.CANCELLED,
                        verified=True,
                        uncertain=False,
                        result_available=True,
                    )
            container = await self._workroom.workroom_container_state(key)
            if container == "running":
                return ExecutionRecoveryAnswer(
                    state=ExecutionState.RUNNING,
                    uncertain=True,
                    result_available=False,
                )
            if container in ("gone", "stopped"):
                return ExecutionRecoveryAnswer(
                    state=ExecutionState.UNKNOWN,
                    uncertain=True,
                    result_available=False,
                    error=ToolError(
                        code="shell_execution_uncertain",
                        message="workroom is gone with no verified terminal record",
                    ),
                )
            return ExecutionRecoveryAnswer(
                state=ExecutionState.UNKNOWN,
                uncertain=True,
                result_available=False,
                error=ToolError(
                    code="shell_container_unknown",
                    message="container state unreadable; holding",
                ),
            )
        except Exception:
            return ExecutionRecoveryAnswer(
                state=ExecutionState.UNKNOWN,
                uncertain=True,
                result_available=False,
                error=ToolError(
                    code="shell_recovery_query_failed",
                    message="provider execution state could not be verified",
                ),
            )

    async def fetch_execution(
        self, reference: ExecutionReference
    ) -> ToolResult | None:
        # Return the preserved original only after correlation and
        # integrity checks. Missing or unverified originals return None,
        # never a rerun. The output keeps the shell_run envelope shape so
        # it stays output-schema compatible, and the metadata carries the
        # execution_started marker.
        answer = await self.query_execution(reference)
        if not answer.result_available:
            return None
        handle = reference.provider_handle
        key = handle.get("workroom_key")
        selector = handle.get("supervisor_id")
        script_sha = handle.get("script_sha256")
        if (
            not isinstance(key, str)
            or not isinstance(selector, str)
            or not isinstance(script_sha, str)
        ):
            return None
        if not await self._workroom.verify_workroom_ownership(key):
            return None
        if self._reference_is_start(handle):
            # Submit acknowledgement, never background output.
            return await self._fetch_submit_ack(reference, handle, key)
        record = await self._workroom.supervisor_record(key, selector)
        meta = await self._workroom.spool_record(key, selector)
        if not (
            isinstance(record, dict)
            and isinstance(meta, dict)
            and ShellWorkroomManager.record_trusted(record)
            and record.get("outcome") in ("completed", "timeout", "cancelled")
            and self._spool_complete(meta)
            and self._spool_correlated(meta, reference)
            and self._meta_times_ok(meta)
            and self._spool_bounds_ok(meta)
            and self._spool_digest_ok(record, meta)
            and self._record_code_ok(record)
        ):
            # No terminal commit: a verified host cancel seal still
            # authorizes the preserved cancelled original. Anything
            # else is unrecoverable without replay.
            return await self._mark_cancelled_original(reference, handle, key, selector)
        assert isinstance(record, dict) and isinstance(meta, dict)
        streams = await self._spool_bytes(key, selector, meta)
        if streams is None:
            return None
        raw_out, raw_err = streams
        try:
            out_entry = meta.get("out")
            err_entry = meta.get("err")
            assert isinstance(out_entry, dict) and isinstance(err_entry, dict)
            stdout = raw_out.decode("utf-8", errors="replace")
            stderr = raw_err.decode("utf-8", errors="replace")
            outcome = record.get("outcome")
            timed_out = outcome == "timeout"
            truncated = bool(out_entry.get("truncated")) or bool(err_entry.get("truncated"))
            # Times are gate-validated finite and ordered above; no
            # fallback clock fabricates a duration here.
            started_raw = meta.get("started_epoch")
            finished_raw = meta.get("finished_epoch")
            if (
                isinstance(started_raw, bool)
                or not isinstance(started_raw, (int, float))
                or isinstance(finished_raw, bool)
                or not isinstance(finished_raw, (int, float))
            ):
                return None
            started_at = datetime.fromtimestamp(float(started_raw), tz=timezone.utc)
            finished_at = datetime.fromtimestamp(float(finished_raw), tz=timezone.utc)
            duration_ms = max(0, int((float(finished_raw) - float(started_raw)) * 1000))
            envelope = {
                "exit_code": None,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": timed_out,
                "truncated": truncated,
                "duration_ms": duration_ms,
                "script_sha256": script_sha,
                "workroom_key": key,
            }
            metadata: dict[str, object] = {
                "tool_name": handle.get("tool", RUN_TOOL_NAME),
                "workroom_key": key,
                "script_sha256": script_sha,
                EXECUTION_STARTED_METADATA_KEY: True,
            }
            if outcome == "cancelled":
                # Verified teardown with preserved raw: failure of the
                # execution (cancelled), not of the command. The RUN
                # envelope keeps schema compatibility.
                return ToolResult(
                    invocation_id=reference.invocation_id,
                    action_id=reference.action_id,
                    tool_id=reference.tool_id,
                    success=False,
                    output=envelope,
                    error=ToolError(
                        code="shell_cancelled",
                        message="execution cancelled after verified teardown",
                        retryable=False,
                    ),
                    started_at=started_at,
                    finished_at=finished_at,
                    metadata=metadata,
                )
            if outcome == "completed":
                code = record.get("code")
                if isinstance(code, int):
                    envelope["exit_code"] = code
            return ToolResult(
                invocation_id=reference.invocation_id,
                action_id=reference.action_id,
                tool_id=reference.tool_id,
                success=True,
                output=envelope,
                error=None,
                started_at=started_at,
                finished_at=finished_at,
                metadata=metadata,
            )
        except Exception:
            return None

    async def _mark_cancelled_original(
        self, reference: ExecutionReference, handle: dict[str, Any],
        key: str, selector: str,
    ) -> ToolResult | None:
        # Cancelled original from a verified host seal plus the sealed
        # spool. The teardown consumed the terminal record, so the
        # seal plus byte-verified spool is the verified original: seal
        # correlation, meta seal/times/bounds, stream hashes/sizes/caps.
        # None on anything unverified. Never replays. Never raises.
        try:
            script_sha = handle.get("script_sha256")
            if not isinstance(script_sha, str):
                return None
            if not await self._workroom.verify_workroom_ownership(key):
                return None
            mark = await self._workroom.read_cancel_mark(key, selector)
            if not self._cancel_mark_ok(
                mark,
                supervisor=selector,
                invocation_id=str(reference.invocation_id),
                input_digest=reference.input_digest,
            ):
                return None
            meta = await self._workroom.spool_record(key, selector)
            if not isinstance(meta, dict):
                return None
            if not self._spool_complete(meta):
                return None
            if not self._spool_correlated(meta, reference):
                return None
            if not self._meta_times_ok(meta):
                return None
            if not self._spool_bounds_ok(meta):
                return None
            streams = await self._spool_bytes(key, selector, meta)
            if streams is None:
                return None
            raw_out, raw_err = streams
            out_entry = meta.get("out")
            err_entry = meta.get("err")
            if not isinstance(out_entry, dict) or not isinstance(err_entry, dict):
                return None
            started_raw = meta.get("started_epoch")
            finished_raw = meta.get("finished_epoch")
            if (
                isinstance(started_raw, bool)
                or not isinstance(started_raw, (int, float))
                or isinstance(finished_raw, bool)
                or not isinstance(finished_raw, (int, float))
            ):
                return None
            envelope = {
                "exit_code": None,
                "stdout": raw_out.decode("utf-8", errors="replace"),
                "stderr": raw_err.decode("utf-8", errors="replace"),
                "timed_out": False,
                "truncated": bool(out_entry.get("truncated")) or bool(err_entry.get("truncated")),
                "duration_ms": max(0, int((float(finished_raw) - float(started_raw)) * 1000)),
                "script_sha256": script_sha,
                "workroom_key": key,
            }
            return ToolResult(
                invocation_id=reference.invocation_id,
                action_id=reference.action_id,
                tool_id=reference.tool_id,
                success=False,
                output=envelope,
                error=ToolError(
                    code="shell_cancelled",
                    message="execution cancelled after verified teardown",
                    retryable=False,
                ),
                started_at=datetime.fromtimestamp(float(started_raw), tz=timezone.utc),
                finished_at=datetime.fromtimestamp(float(finished_raw), tz=timezone.utc),
                metadata={
                    "tool_name": handle.get("tool", RUN_TOOL_NAME),
                    "workroom_key": key,
                    "script_sha256": script_sha,
                    EXECUTION_STARTED_METADATA_KEY: True,
                },
            )
        except Exception:
            return None
