from __future__ import annotations

import asyncio
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.capability.servers.execution import (
    DockerExecutionEngine,
    ExecutionPolicy,
)
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId
from tests.integration.test_execution_agent_e2e import gateway_for

pytestmark = pytest.mark.skipif(
    os.environ.get("REDTEAM_TEST_EXECUTION_DOCKER") != "1",
    reason="set REDTEAM_TEST_EXECUTION_DOCKER=1 under WSL2/Linux with Docker",
)

_DOCKER_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "LANG": os.environ.get("LANG", "C.UTF-8"),
}
_LABEL = "io.whs-redteam.execution.managed=true"


def managed_container_ids() -> list[str]:
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label={_LABEL}"],
        capture_output=True,
        text=True,
        check=True,
        env=_DOCKER_ENV,
    )
    return result.stdout.split()


async def wait_for_containers(count: int) -> list[str]:
    for _ in range(100):
        identifiers = managed_container_ids()
        if len(identifiers) == count:
            return identifiers
        await asyncio.sleep(0.05)
    pytest.fail(f"expected {count} managed execution containers")


def process_rows(container_id: str) -> list[str]:
    result = subprocess.run(
        ["docker", "top", container_id, "-eo", "pid,ppid,comm,args"],
        capture_output=True,
        text=True,
        check=True,
        env=_DOCKER_ENV,
    )
    return [line for line in result.stdout.splitlines()[1:] if line.strip()]


def container_cpu_stat(container_id: str) -> dict[str, int]:
    result = subprocess.run(
        ["docker", "exec", container_id, "/usr/bin/cat", "/sys/fs/cgroup/cpu.stat"],
        capture_output=True,
        text=True,
        check=True,
        env=_DOCKER_ENV,
    )
    return {
        name: int(value)
        for line in result.stdout.splitlines()
        if len(parts := line.split()) == 2
        for name, value in [parts]
    }


async def wait_for_process_tree(container_id: str) -> list[str]:
    for _ in range(100):
        rows = process_rows(container_id)
        if sum("python" in row for row in rows) >= 2 and any(
            "sleep 30" in row for row in rows
        ):
            return rows
        await asyncio.sleep(0.05)
    pytest.fail("child and grandchild processes did not become observable")


async def test_cpu_quota_produces_observable_cgroup_throttling(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "busy_cpu.py").write_text(
        "import time\n"
        "deadline = time.monotonic() + 4\n"
        "while time.monotonic() < deadline:\n"
        "    pass\n",
        encoding="utf-8",
    )
    configured = runtime_policy(
        workspace,
        os.environ["REDTEAM_TEST_EXECUTION_PROCESS_IMAGE"],
        executables={"python": "/usr/local/bin/python"},
    )
    engine = DockerExecutionEngine(configured)
    task = asyncio.create_task(
        engine.run_command(
            "workspace-test", "python", ["busy_cpu.py"], 8, "eng-test", "run-test"
        )
    )
    container_id = (await wait_for_containers(1))[0]
    nano_cpus = subprocess.run(
        [
            "docker",
            "inspect",
            container_id,
            "--format",
            "{{.HostConfig.NanoCpus}}",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=_DOCKER_ENV,
    )
    assert int(nano_cpus.stdout.strip()) == 500_000_000
    before = container_cpu_stat(container_id)
    await asyncio.sleep(2)
    after = container_cpu_stat(container_id)
    result = await task

    assert result.exit_code == 0
    print(
        "execution_cpu_throttling "
        f"nr_throttled_delta={after['nr_throttled'] - before['nr_throttled']} "
        f"throttled_usec_delta={after['throttled_usec'] - before['throttled_usec']}"
    )
    assert after["nr_throttled"] > before["nr_throttled"]
    assert after["throttled_usec"] > before["throttled_usec"]
    assert await wait_for_containers(0) == []


def base_policy(workspace: Path, image: str) -> ExecutionPolicy:
    return ExecutionPolicy(
        asset_ref="workspace-test",
        engagement_id="eng-test",
        run_id="run-test",
        workspace_root=str(workspace.resolve()),
        image=image,
        allowed_executables={"head": "/usr/bin/head"},
        container_user=f"{os.getuid()}:{os.getgid()}",
        max_timeout_seconds=15,
    )


def runtime_policy(
    workspace: Path,
    image: str,
    *,
    executables: dict[str, str],
    options: dict[str, tuple[str, ...]] | None = None,
) -> ExecutionPolicy:
    values = base_policy(workspace, image).model_dump()
    values.update(
        allowed_executables=executables,
        allowed_options=options or {},
    )
    return ExecutionPolicy.model_construct(**values)


def invocation(
    name: str,
    operation: str,
    arguments: dict,
    *,
    run_id: str = "run-test",
    context_run_id: str | None = None,
) -> ToolInvocation:
    run = RunId(run_id)
    return ToolInvocation(
        invocation_id=InvocationId(f"inv-{name}-{run_id}"),
        action_id=ActionId(f"action-{name}-{run_id}"),
        run_id=run,
        tool_id=ToolId(f"analysis.{name}"),
        operation=operation,
        arguments=arguments,
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng-test"),
            run_id=RunId(context_run_id or run_id),
        ),
        created_at=datetime.now(timezone.utc),
    )


async def test_cancel_and_timeout_remove_child_and_grandchild_processes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    image = os.environ["REDTEAM_TEST_EXECUTION_PROCESS_IMAGE"]
    configured = runtime_policy(
        workspace,
        image,
        executables={"python": "/usr/local/bin/python"},
    )
    grandchild_code = (
        "import subprocess,time; "
        "subprocess.Popen(['sleep','30']); "
        "time.sleep(30)"
    )
    process_tree = (
        "import subprocess,time; "
        f"subprocess.Popen(['python','-c',{grandchild_code!r}]); "
        "time.sleep(30)"
    )
    (workspace / "process_tree.py").write_text(process_tree, encoding="utf-8")
    engine = DockerExecutionEngine(configured)
    cancel_task = asyncio.create_task(
        engine.run_command(
            "workspace-test", "python", ["process_tree.py"], 10, "eng-test", "run-test"
        )
    )
    container_id = (await wait_for_containers(1))[0]
    rows = await wait_for_process_tree(container_id)
    assert sum("python" in row for row in rows) >= 2
    assert any("sleep 30" in row for row in rows)

    cancel_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancel_task
    assert await wait_for_containers(0) == []

    timeout_task = asyncio.create_task(
        engine.run_command(
            "workspace-test", "python", ["process_tree.py"], 2, "eng-test", "run-test"
        )
    )
    timeout_container = (await wait_for_containers(1))[0]
    timeout_rows = await wait_for_process_tree(timeout_container)
    assert sum("python" in row for row in timeout_rows) >= 2
    assert any("sleep 30" in row for row in timeout_rows)
    result = await timeout_task
    assert result.timed_out is True
    assert await wait_for_containers(0) == []


async def test_workdir_environment_and_workspace_mount_are_fixed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = os.environ["REDTEAM_TEST_EXECUTION_IMAGE"]
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    (first / "input.txt").write_text("first-workspace\n", encoding="utf-8")
    (second / "input.txt").write_text("second-workspace\n", encoding="utf-8")
    monkeypatch.setenv("REDTEAM_HOST_SECRET", "must-not-enter-container")
    executables = {
        "pwd": "/usr/bin/pwd",
        "env": "/usr/bin/env",
        "head": "/usr/bin/head",
    }
    first_policy = runtime_policy(first, image, executables=executables)
    second_policy = runtime_policy(second, image, executables=executables)
    first_engine = DockerExecutionEngine(first_policy)
    second_engine = DockerExecutionEngine(second_policy)

    cwd = await first_engine.run_command(
        "workspace-test", "pwd", [], 5, "eng-test", "run-test"
    )
    environment = await first_engine.run_command(
        "workspace-test", "env", [], 5, "eng-test", "run-test"
    )
    first_read = await first_engine.run_command(
        "workspace-test", "head", ["input.txt"], 5, "eng-test", "run-test"
    )
    second_read = await second_engine.run_command(
        "workspace-test", "head", ["input.txt"], 5, "eng-test", "run-test"
    )

    assert cwd.stdout.strip() == "/workspace"
    environment_values = {
        line.partition("=")[0]: line.partition("=")[2]
        for line in environment.stdout.splitlines()
        if "=" in line
    }
    assert set(environment_values) == {"PATH", "LANG", "HOME", "HOSTNAME"}
    assert environment_values["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert environment_values["LANG"] == "C.UTF-8"
    assert environment_values["HOME"] == "/"
    assert environment_values["HOME"] != os.environ.get("HOME")
    assert re.fullmatch(r"[0-9a-f]{12}", environment_values["HOSTNAME"])
    assert not any(
        marker in key.upper()
        for key in environment_values
        for marker in ("SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "API_KEY")
    )
    assert "REDTEAM_HOST_SECRET" not in environment.stdout
    assert "first-workspace" in first_read.stdout
    assert "second-workspace" in second_read.stdout
    assert "second-workspace" not in first_read.stdout
    assert "first-workspace" not in second_read.stdout
    with pytest.raises(ValueError, match="execution_invalid_path"):
        await first_engine.run_command(
            "workspace-test",
            "head",
            ["../second/input.txt"],
            5,
            "eng-test",
            "run-test",
        )
    assert await wait_for_containers(0) == []


async def test_gateway_blocks_cross_run_and_cross_asset_job_access(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "input.txt").write_text("line\n", encoding="utf-8")
    configured = runtime_policy(
        workspace,
        os.environ["REDTEAM_TEST_EXECUTION_IMAGE"],
        executables={"tail": "/usr/bin/tail"},
    )
    gateway = await gateway_for(configured)
    started = await gateway.invoke(
        invocation(
            "job_start",
            "command_job_start",
            {
                "asset_ref": "workspace-test",
                "executable": "tail",
                "args": ["-f", "input.txt"],
                "timeout_seconds": 10,
            },
        )
    )
    assert started.success is True
    job_id = started.output["job_id"]
    await wait_for_containers(1)
    try:
        cross_run = await gateway.invoke(
            invocation(
                "job_status",
                "command_job_status",
                {"asset_ref": "workspace-test", "job_id": job_id},
                run_id="run-other",
            )
        )
        cross_asset = await gateway.invoke(
            invocation(
                "job_cancel",
                "command_job_cancel",
                {"asset_ref": "workspace-other", "job_id": job_id},
            )
        )
        assert cross_run.success is False
        assert cross_run.error.code == "execution_context_mismatch"
        assert cross_asset.success is False
        assert cross_asset.error.code == "execution_invalid_arguments"
        assert len(managed_container_ids()) == 1
    finally:
        cancelled = await gateway.invoke(
            invocation(
                "job_cancel",
                "command_job_cancel",
                {"asset_ref": "workspace-test", "job_id": job_id},
            )
        )
        assert cancelled.success is True
    assert await wait_for_containers(0) == []
