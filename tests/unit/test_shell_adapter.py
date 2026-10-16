"""Contract tests for the builtin shell adapter. Owned by Capability Team.

No Docker, no network, no orchestration / control / contracts changes.
A fake workroom implements the manager async surface in memory; argv and
label invariants are checked against the pure builder functions.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest
from jsonschema import Draft202012Validator

from redteam.capability.adapters.mcp.base import InvocationContextBinder
from redteam.capability.manifest import validate_tool_manifest
from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_assembly import (
    build_shell_entries,
    shell_operation_bindings,
    shell_output_limit,
    shell_server_output_limits,
)
from redteam.capability.shell_manifest import (
    JOB_CANCEL_TOOL_NAME,
    JOB_START_TOOL_NAME,
    JOB_STATUS_TOOL_NAME,
    RUN_TOOL_NAME,
    SHELL_SERVER_ID,
    build_shell_descriptors,
    validate_shell_descriptors,
)
from redteam.capability.shell_policy import WORKROOM_KEY_PATTERN, ShellWorkroomPolicy
from redteam.capability.shell_workroom import (
    _SUPERVISOR_SOURCE,
    SHELL_CONTEXT_LABEL_KEY,
    SHELL_MANAGED_LABEL,
    CancelReport,
    ExecResult,
    ExecutionAdmission,
    ProgramRecord,
    ShellWorkroomError,
    check_workroom_key,
    container_name_for,
    create_argv,
    exec_argv,
    workroom_key_for,
)
from redteam.core.contracts.tool import (
    EXECUTION_UNCERTAIN_METADATA_KEY,
    ExecutionContext,
    ToolInvocation,
    execution_outcome_uncertain,
)
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ActionId, AgentInstanceId, EngagementId, InvocationId, RunId, ToolId
from redteam.core.ports.program_discovery import ProgramStaticMetadata
from redteam.core.tool_binding import SELECTED_TOOL


def _policy(**overrides):
    args = {"image": "python:3.11-slim", "allow_unpinned_image": True}
    args.update(overrides)
    return ShellWorkroomPolicy(**args)


class _FakeWorkroom:
    def __init__(
        self,
        slow_scripts=(),
        kill_verified=True,
        kill_outcome="cancelled",
        kill_delay=0.0,
        settle_outcome=None,
        settle_alive=False,
        uncertain_scripts=(),
        container_state="running",
    ):
        self.slow_scripts = set(slow_scripts)
        self.calls = []
        self.removed = []
        self.kill_verified = kill_verified
        self.kill_outcome = kill_outcome
        self.kill_delay = kill_delay
        self.settle_outcome = settle_outcome
        self.settle_alive = settle_alive
        self.uncertain_scripts = set(uncertain_scripts)
        self.container_state = container_state
        self.record_protected = True
        self.record_unreaped = []

    async def exec_script(
        self, key, language, script, timeout_seconds=None, exec_tag=None, **_kwargs
    ):
        self.calls.append((key, language, script, exec_tag))
        if script in self.uncertain_scripts:
            raise ShellWorkroomError("shell_execution_interrupted", uncertainty=True)
        if script in self.slow_scripts:
            await asyncio.sleep(30.0)
        return ExecResult(
            exit_code=0,
            stdout="fake:" + script[:16],
            stderr="",
            timed_out=False,
            truncated=False,
            duration_ms=1,
            workroom_key=key,
            container_name="fake-container",
        )

    async def remove_workroom(self, key):
        self.removed.append(key)
        return True

    async def kill_tagged_exec(self, key, tag):
        self.calls.append(("kill", key, tag))
        if self.kill_delay:
            await asyncio.sleep(self.kill_delay)
        return CancelReport(verified=self.kill_verified, outcome=self.kill_outcome)

    async def supervisor_outcome(self, key, selector):
        self.calls.append(("outcome", key, selector))
        return self.settle_outcome, self.settle_alive

    async def supervisor_record(self, key, selector):
        self.calls.append(("record", key, selector))
        if self.settle_outcome is None:
            return None
        return {
            "outcome": self.settle_outcome,
            "code": 0,
            "reaped": 0,
            "unreaped": list(self.record_unreaped),
            "subreaper": True,
            "protected": self.record_protected,
        }

    async def workroom_container_state(self, key):
        self.calls.append(("container-state", key))
        return self.container_state

    async def probe(self):
        return True


def _descriptor_map(descriptors):
    return {d.tool_name: d for d in descriptors}


def test_descriptors_match_pinned_manifest():
    descriptors = build_shell_descriptors()
    validate_shell_descriptors(descriptors)
    for descriptor in descriptors:
        validate_tool_manifest(descriptor)
        assert descriptor.server_id == SHELL_SERVER_ID
    assert {d.tool_name for d in descriptors} == {
        RUN_TOOL_NAME, JOB_START_TOOL_NAME, JOB_STATUS_TOOL_NAME, JOB_CANCEL_TOOL_NAME,
    }


def test_run_schema_accepts_exact_program_contract_and_rejects_mixed_script_input():
    run = _descriptor_map(build_shell_descriptors())[RUN_TOOL_NAME]
    validator = Draft202012Validator(run.input_schema)
    program = {
        "program": {
            "name": "curl",
            "executable": "/usr/bin/curl",
            "owner_scope": "image",
            "argv": ["--version"],
            "stdin": "",
        },
        "timeout_seconds": 5,
    }
    assert list(validator.iter_errors(program)) == []
    mixed = {**program, "language": "bash", "script": "true"}
    assert list(validator.iter_errors(mixed))
    job_start = _descriptor_map(build_shell_descriptors())[JOB_START_TOOL_NAME]
    assert "program" not in job_start.input_schema["properties"]


def test_risk_and_binding_posture():
    by_name = _descriptor_map(build_shell_descriptors())
    # TASK-008 (FR-007): starting shell work is bounded execution (MEDIUM), so
    # ambiguity alone never forces approval; approval fires only through
    # explicit operator rules or matching command-pattern rules.
    assert by_name[RUN_TOOL_NAME].risk_level == RiskLevel.MEDIUM
    assert by_name[JOB_START_TOOL_NAME].risk_level == RiskLevel.MEDIUM
    assert by_name[JOB_STATUS_TOOL_NAME].risk_level == RiskLevel.LOW
    assert by_name[JOB_CANCEL_TOOL_NAME].risk_level == RiskLevel.MEDIUM
    for descriptor in by_name.values():
        assert descriptor.requires_target is False
        assert descriptor.retry_safe is False
        assert descriptor.outbound_text_fields == []
        assert descriptor.target_types == []
    assert by_name[RUN_TOOL_NAME].command_fields == ["language", "script"]
    assert by_name[JOB_STATUS_TOOL_NAME].command_fields == []


def test_labels_disjoint_from_execution_mcp():
    from redteam.capability.servers import execution as execution_server

    assert SHELL_MANAGED_LABEL != execution_server._MANAGED_LABEL
    assert SHELL_CONTEXT_LABEL_KEY != execution_server._CONTEXT_LABEL_KEY
    assert "shell-workroom" in SHELL_MANAGED_LABEL
    assert "shell-workroom" in SHELL_CONTEXT_LABEL_KEY


def test_create_argv_forces_network_none_and_shell_labels():
    argv = create_argv(_policy(), "whs-shell-abc123", "run1.inst1")
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert "--cap-drop" in argv
    assert SHELL_MANAGED_LABEL in argv
    assert SHELL_CONTEXT_LABEL_KEY + "=run1.inst1" in argv
    assert "sleep" in argv and "infinity" in argv
    joined = " ".join(argv)
    assert "io.whs-redteam.execution." not in joined
    # S2/S4 surface: init reaping plus workdir-derived install env.
    assert "--init" in argv
    assert "HOME=/workroom" in argv
    assert "PYTHONUSERBASE=/workroom/.local" in argv
    custom = create_argv(_policy(workdir="/w2"), "whs-shell-abc123", "run1.inst1")
    assert "HOME=/w2" in custom
    assert "PYTHONUSERBASE=/w2/.local" in custom
    path_entry = argv[argv.index("PATH=/workroom/.local/bin:/usr/local/bin:/usr/bin:/bin")]
    assert path_entry.startswith("PATH=")
    path_custom = custom[custom.index("PATH=/w2/.local/bin:/usr/local/bin:/usr/bin:/bin")]
    assert path_custom.startswith("PATH=")


def test_exec_argv_carries_no_script_text():
    argv = exec_argv(
        "whs-shell-abc123", "bash", 10.0, 2.0,
        run_id="abc123def4567890", workdir="/workroom",
        run_uid=65532, run_gid=65532,
    )
    assert argv[0] == "exec"
    assert argv[1:3] == ("--user", "0")
    assert "-i" in argv
    assert argv[argv.index("-i") + 1] == "whs-shell-abc123"
    assert argv[-4:] == ("python3", "-I", "-u", "/workroom/.whs-supervisor-v4.py")
    assert "WHS_RUN_ID=abc123def4567890" in argv
    assert "WHS_LANG=bash" in argv
    assert "WHS_TIMEOUT_S=10.0" in argv
    assert "WHS_RUN_UID=65532" in argv
    assert "WHS_RUN_GID=65532" in argv
    assert "PATH=/usr/local/bin:/usr/bin:/bin" in argv
    assert "WHS_MODEL_PATH=/workroom/.local/bin:/usr/local/bin:/usr/bin:/bin" in argv
    assert "echo hello" not in " ".join(argv)
    py_argv = exec_argv(
        "whs-shell-abc123", "python3", 10.0, 2.0,
        run_id="abc123def4567890", workdir="/workroom",
        run_uid=65532, run_gid=65532,
    )
    assert "WHS_LANG=python3" in py_argv
    assert "-e" in py_argv
    with pytest.raises(ShellWorkroomError):
        exec_argv("whs-shell-abc123", "sh", 10.0, 2.0,
                    run_id="abc123def4567890", workdir="/workroom",
                    run_uid=65532, run_gid=65532)
    with pytest.raises(ShellWorkroomError):
        exec_argv("whs-shell-abc123", "bash", 10.0, 2.0,
                    run_id="has space", workdir="/workroom",
                    run_uid=65532, run_gid=65532)
    with pytest.raises(ShellWorkroomError):
        exec_argv("whs-shell-abc123", "bash", float("nan"), 2.0,
                    run_id="abc123def4567890", workdir="/workroom",
                    run_uid=65532, run_gid=65532)
    with pytest.raises(ShellWorkroomError):
        exec_argv("whs-shell-abc123", "bash", 10.0, 2.0,
                    run_id="abc123def4567890", workdir="/workroom",
        run_uid=0, run_gid=65532)


def test_program_exec_argv_selects_supervisor_mode_without_exposing_program_arguments():
    argv = exec_argv(
        "whs-shell-abc123", "bash", 10.0, 2.0,
        run_id="abc123def4567890", workdir="/workroom",
        run_uid=65532, run_gid=65532, execution_mode="program",
    )
    assert "WHS_EXECUTION_MODE=program" in argv
    rendered = " ".join(argv)
    assert "/usr/bin/curl" not in rendered
    assert "--data=secret" not in rendered


def test_program_capable_supervisor_source_is_valid_python_without_nul_bytes():
    assert "\x00" not in _SUPERVISOR_SOURCE
    compile(_SUPERVISOR_SOURCE, "<shell-supervisor>", "exec")


async def test_exec_program_transports_exact_argv_and_stdin_to_common_supervisor(monkeypatch):
    from redteam.capability.shell_workroom import ShellWorkroomManager

    manager = ShellWorkroomManager(_policy())
    captured = {}

    async def ensure(key):
        assert key == "run1.inst1"
        return "whs-shell-test"

    async def run_docker(argv, stdin_bytes, timeout_seconds, output_limit):
        captured.update(
            argv=argv,
            stdin_bytes=stdin_bytes,
            timeout_seconds=timeout_seconds,
            output_limit=output_limit,
        )
        return 9, b"out", b"err", False, True

    async def classify(*args, **kwargs):
        return False

    monkeypatch.setattr(manager, "ensure_workroom", ensure)
    monkeypatch.setattr(manager, "_run_docker", run_docker)
    monkeypatch.setattr(manager, "_classify_exit", classify)

    result = await manager.exec_program(
        "run1.inst1",
        "/usr/bin/printf",
        ["%s", "hello world"],
        "stdin-data",
        4.0,
        invocation_id="inv-1",
    )

    header, stdin = captured["stdin_bytes"].split(b"\n", 1)
    assert json.loads(header) == {"argv": ["/usr/bin/printf", "%s", "hello world"]}
    assert stdin == b"stdin-data"
    assert "WHS_EXECUTION_MODE=program" in captured["argv"]
    assert result.exit_code == 9
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert result.truncated is True


def test_workroom_key_helpers():
    assert check_workroom_key("run1.inst1") == "run1.inst1"
    assert workroom_key_for("run_abc", "inst_xyz") == "run_abc.inst_xyz"
    assert container_name_for("run1.inst1").startswith("whs-shell-")
    assert container_name_for("run1.inst1") == container_name_for("run1.inst1")
    for bad in ["", "has space", "../escape", "a" * 200]:
        with pytest.raises(ShellWorkroomError):
            check_workroom_key(bad)


def test_operation_bindings_helper():
    descriptors = build_shell_descriptors()
    bindings = shell_operation_bindings(descriptors)
    assert bindings == {str(d.tool_id): tuple(d.operations) for d in descriptors}


async def test_discover_tools_roundtrip_matches_static_manifest():
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    discovered = await adapter.discover_tools()
    validate_shell_descriptors(discovered)
    expected = build_shell_descriptors(adapter.policy)
    assert [d.tool_id for d in discovered] == [d.tool_id for d in expected]
    assert await adapter.health_check() == ToolHealthStatus.HEALTHY


async def test_run_success_reports_digest_and_output():
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    by_name = _descriptor_map(build_shell_descriptors())
    result = await adapter.invoke(
        by_name[RUN_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "echo hi"},
    )
    assert result.success is True
    assert result.error is None
    assert result.output["workroom_key"] == "run1.inst1"
    assert result.output["stdout"] == "fake:echo hi"
    assert result.output["exit_code"] == 0
    expected = BuiltinShellAdapter.script_digest("bash", "echo hi")
    assert result.output["script_sha256"] == expected
    assert result.metadata["script_sha256"] == expected


async def test_run_rejects_bad_input_without_raising():
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    by_name = _descriptor_map(build_shell_descriptors())
    tool = by_name[RUN_TOOL_NAME]
    bad_calls = [
        {"workroom_key": "run1.inst1", "language": "sh", "script": "echo hi"},
        {"workroom_key": "run1.inst1", "language": "bash", "script": ""},
        {"workroom_key": "not valid!!", "language": "bash", "script": "echo hi"},
        {"workroom_key": "run1.inst1", "language": "bash"},
        {"workroom_key": "run1.inst1", "language": "bash", "script": "x", "extra": 1},
    ]
    for arguments in bad_calls:
        result = await adapter.invoke(tool, arguments)
        assert result.success is False
        assert result.error is not None
        assert result.error.code.startswith("shell_")


async def test_invoke_unknown_tool_name():
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    real = build_shell_descriptors()[0]
    fake = real.model_copy(update={"tool_name": "shell_nonexistent"})
    result = await adapter.invoke(fake, {})
    assert result.success is False
    assert result.error.code == "shell_unknown_tool"


async def test_job_start_status_lifecycle():
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    by_name = _descriptor_map(build_shell_descriptors())
    started = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "python3", "script": "print(1)"},
    )
    assert started.success is True
    job_id = started.output["job_id"]
    assert started.output["status"] == "running"
    for _ in range(100):
        status = await adapter.invoke(
            by_name[JOB_STATUS_TOOL_NAME],
            {"workroom_key": "run1.inst1", "job_id": job_id},
        )
        if status.output["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert status.output["status"] == "completed"
    assert status.output["result"]["stdout"] == "fake:print(1)"
    assert status.output["error"] is None


async def test_job_status_unknown_and_cross_key_isolation():
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    by_name = _descriptor_map(build_shell_descriptors())
    started = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.instA", "language": "bash", "script": "echo x"},
    )
    job_id = started.output["job_id"]
    unknown = await adapter.invoke(
        by_name[JOB_STATUS_TOOL_NAME],
        {"workroom_key": "run1.instA", "job_id": "shjob_0000000000000000"},
    )
    assert unknown.success is False
    assert unknown.error.code == "shell_unknown_job"
    other_key = await adapter.invoke(
        by_name[JOB_STATUS_TOOL_NAME],
        {"workroom_key": "run1.instB", "job_id": job_id},
    )
    assert other_key.success is False
    assert other_key.error.code == "shell_unknown_job"
    await adapter.aclose()


async def test_job_cancel_running_job():
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom(slow_scripts={"sleep 30"}))
    by_name = _descriptor_map(build_shell_descriptors())
    started = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "sleep 30"},
    )
    job_id = started.output["job_id"]
    cancelled = await adapter.invoke(
        by_name[JOB_CANCEL_TOOL_NAME],
        {"workroom_key": "run1.inst1", "job_id": job_id},
    )
    assert cancelled.success is True
    assert cancelled.output["cancelled"] is True
    assert cancelled.output["status"] == "cancelled"
    assert cancelled.metadata["container_teardown_verified"] is True
    assert cancelled.metadata["cancel_outcome"] == "cancelled"
    assert EXECUTION_UNCERTAIN_METADATA_KEY not in cancelled.metadata
    await adapter.aclose()


async def test_assembly_entries_validate_policy_image():
    descriptors, adapter = build_shell_entries(_policy())
    assert len(descriptors) == 4
    assert adapter.server_id == SHELL_SERVER_ID
    with pytest.raises(ValueError):
        build_shell_entries(ShellWorkroomPolicy(image="not-a-digest"))

# ---------------------------------------------------------------------------
# Manager-level fakes: an argv-dispatched fake docker CLI. The fake process
# deliberately has no communicate() method, so any test passing through it
# proves the manager streams (budgeted reads) instead of post-hoc buffering.
# ---------------------------------------------------------------------------


class _FakeStdin:
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def write(self, data):
        self.written.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class _FakeReader:
    def __init__(self, data):
        self._data = bytes(data)
        self._pos = 0

    async def read(self, n):
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class _FakeProcess:
    """Duck-typed asyncio subprocess: streams, wait, kill, returncode."""

    def __init__(self, argv, *, code=0, stdout=b"", stderr=b"", delay=0.0):
        self.argv = argv
        self.stdin = _FakeStdin()
        self.stdout = _FakeReader(stdout)
        self.stderr = _FakeReader(stderr)
        self._code = code
        self._delay = delay
        self.returncode = None
        self.killed = False

    async def wait(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self.returncode is None:
            self.returncode = self._code
        return self.returncode

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9


class _BlockingReader:
    """A pipe that stays open until the process is killed."""

    def __init__(self, release):
        self._release = release

    async def read(self, n):
        await self._release.wait()
        return b""


class _BlockingProcess:
    """A never-exiting process: only kill() releases its pipes and wait()."""

    def __init__(self, argv):
        self.argv = argv
        self.stdin = _FakeStdin()
        self._release = asyncio.Event()
        self.stdout = _BlockingReader(self._release)
        self.stderr = _BlockingReader(self._release)
        self.returncode = None
        self.killed = False

    async def wait(self):
        await self._release.wait()
        if self.returncode is None:
            self.returncode = -9
        return self.returncode

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9
        self._release.set()


class _FakeDocker:
    """Fake docker CLI dispatching on argv[0]. Records every call."""

    def __init__(self, *, containers=None):
        self.containers = dict(containers or {})
        self.exec_result = (0, b"", b"")
        self.exec_delay = 0.0
        self.exec_block = False
        self.ps_names = None
        self.ps_fail = False
        self.create_delay = 0.0
        self.image_id = "sha256:" + "f" * 64
        self.result_present = False
        self.result_payload = {
            "outcome": "completed",
            "code": 0,
            "reaped": 0,
            "unreaped": [],
            "subreaper": True,
            "protected": True,
        }
        self.ls_output = b""
        self.pip_output = b"[]"
        self.inventory_output = b""
        self.venv_pip_output = b"[]"
        self.inventory_fail = False
        self.image_missing = False
        self.adopt_record = None
        self.kill_check_clean = True
        self.calls = []
        self.processes = []

    def _spawn(self, proc):
        self.processes.append(proc)
        return proc

    async def __call__(self, binary, *argv, **kwargs):
        self.calls.append(argv)
        joined = " ".join(argv)
        verb = argv[0]
        if verb == "inspect":
            if any("WHS_ADOPTION_RECORD" in a for a in argv):
                if self.adopt_record is None:
                    return self._spawn(_FakeProcess(argv, code=1, stderr=b"no such object"))
                body = (
                    json.dumps(self.adopt_record["config"]) + chr(10)
                    + json.dumps(self.adopt_record["host"]) + chr(10)
                    + json.dumps(self.adopt_record.get("networks", {"none": {}})) + chr(10)
                    + json.dumps(self.adopt_record["image"]) + chr(10)
                    + "WHS_ADOPTION_RECORD")
                return self._spawn(_FakeProcess(argv, code=0, stdout=body.encode()))
            if "--format" in argv:
                if self.image_missing:
                    return self._spawn(_FakeProcess(argv, code=1, stderr=b"no such image"))
                return self._spawn(
                    _FakeProcess(argv, code=0, stdout=(self.image_id + chr(10)).encode())
                )
            name = argv[-1]
            info = self.containers.get(name)
            if info is None:
                return self._spawn(_FakeProcess(argv, code=1, stderr=b"No such object"))
            running = info.get("running", False)
            out = b"true\n" if running else b"false\n"
            return self._spawn(_FakeProcess(argv, code=0, stdout=out))
        if verb == "create":
            if self.create_delay:
                await asyncio.sleep(self.create_delay)
            name = argv[argv.index("--name") + 1]
            if name in self.containers:
                return self._spawn(_FakeProcess(argv, code=1, stderr=b"Conflict: name in use"))
            self.containers[name] = {"running": False, "startable": True}
            return self._spawn(_FakeProcess(argv, code=0, stdout=(name + "\n").encode()))
        if verb == "start":
            info = self.containers.get(argv[1])
            if info is None:
                return self._spawn(_FakeProcess(argv, code=1, stderr=b"No such container"))
            if not info.get("startable", True):
                return self._spawn(_FakeProcess(argv, code=1, stderr=b"cannot be started"))
            info["running"] = True
            return self._spawn(_FakeProcess(argv, code=0, stdout=(argv[1] + "\n").encode()))
        if verb == "ps":
            if self.ps_fail:
                return self._spawn(_FakeProcess(argv, code=1, stderr=b"daemon exploded"))
            names = self.ps_names if self.ps_names is not None else list(self.containers)
            body = "\n".join(names)
            payload = (body + "\n").encode() if body else b""
            return self._spawn(_FakeProcess(argv, code=0, stdout=payload))
        if verb == "rm":
            name = argv[-1]
            if name in self.containers:
                del self.containers[name]
                return self._spawn(_FakeProcess(argv, code=0))
            if ".whs-" in name:
                return self._spawn(_FakeProcess(argv, code=0))
            return self._spawn(_FakeProcess(argv, code=1, stderr=b"Error: No such container"))
        if verb == "exec":
            if "/bin/mkdir" in argv or "/bin/chmod" in argv:
                return self._spawn(_FakeProcess(argv, code=0))
            if ("tee" in argv or "/usr/bin/tee" in argv) and ".whs-supervisor-v" in joined:
                return self._spawn(_FakeProcess(argv, code=0))
            if ("sh" in argv or "/bin/sh" in argv) and "-c" in argv and "WHS_WORKDIR" in joined:
                return self._spawn(_FakeProcess(argv, code=0))
            if "/run/whs-shell" in joined and "mkdir" in joined:
                return self._spawn(_FakeProcess(argv, code=0))
            if "ls" in argv and ".local/bin" in joined:
                return self._spawn(_FakeProcess(argv, code=0, stdout=self.ls_output))
            if "WHS_INVENTORY_SCAN" in joined:
                if self.inventory_fail:
                    return self._spawn(_FakeProcess(argv, code=1, stderr=b"scan failed"))
                return self._spawn(_FakeProcess(argv, code=0, stdout=self.inventory_output))
            if "pip" in argv:
                if "--user" in argv:
                    return self._spawn(_FakeProcess(argv, code=0, stdout=self.pip_output))
                return self._spawn(_FakeProcess(argv, code=0, stdout=self.venv_pip_output))
            if ("cat" in argv or "/bin/cat" in argv) and ".whs-result-" in joined:
                if self.result_present:
                    return self._spawn(
                        _FakeProcess(argv, code=0, stdout=json.dumps(self.result_payload).encode())
                    )
                return self._spawn(_FakeProcess(argv, code=1, stderr=b"No such file"))
            if ("rm" in argv or "/bin/rm" in argv) and ".whs-" in joined:
                return self._spawn(_FakeProcess(argv, code=0))
            if ("bash" in argv or "/bin/bash" in argv) and "-c" in argv:
                if "WHS_SHELL_MODE=check" in argv:
                    return self._spawn(
                        _FakeProcess(argv, code=0 if self.kill_check_clean else 1)
                    )
                return self._spawn(_FakeProcess(argv, code=0))
            if ".whs-supervisor-v" in joined:
                if self.exec_block:
                    return self._spawn(_BlockingProcess(argv))
                code, out, err = self.exec_result
                proc = _FakeProcess(argv, code=code, stdout=out, stderr=err, delay=self.exec_delay)
                return self._spawn(proc)
            if self.exec_block:
                return self._spawn(_BlockingProcess(argv))
            code, out, err = self.exec_result
            proc = _FakeProcess(argv, code=code, stdout=out, stderr=err, delay=self.exec_delay)
            return self._spawn(proc)
        if verb == "version":
            return self._spawn(_FakeProcess(argv, code=0, stdout=b"29.4.2\n"))
        raise AssertionError("unexpected docker verb: " + verb)


def _manager(factory, **overrides):
    from redteam.capability.shell_workroom import ShellWorkroomManager

    return ShellWorkroomManager(_policy(**overrides), process_factory=factory)


def _adopt_record(fake, key, *, user="65532:65532", workdir="/workroom"):
    """Valid adoption record matching the default test policy and image."""
    managed_key = SHELL_MANAGED_LABEL.split("=", 1)[0]
    return {
        "config": {
            "Labels": {managed_key: "true", SHELL_CONTEXT_LABEL_KEY: key},
            "User": user,
            "WorkingDir": workdir,
            "Image": "python:3.11-slim",
        },
        "host": {
            "NetworkMode": "none",
            "Privileged": False,
            "CapAdd": ["SETUID", "SETGID", "KILL"],
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Memory": 536870912,
            "NanoCpus": 1000000000,
            "PidsLimit": 128,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=67108864"},
        },
        "image": fake.image_id,
        "networks": {"none": {}},
    }


async def _wait_terminal(adapter, tool, key, job_id):
    for _ in range(200):
        status = await adapter.invoke(tool, {"workroom_key": key, "job_id": job_id})
        assert status.success is True
        if status.output["status"] != "running":
            return status
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish: " + job_id)


async def _start_script(adapter, by_name, script):
    started = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": script},
    )
    assert started.success is True
    return started.output["job_id"]


async def _job_status(adapter, by_name, job_id):
    return await adapter.invoke(
        by_name[JOB_STATUS_TOOL_NAME],
        {"workroom_key": "run1.inst1", "job_id": job_id},
    )


def test_adapter_init_validates_image_with_injected_workroom():
    """S8: the pin holds at the single construction boundary."""
    with pytest.raises(ValueError):
        BuiltinShellAdapter(ShellWorkroomPolicy(image="python:3.11-slim"), workroom=_FakeWorkroom())


def test_descriptors_derive_bounds_from_policy():
    """S6: schema advertises exactly what the runtime enforces."""
    policy = _policy(max_timeout_seconds=120.0, max_script_bytes=4096)
    by_name = _descriptor_map(build_shell_descriptors(policy))
    validate_shell_descriptors(build_shell_descriptors(policy), policy)
    run_props = by_name[RUN_TOOL_NAME].input_schema["properties"]
    assert run_props["timeout_seconds"]["maximum"] == 120.0
    assert run_props["script"]["maxLength"] == 4096
    assert "byte" in run_props["script"]["description"]
    assert run_props["workroom_key"]["pattern"] == WORKROOM_KEY_PATTERN
    job_props = by_name[JOB_STATUS_TOOL_NAME].input_schema["properties"]
    assert job_props["workroom_key"]["pattern"] == WORKROOM_KEY_PATTERN
    default = _descriptor_map(build_shell_descriptors())
    default_run = default[RUN_TOOL_NAME].input_schema["properties"]
    fields = ShellWorkroomPolicy.model_fields
    assert default_run["timeout_seconds"]["maximum"] == fields["max_timeout_seconds"].default
    assert default_run["script"]["maxLength"] == fields["max_script_bytes"].default
    assert default_run["workroom_key"]["pattern"] == WORKROOM_KEY_PATTERN
    with pytest.raises(ValueError):
        validate_shell_descriptors(
            build_shell_descriptors(policy), _policy(max_timeout_seconds=60.0)
        )


def test_exec_argv_job_tag_travels_via_env():
    tag = "shjob_0123456789abcdef"
    argv = exec_argv("whs-shell-abc123", "bash", 10.0, 2.0, tag,
                      run_id=tag, workdir="/workroom",
                      run_uid=65532, run_gid=65532)
    assert "WHS_SHELL_JOB=" + tag in argv
    assert "WHS_RUN_ID=" + tag in argv
    assert "echo hello" not in " ".join(argv)
    plain = exec_argv("whs-shell-abc123", "bash", 10.0, 2.0,
                        run_id="abc123def4567890", workdir="/workroom",
                        run_uid=65532, run_gid=65532)
    assert not any(a.startswith("WHS_SHELL_JOB=") for a in plain)
    assert "WHS_RUN_ID=abc123def4567890" in plain
    with pytest.raises(ShellWorkroomError):
        exec_argv("whs-shell-abc123", "bash", 10.0, 2.0, "has space",
                    run_id="abc123def4567890", workdir="/workroom",
                    run_uid=65532, run_gid=65532)


# NOTE: under the supervisor design a real timeout always surfaces as 124
# with a timeout result record (see test_plain_container_timeout_reports_null_exit
# and the real-Docker TERM-trap test); a bare 137 with no record is
# supervisor loss (see test_interference_codes_raise_uncertain).


async def test_bare_124_without_record_is_uncertain():
    """A 124 exit with no completion record is uncertainty, never timeout."""
    fake = _FakeDocker()
    fake.exec_result = (124, b"", b"")
    fake.exec_delay = 0.3
    fake.result_present = False
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.exec_script("run1.inst1", "bash", "sleep 30", 0.2)
    assert exc.value.reason_code == "shell_execution_interrupted"
    assert exc.value.uncertainty is True


async def test_fast_124_exit_is_not_a_timeout():
    """Exact: a completed 124 record proves a genuine exit, not a timeout."""
    fake = _FakeDocker()
    fake.exec_result = (124, b"self-inflicted", b"")
    fake.result_present = True
    fake.result_payload = {"outcome": "completed", "code": 124, "reaped": 0,
                             "unreaped": [], "subreaper": True,
                             "protected": True}
    manager = _manager(fake)
    result = await manager.exec_script("run1.inst1", "bash", "exit 124", 60.0)
    assert result.timed_out is False
    assert result.exit_code == 124
    assert result.stdout == "self-inflicted"


async def test_fast_137_exit_is_not_a_timeout():
    fake = _FakeDocker()
    fake.exec_result = (137, b"", b"")
    fake.result_present = True
    fake.result_payload = {"outcome": "completed", "code": 137, "reaped": 0,
                             "unreaped": [], "subreaper": True,
                             "protected": True}
    manager = _manager(fake)
    result = await manager.exec_script("run1.inst1", "bash", "kill -9 self", 60.0)
    assert result.timed_out is False
    assert result.exit_code == 137


async def test_output_budget_bounds_collection():
    """S3: 200KB of output through a 64KB budget stays bounded and flagged."""
    fake = _FakeDocker()
    fake.exec_result = (0, b"x" * 200000, b"tiny-err")
    fake.result_present = True
    manager = _manager(fake, max_output_bytes=65536)
    result = await manager.exec_script("run1.inst1", "bash", "chatty", 60.0)
    assert result.truncated is True
    assert len(result.stdout.encode("utf-8")) <= 65536
    assert result.stderr == "tiny-err"
    assert result.timed_out is False
    assert result.exit_code == 0


async def test_cancel_reaps_host_child():
    """S4: cancelling the wait kills the host docker-exec child, none linger."""
    from redteam.capability.shell_workroom import container_name_for

    key = "run1.inst1"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": True}})
    fake.exec_block = True
    fake.adopt_record = _adopt_record(fake, key)
    manager = _manager(fake)
    manager._known[key] = name
    task = asyncio.create_task(manager.exec_script(key, "bash", "sleep 60", 60.0))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    blocked = [p for p in fake.processes if isinstance(p, _BlockingProcess)]
    assert blocked
    assert all(proc.killed for proc in blocked)
    assert all(proc.returncode is not None for proc in blocked)
    assert blocked[0].stdin.written == b"sleep 60"


async def test_kill_tagged_scans_by_env():
    """Termination signals the supervisor via env and verifies the result.

    Also guards F1: no argv anywhere in the lifecycle may carry a NUL.
    """
    from redteam.capability.shell_workroom import container_name_for

    key = "run1.inst1"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": True}})
    fake.adopt_record = _adopt_record(fake, key)
    fake.result_present = True
    manager = _manager(fake)
    manager._known[key] = name
    report = await manager.kill_tagged_exec(key, "shjob_0123456789abcdef")
    assert report.verified is True
    scans = [c for c in fake.calls if c[0] == "exec" and ("bash" in c or "/bin/bash" in c)]
    assert any(a == "WHS_SHELL_MODE=term" for c in scans for a in c)
    assert any(a == "WHS_SHELL_MODE=check" for c in scans for a in c)
    assert any(a == "WHS_SHELL_KILL=shjob_0123456789abcdef" for c in scans for a in c)
    for call in fake.calls:
        for element in call:
            assert "\x00" not in element
    bad = await manager.kill_tagged_exec(key, "has space")
    assert bad.verified is False
    assert bad.outcome is None


async def test_kill_escalates_when_check_stays_dirty():
    """A dirty first check escalates to the kill round before reporting."""
    from redteam.capability.shell_workroom import container_name_for

    key = "run1.inst1"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": True}})
    fake.adopt_record = _adopt_record(fake, key)
    fake.result_present = True
    fake.kill_check_clean = False
    manager = _manager(fake)
    manager._known[key] = name
    report = await manager.kill_tagged_exec(key, "shjob_0123456789abcdef")
    assert report.verified is False
    scans = [c for c in fake.calls if c[0] == "exec" and ("bash" in c or "/bin/bash" in c)]
    assert any(a == "WHS_SHELL_MODE=kill" for c in scans for a in c)


async def test_script_travels_on_stdin_never_argv():
    """Stdin injection survives the tag/error plumbing."""
    fake = _FakeDocker()
    fake.exec_result = (0, b"hi\n", b"")
    fake.result_present = True
    manager = _manager(fake)
    result = await manager.exec_script("run1.inst1", "bash", "echo secret-marker", 60.0)
    assert result.stdout == "hi\n"
    exec_calls = [c for c in fake.calls if ".whs-supervisor-v" in " ".join(c)]
    assert exec_calls
    assert "echo secret-marker" not in " ".join(exec_calls[-1])
    mains = [p for p in fake.processes if p.stdin.written]
    assert mains and mains[-1].stdin.written == b"echo secret-marker"


async def test_create_collision_adopts_stopped_container():
    """S5: a stopped same-name leftover is started and adopted."""
    from redteam.capability.shell_workroom import container_name_for

    key = "run1.inst9"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": False, "startable": True}})
    fake.adopt_record = _adopt_record(fake, key)
    manager = _manager(fake)
    assert manager.known_workrooms() == ()
    assert await manager.ensure_workroom(key) == name
    assert key in manager.known_workrooms()
    assert fake.containers[name]["running"] is True


async def test_fresh_manager_adopts_running_container_without_create():
    """S5: a live leftover is reused; no create is attempted."""
    from redteam.capability.shell_workroom import container_name_for

    key = "run1.inst9"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": True}})
    fake.adopt_record = _adopt_record(fake, key)
    manager = _manager(fake)
    assert await manager.ensure_workroom(key) == name
    assert not any(c[0] == "create" for c in fake.calls)


async def test_create_collision_unstartable_is_identifiable():
    """S5: an unstartable leftover raises exists_stopped, not create_failed."""
    from redteam.capability.shell_workroom import container_name_for

    key = "run1.inst9"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": False, "startable": False}})
    fake.adopt_record = _adopt_record(fake, key)
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.ensure_workroom(key)
    assert str(exc.value) == "shell_workroom_exists_stopped"


async def test_label_count_enforces_cap_across_restart():
    """S5: the cap counts label-listed containers, not process memory."""
    from redteam.capability.shell_workroom import container_name_for

    fake = _FakeDocker(containers={
        container_name_for("old.a"): {"running": False},
        container_name_for("old.b"): {"running": False},
    })
    manager = _manager(fake, max_workrooms=2)
    assert manager.known_workrooms() == ()
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.ensure_workroom("new.key")
    assert str(exc.value) == "shell_too_many_workrooms"


async def test_verified_cancel_frees_slot_immediately():
    """A verified teardown reclaims the ACTIVE slot at once."""
    adapter = BuiltinShellAdapter(
        _policy(max_jobs=1, completed_job_ttl_seconds=3600.0),
        workroom=_FakeWorkroom(slow_scripts={"sleep 30"}),
    )
    by_name = _descriptor_map(build_shell_descriptors())
    job_id = await _start_script(adapter, by_name, "sleep 30")
    cancelled = await adapter.invoke(
        by_name[JOB_CANCEL_TOOL_NAME],
        {"workroom_key": "run1.inst1", "job_id": job_id},
    )
    assert cancelled.success is True
    assert cancelled.output["cancelled"] is True
    assert cancelled.output["status"] == "cancelled"
    assert cancelled.metadata["container_teardown_verified"] is True
    assert EXECUTION_UNCERTAIN_METADATA_KEY not in cancelled.metadata
    assert (await _start_script(adapter, by_name, "echo hi")) != job_id
    await adapter.aclose()


async def test_unverified_cancel_holds_slot_until_settled():
    """An unverified cancel holds the slot; TTL alone never frees it."""
    workroom = _FakeWorkroom(
        slow_scripts={"sleep 30"},
        kill_verified=False,
        kill_outcome=None,
        settle_outcome=None,
        settle_alive=True,
    )
    adapter = BuiltinShellAdapter(
        _policy(max_jobs=1, completed_job_ttl_seconds=1.0),
        workroom=workroom,
    )
    by_name = _descriptor_map(build_shell_descriptors())
    job_id = await _start_script(adapter, by_name, "sleep 30")
    cancelled = await adapter.invoke(
        by_name[JOB_CANCEL_TOOL_NAME],
        {"workroom_key": "run1.inst1", "job_id": job_id},
    )
    assert cancelled.success is False
    assert cancelled.error.code == "shell_cancel_unverified"
    assert cancelled.output["status"] == "cleanup-unknown"
    assert cancelled.metadata[EXECUTION_UNCERTAIN_METADATA_KEY] is True
    assert execution_outcome_uncertain(cancelled.metadata) is True
    assert execution_outcome_uncertain({}) is False
    # TTL expires (0.2s) while the supervisor still reports alive: held.
    # TTL expires (1.0s) while the supervisor still reports alive: held.
    await asyncio.sleep(1.2)
    held = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "echo hi"},
    )
    assert held.success is False
    assert held.error.code == "shell_job_limit"
    # Supervisor confirms teardown: the next start settles and succeeds.
    workroom.settle_alive = False
    workroom.settle_outcome = "cancelled"
    assert (await _start_script(adapter, by_name, "echo hi")) != job_id
    await adapter.aclose()


async def test_completed_jobs_evict_oldest():
    """S7: the start-time sweep evicts the oldest completed job past the cap."""
    adapter = BuiltinShellAdapter(
        _policy(max_jobs=2, completed_job_ttl_seconds=3600.0),
        workroom=_FakeWorkroom(),
    )
    by_name = _descriptor_map(build_shell_descriptors())
    status_tool = by_name[JOB_STATUS_TOOL_NAME]
    first = await _start_script(adapter, by_name, "one")
    await _wait_terminal(adapter, status_tool, "run1.inst1", first)
    second = await _start_script(adapter, by_name, "two")
    await _wait_terminal(adapter, status_tool, "run1.inst1", second)
    # Starting the third job sweeps the oldest completed entry (first).
    third = await _start_script(adapter, by_name, "three")
    await _wait_terminal(adapter, status_tool, "run1.inst1", third)
    gone = await _job_status(adapter, by_name, first)
    assert gone.success is False
    assert gone.error.code == "shell_unknown_job"
    kept = await _job_status(adapter, by_name, second)
    assert kept.success is True
    assert kept.output["status"] == "completed"
    await adapter.aclose()


def _shell_invocation(arguments, *, run="run1", instance="inst1", context_run=None):
    return ToolInvocation(
        invocation_id=InvocationId("inv-t3"),
        action_id=ActionId("action-t3"),
        run_id=RunId(run),
        tool_id=ToolId("analysis.shell_run"),
        binding_mode=SELECTED_TOOL,
        arguments=arguments,
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng-t3"),
            run_id=RunId(context_run if context_run is not None else run),
            instance_id=AgentInstanceId(instance) if instance is not None else None,
        ),
        created_at=datetime.now(timezone.utc),
    )


def _shell_start_invocation(invocation_id: str, script: str = "sleep 30"):
    return ToolInvocation(
        invocation_id=InvocationId(invocation_id),
        action_id=ActionId("action-start-test"),
        run_id=RunId("run1"),
        tool_id=ToolId("analysis.shell_job_start"),
        binding_mode=SELECTED_TOOL,
        arguments={"workroom_key": "run1.inst1", "language": "bash", "script": script},
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng-start-test"),
            run_id=RunId("run1"),
            instance_id=AgentInstanceId("inst1"),
        ),
        created_at=datetime.now(timezone.utc),
    )


class _ContextStartWorkroom(_FakeWorkroom):
    def __init__(
        self,
        *,
        slow_scripts=(),
        uncertain_scripts=(),
        fail_manifest=False,
        trace_present=True,
        block_ensure=False,
        block_manifest=False,
        block_first_outcome=False,
        kill_verified=True,
    ):
        super().__init__(
            slow_scripts=slow_scripts,
            uncertain_scripts=uncertain_scripts,
            kill_verified=kill_verified,
        )
        self.fail_manifest = fail_manifest
        self.trace_present = trace_present
        self.block_ensure = block_ensure
        self.block_manifest = block_manifest
        self.block_first_outcome = block_first_outcome
        self.ensure_calls = 0
        self.manifest_calls = 0
        self.manifests = {}
        self.probe_calls = 0
        self.ensure_entered = asyncio.Event()
        self.two_ensures_entered = asyncio.Event()
        self.release_ensure = asyncio.Event()
        if not block_ensure:
            self.release_ensure.set()
        self.manifest_written = asyncio.Event()
        self.release_manifest = asyncio.Event()
        if not block_manifest:
            self.release_manifest.set()
        self.probe_entered = asyncio.Event()
        self.release_probe = asyncio.Event()
        if not block_first_outcome:
            self.release_probe.set()

    async def ensure_workroom(self, key):
        self.ensure_calls += 1
        self.ensure_entered.set()
        if self.ensure_calls >= 2:
            self.two_ensures_entered.set()
        if self.block_ensure:
            await self.release_ensure.wait()
        return "fake-container"

    async def write_job_manifest(self, key, job_id, manifest):
        self.manifest_calls += 1
        if self.fail_manifest:
            raise ShellWorkroomError("shell_job_manifest_unwritable")
        self.manifests[job_id] = dict(manifest)
        self.manifest_written.set()
        if self.block_manifest:
            await self.release_manifest.wait()

    async def read_job_manifest(self, key, job_id):
        found = self.manifests.get(job_id)
        return dict(found) if isinstance(found, dict) else None

    async def supervisor_trace_present(self, key, supervisor_id):
        return self.trace_present

    async def spool_record(self, key, supervisor_id):
        return None

    async def read_cancel_mark(self, key, supervisor_id):
        return None

    async def supervisor_outcome(self, key, selector):
        self.probe_calls += 1
        if self.block_first_outcome and self.probe_calls == 1:
            self.probe_entered.set()
            await self.release_probe.wait()
        return self.settle_outcome, self.settle_alive


class _ProgramWorkroom(_FakeWorkroom):
    def __init__(self, metadata=None):
        super().__init__()
        self.program_calls = []
        self._metadata = metadata or ProgramStaticMetadata(
            purposes=["local", "target"],
            path_status="effective",
            resolved_path="/usr/bin/printf",
        )

    async def list_image_programs(self):
        return [ProgramRecord(
            name="printf",
            executable="/usr/bin/printf",
            version="1",
            owner_scope="image",
            provenance="test",
            metadata=self._metadata,
        )]

    async def exec_program(
        self, key, executable, argv, stdin="", timeout_seconds=None, exec_tag=None, **kwargs
    ):
        self.program_calls.append((key, executable, argv, stdin, timeout_seconds, exec_tag))
        return ExecResult(
            exit_code=7,
            stdout="program-output",
            stderr="program-error",
            timed_out=False,
            truncated=True,
            duration_ms=3,
            workroom_key=key,
            container_name="fake-container",
        )


async def test_contextual_start_reserves_capacity_before_independent_ensure_calls():
    workroom = _ContextStartWorkroom(block_ensure=True, slow_scripts={"sleep 30"})
    adapter = BuiltinShellAdapter(_policy(max_jobs=2), workroom=workroom)
    first = asyncio.create_task(
        adapter.invoke_contextual(_shell_start_invocation("inv-start-first"))
    )
    await asyncio.wait_for(workroom.ensure_entered.wait(), 1.0)
    second = asyncio.create_task(
        adapter.invoke_contextual(_shell_start_invocation("inv-start-second"))
    )

    await asyncio.wait_for(workroom.two_ensures_entered.wait(), 1.0)
    assert adapter._active_count() == 2
    assert workroom.ensure_calls == 2
    workroom.release_ensure.set()
    results = await asyncio.gather(first, second)
    assert all(result.success for result in results)
    assert len({result.output["job_id"] for result in results}) == 2
    await adapter.aclose()


async def test_contextual_duplicate_waits_for_reservation_and_max_jobs_counts_it():
    workroom = _ContextStartWorkroom(block_ensure=True, slow_scripts={"sleep 30"})
    adapter = BuiltinShellAdapter(_policy(max_jobs=1), workroom=workroom)
    same = _shell_start_invocation("inv-start-duplicate")
    first = asyncio.create_task(adapter.invoke_contextual(same))
    await asyncio.wait_for(workroom.ensure_entered.wait(), 1.0)
    duplicate = asyncio.create_task(adapter.invoke_contextual(same))
    await asyncio.sleep(0)

    limited = await adapter.invoke_contextual(_shell_start_invocation("inv-start-full"))
    assert limited.success is False
    assert limited.error.code == "shell_job_limit"
    assert adapter._active_count() == 1
    assert workroom.ensure_calls == 1

    workroom.release_ensure.set()
    first_result, duplicate_result = await asyncio.gather(first, duplicate)
    assert first_result.success is True
    assert duplicate_result.success is True
    assert duplicate_result.output["job_id"] == first_result.output["job_id"]
    assert workroom.ensure_calls == 1
    assert workroom.manifest_calls == 1
    await asyncio.sleep(0)
    assert sum(
        1 for call in workroom.calls
        if len(call) == 4 and call[2] == "sleep 30"
    ) == 1
    await adapter.aclose()


async def test_slow_unknown_probe_does_not_block_submit_when_a_slot_opens():
    workroom = _ContextStartWorkroom(
        slow_scripts={"sleep 30"},
        uncertain_scripts={"boom"},
        block_first_outcome=True,
    )
    adapter = BuiltinShellAdapter(_policy(max_jobs=2), workroom=workroom)
    by_name = _descriptor_map(build_shell_descriptors())
    unknown = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "boom"},
    )
    unknown_job = adapter._jobs[unknown.output["job_id"]]
    await asyncio.wait_for(asyncio.shield(unknown_job.task), 1.0)
    assert unknown_job.status == "uncertain"
    unknown_job.created_at -= 3600.0

    running = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "sleep 30"},
    )
    slow_probe = asyncio.create_task(
        adapter.invoke_contextual(_shell_start_invocation("inv-start-slow-probe"))
    )
    await asyncio.wait_for(workroom.probe_entered.wait(), 1.0)

    cancelled = await adapter.invoke(
        by_name[JOB_CANCEL_TOOL_NAME],
        {"workroom_key": "run1.inst1", "job_id": running.output["job_id"]},
    )
    assert cancelled.success is True
    independent = await asyncio.wait_for(
        adapter.invoke_contextual(_shell_start_invocation("inv-start-spare-slot")),
        1.0,
    )
    assert independent.success is True
    assert workroom.probe_calls == 1
    assert slow_probe.done() is False

    workroom.release_probe.set()
    limited = await slow_probe
    assert limited.success is False
    assert limited.error.code == "shell_job_limit"
    assert adapter._jobs[unknown.output["job_id"]].status == "uncertain"
    await adapter.aclose()


async def test_contextual_manifest_failure_releases_only_before_spawn(monkeypatch):
    workroom = _ContextStartWorkroom(
        fail_manifest=True,
        trace_present=False,
        slow_scripts={"sleep 30"},
    )
    adapter = BuiltinShellAdapter(_policy(max_jobs=1), workroom=workroom)
    invocation = _shell_start_invocation("inv-start-manifest-fail")
    failed = await adapter.invoke_contextual(invocation)
    assert failed.success is False
    assert failed.error.code == "shell_job_manifest_unwritable"
    assert adapter._active_count() == 0
    assert adapter._jobs == {}

    async def no_trace(_key, _supervisor_id, *, timeout_seconds=15.0):
        return False

    monkeypatch.setattr(adapter, "_await_spawn_verified", no_trace)
    workroom.fail_manifest = False
    started = await adapter.invoke_contextual(invocation)
    assert started.success is False
    assert started.error.code == "shell_submit_unverified"
    assert started.metadata[EXECUTION_UNCERTAIN_METADATA_KEY] is True
    assert adapter._active_count() == 1
    duplicate = await adapter.invoke_contextual(invocation)
    assert duplicate.error.code == "shell_submit_unverified"
    assert workroom.ensure_calls == 2
    assert workroom.manifest_calls == 2
    limited = await adapter.invoke_contextual(_shell_start_invocation("inv-start-after-unknown"))
    assert limited.error.code == "shell_job_limit"
    assert adapter._active_count() == 1
    await adapter.aclose()


@pytest.mark.parametrize("stage", ["ensure", "manifest"])
async def test_contextual_cancel_during_pre_spawn_keeps_unknown_reservation(
    stage, monkeypatch
):
    workroom = _ContextStartWorkroom(
        block_ensure=stage == "ensure",
        block_manifest=stage == "manifest",
        trace_present=False,
        slow_scripts={"sleep 30"},
    )
    adapter = BuiltinShellAdapter(_policy(max_jobs=1), workroom=workroom)
    invocation = _shell_start_invocation("inv-start-cancel-" + stage)
    reference = adapter.prepare_reference(invocation)
    assert reference is not None
    job_id = reference.provider_handle["job_id"]
    start = asyncio.create_task(adapter.invoke_contextual(invocation))

    if stage == "ensure":
        await asyncio.wait_for(workroom.ensure_entered.wait(), 1.0)
    else:
        await asyncio.wait_for(workroom.manifest_written.wait(), 1.0)
        await adapter._lock.acquire()
        workroom.release_manifest.set()
        await asyncio.sleep(0)
        assert start.done() is False
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    if stage == "manifest":
        adapter._lock.release()

    reserved = adapter._jobs[job_id]
    assert reserved.status == "uncertain"
    assert reserved.uncertain is True
    assert reserved.task is None
    assert adapter._active_count() == 1
    assert (job_id in workroom.manifests) is (stage == "manifest")

    async def no_trace(_key, _supervisor_id, *, timeout_seconds=15.0):
        return False

    monkeypatch.setattr(adapter, "_await_spawn_verified", no_trace)
    duplicate = await adapter.invoke_contextual(invocation)
    assert duplicate.error.code == "shell_submit_unverified"
    assert workroom.ensure_calls == 1
    assert workroom.manifest_calls == (1 if stage == "manifest" else 0)
    assert adapter._active_count() == 1

    if stage == "manifest":
        fresh = BuiltinShellAdapter(_policy(max_jobs=1), workroom=workroom)
        status = await fresh.invoke(
            _descriptor_map(build_shell_descriptors())[JOB_STATUS_TOOL_NAME],
            {"workroom_key": "run1.inst1", "job_id": job_id},
        )
        assert status.success is True
        assert status.output["status"] == "running"
        assert fresh._active_count() == 1
        await fresh.aclose()
    cancelled = await adapter.invoke(
        _descriptor_map(build_shell_descriptors())[JOB_CANCEL_TOOL_NAME],
        {"workroom_key": "run1.inst1", "job_id": job_id},
    )
    assert cancelled.success is True
    assert cancelled.output["status"] == "cancelled"
    assert adapter._active_count() == 0
    await adapter.aclose()


async def test_contextual_structured_program_binds_discovery_argv_stdin_and_evidence():
    workroom = _ProgramWorkroom()
    adapter = BuiltinShellAdapter(_policy(), workroom=workroom)
    invocation = _shell_invocation({
        "workroom_key": "run1.inst1",
        "program": {
            "name": "printf",
            "executable": "/usr/bin/printf",
            "owner_scope": "image",
            "argv": ["%s", "hello"],
            "stdin": "input",
        },
        "timeout_seconds": 4.0,
    })

    result = await adapter.invoke_contextual(invocation)

    assert result.success is True
    assert workroom.program_calls == [
        ("run1.inst1", "/usr/bin/printf", ["%s", "hello"], "input", 4.0, "inv-t3")
    ]
    assert result.output["exit_code"] == 7
    assert result.output["stderr"] == "program-error"
    assert result.output["truncated"] is True
    assert result.output["execution_kind"] == "program"
    assert result.output["program_argv"] == ["/usr/bin/printf", "%s", "hello"]
    assert result.metadata["route_purpose"] == "local"


async def test_structured_program_rejects_wrong_discovery_identity_before_execution():
    workroom = _ProgramWorkroom()
    adapter = BuiltinShellAdapter(_policy(), workroom=workroom)
    invocation = _shell_invocation({
        "workroom_key": "run1.inst1",
        "program": {
            "name": "printf",
            "executable": "/usr/local/bin/printf",
            "owner_scope": "image",
            "argv": [],
        },
    })

    result = await adapter.invoke_contextual(invocation)

    assert result.success is False
    assert result.error.code == "shell_program_not_discovered"
    assert workroom.program_calls == []


async def test_structured_program_refuses_required_privilege_without_operator_profile():
    workroom = _ProgramWorkroom(ProgramStaticMetadata(
        purposes=["local", "privileged"],
        required_capabilities=["CAP_NET_RAW"],
        unsupported_modes=["syn_scan"],
        unprivileged_alternatives=["connect_scan"],
        path_status="effective",
        resolved_path="/usr/bin/printf",
    ))
    adapter = BuiltinShellAdapter(_policy(), workroom=workroom)
    invocation = _shell_invocation({
        "workroom_key": "run1.inst1",
        "program": {
            "name": "printf",
            "executable": "/usr/bin/printf",
            "owner_scope": "image",
            "argv": [],
        },
    })

    result = await adapter.invoke_contextual(invocation)

    assert result.success is False
    assert result.error.code == "shell_program_privilege_unsupported"
    assert result.metadata["unprivileged_alternatives"] == ["connect_scan"]
    assert workroom.program_calls == []


def test_structured_program_recovery_reference_binds_changed_argv():
    adapter = BuiltinShellAdapter(_policy(), workroom=_ProgramWorkroom())
    base = {
        "workroom_key": "run1.inst1",
        "program": {
            "name": "printf",
            "executable": "/usr/bin/printf",
            "owner_scope": "image",
            "argv": ["one"],
        },
    }
    first = adapter.prepare_reference(_shell_invocation(base))
    changed = dict(base)
    changed["program"] = {**base["program"], "argv": ["two"]}
    second = adapter.prepare_reference(_shell_invocation(changed))

    assert first is not None and second is not None
    assert first.input_digest != second.input_digest
    assert first.provider_handle["program_sha256"] != second.provider_handle["program_sha256"]
    assert first.provider_handle["execution_kind"] == "program"


def test_binder_protocol_and_prefix():
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    assert isinstance(adapter, InvocationContextBinder)
    assert adapter.error_prefix == "shell"


def test_bind_context_injects_when_omitted():
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    bound = adapter.bind_context(
        _shell_invocation({"language": "bash", "script": "echo hi"})
    )
    assert bound["workroom_key"] == "run1.inst1"
    assert bound["language"] == "bash"


def test_bind_context_accepts_matching_key():
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    bound = adapter.bind_context(
        _shell_invocation({"workroom_key": "run1.inst1", "language": "bash",
                             "script": "echo hi"})
    )
    assert bound["workroom_key"] == "run1.inst1"


def test_bind_context_rejects_mismatch():
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    with pytest.raises(ValueError):
        adapter.bind_context(
            _shell_invocation({"workroom_key": "run1.other", "language": "bash",
                                 "script": "echo hi"})
    )


def test_bind_context_requires_identity():
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    with pytest.raises(ValueError):
        adapter.bind_context(
            _shell_invocation({"language": "bash", "script": "echo hi"},
                                instance=None)
        )
    with pytest.raises(ValueError):
        adapter.bind_context(
            _shell_invocation({"language": "bash", "script": "echo hi"},
                                context_run="other-run")
        )


def test_bound_arguments_validate_against_schema():
    """Bound args must satisfy the descriptor schema the gateway checks."""
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    by_name = _descriptor_map(build_shell_descriptors(adapter.policy))
    cases = [
        (RUN_TOOL_NAME, {"language": "bash", "script": "echo hi"}),
        (JOB_START_TOOL_NAME, {"language": "bash", "script": "echo hi"}),
        (JOB_STATUS_TOOL_NAME, {"job_id": "shjob_0123456789abcdef"}),
        (JOB_CANCEL_TOOL_NAME, {"job_id": "shjob_0123456789abcdef"}),
    ]
    for tool_name, arguments in cases:
        invocation = _shell_invocation(dict(arguments))
        bound = adapter.bind_context(invocation)
        assert bound["workroom_key"] == "run1.inst1"
        Draft202012Validator(by_name[tool_name].input_schema).validate(bound)


async def test_timeout_record_mismatch_means_timeout():
    """A trusted timeout 124 record is a clean timeout, not genuine."""
    fake = _FakeDocker()
    fake.exec_result = (124, b"", b"")
    fake.result_present = True
    fake.result_payload = {"outcome": "timeout", "code": 124, "reaped": 1,
                             "unreaped": [], "subreaper": True,
                             "protected": True}
    manager = _manager(fake)
    result = await manager.exec_script("run1.inst1", "bash", "sleep 30", 60.0)
    assert result.timed_out is True
    assert result.exit_code is None


async def test_absent_record_with_124_is_uncertain():
    """A 124 with no record means a disappeared supervisor: uncertain."""
    fake = _FakeDocker()
    fake.exec_result = (124, b"", b"")
    fake.result_present = False
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.exec_script("run1.inst1", "bash", "sleep 30", 60.0)
    assert exc.value.reason_code == "shell_execution_interrupted"
    assert exc.value.uncertainty is True


async def test_interference_codes_raise_uncertain():
    """142 and record-less 137 fail loudly with the uncertainty flag."""
    for code in (142, 137):
        fake = _FakeDocker()
        fake.exec_result = (code, b"", b"")
        fake.result_present = False
        manager = _manager(fake)
        with pytest.raises(ShellWorkroomError) as exc:
            await manager.exec_script("run1.inst1", "bash", "weird", 60.0)
        assert str(exc.value) == "shell_execution_interrupted"
        assert exc.value.uncertainty is True


async def test_uncertainty_marker_on_supervisor_loss():
    """Post-start loss marks the result; the helper reads it back."""

    class _RaisingWorkroom(_FakeWorkroom):
        async def exec_script(
            self, key, language, script, timeout_seconds=None, exec_tag=None, **_kwargs
        ):
            raise ShellWorkroomError("shell_execution_interrupted", uncertainty=True)

    adapter = BuiltinShellAdapter(_policy(), workroom=_RaisingWorkroom())
    by_name = _descriptor_map(build_shell_descriptors())
    result = await adapter.invoke(
        by_name[RUN_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "echo hi"},
    )
    assert result.success is False
    assert result.error.code == "shell_execution_interrupted"
    assert result.metadata[EXECUTION_UNCERTAIN_METADATA_KEY] is True
    assert execution_outcome_uncertain(result.metadata) is True


async def test_no_marker_on_before_start_rejections():
    """Validation failures never claim uncertainty."""
    adapter = BuiltinShellAdapter(_policy(), workroom=_FakeWorkroom())
    by_name = _descriptor_map(build_shell_descriptors())
    bad = await adapter.invoke(
        by_name[RUN_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "sh", "script": "echo hi"},
    )
    assert bad.success is False
    assert EXECUTION_UNCERTAIN_METADATA_KEY not in bad.metadata
    assert execution_outcome_uncertain(bad.metadata) is False
    unknown = await adapter.invoke(
        by_name[JOB_STATUS_TOOL_NAME],
        {"workroom_key": "run1.inst1", "job_id": "shjob_0000000000000000"},
    )
    assert unknown.success is False
    assert EXECUTION_UNCERTAIN_METADATA_KEY not in unknown.metadata


async def test_fail_closed_count_refuses_creation():
    """An unreadable daemon count refuses creation (no memory fallback)."""
    fake = _FakeDocker()
    fake.ps_fail = True
    manager = _manager(fake)
    assert manager.known_workrooms() == ()
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.ensure_workroom("new.key")
    assert str(exc.value) == "shell_workroom_count_failed"
    assert manager.known_workrooms() == ()


async def test_fail_closed_prefix_lists_raise():
    fake = _FakeDocker()
    fake.ps_fail = True
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError):
        await manager.remove_workrooms_by_prefix("run.")


async def test_list_managed_parses_name_and_context():
    """The daemon listing format yields (name, key) pairs."""
    fake = _FakeDocker()
    fake.ps_names = [
        "whs-shell-aaa|run1.inst1",
        "whs-shell-bbb|run1.inst2",
        "whs-shell-ccc",
    ]
    manager = _manager(fake)
    assert await manager._list_managed() == [
        ("whs-shell-aaa", "run1.inst1"),
        ("whs-shell-bbb", "run1.inst2"),
    ]


async def test_concurrent_create_cap_one():
    """Two managers racing a cap of 1 leave exactly one container."""
    from redteam.capability.shell_workroom import container_name_for

    fake = _FakeDocker()
    fake.create_delay = 0.2
    first = _manager(fake, max_workrooms=1)
    second = _manager(fake, max_workrooms=1)
    outcomes = await asyncio.gather(
        first.ensure_workroom("race.one"),
        second.ensure_workroom("race.two"),
        return_exceptions=True,
    )
    wins = [o for o in outcomes if not isinstance(o, Exception)]
    losses = [o for o in outcomes if isinstance(o, Exception)]
    assert len(wins) == 1
    assert len(losses) == 1
    assert str(losses[0]) == "shell_too_many_workrooms"
    assert len(fake.containers) == 1
    assert container_name_for("race.one") in fake.containers or \
        container_name_for("race.two") in fake.containers


async def test_retry_cancel_verifies():
    """A second cancel can verify what the first could not."""
    workroom = _FakeWorkroom(slow_scripts={"sleep 30"}, kill_verified=False)
    adapter = BuiltinShellAdapter(_policy(max_jobs=1), workroom=workroom)
    by_name = _descriptor_map(build_shell_descriptors())
    job_id = await _start_script(adapter, by_name, "sleep 30")
    first = await adapter.invoke(
        by_name[JOB_CANCEL_TOOL_NAME],
        {"workroom_key": "run1.inst1", "job_id": job_id},
    )
    assert first.success is False
    assert first.output["status"] == "cleanup-unknown"
    workroom.kill_verified = True
    second = await adapter.invoke(
        by_name[JOB_CANCEL_TOOL_NAME],
        {"workroom_key": "run1.inst1", "job_id": job_id},
    )
    assert second.success is True
    assert second.output["cancelled"] is True
    assert second.output["status"] == "cancelled"
    assert (await _start_script(adapter, by_name, "echo hi")) != job_id
    await adapter.aclose()


async def test_cancel_reap_bound_marks_unknown():
    """A teardown past the bound reports unknown instead of hanging."""
    import redteam.capability.shell_adapter as adapter_module

    workroom = _FakeWorkroom(slow_scripts={"sleep 30"}, kill_delay=30.0)
    adapter = BuiltinShellAdapter(_policy(max_jobs=1), workroom=workroom)
    by_name = _descriptor_map(build_shell_descriptors())
    job_id = await _start_script(adapter, by_name, "sleep 30")
    old_bound, adapter_module._CANCEL_REAP_TIMEOUT_SECONDS = \
        adapter_module._CANCEL_REAP_TIMEOUT_SECONDS, 0.2
    try:
        started = time.monotonic()
        cancelled = await adapter.invoke(
            by_name[JOB_CANCEL_TOOL_NAME],
            {"workroom_key": "run1.inst1", "job_id": job_id},
        )
        elapsed = time.monotonic() - started
    finally:
        adapter_module._CANCEL_REAP_TIMEOUT_SECONDS = old_bound
    assert elapsed < 10.0
    assert cancelled.output["status"] == "cleanup-unknown"
    assert cancelled.success is False
    assert cancelled.metadata[EXECUTION_UNCERTAIN_METADATA_KEY] is True
    await adapter.aclose()


def test_outer_cap_covers_hostile_maximum():
    """The outer bound always exceeds the worst serialized result."""
    import json as json_module

    for max_output in (65536, 1048576):
        policy = _policy(max_output_bytes=max_output)
        outer = shell_output_limit(policy)
        assert shell_server_output_limits(policy) == {SHELL_SERVER_ID: outer}
        hostile = "\x00" * max_output
        output = {
            "exit_code": 0,
            "stdout": hostile,
            "stderr": hostile,
            "timed_out": False,
            "truncated": True,
            "duration_ms": 999999999,
            "script_sha256": "f" * 64,
            "workroom_key": "k" * 128,
        }
        serialized = json_module.dumps(
            output, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        assert len(serialized) <= outer


async def test_supervisor_provisioned_on_create_and_adopt():
    """Create and adopt both write the versioned supervisor as root."""
    from redteam.capability.shell_workroom import container_name_for

    key = "run1.provision"
    name = container_name_for(key)
    fake = _FakeDocker()
    fake.adopt_record = _adopt_record(fake, key)
    manager = _manager(fake)
    assert await manager.ensure_workroom(key) == name
    tees = [c for c in fake.calls if "tee" in c or "/usr/bin/tee" in c]
    assert tees
    assert any(".whs-supervisor-v4.py" in a for c in tees for a in c)
    assert any("--user" in c and c[c.index("--user") + 1] == "0" for c in tees)
    sources = [p.stdin.written for p in fake.processes if p.stdin.written]
    assert any(b"_enable_subreaper" in blob for blob in sources)
    fresh = _manager(fake)
    assert await fresh.ensure_workroom(key) == name
    assert len([c for c in fake.calls if "tee" in c or "/usr/bin/tee" in c]) >= 2


async def test_image_programs_from_manifest(tmp_path):
    """Image programs enumerate commands; corrupt manifests fail loudly."""
    from redteam.capability.shell_workroom import ProgramRecord

    manifest = tmp_path / "program-manifest.json"
    manifest.write_text(json.dumps({
        "image_digest": "sha256:" + "f" * 64,
        "programs": [{
            "package": "dnsutils",
            "version": "1:9.20.29-1",
            "origin": "kali-rolling/main",
            "executables": [
                {"command": "dig", "path": "/usr/bin/dig"},
                {"command": "nslookup", "path": "/usr/bin/nslookup"},
            ],
        }]
    }), encoding="utf-8")
    manager = _manager(_FakeDocker())
    records = await manager.list_image_programs(str(manifest))
    assert records == [
        ProgramRecord(name="dig", executable="/usr/bin/dig",
                        version="1:9.20.29-1", owner_scope="image",
                        provenance="kali-rolling/main"),
        ProgramRecord(name="nslookup", executable="/usr/bin/nslookup",
                        version="1:9.20.29-1", owner_scope="image",
                        provenance="kali-rolling/main"),
    ]
    manifest.write_text("{not json", encoding="utf-8")
    with pytest.raises(ShellWorkroomError):
        await manager.list_image_programs(str(manifest))


async def test_image_programs_refuse_foreign_manifest(tmp_path):
    """A digest mismatch never publishes another image's inventory."""
    manifest = tmp_path / "program-manifest.json"
    manifest.write_text(json.dumps({
        "image_digest": "sha256:" + "0" * 64,
        "programs": [{
            "package": "dnsutils",
            "version": "1:9.20.29-1",
            "origin": "kali-rolling/main",
            "executables": [{"command": "dig", "path": "/usr/bin/dig"}],
        }]
    }), encoding="utf-8")
    fake = _FakeDocker()
    fake.image_id = "sha256:" + "f" * 64
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.list_image_programs(str(manifest))
    assert str(exc.value) == "shell_image_manifest_mismatch"
    manifest.write_text(json.dumps({"programs": []}), encoding="utf-8")
    with pytest.raises(ShellWorkroomError):
        await manager.list_image_programs(str(manifest))


async def test_instance_programs_trusted_identity():
    """Instance inventory derives the key; model keys are compare-only."""
    from redteam.capability.shell_workroom import container_name_for

    key = "run1.inst1"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": True}})
    workdir = _policy().workdir.rstrip("/")
    fake.inventory_output = (workdir + "/.local/bin/hello-tool" + chr(10)).encode()
    fake.pip_output = json.dumps([{"name": "hello-tool", "version": "0.1"}]).encode()
    manager = _manager(fake)
    records = await manager.list_instance_programs(run_id="run1", instance_id="inst1")
    assert [(r.name, r.executable, r.version, r.owner_scope) for r in records] == [
        ("hello-tool", manager.policy.workdir.rstrip("/") + "/.local/bin/hello-tool",
         "0.1", "instance"),
    ]
    matched = await manager.list_instance_programs(
        run_id="run1", instance_id="inst1", workroom_key="run1.inst1")
    assert len(matched) == 1
    with pytest.raises(ShellWorkroomError):
        await manager.list_instance_programs(
            run_id="run1", instance_id="inst1", workroom_key="run1.other")
    with pytest.raises(ShellWorkroomError):
        await manager.list_instance_programs(run_id="run1", instance_id="")
    stopped = _manager(_FakeDocker(containers={name: {"running": False}}))
    with pytest.raises(ShellWorkroomError):
        await stopped.list_instance_programs(run_id="run1", instance_id="inst1")


def test_program_source_property():
    manager = _FakeWorkroom()
    adapter = BuiltinShellAdapter(_policy(), workroom=manager)
    assert adapter.program_source is manager


async def test_instance_programs_executable_filtering():
    """Only workdir executable paths become records; nothing else does."""
    key = "run1.inst1"
    name = container_name_for(key)
    workdir = _policy().workdir.rstrip("/")
    fake = _FakeDocker(containers={name: {"running": True}})
    fake.inventory_output = (
        workdir + "/.local/bin/hello-tool" + chr(10)
        + "/etc/passwd" + chr(10)
        + "relative/path" + chr(10)
        + workdir + "/myvenv/bin/venvshell" + chr(10)
    ).encode()
    fake.pip_output = json.dumps([{"name": "hello-tool", "version": "0.1"}]).encode()
    fake.venv_pip_output = json.dumps([{"name": "venvshell", "version": "2.0"}]).encode()
    manager = _manager(fake)
    records = await manager.list_instance_programs(run_id="run1", instance_id="inst1")
    assert [(r.name, r.executable, r.version, r.owner_scope) for r in records] == [
        ("hello-tool", workdir + "/.local/bin/hello-tool", "0.1", "instance"),
        ("venvshell", workdir + "/myvenv/bin/venvshell", "2.0", "instance"),
    ]
    assert all(r.provenance.startswith("workroom:") for r in records)


async def test_instance_programs_probe_failure_is_loud():
    """A failed inventory probe never reports an empty list as success."""
    key = "run1.inst1"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": True}})
    fake.inventory_fail = True
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.list_instance_programs(run_id="run1", instance_id="inst1")
    assert exc.value.reason_code == "shell_inventory_failed"


async def test_image_programs_unavailable_image(tmp_path):
    """An unresolvable configured image never passes provenance."""
    manifest = tmp_path / "program-manifest.json"
    payload = {"image_digest": "sha256:" + "f" * 64, "programs": []}
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    fake = _FakeDocker()
    fake.image_missing = True
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.list_image_programs(str(manifest))
    assert exc.value.reason_code == "shell_image_unavailable"


def test_error_reason_code_matches_message():
    """The stable code travels structurally, never parsed from text."""
    assert ShellWorkroomError("shell_nope").reason_code == "shell_nope"
    assert str(ShellWorkroomError("shell_nope")) == "shell_nope"


async def test_adopt_rejects_changed_image():
    """Same key with a changed policy image is rejected, never reused."""
    key = "run1.inst9"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": True}})
    fake.adopt_record = _adopt_record(fake, key)
    manager = _manager(fake)
    assert await manager.ensure_workroom(key) == name
    tees_before = len([c for c in fake.calls if "tee" in c or "/usr/bin/tee" in c])
    assert tees_before >= 1
    fake.image_id = "sha256:" + "1" * 64
    changed = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await changed.ensure_workroom(key)
    assert exc.value.reason_code == "shell_workroom_adoption_rejected"
    assert exc.value.uncertainty is False
    assert name in fake.containers
    assert len([c for c in fake.calls if "tee" in c or "/usr/bin/tee" in c]) == tees_before


async def test_adopt_rejects_foreign_container_untouched():
    """A same-name container without our labels is rejected, not mutated."""
    key = "run1.inst9"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": False, "startable": True}})
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.ensure_workroom(key)
    assert exc.value.reason_code == "shell_workroom_adoption_rejected"
    assert name in fake.containers
    assert fake.containers[name]["running"] is False
    assert not any(c[0] == "start" for c in fake.calls)
    assert not any("tee" in c or "/usr/bin/tee" in c for c in fake.calls)


async def test_adopt_rejects_relaxed_bounds():
    """Same key with looser resource bounds is rejected, never reused."""
    key = "run1.inst9"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": True}})
    fake.adopt_record = _adopt_record(fake, key)
    fake.adopt_record["host"]["Memory"] = 268435456
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.ensure_workroom(key)
    assert exc.value.reason_code == "shell_workroom_adoption_rejected"
    assert name in fake.containers
    assert not any("tee" in c or "/usr/bin/tee" in c for c in fake.calls)


async def test_adopt_rejects_added_caps():
    """Same key with added capabilities is rejected, never reused."""
    key = "run1.inst9"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": True}})
    fake.adopt_record = _adopt_record(fake, key)
    fake.adopt_record["host"]["CapAdd"] = ["SYS_PTRACE"]
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.ensure_workroom(key)
    assert exc.value.reason_code == "shell_workroom_adoption_rejected"
    assert name in fake.containers
    assert not any("tee" in c or "/usr/bin/tee" in c for c in fake.calls)


async def test_adopt_rejects_live_bridge_with_stale_network_mode():
    """A stale none NetworkMode with a live bridge attachment is rejected.

    Reproduces the disconnect-none/connect-bridge hole: HostConfig still
    reports NetworkMode none while NetworkSettings carries a live bridge
    endpoint. The guard reads the live attachments and rejects before any
    start/setup mutation, so the container is never touched.
    """
    key = "run1.inst9"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": True}})
    fake.adopt_record = _adopt_record(fake, key)
    fake.adopt_record["host"]["NetworkMode"] = "none"
    fake.adopt_record["networks"] = {
        "t005-bridge": {
            "EndpointID": "deadbeef" + "0" * 56,
            "IPAddress": "172.22.0.2",
        }
    }
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.ensure_workroom(key)
    assert exc.value.reason_code == "shell_workroom_adoption_rejected"
    assert exc.value.uncertainty is False
    assert name in fake.containers
    assert not any(c[0] == "start" for c in fake.calls)
    assert not any("tee" in c or "/usr/bin/tee" in c for c in fake.calls)


@pytest.mark.parametrize("extra_network", [False, True])
async def test_adoption_requires_exact_admission_owned_network(extra_network):
    key = "run1.inst9"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": True}})
    fake.adopt_record = _adopt_record(fake, key)
    fake.adopt_record["networks"] = {"owned-window": {}}
    if extra_network:
        fake.adopt_record["networks"]["foreign"] = {}

    class OwnedAdmission(ExecutionAdmission):
        def expected_networks(self, key: str) -> frozenset[str]:
            return frozenset({"owned-window"})

    manager = _manager(fake)
    manager.set_execution_admission(OwnedAdmission())
    if extra_network:
        with pytest.raises(ShellWorkroomError, match="shell_workroom_adoption_rejected"):
            await manager.ensure_workroom(key)
    else:
        assert await manager.ensure_workroom(key) == name


async def test_adopt_rejects_missing_networks_answer():
    """An unreadable live-network answer fails closed, never adopted."""
    key = "run1.inst9"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": True}})
    fake.adopt_record = _adopt_record(fake, key)
    fake.adopt_record["networks"] = None
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.ensure_workroom(key)
    assert exc.value.reason_code == "shell_workroom_adoption_rejected"
    assert name in fake.containers
    assert not any("tee" in c or "/usr/bin/tee" in c for c in fake.calls)


async def test_nan_timeout_is_rejected_without_starting_a_container():
    """A NaN timeout is rejected at the execution boundary, never run."""
    import math as _math
    fake = _FakeDocker()
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.exec_script("run1.inst1", "bash", "echo hi", _math.nan)
    assert exc.value.reason_code == "shell_timeout_out_of_range"
    assert exc.value.uncertainty is False
    assert not any(c[0] == "create" for c in fake.calls)
    assert not any(c[0] == "start" for c in fake.calls)
    assert not any(c[0] == "exec" for c in fake.calls)


async def test_inf_timeout_is_rejected_without_starting_a_container():
    """An infinite timeout is rejected at the execution boundary, never run."""
    import math as _math
    fake = _FakeDocker()
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.exec_script("run1.inst1", "bash", "echo hi", _math.inf)
    assert exc.value.reason_code == "shell_timeout_out_of_range"
    assert exc.value.uncertainty is False
    assert not any(c[0] == "create" for c in fake.calls)


async def test_unsafe_record_is_retained_for_audit():
    """An unsafe completion record is retained, not removed, on uncertainty."""
    fake = _FakeDocker()
    fake.exec_result = (0, b"hi", b"")
    fake.result_present = True
    fake.result_payload = {"outcome": "completed", "code": 0,
        "reaped": 0, "unreaped": [99], "subreaper": True,
        "protected": True}
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.exec_script("run1.inst1", "bash", "echo hi", 60.0)
    assert exc.value.uncertainty is True
    rm_calls = [c for c in fake.calls if "/bin/rm" in c and ".whs-" in " ".join(c)]
    assert rm_calls == []


async def test_supervisor_loss_holds_uncertain_slot():
    """Supervisor loss keeps uncertainty-active: slot held, marked, never clean."""
    workroom = _FakeWorkroom(uncertain_scripts={"boom"})
    adapter = BuiltinShellAdapter(_policy(max_jobs=1), workroom=workroom)
    by_name = _descriptor_map(build_shell_descriptors())
    started = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "boom"},
    )
    assert started.success is True
    job_id = started.output["job_id"]
    job = adapter._jobs[job_id]
    if job.task is not None:
        await asyncio.wait_for(asyncio.shield(job.task), 5.0)
    assert job.status == "uncertain"
    assert job.uncertain is True
    assert job.error == "shell_execution_interrupted"
    assert adapter._active_count() == 1
    status = await adapter.invoke(
        by_name[JOB_STATUS_TOOL_NAME],
        {"workroom_key": "run1.inst1", "job_id": job_id},
    )
    assert status.success is True
    assert status.output["status"] == "uncertain"
    assert status.metadata.get(EXECUTION_UNCERTAIN_METADATA_KEY) is True
    limited = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "other"},
    )
    assert limited.success is False
    assert limited.error.code == "shell_job_limit"
    await adapter.aclose()


async def test_missing_outcome_with_live_container_holds():
    """Outcome None plus a running container never frees the slot."""
    workroom = _FakeWorkroom(uncertain_scripts={"boom"}, container_state="running")
    adapter = BuiltinShellAdapter(_policy(max_jobs=1), workroom=workroom)
    by_name = _descriptor_map(build_shell_descriptors())
    started = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "boom"},
    )
    job_id = started.output["job_id"]
    job = adapter._jobs[job_id]
    if job.task is not None:
        await asyncio.wait_for(asyncio.shield(job.task), 5.0)
    assert job.status == "uncertain"
    job.created_at -= 3600.0
    workroom.settle_outcome = None
    limited = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "other"},
    )
    assert limited.success is False
    assert limited.error.code == "shell_job_limit"
    assert adapter._jobs[job_id].status == "uncertain"
    await adapter.aclose()


async def test_verified_outcome_settles_uncertain_job():
    """A supervisor-verified outcome ends uncertainty and frees the slot."""
    workroom = _FakeWorkroom(uncertain_scripts={"boom"}, container_state="running")
    adapter = BuiltinShellAdapter(_policy(max_jobs=1), workroom=workroom)
    by_name = _descriptor_map(build_shell_descriptors())
    started = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "boom"},
    )
    job_id = started.output["job_id"]
    job = adapter._jobs[job_id]
    if job.task is not None:
        await asyncio.wait_for(asyncio.shield(job.task), 5.0)
    assert job.status == "uncertain"
    workroom.settle_outcome = "cancelled"
    second = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "other"},
    )
    assert second.success is True
    settled = adapter._jobs[job_id]
    assert settled.status == "cancelled"
    assert settled.error == "shell_cancel_settled:cancelled"
    assert settled.uncertain is False
    status = await adapter.invoke(
        by_name[JOB_STATUS_TOOL_NAME],
        {"workroom_key": "run1.inst1", "job_id": job_id},
    )
    assert status.metadata.get(EXECUTION_UNCERTAIN_METADATA_KEY) is None
    await adapter.aclose()


async def test_gone_container_settles_without_supervisor_word():
    """Authoritative container absence ends uncertainty without an outcome."""
    workroom = _FakeWorkroom(uncertain_scripts={"boom"}, container_state="gone")
    adapter = BuiltinShellAdapter(_policy(max_jobs=1), workroom=workroom)
    by_name = _descriptor_map(build_shell_descriptors())
    started = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "boom"},
    )
    job_id = started.output["job_id"]
    job = adapter._jobs[job_id]
    if job.task is not None:
        await asyncio.wait_for(asyncio.shield(job.task), 5.0)
    assert job.status == "uncertain"
    job.created_at -= 3600.0
    workroom.settle_outcome = None
    second = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "other"},
    )
    assert second.success is True
    settled = adapter._jobs[job_id]
    assert settled.status == "cancelled"
    assert settled.error == "shell_container_torn_down"
    await adapter.aclose()


async def test_unprotected_record_is_uncertain():
    """A completed record from an unprotected run is never clean."""
    fake = _FakeDocker()
    fake.exec_result = (0, b"hi", b"")
    fake.result_present = True
    fake.result_payload = {"outcome": "completed", "code": 0,
        "reaped": 0, "unreaped": [], "subreaper": True,
        "protected": False}
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.exec_script("run1.inst1", "bash", "echo hi", 60.0)
    assert exc.value.reason_code == "shell_execution_interrupted"
    assert exc.value.uncertainty is True


async def test_unreaped_record_is_uncertain():
    """A completed record with survivors is never clean."""
    fake = _FakeDocker()
    fake.exec_result = (0, b"hi", b"")
    fake.result_present = True
    fake.result_payload = {"outcome": "completed", "code": 0,
        "reaped": 0, "unreaped": [99], "subreaper": True,
        "protected": True}
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.exec_script("run1.inst1", "bash", "echo hi", 60.0)
    assert exc.value.uncertainty is True


async def test_absent_record_with_clean_exit_is_uncertain():
    """Exit 0 with no completion record is tamper, never clean."""
    fake = _FakeDocker()
    fake.exec_result = (0, b"hi", b"")
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.exec_script("run1.inst1", "bash", "echo hi", 60.0)
    assert exc.value.uncertainty is True


async def test_settle_distrusts_forged_outcome():
    """A live writer forged outcome never settles the slot."""
    workroom = _FakeWorkroom(uncertain_scripts={"boom"})
    adapter = BuiltinShellAdapter(_policy(max_jobs=1), workroom=workroom)
    by_name = _descriptor_map(build_shell_descriptors())
    started = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "boom"},
    )
    job_id = started.output["job_id"]
    job = adapter._jobs[job_id]
    if job.task is not None:
        await asyncio.wait_for(asyncio.shield(job.task), 5.0)
    assert job.status == "uncertain"
    job.created_at -= 3600.0
    workroom.settle_outcome = "cancelled"
    workroom.record_protected = False
    limited = await adapter.invoke(
        by_name[JOB_START_TOOL_NAME],
        {"workroom_key": "run1.inst1", "language": "bash", "script": "other"},
    )
    assert limited.success is False
    assert limited.error.code == "shell_job_limit"
    assert adapter._jobs[job_id].status == "uncertain"
    await adapter.aclose()


async def test_subreaper_failure_record_is_uncertain():
    """A record without subreaper authority is never trusted."""
    fake = _FakeDocker()
    fake.exec_result = (0, b"hi", b"")
    fake.result_present = True
    fake.result_payload = {"outcome": "completed", "code": 0,
        "reaped": 0, "unreaped": [], "subreaper": False,
        "protected": True}
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.exec_script("run1.inst1", "bash", "echo hi", 60.0)
    assert exc.value.uncertainty is True


async def test_outer_timeout_without_record_is_uncertain(monkeypatch):
    """A vanished supervisor past the host wait is uncertainty, not timeout."""
    monkeypatch.setattr("redteam.capability.shell_workroom._HOST_OUTER_MARGIN_SECONDS", 0.1)
    fake = _FakeDocker()
    fake.exec_block = True
    fake.result_present = False
    manager = _manager(fake)
    with pytest.raises(ShellWorkroomError) as exc:
        await manager.exec_script("run1.inst1", "bash", "sleep 30", 0.2)
    assert exc.value.uncertainty is True


async def test_outer_timeout_with_trusted_record_honors_it(monkeypatch):
    """Host slowness with a trusted timeout record stays a clean timeout."""
    monkeypatch.setattr("redteam.capability.shell_workroom._HOST_OUTER_MARGIN_SECONDS", 0.1)
    fake = _FakeDocker()
    fake.exec_block = True
    fake.result_present = True
    fake.result_payload = {"outcome": "timeout", "code": 124,
        "reaped": 1, "unreaped": [], "subreaper": True,
        "protected": True}
