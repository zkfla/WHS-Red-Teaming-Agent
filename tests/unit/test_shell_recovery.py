# TASK-007 provider recovery: reference purity, contextual identity, spool query/fetch.
# No Docker: the workroom is a fake in-memory spool store.
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, cast

import pytest

from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_manifest import SHELL_SERVER_ID
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import ExecResult, ShellWorkroomManager
from redteam.core.contracts.execution import ExecutionState
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.convert import execution_input_digest
from redteam.core.ids import (
    ActionId,
    AgentId,
    AgentInstanceId,
    EngagementId,
    InvocationId,
    RunId,
    ToolId,
)

RUN_ID = RunId("run_recovery_1")
INSTANCE_ID = AgentInstanceId("inst_recovery_1")
ENGAGEMENT_ID = EngagementId("eng_recovery_1")
RUN_TOOL_ID = ToolId("analysis.shell_run")
STATUS_TOOL_ID = ToolId("analysis.shell_job_status")
def _policy() -> ShellWorkroomPolicy:
    return ShellWorkroomPolicy(image="recovery-test", allow_unpinned_image=True)
def _key() -> str:
    return str(RUN_ID) + "." + str(INSTANCE_ID)
def _invocation(tool_id: ToolId, invocation_id: str,
                arguments: dict[str, Any], approved=None) -> ToolInvocation:
    return ToolInvocation(
        invocation_id=InvocationId(invocation_id),
        action_id=ActionId("action_1"),
        run_id=RUN_ID,
        tool_id=tool_id,
        arguments=dict(arguments),
        timeout_seconds=30.0,
        execution_context=ExecutionContext(
            engagement_id=ENGAGEMENT_ID,
            run_id=RUN_ID,
            agent_id=AgentId("agent_1"),
            instance_id=INSTANCE_ID,
            approved_target=approved,
        ),
        created_at=datetime.now(timezone.utc),
    )
def _script_args() -> dict[str, Any]:
    return {"workroom_key": _key(), "language": "bash", "script": "echo hi"}
class _FakeWorkroom:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.owned = True
        self.captured: dict[str, Any] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.metas: dict[str, dict[str, Any]] = {}
        self.streams: dict[tuple[str, str], bytes] = {}
        self.container_state = "running"
        self.manifests: dict[str, dict[str, Any]] = {}
        self.trace_present = True
        self.marks: dict[str, dict[str, Any]] = {}
    async def exec_script(self, key: str, language: str, script: str,
                          timeout_seconds: float | None = None,
                          exec_tag: str | None = None, **kwargs: Any) -> ExecResult:
        self.calls.append("exec_script")
        self.captured = {"exec_tag": exec_tag, "kwargs": dict(kwargs)}
        return ExecResult(exit_code=0, stdout="hi", stderr="", timed_out=False,
                          truncated=False, duration_ms=5, workroom_key=key,
                          container_name="fake")
    async def supervisor_record(self, key: str, selector: str):
        self.calls.append("supervisor_record")
        return self.records.get(selector)
    async def spool_record(self, key: str, supervisor_id: str):
        self.calls.append("spool_record")
        return self.metas.get(supervisor_id)
    async def read_spool_stream(self, key: str, supervisor_id: str, *, stream: str = "out"):
        self.calls.append("read_spool_stream:" + stream)
        return self.streams.get((supervisor_id, stream))
    async def workroom_container_state(self, key: str) -> str:
        self.calls.append("workroom_container_state")
        return self.container_state
    async def supervisor_trace_present(self, key: str, supervisor_id: str) -> bool:
        self.calls.append("supervisor_trace_present")
        return self.trace_present
    async def ensure_workroom(self, key: str) -> str:
        self.calls.append("ensure_workroom")
        return "fake-container"
    async def write_job_manifest(self, key: str, job_id: str, manifest: dict) -> None:
        self.calls.append("write_job_manifest")
        self.manifests[job_id] = dict(manifest)
    async def read_job_manifest(self, key: str, job_id: str):
        self.calls.append("read_job_manifest")
        found = self.manifests.get(job_id)
        return dict(found) if isinstance(found, dict) else None
    async def annotate_job_manifest(self, key: str, job_id: str, patch: dict) -> bool:
        self.calls.append("annotate_job_manifest")
        found = self.manifests.get(job_id)
        if not isinstance(found, dict) or set(patch) != {"retained_window"}:
            return False
        merged = dict(found)
        merged["retained_window"] = patch["retained_window"]
        self.manifests[job_id] = merged
        return True
    async def supervisor_outcome(self, key: str, selector: str):
        return (None, True)
    async def verify_workroom_ownership(self, key: str) -> bool:
        self.calls.append("verify_workroom_ownership")
        return self.owned
    async def kill_tagged_exec(self, key: str, tag: str):
        raise AssertionError("must not terminate during recovery tests")
    async def write_cancel_mark(self, key: str, supervisor_id: str, mark: dict) -> None:
        self.calls.append("write_cancel_mark")
        self.marks[supervisor_id] = dict(mark)
    async def read_cancel_mark(self, key: str, supervisor_id: str):
        self.calls.append("read_cancel_mark")
        found = self.marks.get(supervisor_id)
        return dict(found) if isinstance(found, dict) else None
def _adapter(fake):
    return BuiltinShellAdapter(_policy(), workroom=cast(Any, fake))
def _trusted_record(meta: dict[str, Any] | None = None) -> dict[str, Any]:
    digest = None
    if meta is not None:
        digest = hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest()
    return {"outcome": "completed", "code": 0, "reaped": 1, "unreaped": [],
            "subreaper": True, "protected": True,
            "spool": "complete", "spool_digest": digest}
def _sealed_meta(invocation) -> dict[str, Any]:
    out = b"hi"
    return {"sealed": True, "invocation_id": str(invocation.invocation_id),
            "input_digest": execution_input_digest(invocation),
            "cap_bytes": 65536, "started_epoch": 100.0, "finished_epoch": 100.005,
            "out": {"truncated": False,
                    "sha256": hashlib.sha256(out).hexdigest(),
                    "size": len(out), "complete": True},
            "err": {"truncated": False,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "size": 0, "complete": True}}
def test_prepare_reference_is_pure_and_deterministic() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    invocation = _invocation(RUN_TOOL_ID, "inv_prepare01", _script_args())
    first = adapter.prepare_reference(invocation)
    second = adapter.prepare_reference(invocation)
    assert first is not None and second is not None
    assert first == second
    assert first.input_digest == execution_input_digest(invocation)
    assert first.provider_handle["supervisor_id"] == "inv_prepare01"
    assert first.provider_handle["workroom_key"] == _key()
    assert first.run_id == RUN_ID and first.instance_id == INSTANCE_ID
    assert first.action_id == invocation.action_id
    assert first.tool_id == RUN_TOOL_ID
    assert fake.calls == []
def test_prepare_returns_none_for_status_tools() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    invocation = _invocation(STATUS_TOOL_ID, "inv_status01",
                             {"workroom_key": _key(), "job_id": "shjob_0123456789abcdef"})
    assert adapter.prepare_reference(invocation) is None
def test_prepare_returns_none_on_key_mismatch() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    args = _script_args()
    args["workroom_key"] = "run_recovery_1.inst_other"
    invocation = _invocation(RUN_TOOL_ID, "inv_mismatch01", args)
    assert adapter.prepare_reference(invocation) is None
async def test_invoke_contextual_uses_invocation_supervisor_id() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    invocation = _invocation(RUN_TOOL_ID, "inv_context01", _script_args())
    result = await adapter.invoke_contextual(invocation)
    assert result.success is True
    assert fake.captured["exec_tag"] == "inv_context01"
    assert fake.captured["kwargs"].get("invocation_id") == "inv_context01"
    assert fake.captured["kwargs"].get("input_digest") == execution_input_digest(invocation)
    assert result.metadata.get("execution_started") is True
async def test_job_run_retains_terminal_record() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    invocation = _invocation(ToolId("analysis.shell_job_start"), "inv_retain01", _script_args())
    started = await adapter.invoke_contextual(invocation)
    assert started.success is True
    job_id = started.output["job_id"]
    for _ in range(100):
        job = adapter._jobs.get(job_id)
        if job is not None and job.status == "completed":
            break
        await asyncio.sleep(0.05)
    assert adapter._jobs[job_id].status == "completed"
    assert fake.captured["kwargs"].get("retain_record") is True
async def test_sync_run_retains_terminal_record() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    invocation = _invocation(RUN_TOOL_ID, "inv_retain_sync01", _script_args())
    result = await adapter.invoke_contextual(invocation)
    assert result.success is True
    assert fake.captured["kwargs"].get("retain_record") is True
async def test_invoke_contextual_rejects_unknown_tool() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    invocation = _invocation(ToolId("analysis.nope"), "inv_unknown01", {})
    result = await adapter.invoke_contextual(invocation)
    assert result.success is False
    assert result.error is not None and result.error.code == "shell_unknown_tool"
async def test_query_fetch_completed_round_trip() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    args = _script_args()
    invocation = _invocation(RUN_TOOL_ID, "inv_roundtrip01", args)
    reference = adapter.prepare_reference(invocation)
    assert reference is not None
    sid = reference.provider_handle["supervisor_id"]
    fake.metas[sid] = _sealed_meta(invocation)
    fake.records[sid] = _trusted_record(fake.metas[sid])
    fake.streams[(sid, "out")] = b"hi"
    fake.streams[(sid, "err")] = b""
    answer = await adapter.query_execution(reference)
    assert answer.state == ExecutionState.SUCCEEDED
    assert answer.verified is True and answer.uncertain is False
    assert answer.result_available is True
    fetched = await adapter.fetch_execution(reference)
    assert fetched is not None
    assert fetched.invocation_id == invocation.invocation_id
    assert fetched.action_id == invocation.action_id
    assert fetched.tool_id == RUN_TOOL_ID
    assert fetched.success is True
    assert fetched.output["stdout"] == "hi"
    assert fetched.output["exit_code"] == 0
    assert fetched.metadata.get("execution_started") is True
async def test_fetch_rejects_tampered_spool() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    args = _script_args()
    invocation = _invocation(RUN_TOOL_ID, "inv_tamper01", args)
    reference = adapter.prepare_reference(invocation)
    assert reference is not None
    sid = reference.provider_handle["supervisor_id"]
    fake.metas[sid] = _sealed_meta(invocation)
    fake.records[sid] = _trusted_record(fake.metas[sid])
    fake.streams[(sid, "out")] = b"forged"
    fake.streams[(sid, "err")] = b""
    assert await adapter.fetch_execution(reference) is None
async def test_query_gone_without_record_is_uncertain_unknown() -> None:
    fake = _FakeWorkroom()
    fake.container_state = "gone"
    adapter = _adapter(fake)
    reference = adapter.prepare_reference(
        _invocation(RUN_TOOL_ID, "inv_gone01", _script_args()))
    assert reference is not None
    answer = await adapter.query_execution(reference)
    assert answer.state == ExecutionState.UNKNOWN
    assert answer.uncertain is True and answer.verified is False
    assert answer.result_available is False
    assert await adapter.fetch_execution(reference) is None
def test_manager_source_id_matches_server() -> None:
    assert isinstance(ShellWorkroomManager.source_id, property)
    assert SHELL_SERVER_ID == "shell-workroom"


async def test_meta_tamper_breaks_seal_binding() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    args = _script_args()
    invocation = _invocation(RUN_TOOL_ID, "inv_metatamper01", args)
    reference = adapter.prepare_reference(invocation)
    assert reference is not None
    sid = reference.provider_handle["supervisor_id"]
    meta = _sealed_meta(invocation)
    fake.records[sid] = _trusted_record(meta)
    meta["cap_bytes"] = 1
    fake.metas[sid] = meta
    fake.streams[(sid, "out")] = b"hi"
    fake.streams[(sid, "err")] = b""
    answer = await adapter.query_execution(reference)
    assert answer.uncertain is True and answer.verified is False
    assert answer.result_available is False
    assert await adapter.fetch_execution(reference) is None


async def test_unverified_ownership_holds_without_reads() -> None:
    fake = _FakeWorkroom()
    fake.owned = False
    adapter = _adapter(fake)
    reference = adapter.prepare_reference(
        _invocation(RUN_TOOL_ID, "inv_unowned01", _script_args()))
    assert reference is not None
    answer = await adapter.query_execution(reference)
    assert answer.state == ExecutionState.UNKNOWN
    assert answer.uncertain is True and answer.result_available is False
    assert "verify_workroom_ownership" in fake.calls
    assert "supervisor_record" not in fake.calls
    assert await adapter.fetch_execution(reference) is None


def _selected_invocation(invocation_id: str,
                         arguments: dict[str, Any]) -> ToolInvocation:
    from redteam.core.tool_binding import SELECTED_TOOL
    invocation = _invocation(RUN_TOOL_ID, invocation_id, arguments)
    return invocation.model_copy(update={"binding_mode": SELECTED_TOOL, "operation": ""})


def _gateway_with_shell(fake: _FakeWorkroom):
    from redteam.capability.gateway import MCPToolGateway
    from redteam.capability.registry import MCPToolRegistry
    from redteam.capability.shell_manifest import build_shell_descriptors
    adapter = _adapter(fake)
    registry = MCPToolRegistry()
    registry.replace_server_tools(SHELL_SERVER_ID, build_shell_descriptors(_policy()))
    return MCPToolGateway(registry, {SHELL_SERVER_ID: adapter}), adapter


def test_adapter_satisfies_optional_provider_ports() -> None:
    from redteam.capability.adapters.mcp.base import ContextualInvocationAdapter
    from redteam.core.ports.execution_recovery import ExecutionRecoveryProvider
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    assert isinstance(adapter, ContextualInvocationAdapter)
    assert isinstance(adapter, ExecutionRecoveryProvider)


async def test_gateway_recovery_routing_uses_provider_reference() -> None:
    fake = _FakeWorkroom()
    fake.container_state = "gone"
    gateway, _adapter = _gateway_with_shell(fake)
    args = _script_args()
    invocation = _selected_invocation("inv_gateway01", args)
    reference = gateway.prepare_reference(invocation)
    assert reference is not None
    assert reference.input_digest == execution_input_digest(invocation)
    assert reference.provider_handle["supervisor_id"] == "inv_gateway01"
    answer = await gateway.query_execution(reference)
    assert answer.state == ExecutionState.UNKNOWN
    assert answer.uncertain is True
    assert await gateway.fetch_execution(reference) is None


class _FakeGatedPolicy:
    def __init__(self) -> None:
        self.calls: list = []
    async def execute(self, invocation, parsed, delegate):
        self.calls.append((invocation, parsed))
        return await delegate()
    def install_route_for(self, mirror_name):
        assert isinstance(mirror_name, str) and mirror_name.strip()
        return {"mirror": mirror_name}
    def install_forward_env(self, route):
        assert isinstance(route, dict) and route.get("mirror") == "m1"
        return {
            "HTTPS_PROXY": "http://proxy-test:3128",
            "https_proxy": "http://proxy-test:3128",
            "NO_PROXY": "127.0.0.1,localhost",
        }


def _gated_descriptor(name="gated_run", operation="shell_run"):
    from redteam.core.contracts.tool import ToolDescriptor
    return ToolDescriptor(
        tool_id=ToolId("gated." + name),
        server_id="gated-test",
        tool_name=name,
        description="gated test tool",
        input_schema={"type": "object", "properties": {}},
        operations=[operation],
    )


async def test_gated_hook_advertises_and_routes_only_when_configured() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    plain = await adapter.discover_tools()
    assert all(item.tool_name != "gated_run" for item in plain)
    policy = _FakeGatedPolicy()
    adapter.set_gated_policy(policy, [_gated_descriptor()])
    names = [item.tool_name for item in await adapter.discover_tools()]
    assert "gated_run" in names and "shell_run" in names
    args = _script_args()
    invocation = _invocation(ToolId("gated.gated_run"), "inv_gated01", args)
    result = await adapter.invoke_contextual(invocation)
    assert result.success is True
    assert len(policy.calls) == 1
    seen_invocation, seen_parsed = policy.calls[0]
    assert seen_invocation.invocation_id == invocation.invocation_id
    assert seen_parsed["language"] == "bash" and seen_parsed["script"] == "echo hi"
    assert fake.captured["exec_tag"] == "inv_gated01"


def _install_descriptor():
    from redteam.core.contracts.tool import ToolDescriptor
    return ToolDescriptor(
        tool_id=ToolId("gated.gated_install"),
        server_id="gated-test",
        tool_name="gated_install",
        description="gated install test tool",
        input_schema={"type": "object", "properties": {}},
        operations=["shell_install_run", "shell_run"],
    )


async def test_install_delegate_seeds_provider_env() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    adapter.set_gated_policy(_FakeGatedPolicy(), [_install_descriptor()])
    args = _script_args()
    args["target_ref"] = "m1"
    invocation = _invocation(ToolId("gated.gated_install"), "inv_inst_env01", args)
    result = await adapter.invoke_contextual(invocation)
    assert result.success is True
    seeded = fake.captured["kwargs"].get("extra_env")
    assert seeded == {
        "HTTPS_PROXY": "http://proxy-test:3128",
        "https_proxy": "http://proxy-test:3128",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    assert fake.captured["kwargs"].get("retain_record") is True


async def test_install_unknown_mirror_fails_closed() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    adapter.set_gated_policy(_FakeGatedPolicy(), [_install_descriptor()])
    args = _script_args()
    args["target_ref"] = "nope-mirror"
    invocation = _invocation(ToolId("gated.gated_install"), "inv_inst_env02", args)
    result = await adapter.invoke_contextual(invocation)
    assert result.success is False
    assert result.error is not None
    assert "exec_script" not in fake.calls


async def test_plain_run_passes_no_extra_env() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    adapter.set_gated_policy(_FakeGatedPolicy(), [_gated_descriptor()])
    invocation = _invocation(ToolId("gated.gated_run"), "inv_noenv01", _script_args())
    result = await adapter.invoke_contextual(invocation)
    assert result.success is True
    assert fake.captured["kwargs"].get("extra_env") is None


class _SeamGatedPolicy(_FakeGatedPolicy):
    def __init__(self, record=None):
        super().__init__()
        self._record = record
    def retained_for(self, key):
        return self._record


def _seam_record():
    from types import SimpleNamespace
    return SimpleNamespace(to_record=lambda: {"window_id": "gwin-seam-1"})


def _gated_start_descriptor():
    from redteam.core.contracts.tool import ToolDescriptor
    return ToolDescriptor(
        tool_id=ToolId("gated.gated_start"),
        server_id="gated-test",
        tool_name="gated_start",
        description="gated start test tool",
        input_schema={"type": "object", "properties": {}},
        operations=["shell_target_job_start", "shell_job_start"],
    )


async def test_gated_start_without_cargo_refuses_pre_spawn() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    adapter.set_gated_policy(_SeamGatedPolicy(record=None), [_gated_start_descriptor()])
    invocation = _invocation(ToolId("gated.gated_start"), "inv_seamref01", _script_args())
    result = await adapter.invoke_contextual(invocation)
    assert result.success is False
    assert result.error is not None
    assert result.error.code == "gated_window_unretained"
    assert "exec_script" not in fake.calls
    assert fake.manifests == {}


async def test_gated_start_merges_cargo_before_spawn() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    adapter.set_gated_policy(_SeamGatedPolicy(record=_seam_record()), [_gated_start_descriptor()])
    invocation = _invocation(ToolId("gated.gated_start"), "inv_seamok01", _script_args())
    result = await adapter.invoke_contextual(invocation)
    assert result.success is True
    job_id = result.output["job_id"]
    stored = fake.manifests[job_id]
    assert stored["retained_window"] == {"window_id": "gwin-seam-1"}
    assert stored["window_annotated"] is True


async def test_annotate_backfill_reports_loss_truthfully() -> None:
    from datetime import datetime, timezone

    from redteam.capability.adapters.mcp.base import MCPCallResult
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    adapter.set_gated_policy(_SeamGatedPolicy(record=None), [_gated_start_descriptor()])
    invocation = _invocation(ToolId("gated.gated_start"), "inv_seamack01", _script_args())
    now = datetime.now(timezone.utc)
    ack = MCPCallResult(success=True, started_at=now, finished_at=now,
                        output={"job_id": "shjob_0123456789abcdef", "status": "running",
                                "workroom_key": _key()},
                        error=None, metadata={})
    assert await adapter._annotate_retained_window(
        invocation, _gated_start_descriptor(), ack) is False
    adapter2 = _adapter(_FakeWorkroom())
    adapter2.set_gated_policy(_FakeGatedPolicy(), [_gated_descriptor()])
    assert await adapter2._annotate_retained_window(
        invocation, _gated_descriptor(), ack) is True


def test_gated_hook_rejects_bad_configuration() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    try:
        adapter.set_gated_policy(object(), [_gated_descriptor()])
    except ValueError:
        pass
    else:
        raise AssertionError("executor without execute() must be refused")
    try:
        adapter.set_gated_policy(_FakeGatedPolicy(), ["not-a-descriptor"])
    except ValueError:
        pass
    else:
        raise AssertionError("non-descriptor entries must be refused")
    assert adapter._gated_policy is None


async def test_gated_unknown_operation_fails_closed() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    adapter.set_gated_policy(_FakeGatedPolicy(), [_gated_descriptor("gated_weird", "nope_op")])
    invocation = _invocation(ToolId("gated.gated_weird"), "inv_gated02", _script_args())
    result = await adapter.invoke_contextual(invocation)
    assert result.success is False
    assert result.error is not None and result.error.code == "shell_unknown_tool"
    assert fake.captured == {}


async def test_gated_selector_dict_reaches_policy_with_digest_intact() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    policy = _FakeGatedPolicy()
    adapter.set_gated_policy(policy, [_gated_descriptor()])
    args = _script_args()
    args["target_ref"] = "host:example"
    args["port"] = 80
    invocation = _invocation(ToolId("gated.gated_run"), "inv_gated03", args)
    result = await adapter.invoke_contextual(invocation)
    assert result.success is True
    assert len(policy.calls) == 1
    seen_invocation, seen_parsed = policy.calls[0]
    assert isinstance(seen_parsed, dict)
    assert seen_parsed["language"] == "bash" and seen_parsed["script"] == "echo hi"
    assert "target_ref" not in seen_parsed and "port" not in seen_parsed
    assert seen_invocation.arguments["target_ref"] == "host:example"
    assert seen_invocation.arguments["port"] == 80
    assert fake.captured["exec_tag"] == "inv_gated03"
    assert fake.captured["kwargs"].get("input_digest") == execution_input_digest(seen_invocation)


def _gated_invocation(name, invocation_id, arguments):
    return _invocation(ToolId("analysis." + name), invocation_id, arguments)


def _approved_invocation(name, invocation_id, arguments, host):
    from redteam.core.contracts.target import HostTarget
    invocation = _gated_invocation(name, invocation_id, arguments)
    context = invocation.execution_context.model_copy(
        update={"approved_target": HostTarget(host=host)}
    )
    return invocation.model_copy(update={"execution_context": context})


def _real_gated_descriptors():
    from redteam.capability.gated_manifest import build_gated_descriptors
    return build_gated_descriptors()


def test_prepare_kinds_cover_real_gated_descriptors() -> None:
    from redteam.capability.gated_manifest import (
        SHELL_INSTALL_RUN,
        SHELL_TARGET_JOB_START,
        SHELL_TARGET_RUN,
    )
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    adapter.set_gated_policy(_FakeGatedPolicy(), _real_gated_descriptors())
    args = _script_args()
    args["target_ref"] = "reg1"
    args["port"] = 8080
    run_ref = adapter.prepare_reference(_gated_invocation(SHELL_TARGET_RUN, "inv_gkind01", args))
    assert run_ref is not None and run_ref.provider_handle["kind"] == "run"
    assert run_ref.provider_handle["tool"] == SHELL_TARGET_RUN
    assert "job_id" not in run_ref.provider_handle
    start_ref = adapter.prepare_reference(
        _gated_invocation(SHELL_TARGET_JOB_START, "inv_gkind02", args)
    )
    assert start_ref is not None and start_ref.provider_handle["kind"] == "job_start"
    assert start_ref.provider_handle["tool"] == SHELL_TARGET_JOB_START
    assert isinstance(start_ref.provider_handle.get("job_id"), str)
    install_ref = adapter.prepare_reference(
        _gated_invocation(SHELL_INSTALL_RUN, "inv_gkind03", args)
    )
    assert install_ref is not None and install_ref.provider_handle["kind"] == "run"
    assert fake.calls == []


async def test_gated_start_ack_fetch_and_restore() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    adapter.set_gated_policy(_FakeGatedPolicy(), _real_gated_descriptors())
    from redteam.capability.gated_manifest import SHELL_TARGET_JOB_START
    args = _script_args()
    args["target_ref"] = "reg1"
    args["port"] = 8080
    invocation = _gated_invocation(SHELL_TARGET_JOB_START, "inv_gack01", args)
    reference = adapter.prepare_reference(invocation)
    assert reference is not None
    job_id = reference.provider_handle["job_id"]
    assert isinstance(job_id, str) and job_id.startswith("shjob_")
    manifest = {
        "job_id": job_id,
        "supervisor_id": reference.provider_handle["supervisor_id"],
        "workroom_key": _key(),
        "invocation_id": "inv_gack01",
        "input_digest": reference.input_digest,
        "script_sha256": "0" * 64,
        "tool": SHELL_TARGET_JOB_START,
        "language": "bash",
        "timeout_seconds": 30.0,
        "created_epoch": 200.0,
    }
    fake.manifests[job_id] = manifest
    answer = await adapter.query_execution(reference)
    assert answer.state == ExecutionState.SUCCEEDED
    assert answer.verified is True and answer.result_available is True
    fetched = await adapter.fetch_execution(reference)
    assert fetched is not None and fetched.success is True
    assert fetched.tool_id == ToolId("analysis." + SHELL_TARGET_JOB_START)
    assert fetched.output == {"job_id": job_id, "status": "running", "workroom_key": _key()}
    from redteam.capability.shell_manifest import build_shell_descriptors
    by_name = {item.tool_name: item for item in build_shell_descriptors()}
    status = await adapter.invoke(
        by_name["shell_job_status"], {"workroom_key": _key(), "job_id": job_id}
    )
    assert status.success is True and status.output["status"] == "running"


async def test_fetch_cancelled_original_is_failure_with_envelope() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    args = _script_args()
    invocation = _invocation(RUN_TOOL_ID, "inv_cancel01", args)
    reference = adapter.prepare_reference(invocation)
    assert reference is not None
    sid = reference.provider_handle["supervisor_id"]
    meta = _sealed_meta(invocation)
    record = _trusted_record(meta)
    record["outcome"] = "cancelled"
    record["code"] = 142
    record["spool_digest"] = hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest()
    fake.records[sid] = record
    fake.metas[sid] = meta
    fake.streams[(sid, "out")] = b"hi"
    fake.streams[(sid, "err")] = b""
    answer = await adapter.query_execution(reference)
    assert answer.state == ExecutionState.CANCELLED
    assert answer.verified is True and answer.result_available is True
    fetched = await adapter.fetch_execution(reference)
    assert fetched is not None and fetched.success is False
    assert fetched.error is not None and fetched.error.code == "shell_cancelled"
    assert fetched.output["stdout"] == "hi" and fetched.output["timed_out"] is False


class _FakeWindowExtension:
    def __init__(self, policy):
        self.policy = policy
        self.ledger = None
        self.opened = []
        self.closed = []
    async def open_window(self, *, run_id, instance_id, epoch, caps, allowed,
                          holder_action_id, holder_invocation_id, holder_work_id,
                          internal=False):
        self.opened.append({"key": run_id + "." + instance_id, "epoch": epoch})
        from types import SimpleNamespace
        return SimpleNamespace(window_id="gwin-fake-1")
    async def close_window(self, key):
        self.closed.append(key)
        from redteam.core.contracts.network import WindowSettlement, WindowState
        return WindowSettlement(window_id="gwin-fake-1", state=WindowState.RELEASED,
                                charged_deliveries=0, charged_bytes=0,
                                released_deliveries=0, released_bytes=0,
                                committed_deliveries=0, outcome="closed_verified")
    def companion_for(self, window_id):
        return None
    def is_transmit_open(self, key):
        return False


def _real_gated_policy():
    from redteam.capability.gated_network_policy import (
        GatedNetworkPolicy,
        InstallProxyConfig,
        PinnedRegistry,
    )
    from redteam.capability.gated_strategy import GatedExecutionPolicy
    from redteam.core.contracts.network import (
        NetworkBudget,
        NetworkCapacity,
        NetworkPurpose,
        WindowCaps,
    )
    budget = NetworkBudget(
        global_capacity=NetworkCapacity(max_flow_init_deliveries=1000, max_output_bytes=1000000),
        target_capacity=NetworkCapacity(max_flow_init_deliveries=100, max_output_bytes=100000),
        install_capacity=NetworkCapacity(max_flow_init_deliveries=100, max_output_bytes=100000))
    tcaps = WindowCaps(max_flow_init_deliveries=10, max_output_bytes=100000,
                        duration_seconds=60.0, purpose=NetworkPurpose.TARGET)
    icaps = WindowCaps(max_flow_init_deliveries=10, max_output_bytes=100000,
                        duration_seconds=60.0, purpose=NetworkPurpose.INSTALL)
    registry = PinnedRegistry(targets={"reg1": ("10.0.0.11",)}, mirrors={"m1": ("10.0.0.12",)})
    proxy = InstallProxyConfig(endpoint="http://10.0.0.13:8080",
                               pinned_hosts=("10.0.0.13",), mirror_upstream="http://m1:8080")
    net_policy = GatedNetworkPolicy(budget=budget, default_target_caps=tcaps,
                                    default_install_caps=icaps, registry=registry,
                                    install_proxy=proxy)
    extension = _FakeWindowExtension(net_policy)
    def scope(target, *, denials_only=False):
        return (True, [], None)
    return GatedExecutionPolicy(extension=extension, scope_validator=scope, evidence_store=None)


async def test_composed_gated_run_body_end_to_end() -> None:
    from redteam.capability.gated_manifest import SHELL_TARGET_RUN
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    policy = _real_gated_policy()
    adapter.set_gated_policy(policy, _real_gated_descriptors())
    args = _script_args()
    args["target_ref"] = "reg1"
    args["port"] = 8080
    invocation = _approved_invocation(SHELL_TARGET_RUN, "inv_comp_run01", args, "reg1")
    result = await adapter.invoke_contextual(invocation)
    assert result.success is True
    assert result.metadata.get("gated_window_id") == "gwin-fake-1"
    assert fake.captured["exec_tag"] == "inv_comp_run01"
    assert _key() in policy._extension.closed
    reference = adapter.prepare_reference(invocation)
    assert reference is not None and reference.input_digest == execution_input_digest(invocation)


def _cancel_mark_for(invocation, supervisor: str) -> dict:
    return {
        "supervisor_id": supervisor,
        "outcome": "cancelled",
        "code": 142,
        "verified_teardown": True,
        "invocation_id": str(invocation.invocation_id),
        "input_digest": execution_input_digest(invocation),
        "observed_epoch": 200.0,
    }


async def test_query_fetch_from_cancel_mark_without_record() -> None:
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    args = _script_args()
    invocation = _invocation(RUN_TOOL_ID, "inv_mark01", args)
    reference = adapter.prepare_reference(invocation)
    assert reference is not None
    sid = reference.provider_handle["supervisor_id"]
    fake.metas[sid] = _sealed_meta(invocation)
    fake.streams[(sid, "out")] = b"hi"
    fake.streams[(sid, "err")] = b""
    fake.marks[sid] = _cancel_mark_for(invocation, sid)
    answer = await adapter.query_execution(reference)
    assert answer.state == ExecutionState.CANCELLED
    assert answer.verified is True and answer.uncertain is False
    assert answer.result_available is True
    fetched = await adapter.fetch_execution(reference)
    assert fetched is not None
    assert fetched.success is False
    assert fetched.error is not None and fetched.error.code == "shell_cancelled"
    assert fetched.output["stdout"] == "hi"
    assert fetched.output["exit_code"] is None
    assert fetched.output["timed_out"] is False
    assert fetched.metadata.get("execution_started") is True


async def test_restore_from_cancel_mark_settles_cancelled() -> None:
    from redteam.capability.shell_manifest import build_shell_descriptors
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    args = _script_args()
    invocation = _invocation(ToolId("analysis.shell_job_start"), "inv_mark02", args)
    reference = adapter.prepare_reference(invocation)
    assert reference is not None and reference.provider_handle["kind"] == "job_start"
    job_id = reference.provider_handle["job_id"]
    fake.manifests[job_id] = {
        "job_id": job_id,
        "supervisor_id": "inv_mark02",
        "workroom_key": _key(),
        "invocation_id": "inv_mark02",
        "input_digest": reference.input_digest,
        "script_sha256": "0" * 64,
        "tool": "shell_job_start",
        "language": "bash",
        "timeout_seconds": 30.0,
        "created_epoch": 100.0,
    }
    fake.marks["inv_mark02"] = _cancel_mark_for(invocation, "inv_mark02")
    wiped = _adapter(fake)
    by_name = {item.tool_name: item for item in build_shell_descriptors()}
    status = await wiped.invoke(
        by_name["shell_job_status"], {"workroom_key": _key(), "job_id": job_id}
    )
    assert status.success is True
    assert status.output["status"] == "cancelled"


async def test_timeout_restore_returns_output() -> None:
    from redteam.capability.shell_manifest import build_shell_descriptors
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    args = _script_args()
    invocation = _invocation(ToolId("analysis.shell_job_start"), "inv_mark03", args)
    reference = adapter.prepare_reference(invocation)
    assert reference is not None and reference.provider_handle["kind"] == "job_start"
    job_id = reference.provider_handle["job_id"]
    fake.manifests[job_id] = {
        "job_id": job_id,
        "supervisor_id": "inv_mark03",
        "workroom_key": _key(),
        "invocation_id": "inv_mark03",
        "input_digest": reference.input_digest,
        "script_sha256": "0" * 64,
        "tool": "shell_job_start",
        "language": "bash",
        "timeout_seconds": 30.0,
        "created_epoch": 100.0,
    }
    meta = _sealed_meta(invocation)
    fake.metas["inv_mark03"] = meta
    fake.records["inv_mark03"] = {
        "outcome": "timeout", "code": 124, "reaped": 2, "unreaped": [],
        "subreaper": True, "protected": True, "spool": "complete",
        "spool_digest": hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest(),
    }
    fake.streams[("inv_mark03", "out")] = b"hi"
    fake.streams[("inv_mark03", "err")] = b""
    wiped = _adapter(fake)
    by_name = {item.tool_name: item for item in build_shell_descriptors()}
    status = await wiped.invoke(
        by_name["shell_job_status"], {"workroom_key": _key(), "job_id": job_id}
    )
    assert status.success is True
    assert status.output["status"] == "completed"
    assert status.output["result"] is not None
    assert status.output["result"]["stdout"] == "hi"
    assert status.output["result"]["timed_out"] is True
    assert status.output["error"] == "shell_execution_timeout"


async def test_composed_gated_job_start_retains_and_annotates() -> None:
    from redteam.capability.gated_manifest import SHELL_TARGET_JOB_START
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    policy = _real_gated_policy()
    adapter.set_gated_policy(policy, _real_gated_descriptors())
    args = _script_args()
    args["target_ref"] = "reg1"
    args["port"] = 8080
    invocation = _approved_invocation(SHELL_TARGET_JOB_START, "inv_comp_start01", args, "reg1")
    result = await adapter.invoke_contextual(invocation)
    assert result.success is True
    job_id = result.output["job_id"]
    retained = policy.retained_for(_key())
    assert retained is not None and retained.window_id == "gwin-fake-1"
    stored = fake.manifests[job_id]
    assert stored["retained_window"]["window_id"] == "gwin-fake-1"
    assert stored["retained_window"]["holder_invocation_id"] == "inv_comp_start01"
    assert stored["tool"] == SHELL_TARGET_JOB_START
    reference = adapter.prepare_reference(invocation)
    assert reference is not None and reference.provider_handle["kind"] == "job_start"
    fetched = await adapter.fetch_execution(reference)
    assert fetched is not None and fetched.success is True
    assert fetched.output["job_id"] == job_id


async def test_composed_gated_install_body_end_to_end() -> None:
    from redteam.capability.gated_manifest import SHELL_INSTALL_RUN
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    policy = _real_gated_policy()
    adapter.set_gated_policy(policy, _real_gated_descriptors())
    args = _script_args()
    args["target_ref"] = "m1"
    invocation = _approved_invocation(SHELL_INSTALL_RUN, "inv_comp_inst01", args, "m1")
    result = await adapter.invoke_contextual(invocation)
    assert result.success is True
    assert result.metadata.get("gated_window_id") == "gwin-fake-1"
    assert _key() in policy._extension.closed

async def test_live_gated_completion_closes_without_status_poll() -> None:
    from redteam.capability.gated_manifest import SHELL_TARGET_JOB_START
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    policy = _real_gated_policy()
    adapter.set_gated_policy(policy, _real_gated_descriptors())
    args = {**_script_args(), "target_ref": "reg1", "port": 8080}
    invocation = _approved_invocation(SHELL_TARGET_JOB_START, "inv_live_close01", args, "reg1")
    started = await adapter.invoke_contextual(invocation)
    assert started.success is True
    job = adapter._jobs[started.output["job_id"]]
    if job.task is not None:
        await asyncio.wait_for(job.task, 2.0)
    assert job.status == "completed"
    assert job.result is not None and job.result["stdout"] == "hi"
    assert policy._extension.closed == [_key()]
    assert policy.retained_for(_key()) is None
    assert job.window_settled is True
    assert job.uncertain is False


async def test_window_sink_failure_preserves_known_execution() -> None:
    from redteam.capability.gated_manifest import SHELL_TARGET_JOB_START
    from redteam.capability.gated_strategy import RetainedSettlement
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    policy = _real_gated_policy()

    async def failing_close(key, *, window_id=None):
        return RetainedSettlement(settlement=None, evidence_error="gated_evidence_failed")

    policy.close_retained = failing_close
    adapter.set_gated_policy(policy, _real_gated_descriptors())
    args = {**_script_args(), "target_ref": "reg1", "port": 8080}
    invocation = _approved_invocation(SHELL_TARGET_JOB_START, "inv_sink_failure01", args, "reg1")
    started = await adapter.invoke_contextual(invocation)
    job_id = started.output["job_id"]
    job = adapter._jobs[job_id]
    if job.task is not None:
        await asyncio.wait_for(job.task, 2.0)
    assert job.status == "completed" and job.uncertain is False
    assert job.result is not None and job.result["stdout"] == "hi"
    from redteam.capability.shell_manifest import build_shell_descriptors
    status_tool = next(d for d in build_shell_descriptors() if d.tool_name == "shell_job_status")
    status = await adapter.invoke(status_tool, {"workroom_key": _key(), "job_id": job_id})
    assert status.output["status"] == "completed"
    assert status.metadata["gated_settlement"] == "gated_evidence_failed"
    assert "execution_uncertain" not in status.metadata

@pytest.mark.parametrize("foreign_holder", [False, True])
async def test_fresh_adapter_restores_and_settles_real_network_ledger(
    tmp_path, foreign_holder
) -> None:
    from redteam.capability.gated_manifest import SHELL_TARGET_JOB_START
    from redteam.capability.gated_strategy import RetainedWindow
    from redteam.capability.gated_window import AllowedFlow
    from redteam.control.network_ledger import SQLiteNetworkGrantLedger
    from redteam.core.contracts.network import (
        GrantRefused,
        NetworkGrant,
        NetworkPurpose,
        WindowState,
    )
    fake = _FakeWorkroom()
    adapter = _adapter(fake)
    policy = _real_gated_policy()
    ext = policy._extension
    ledger = SQLiteNetworkGrantLedger(str(tmp_path / "network.db"),
                                     engagement_id=str(ENGAGEMENT_ID), budget=ext.policy.budget)
    await ledger.setup()
    ext.ledger = ledger
    adapter.set_gated_policy(policy, _real_gated_descriptors())
    args = {**_script_args(), "target_ref": "reg1", "port": 8080}
    invocation = _approved_invocation(SHELL_TARGET_JOB_START, "inv_real_restore01", args, "reg1")
    reference = adapter.prepare_reference(invocation)
    assert reference is not None
    grant = await ledger.request_window(run_id=str(RUN_ID), instance_id=str(INSTANCE_ID),
                                        epoch=1, caps=ext.policy.default_target_caps)
    assert isinstance(grant, NetworkGrant)
    record = RetainedWindow(
        key=_key(), window_id=grant.window_id, engagement_id=ENGAGEMENT_ID,
        run_id=RUN_ID, instance_id=INSTANCE_ID, epoch=1, purpose=NetworkPurpose.TARGET,
        allowed=(AllowedFlow(ip="10.0.0.11", port=8080, proto="tcp"),),
        scope_host="reg1", scope_port=8080, holder_action_id=invocation.action_id,
        holder_invocation_id=invocation.invocation_id,
    )
    job_id = reference.provider_handle["job_id"]
    fake.manifests[job_id] = {
        "job_id": job_id, "workroom_key": _key(), "supervisor_id": str(invocation.invocation_id),
        "invocation_id": str(invocation.invocation_id), "input_digest": reference.input_digest,
        "script_sha256": "0" * 64, "language": "bash", "timeout_seconds": 30.0,
        "created_epoch": 100.0, "retained_window": record.to_record(),
        "holder": {"run_id": str(RUN_ID), "instance_id": str(INSTANCE_ID),
                   "action_id": str(invocation.action_id)},
    }
    if foreign_holder:
        fake.manifests[job_id]["retained_window"]["holder_invocation_id"] = "foreign-inv"
    fake.marks[str(invocation.invocation_id)] = _cancel_mark_for(
        invocation, str(invocation.invocation_id)
    )
    from redteam.capability.shell_manifest import build_shell_descriptors
    status_tool = next(d for d in build_shell_descriptors() if d.tool_name == "shell_job_status")
    status = await adapter.invoke(status_tool, {"workroom_key": _key(), "job_id": job_id})
    assert status.output["status"] == "cancelled"
    assert "execution_uncertain" not in status.metadata
    next_grant = await ledger.request_window(
        run_id=str(RUN_ID), instance_id=str(INSTANCE_ID), epoch=2,
        caps=ext.policy.default_target_caps,
    )
    assert isinstance(next_grant, GrantRefused)
    if foreign_holder:
        assert next_grant.code == "window_busy"
        assert status.metadata["gated_settlement"] == "gated_window_holder_unverified"
        assert adapter._jobs[job_id].window_settled is False
        return
    assert next_grant.code == "quarantined"
    settlement = await ledger.settle_window(
        window_id=grant.window_id, meter=None, verified_closed=False
    )
    assert settlement.state is WindowState.QUARANTINED
    assert settlement.charged_bytes == grant.reserved_bytes
    assert settlement.released_bytes == 0
    assert adapter._jobs[job_id].window_settled is True
