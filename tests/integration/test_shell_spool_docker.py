"""TASK-007 spool Docker proof (slim + Kali). Provider scope only.

Local Docker only; no real targets. Every test removes its own workroom
in a finally block. Nothing here touches the bank container; the final
sweep test asserts it is still present and running.

Coverage: spool-first seal + fetch round trip, host-pipe-closed relay
draining, cap-prefix truncation, timeout/cancel seals, retention of clean
records, gone-room hold, tampered-digest uncertainty, foreign-container
rejection, manifest restore after a memory wipe, and the Kali image.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import shutil
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_manifest import build_shell_descriptors
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import (
    _SUPERVISOR_RUNDIR,
    _SUPERVISOR_SOURCE,
    ShellWorkroomManager,
    container_name_for,
    exec_argv,
    workroom_key_for,
)
from redteam.core.contracts.execution import ExecutionState
from redteam.core.contracts.tool import (
    EXECUTION_STARTED_METADATA_KEY,
    ExecutionContext,
    ToolInvocation,
)
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

SLIM_IMAGE = os.environ.get("SHELL_TEST_IMAGE", "python:3.11-slim")
KALI_IMAGE = os.environ.get("SHELL_KALI_IMAGE")
BANK_CONTAINER = "redteam-local-bank-mock-bank-1"
PREFIX = "t007spool."

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker unavailable")
needs_kali = pytest.mark.skipif(not KALI_IMAGE, reason="SHELL_KALI_IMAGE unset")

RUN_TOOL_ID = ToolId("analysis.shell_run")
START_TOOL_ID = ToolId("analysis.shell_job_start")

_CREATED_KEYS: list[str] = []


def _policy(image: str, **overrides: Any) -> ShellWorkroomPolicy:
    args: dict[str, Any] = {
        "image": image,
        "allow_unpinned_image": True,
        "default_timeout_seconds": 30.0,
        "max_timeout_seconds": 120.0,
    }
    args.update(overrides)
    return ShellWorkroomPolicy(**args)


def _fresh_key() -> str:
    key = PREFIX + uuid.uuid4().hex[:12]
    _CREATED_KEYS.append(key)
    return key


async def _image_available(image: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "docker", "images", "-q", image,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return proc.returncode == 0 and bool(out.strip())


async def _require_manager(image: str, **overrides: Any) -> ShellWorkroomManager:
    manager = ShellWorkroomManager(_policy(image, **overrides))
    if not await manager.probe():
        pytest.skip("docker daemon unavailable")
    if not await _image_available(image):
        pytest.skip("test image unavailable: " + image)
    return manager


def _invocation(key: str, tool_id: ToolId, invocation_id: str,
                arguments: dict[str, Any], approved: Any = None) -> ToolInvocation:
    head, _, tail = key.partition(".")
    run_id = RunId(head)
    instance_id = AgentInstanceId(tail)
    assert workroom_key_for(str(run_id), str(instance_id)) == key
    return ToolInvocation(
        invocation_id=InvocationId(invocation_id),
        action_id=ActionId("act_t007_spool"),
        run_id=run_id,
        tool_id=tool_id,
        arguments=dict(arguments),
        timeout_seconds=30.0,
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_t007_spool"),
            run_id=run_id,
            agent_id=AgentId("agent_t007"),
            instance_id=instance_id,
            approved_target=approved,
        ),
        created_at=datetime.now(timezone.utc),
    )


def _script_args(key: str, script: str, timeout: float | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"workroom_key": key, "language": "bash", "script": script}
    if timeout is not None:
        args["timeout_seconds"] = timeout
    return args


def _selector() -> str:
    return "inv_t007_" + uuid.uuid4().hex[:12]


async def _wait_record(manager: ShellWorkroomManager, key: str, selector: str,
                       timeout: float = 60.0) -> dict[str, Any] | None:
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() - start < timeout:
        record = await manager.supervisor_record(key, selector)
        if record is not None:
            return record
        await asyncio.sleep(0.5)
    return None


async def _docker(*args: str, input: bytes | None = None) -> tuple[int, bytes]:
    stdin = asyncio.subprocess.PIPE if input is not None else None
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate(input)
    assert proc.returncode is not None
    return proc.returncode, out


async def _launch_detached(manager: ShellWorkroomManager, key: str, script: str,
                           selector: str, digest: str, timeout: float) -> None:
    """Launch the shipped supervisor detached: the host never observes it.

    This is the crash-window shape: the supervisor seals (or times out)
    with no host consumer, so the terminal record persists for a later
    resume query. Argv comes from the product exec_argv builder; only
    the wait is skipped (-d) and stdin is redirected from a staged file.
    """
    name = container_name_for(key)
    await manager.ensure_workroom(key)
    staged = "/workroom/.t007_detached.sh"
    body = "cat > " + staged + " <<'T007EOF'\n" + script + "\nT007EOF\n"
    assert "T007EOF\n" not in script
    wrote = await manager.exec_script(key, "bash", body, 15.0)
    assert wrote.exit_code == 0, wrote.stderr
    policy = manager.policy
    uid_text, _, gid_text = policy.container_user.partition(":")
    argv = exec_argv(
        name, "bash", timeout, policy.exec_grace_seconds,
        selector,
        run_id=selector,
        workdir=policy.workdir,
        run_uid=int(uid_text),
        run_gid=int(gid_text),
        invocation_id=selector,
        input_digest=digest,
        spool_cap_bytes=policy.max_output_bytes,
    )
    assert argv[0] == "exec"
    env_parts: list[str] = []
    rest: list[str] = []
    seen_dash_i = False
    for part in argv[1:]:
        if part == "-i" and not seen_dash_i:
            seen_dash_i = True
            continue
        (env_parts if not seen_dash_i else rest).append(part)
    supervisor = policy.workdir.rstrip("/") + "/.whs-supervisor-v4.py"
    assert rest[0] == name
    assert rest[1:4] == ["python3", "-I", "-u"]
    assert rest[4] == supervisor
    wrapper = "exec python3 -I -u " + supervisor + " <" + staged
    cmd = ("docker", "exec", "-d") + tuple(env_parts) + (name, "/bin/bash", "-c", wrapper)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    assert await proc.wait() == 0


async def _read_pgid(name: str, selector: str) -> str | None:
    rc, out = await _docker(
        "exec", "--user", "0", name, "/bin/cat",
        _SUPERVISOR_RUNDIR + "/.whs-pgid-" + selector,
    )
    if rc != 0:
        return None
    text = out.decode().strip()
    return text if text.isdigit() else None


class _ClosedPipe:
    """Host pipe that is already closed: every write raises."""

    def write(self, _data: bytes) -> None:
        raise BrokenPipeError("host pipe closed")

    def flush(self) -> None:
        raise BrokenPipeError("host pipe closed")


def _shipped_spool_relay():
    """The exact relay shipped inside containers, loaded without running main."""
    namespace: dict[str, Any] = {"__name__": "spool_probe"}
    exec(_SUPERVISOR_SOURCE, namespace)
    relay = namespace.get("_spool_relay")
    assert callable(relay)
    return relay


async def test_spool_relay_drains_when_host_pipe_closed(tmp_path):
    """Spool-first: a closed host pipe never stops the spool drain."""
    raw = bytes(i % 251 for i in range(10000))
    cap = 4096
    spool_path = str(tmp_path / "out.spool")
    relay_fn = _shipped_spool_relay()
    relay = relay_fn(io.BytesIO(raw), _ClosedPipe(), spool_path, cap)
    truncated, digest, size, complete = relay
    assert complete is True
    assert truncated is True
    assert size == cap
    with open(spool_path, "rb") as handle:
        stored = handle.read()
    assert stored == raw[:cap]
    assert digest == hashlib.sha256(raw[:cap]).hexdigest()


async def test_detached_completion_recovers_original():
    """Crash window: an unobserved supervisor seal is recoverable as-is."""
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    script = "echo spool-proof-marker"
    args = _script_args(key, script)
    selector = _selector()
    try:
        invocation = _invocation(key, RUN_TOOL_ID, selector, args)
        digest = execution_input_digest(invocation)
        await _launch_detached(manager, key, script, selector, digest, 30.0)
        record = await _wait_record(manager, key, selector, timeout=90.0)
        assert record is not None
        assert record.get("outcome") == "completed"
        assert record.get("spool") == "complete"
        assert isinstance(record.get("spool_digest"), str)
        assert ShellWorkroomManager.record_trusted(record) is True
        meta = await manager.spool_record(key, selector)
        assert meta is not None
        assert meta.get("sealed") is True
        assert meta.get("invocation_id") == selector
        assert meta.get("input_digest") == digest
        raw_out = await manager.read_spool_stream(key, selector, stream="out")
        assert raw_out == b"spool-proof-marker\n"
        reference = adapter.prepare_reference(invocation)
        assert reference is not None
        answer = await adapter.query_execution(reference)
        assert answer.state == ExecutionState.SUCCEEDED
        assert answer.verified is True
        assert answer.uncertain is False
        assert answer.result_available is True
        fetched = await adapter.fetch_execution(reference)
        assert fetched is not None
        assert fetched.output["stdout"] == "spool-proof-marker\n"
        assert fetched.metadata.get(EXECUTION_STARTED_METADATA_KEY) is True
    finally:
        await manager.remove_workroom(key)
        await adapter.aclose()


async def test_sync_run_retains_and_fetches_original():
    """Live path: the terminal seal is retained, so later query fetches it."""
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    script = "echo consumed-marker"
    args = _script_args(key, script)
    selector = _selector()
    try:
        invocation = _invocation(key, RUN_TOOL_ID, selector, args)
        digest = execution_input_digest(invocation)
        result = await manager.exec_script(
            key, "bash", script, 30.0,
            exec_tag=selector, invocation_id=selector, input_digest=digest,
        )
        assert result.exit_code == 0
        assert result.stdout == "consumed-marker\n"
        record = await manager.supervisor_record(key, selector)
        assert record is not None
        assert record.get("spool") == "complete"
        assert ShellWorkroomManager.record_trusted(record) is True
        meta = await manager.spool_record(key, selector)
        assert meta is not None
        assert meta.get("sealed") is True
        assert meta.get("invocation_id") == selector
        assert meta.get("input_digest") == digest
        raw_out = await manager.read_spool_stream(key, selector, stream="out")
        assert raw_out == b"consumed-marker\n"
        await asyncio.sleep(2.0)
        assert await manager.spool_record(key, selector) is not None
        fresh = BuiltinShellAdapter(manager.policy, workroom=manager)
        reference = adapter.prepare_reference(invocation)
        assert reference is not None
        answer = await fresh.query_execution(reference)
        assert answer.state == ExecutionState.SUCCEEDED
        assert answer.verified is True
        assert answer.result_available is True
        fetched = await fresh.fetch_execution(reference)
        assert fetched is not None
        assert fetched.output["stdout"] == "consumed-marker\n"
        assert fetched.metadata.get(EXECUTION_STARTED_METADATA_KEY) is True
        await fresh.aclose()
    finally:
        await manager.remove_workroom(key)
        await adapter.aclose()


async def test_cap_truncation_seals_with_prefix():
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE, max_output_bytes=1024)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    script = "for i in $(seq 1 3000); do echo line-$i-pad-to-grow-output; done"
    args = _script_args(key, script)
    selector = _selector()
    try:
        invocation = _invocation(key, RUN_TOOL_ID, selector, args)
        digest = execution_input_digest(invocation)
        result = await manager.exec_script(
            key, "bash", script, 60.0,
            exec_tag=selector, invocation_id=selector, input_digest=digest,
        )
        assert result.truncated is True
        meta = await manager.spool_record(key, selector)
        assert meta is not None
        out_entry = meta.get("out")
        assert isinstance(out_entry, dict)
        assert out_entry.get("truncated") is True
        assert out_entry.get("size") == 1024
        raw_out = await manager.read_spool_stream(key, selector, stream="out")
        assert raw_out is not None
        assert len(raw_out) == 1024
        assert hashlib.sha256(raw_out).hexdigest() == out_entry.get("sha256")
        record = await manager.supervisor_record(key, selector)
        assert record is not None
        assert record.get("spool") == "complete"
        reference = adapter.prepare_reference(invocation)
        assert reference is not None
        answer = await adapter.query_execution(reference)
        assert answer.state == ExecutionState.SUCCEEDED
        assert answer.verified is True
        fetched = await adapter.fetch_execution(reference)
        assert fetched is not None
        assert fetched.output["truncated"] is True
        assert len(fetched.output["stdout"].encode()) == 1024
    finally:
        await manager.remove_workroom(key)
        await adapter.aclose()


async def test_detached_timeout_recovers_known_terminal():
    """A trusted timeout seal is a known terminal, never a hold."""
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    script = "echo begin; sleep 60; echo never"
    args = _script_args(key, script)
    selector = _selector()
    try:
        invocation = _invocation(key, RUN_TOOL_ID, selector, args)
        digest = execution_input_digest(invocation)
        await _launch_detached(manager, key, script, selector, digest, 4.0)
        record = await _wait_record(manager, key, selector, timeout=90.0)
        assert record is not None
        assert record.get("outcome") == "timeout"
        assert record.get("code") == 124
        assert record.get("spool") == "complete"
        assert ShellWorkroomManager.record_trusted(record) is True
        reference = adapter.prepare_reference(invocation)
        assert reference is not None
        answer = await adapter.query_execution(reference)
        assert answer.state == ExecutionState.SUCCEEDED
        assert answer.verified is True
        assert answer.uncertain is False
        assert answer.result_available is True
        fetched = await adapter.fetch_execution(reference)
        assert fetched is not None
        assert fetched.output["timed_out"] is True
        assert "begin\n" in fetched.output["stdout"]
    finally:
        await manager.remove_workroom(key)
        await adapter.aclose()


async def test_cancel_seals_spool_without_replay():
    """Host cancel tears down via the supervisor; the spool stays sealed."""
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    marker = "/workroom/t007_cancel_" + key.split(".")[1] + ".txt"
    script = "echo started > " + marker + "; sleep 120"
    args = _script_args(key, script, timeout=90.0)
    selector = _selector()
    invocation = _invocation(key, RUN_TOOL_ID, selector, args)
    try:
        task = asyncio.create_task(adapter.invoke_contextual(invocation))
        seen = False
        for _ in range(60):
            probe = await manager.exec_script(key, "bash", "cat " + marker, 8.0)
            if probe.exit_code == 0 and probe.stdout.strip() == "started":
                seen = True
                break
            await asyncio.sleep(1.0)
        assert seen is True
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        meta = await manager.spool_record(key, selector)
        assert meta is not None
        assert meta.get("sealed") is True
        assert meta.get("invocation_id") == selector
        assert meta.get("input_digest") == execution_input_digest(invocation)
        reference = adapter.prepare_reference(invocation)
        assert reference is not None
        mark = await manager.read_cancel_mark(key, selector)
        assert isinstance(mark, dict)
        assert mark.get("outcome") == "cancelled"
        assert mark.get("verified_teardown") is True
        assert mark.get("invocation_id") == selector
        assert mark.get("input_digest") == execution_input_digest(invocation)
        meta = await manager.spool_record(key, selector)
        assert meta is not None
        assert meta.get("sealed") is True
        fresh = BuiltinShellAdapter(manager.policy, workroom=manager)
        reference = adapter.prepare_reference(invocation)
        assert reference is not None
        answer = await fresh.query_execution(reference)
        assert answer.state == ExecutionState.CANCELLED
        assert answer.verified is True
        assert answer.uncertain is False
        assert answer.result_available is True
        fetched = await fresh.fetch_execution(reference)
        assert fetched is not None
        assert fetched.success is False
        assert fetched.error is not None
        assert fetched.error.code == "shell_cancelled"
        assert isinstance(fetched.output, dict)
        await fresh.aclose()
        follow = await manager.exec_script(key, "bash", "echo sibling-ok", 15.0)
        assert follow.stdout == "sibling-ok\n"
    finally:
        await manager.remove_workroom(key)
        await adapter.aclose()


async def test_detached_cancel_recovers_cancelled_original():
    """External TERM of the execution group seals a cancelled original."""
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    marker = "/workroom/t007_dcancel_" + key.split(".")[1] + ".txt"
    script = "echo started > " + marker + "; sleep 120"
    args = _script_args(key, script)
    selector = _selector()
    name = container_name_for(key)
    try:
        invocation = _invocation(key, RUN_TOOL_ID, selector, args)
        digest = execution_input_digest(invocation)
        await _launch_detached(manager, key, script, selector, digest, 120.0)
        seen = False
        for _ in range(60):
            probe = await manager.exec_script(key, "bash", "cat " + marker, 8.0)
            if probe.exit_code == 0 and probe.stdout.strip() == "started":
                seen = True
                break
            await asyncio.sleep(1.0)
        assert seen is True
        outcome, alive = await manager.supervisor_outcome(key, selector)
        assert alive is True
        term_script = (
            'sup=""; '
            'for d in /proc/[0-9]*/environ; do pid=${d#/proc/}; pid=${pid%/environ}; '
            'case $pid in \'\'|*[!0-9]*) continue;; esac; '
            'if [ "$pid" = "$$" ]; then continue; fi; '
            'if /bin/grep -qzF "WHS_SHELL_JOB=$WHS_SHELL_KILL" "$d" 2>/dev/null; then '
            'if /bin/grep -qF ".whs-supervisor-v" "/proc/$pid/cmdline" 2>/dev/null; then '
            'sup="$sup $pid"; fi; fi; done; '
            '[ -n "$sup" ] || exit 3; '
            'for p in $sup; do kill -TERM "$p" 2>/dev/null; done; exit 0'
        )
        rc, _ = await _docker(
            "exec", "--user", "0",
            "-e", "WHS_SHELL_KILL=" + selector,
            name, "/bin/bash", "-c", term_script,
        )
        assert rc == 0
        record = await _wait_record(manager, key, selector, timeout=90.0)
        assert record is not None
        assert record.get("outcome") == "cancelled"
        assert record.get("code") == 142
        assert record.get("spool") == "complete"
        assert ShellWorkroomManager.record_trusted(record) is True
        reference = adapter.prepare_reference(invocation)
        assert reference is not None
        answer = await adapter.query_execution(reference)
        assert answer.state == ExecutionState.CANCELLED
        assert answer.verified is True
        assert answer.uncertain is False
        assert answer.result_available is True
        fetched = await adapter.fetch_execution(reference)
        assert fetched is not None
        assert fetched.success is False
        assert fetched.error is not None
        assert fetched.error.code == "shell_cancelled"
        assert isinstance(fetched.output, dict)
        assert "stdout" in fetched.output
    finally:
        await manager.remove_workroom(key)
        await adapter.aclose()


async def test_spool_retained_after_clean_completion():
    """Clean records are retained (both streams), never deleted on success."""
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    script = "echo retain-out; echo retain-err >&2"
    args = _script_args(key, script)
    selector = _selector()
    try:
        invocation = _invocation(key, RUN_TOOL_ID, selector, args)
        digest = execution_input_digest(invocation)
        result = await manager.exec_script(
            key, "bash", script, 30.0,
            exec_tag=selector, invocation_id=selector, input_digest=digest,
        )
        assert result.exit_code == 0
        await asyncio.sleep(2.0)
        meta = await manager.spool_record(key, selector)
        assert meta is not None
        assert meta.get("sealed") is True
        raw_out = await manager.read_spool_stream(key, selector, stream="out")
        assert raw_out == b"retain-out\n"
        raw_err = await manager.read_spool_stream(key, selector, stream="err")
        assert raw_err == b"retain-err\n"
        assert await manager.spool_record(key, selector) is not None
        reference = adapter.prepare_reference(invocation)
        assert reference is not None
        answer = await adapter.query_execution(reference)
        assert answer.state == ExecutionState.SUCCEEDED
        assert answer.verified is True
        fetched = await adapter.fetch_execution(reference)
        assert fetched is not None
        assert fetched.output["stdout"] == "retain-out\n"
        assert fetched.output["stderr"] == "retain-err\n"
    finally:
        await manager.remove_workroom(key)
        await adapter.aclose()


async def test_gone_workroom_holds_uncertain():
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    script = "echo gone-room-marker"
    args = _script_args(key, script)
    selector = _selector()
    invocation = _invocation(key, RUN_TOOL_ID, selector, args)
    digest = execution_input_digest(invocation)
    result = await manager.exec_script(
        key, "bash", script, 30.0,
        exec_tag=selector, invocation_id=selector, input_digest=digest,
    )
    assert result.exit_code == 0
    reference = adapter.prepare_reference(invocation)
    assert reference is not None
    assert await manager.remove_workroom(key) is True
    answer = await adapter.query_execution(reference)
    assert answer.state == ExecutionState.UNKNOWN
    assert answer.uncertain is True
    assert answer.result_available is False
    assert await adapter.fetch_execution(reference) is None
    missing = _invocation(key, RUN_TOOL_ID, _selector(), args)
    missing_ref = adapter.prepare_reference(missing)
    assert missing_ref is not None
    missing_answer = await adapter.query_execution(missing_ref)
    assert missing_answer.state == ExecutionState.UNKNOWN
    assert missing_answer.result_available is False
    await adapter.aclose()


async def test_tampered_digest_is_uncertain():
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    script = "echo original-bytes"
    args = _script_args(key, script)
    selector = _selector()
    try:
        invocation = _invocation(key, RUN_TOOL_ID, selector, args)
        digest = execution_input_digest(invocation)
        result = await manager.exec_script(
            key, "bash", script, 30.0,
            exec_tag=selector, invocation_id=selector, input_digest=digest,
        )
        assert result.exit_code == 0
        forged_args = _script_args(key, "echo forged-bytes")
        forged = _invocation(key, RUN_TOOL_ID, selector, forged_args)
        forged_ref = adapter.prepare_reference(forged)
        assert forged_ref is not None
        answer = await adapter.query_execution(forged_ref)
        assert answer.uncertain is True
        assert answer.result_available is False
        assert await adapter.fetch_execution(forged_ref) is None
    finally:
        await manager.remove_workroom(key)
        await adapter.aclose()


async def test_foreign_container_cannot_supply_results():
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    name = container_name_for(key)
    created = await asyncio.create_subprocess_exec(
        "docker", "create", "--name", name, "--network", "none",
        SLIM_IMAGE, "sleep", "infinity",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    assert await created.wait() == 0
    try:
        assert await manager.verify_workroom_ownership(key) is False
        args = _script_args(key, "echo never-runs")
        invocation = _invocation(key, RUN_TOOL_ID, _selector(), args)
        reference = adapter.prepare_reference(invocation)
        assert reference is not None
        answer = await adapter.query_execution(reference)
        assert answer.state == ExecutionState.UNKNOWN
        assert answer.result_available is False
        assert await adapter.fetch_execution(reference) is None
        rc, state = await _docker("inspect", "-f", "{{.State.Running}}", name)
        assert rc == 0
        assert state.strip() == b"false"
    finally:
        gone = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await gone.wait()
        await adapter.aclose()


async def test_manifest_restore_after_memory_wipe():
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    first = BuiltinShellAdapter(manager.policy, workroom=manager)
    script = "sleep 15; echo restored-marker"
    args = _script_args(key, script, timeout=60.0)
    selector = _selector()
    invocation = _invocation(key, START_TOOL_ID, selector, args)
    try:
        started = await first.invoke_contextual(invocation)
        assert started.success is True
        assert isinstance(started.output, dict)
        assert started.output.get("status") == "running"
        job_id = started.output.get("job_id")
        assert isinstance(job_id, str)
        manifest = await manager.read_job_manifest(key, job_id)
        assert isinstance(manifest, dict)
        assert manifest.get("job_id") == job_id
        assert manifest.get("supervisor_id") == selector
        assert manifest.get("invocation_id") == selector
        assert manifest.get("input_digest") == execution_input_digest(invocation)
        holder = manifest.get("holder")
        assert isinstance(holder, dict)
        assert holder.get("instance_id") == key.split(".")[1]
        route = manifest.get("route")
        assert isinstance(route, dict)
        assert route.get("workroom_key") == key
        wiped = BuiltinShellAdapter(manager.policy, workroom=manager)
        by_name = {d.tool_name: d for d in build_shell_descriptors(wiped.policy)}
        settled = False
        for _ in range(90):
            _, alive = await manager.supervisor_outcome(key, selector)
            if not alive:
                settled = True
                break
            await asyncio.sleep(1.0)
        assert settled is True
        wiped = BuiltinShellAdapter(manager.policy, workroom=manager)
        by_name = {d.tool_name: d for d in build_shell_descriptors(wiped.policy)}
        done = None
        for _ in range(45):
            poll = await wiped.invoke(
                by_name["shell_job_status"], {"workroom_key": key, "job_id": job_id},
            )
            assert poll.success is True
            if poll.output["status"] == "completed":
                done = poll
                break
            await asyncio.sleep(1.0)
        assert done is not None
        assert done.output["result"] is not None
        assert "restored-marker" in done.output["result"].get("stdout", "")
        await wiped.aclose()
    finally:
        await manager.remove_workroom(key)
        await first.aclose()


@needs_kali
async def test_kali_spool_seal_and_fetch():
    assert KALI_IMAGE is not None
    key = _fresh_key()
    manager = await _require_manager(KALI_IMAGE)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    script = "echo kali-spool-marker"
    args = _script_args(key, script)
    selector = _selector()
    try:
        invocation = _invocation(key, RUN_TOOL_ID, selector, args)
        digest = execution_input_digest(invocation)
        await _launch_detached(manager, key, script, selector, digest, 60.0)
        record = await _wait_record(manager, key, selector, timeout=120.0)
        assert record is not None
        assert record.get("outcome") == "completed"
        assert record.get("spool") == "complete"
        reference = adapter.prepare_reference(invocation)
        assert reference is not None
        answer = await adapter.query_execution(reference)
        assert answer.state == ExecutionState.SUCCEEDED
        assert answer.verified is True
        fetched = await adapter.fetch_execution(reference)
        assert fetched is not None
        assert fetched.output["stdout"] == "kali-spool-marker\n"
        assert fetched.metadata.get(EXECUTION_STARTED_METADATA_KEY) is True
    finally:
        await manager.remove_workroom(key)
        await adapter.aclose()


@needs_kali
async def test_kali_timeout_seals():
    assert KALI_IMAGE is not None
    key = _fresh_key()
    manager = await _require_manager(KALI_IMAGE)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    script = "echo kali-begin; sleep 60"
    args = _script_args(key, script)
    selector = _selector()
    try:
        invocation = _invocation(key, RUN_TOOL_ID, selector, args)
        digest = execution_input_digest(invocation)
        result = await manager.exec_script(
            key, "bash", script, 4.0,
            exec_tag=selector, invocation_id=selector, input_digest=digest,
        )
        assert result.timed_out is True
        meta = await manager.spool_record(key, selector)
        assert meta is not None
        assert meta.get("sealed") is True
        assert meta.get("invocation_id") == selector
        assert meta.get("input_digest") == digest
        reference = adapter.prepare_reference(invocation)
        assert reference is not None
        answer = await adapter.query_execution(reference)
        assert answer.state == ExecutionState.SUCCEEDED
        assert answer.verified is True
        fetched = await adapter.fetch_execution(reference)
        assert fetched is not None
        assert fetched.output["timed_out"] is True
    finally:
        await manager.remove_workroom(key)
        await adapter.aclose()


def _install_gated_policy():
    from redteam.capability.gated_network_policy import (
        GatedNetworkPolicy,
        InstallProxyConfig,
        PinnedRegistry,
    )
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
    icaps = WindowCaps(max_flow_init_deliveries=10, max_output_bytes=100000,
                        duration_seconds=60.0, purpose=NetworkPurpose.INSTALL)
    tcaps = WindowCaps(max_flow_init_deliveries=10, max_output_bytes=100000,
                        duration_seconds=60.0, purpose=NetworkPurpose.TARGET)
    registry = PinnedRegistry(targets={}, mirrors={"m1": ("10.0.0.12",)})
    proxy = InstallProxyConfig(endpoint="http://10.0.0.13:8080",
                               pinned_hosts=("10.0.0.13",), mirror_upstream="http://m1:8080")
    net_policy = GatedNetworkPolicy(budget=budget, default_target_caps=tcaps,
                                    default_install_caps=icaps, registry=registry,
                                    install_proxy=proxy)
    return net_policy


class _DockerWindowExtension:
    def __init__(self, policy):
        self.policy = policy
        self.opened = []
        self.closed = []
    async def open_window(self, *, run_id, instance_id, epoch, caps, allowed,
                          holder_action_id, holder_invocation_id, holder_work_id,
                          internal=False):
        self.opened.append(run_id + "." + instance_id)
        from types import SimpleNamespace
        return SimpleNamespace(window_id="gwin-docker-1")
    async def close_window(self, key):
        self.closed.append(key)
        from redteam.core.contracts.network import WindowSettlement, WindowState
        return WindowSettlement(window_id="gwin-docker-1", state=WindowState.RELEASED,
                                charged_deliveries=0, charged_bytes=0,
                                released_deliveries=0, released_bytes=0,
                                committed_deliveries=0)
    def companion_for(self, window_id):
        return None
    def is_transmit_open(self, key):
        return False


async def test_install_env_seeded_and_model_vars_scrubbed():
    from redteam.capability.gated_manifest import SHELL_INSTALL_RUN, build_gated_descriptors
    from redteam.capability.gated_strategy import GatedExecutionPolicy
    from redteam.core.contracts.target import DomainTarget
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    net_policy = _install_gated_policy()
    extension = _DockerWindowExtension(net_policy)
    def _allow_all(target, **kwargs):
        return (True, [], None)
    policy = GatedExecutionPolicy(
        extension=extension, scope_validator=_allow_all, evidence_store=None
    )
    adapter.set_gated_policy(policy, build_gated_descriptors(manager.policy))
    script = (
        "env | grep -i -E 'proxy|PIP_|npm_' | sort; echo ---override---; "
        "export HTTPS_PROXY=http://evil:9; export PIP_INDEX_URL=http://evil/x; "
        "env | grep -i -E 'proxy|PIP_' | sort"
    )
    args = _script_args(key, script)
    args["target_ref"] = "m1"
    selector = _selector()
    try:
        invocation = _invocation(key, ToolId("analysis." + SHELL_INSTALL_RUN), selector, args,
                                 approved=DomainTarget(domain="m1"))
        result = await adapter.invoke_contextual(invocation)
        assert result.success is True, result.error
        out = result.output["stdout"]
        assert "HTTPS_PROXY=http://10.0.0.13:8080" in out
        seeded, overridden = out.split("---override---", 1)
        assert "evil" not in seeded
        assert "PIP_INDEX_URL" not in seeded
        # The script can change its own environment; destination authority
        # remains at the gated network boundary, not in these variables.
        assert "HTTPS_PROXY=http://evil:9" in overridden
        assert result.metadata.get("gated_window_id") == "gwin-docker-1"
    finally:
        await manager.remove_workroom(key)
        await adapter.aclose()


async def test_cancel_mark_survives_host_crash():
    """Fresh adapters settle a verified cancel from the durable mark."""
    from redteam.capability.shell_manifest import build_shell_descriptors
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    starter = BuiltinShellAdapter(manager.policy, workroom=manager)
    marker = "/workroom/t007_crashcancel_" + key.split(".")[1] + ".txt"
    script = "echo started > " + marker + "; sleep 120"
    args = _script_args(key, script, timeout=120.0)
    selector = _selector()
    invocation = _invocation(key, START_TOOL_ID, selector, args)
    try:
        started = await starter.invoke_contextual(invocation)
        assert started.success is True
        job_id = started.output["job_id"]
        seen = False
        for _ in range(60):
            probe = await manager.exec_script(key, "bash", "cat " + marker, 8.0)
            if probe.exit_code == 0 and probe.stdout.strip() == "started":
                seen = True
                break
            await asyncio.sleep(1.0)
        assert seen is True
        canceller = BuiltinShellAdapter(manager.policy, workroom=manager)
        by_name = {d.tool_name: d for d in build_shell_descriptors(canceller.policy)}
        cancelled = await canceller.invoke(
            by_name["shell_job_cancel"], {"workroom_key": key, "job_id": job_id}
        )
        assert cancelled.success is True
        assert cancelled.output["status"] == "cancelled"
        assert cancelled.metadata.get("cancel_sealed") is True
        mark = await manager.read_cancel_mark(key, selector)
        assert isinstance(mark, dict)
        assert mark.get("verified_teardown") is True
        assert mark.get("invocation_id") == selector
        reader = BuiltinShellAdapter(manager.policy, workroom=manager)
        reference = starter.prepare_reference(invocation)
        assert reference is not None
        answer = await reader.query_execution(reference)
        assert answer.state == ExecutionState.SUCCEEDED
        assert answer.verified is True
        fetched = await reader.fetch_execution(reference)
        assert fetched is not None
        assert fetched.success is True
        assert fetched.output["job_id"] == job_id
        assert fetched.output["status"] == "running"
        status = await reader.invoke(
            by_name["shell_job_status"], {"workroom_key": key, "job_id": job_id}
        )
        assert status.output["status"] == "cancelled"
        await reader.aclose()
        settled = BuiltinShellAdapter(manager.policy, workroom=manager)
        again = await settled.invoke(
            by_name["shell_job_status"], {"workroom_key": key, "job_id": job_id}
        )
        assert again.output["status"] == "cancelled"
        await settled.aclose()
        await canceller.aclose()
    finally:
        await manager.remove_workroom(key)
        await starter.aclose()


async def test_crash_post_spawn_pre_ack_recovers_submit():
    """A spawn that never ACKed is still recoverable from the manifest."""
    from redteam.capability.gated_manifest import SHELL_TARGET_JOB_START, build_gated_descriptors
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    crashed = BuiltinShellAdapter(manager.policy, workroom=manager)
    script = "sleep 30; echo never-acked"
    args = _script_args(key, script, timeout=90.0)
    selector = _selector()
    invocation = _invocation(key, ToolId("analysis." + SHELL_TARGET_JOB_START), selector, args)

    class _SimulatedCrash(BaseException):
        pass

    class _CrashPolicy:
        async def execute(self, inner_invocation, parsed, delegate):
            await delegate()
            raise _SimulatedCrash("simulated host crash post-spawn pre-ACK")

    descriptors = build_gated_descriptors(manager.policy)
    crashed.set_gated_policy(_CrashPolicy(), descriptors)
    try:
        with pytest.raises(_SimulatedCrash):
            await crashed.invoke_contextual(invocation)
        manifest = await manager.read_job_manifest(key, crashed._deterministic_job_id(
            selector, execution_input_digest(invocation)))
        assert isinstance(manifest, dict)
        assert manifest.get("supervisor_id") == selector
        assert manifest.get("tool") == SHELL_TARGET_JOB_START
        fresh = BuiltinShellAdapter(manager.policy, workroom=manager)
        reference = crashed.prepare_reference(invocation)
        assert reference is not None
        assert reference.provider_handle["kind"] == "job_start"
        answer = await fresh.query_execution(reference)
        assert answer.state == ExecutionState.SUCCEEDED
        assert answer.verified is True
        assert answer.result_available is True
        fetched = await fresh.fetch_execution(reference)
        assert fetched is not None
        assert fetched.success is True
        assert fetched.output["status"] == "running"
        assert fetched.output["job_id"] == reference.provider_handle["job_id"]
        await fresh.aclose()
    finally:
        await manager.remove_workroom(key)
        await crashed.aclose()


async def test_timeout_restore_returns_output():
    """A timed-out job restores with its bounded actual output."""
    from redteam.capability.shell_manifest import build_shell_descriptors
    key = _fresh_key()
    manager = await _require_manager(SLIM_IMAGE)
    starter = BuiltinShellAdapter(manager.policy, workroom=manager)
    script = "echo timeout-marker; sleep 60"
    args = _script_args(key, script, timeout=4.0)
    selector = _selector()
    invocation = _invocation(key, START_TOOL_ID, selector, args)
    try:
        started = await starter.invoke_contextual(invocation)
        assert started.success is True
        job_id = started.output["job_id"]
        settled = False
        for _ in range(90):
            _, alive = await manager.supervisor_outcome(key, selector)
            if not alive:
                settled = True
                break
            await asyncio.sleep(1.0)
        assert settled is True
        fresh = BuiltinShellAdapter(manager.policy, workroom=manager)
        by_name = {d.tool_name: d for d in build_shell_descriptors(fresh.policy)}
        done = None
        for _ in range(30):
            poll = await fresh.invoke(
                by_name["shell_job_status"], {"workroom_key": key, "job_id": job_id}
            )
            assert poll.success is True
            if poll.output["status"] == "completed":
                done = poll
                break
            await asyncio.sleep(1.0)
        assert done is not None
        assert done.output["result"] is not None
        assert "timeout-marker" in done.output["result"]["stdout"]
        assert done.output["result"]["timed_out"] is True
        assert done.output["error"] == "shell_execution_timeout"
        await fresh.aclose()
    finally:
        await manager.remove_workroom(key)
        await starter.aclose()


async def test_zzz_no_residue_bank_untouched():
    manager = await _require_manager(SLIM_IMAGE)
    for key in list(_CREATED_KEYS):
        try:
            await manager.remove_workroom(key)
        except Exception:
            pass
    try:
        await manager.remove_workrooms_by_prefix(PREFIX)
    except Exception:
        pass
    for key in list(_CREATED_KEYS):
        name = container_name_for(key)
        rc, _ = await _docker("inspect", name)
        assert rc != 0, "residue container remains: " + name
    rc, state = await _docker("inspect", "-f", "{{.State.Running}}", BANK_CONTAINER)
    if rc != 0:
        pytest.skip("bank container absent in this environment")
    assert state.strip() == b"true"
