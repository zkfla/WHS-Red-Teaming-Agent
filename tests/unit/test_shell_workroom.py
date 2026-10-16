"""Focused boundary tests for the embedded shell-workroom supervisor."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import (
    _SUPERVISOR_FILENAME,
    _SUPERVISOR_SOURCE,
    ShellWorkroomError,
    ShellWorkroomManager,
    exec_argv,
)

_FAKE_SPAWN_HARNESS = r'''
import json
import subprocess
import sys

source_path, capture_path = sys.argv[1:3]
namespace = {"__name__": "embedded_supervisor_probe"}
with open(source_path, encoding="utf-8") as source_file:
    exec(compile(source_file.read(), source_path, "exec"), namespace)
namespace["_enable_subreaper"] = lambda: True

def fake_popen(argv, **kwargs):
    with open(capture_path, "w", encoding="utf-8") as capture_file:
        json.dump({"argv": argv}, capture_file)
    raise OSError("test stops before executing a child")

subprocess.Popen = fake_popen
raise SystemExit(namespace["main"]())
'''


def _embedded_supervisor(
    tmp_path, argument_bytes: int, *, trusted_limit: int | None,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[bytes], list[str] | None]:
    source_path = tmp_path / _SUPERVISOR_FILENAME
    source_path.write_text(_SUPERVISOR_SOURCE, encoding="utf-8")
    capture_path = tmp_path / "spawn.json"
    run_dir = tmp_path / "run"
    environment = os.environ.copy()
    for name in ("WHS_ARGV_LIMIT", "WHS_EXTRA_ENV"):
        environment.pop(name, None)
    environment.update({
        "WHS_RUN_ID": "aabbccddeeff0011",
        "WHS_LANG": "bash",
        "WHS_EXECUTION_MODE": "program",
        "WHS_RUN_UID": "65532",
        "WHS_RUN_GID": "65532",
        "WHS_WORKDIR": str(tmp_path),
        "WHS_RUNDIR": str(run_dir),
        "WHS_TIMEOUT_S": "2",
        "WHS_GRACE_S": "1",
    })
    if trusted_limit is not None:
        environment["WHS_ARGV_LIMIT"] = str(trusted_limit)
    if extra_env is not None:
        environment["WHS_EXTRA_ENV"] = json.dumps(extra_env, separators=(",", ":"))

    argument = "x" * argument_bytes
    executable = os.path.abspath(str(tmp_path / "fake-program"))
    header = json.dumps({"argv": [executable, argument]}, separators=(",", ":"))
    result = subprocess.run(
        [sys.executable, "-I", "-c", _FAKE_SPAWN_HARNESS,
         str(source_path), str(capture_path)],
        input=header.encode("utf-8") + b"\n",
        capture_output=True,
        env=environment,
        timeout=10.0,
        check=False,
    )
    captured_argv = None
    if capture_path.exists():
        captured_argv = json.loads(capture_path.read_text(encoding="utf-8"))["argv"]
    return result, captured_argv


def test_exec_argv_carries_only_bounded_private_provider_allowance():
    arguments = {
        "container_name": "whs-shell-test",
        "language": "bash",
        "timeout_seconds": 10.0,
        "grace_seconds": 2.0,
        "run_id": "aabbccddeeff0011",
        "workdir": "/workroom",
        "run_uid": 65532,
        "run_gid": 65532,
        "execution_mode": "program",
    }
    ordinary = exec_argv(**arguments)
    assert "WHS_ARGV_LIMIT=4096" in ordinary
    assert ".whs-supervisor-v4.py" in ordinary[-1]

    provider = exec_argv(**arguments, _provider_argv_limit=65536)
    assert "WHS_ARGV_LIMIT=65536" in provider

    with pytest.raises(ShellWorkroomError):
        exec_argv(**arguments, _provider_argv_limit=65537)
    with pytest.raises(ShellWorkroomError):
        exec_argv(**arguments, _provider_argv_limit=65536.0)
    with pytest.raises(ShellWorkroomError):
        exec_argv(**{**arguments, "execution_mode": "script"}, _provider_argv_limit=65536)


async def test_exec_program_propagates_trusted_provider_allowance(monkeypatch):
    manager = ShellWorkroomManager(ShellWorkroomPolicy(
        image="python:3.11-slim", allow_unpinned_image=True,
    ))
    captured = {}

    async def ensure(_key):
        return "whs-shell-test"

    async def run_docker(argv, _stdin, _timeout, _output_limit):
        captured["argv"] = argv
        return 0, b"", b"", False, False

    async def classify(*_args, **_kwargs):
        return False

    monkeypatch.setattr(manager, "ensure_workroom", ensure)
    monkeypatch.setattr(manager, "_run_docker", run_docker)
    monkeypatch.setattr(manager, "_classify_exit", classify)

    await manager.exec_program(
        "run1.inst1", "/usr/bin/python3", ["x" * 8192],
        _provider_argv_limit=65536,
    )
    assert "WHS_ARGV_LIMIT=65536" in captured["argv"]


@pytest.mark.parametrize(
    ("argument_bytes", "trusted_limit", "extra_env", "spawn_expected"),
    [
        (8192, 65536, None, True),
        (65537, 65536, None, False),
        (4096, None, None, True),
        (4097, None, None, False),
        (8192, 4096, {"WHS_ARGV_LIMIT": "65536"}, False),
    ],
)
def test_embedded_supervisor_enforces_private_per_argument_limit(
    tmp_path, argument_bytes, trusted_limit, extra_env, spawn_expected,
):
    result, captured_argv = _embedded_supervisor(
        tmp_path, argument_bytes, trusted_limit=trusted_limit, extra_env=extra_env,
    )

    # The fake Popen always raises after recording, so accepted requests also
    # return 127. Reaching it proves validation passed without executing a target.
    assert result.returncode == 127
    assert (captured_argv is not None) is spawn_expected
    if spawn_expected:
        assert len(captured_argv[1].encode("utf-8")) == argument_bytes
    else:
        assert not (tmp_path / "run" / ".whs-result-aabbccddeeff0011").exists()
