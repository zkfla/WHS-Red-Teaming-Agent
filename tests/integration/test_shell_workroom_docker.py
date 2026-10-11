"""Local-Docker validation for the shell workroom. Owned by Capability Team.

TASK-003 scope only: warm stateful execution, sync/job semantics, and
network isolation, all inside local Docker. No remote target is ever
contacted: the isolation check reads the container-local interface list
only. Tests skip when docker or the configured image is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid

import pytest

from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_manifest import build_shell_descriptors
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import (
    ShellWorkroomError,
    ShellWorkroomManager,
    container_name_for,
)

IMAGE = os.environ.get("SHELL_TEST_IMAGE", "python:3.11-slim")

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker unavailable")


def _policy(**overrides):
    args = {
        "image": IMAGE,
        "allow_unpinned_image": True,
        "default_timeout_seconds": 30.0,
        "max_timeout_seconds": 120.0,
    }
    args.update(overrides)
    return ShellWorkroomPolicy(**args)


async def _image_available(image: str) -> bool:
    """Image presence without touching manager privates."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "images",
        "-q",
        image,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return proc.returncode == 0 and bool(out.strip())


@pytest.fixture()
async def room():
    manager = ShellWorkroomManager(_policy())
    if not await manager.probe():
        pytest.skip("docker daemon unavailable")
    if not await _image_available(IMAGE):
        pytest.skip("test image unavailable: " + IMAGE)
    key = "t003." + uuid.uuid4().hex[:12]
    yield manager, key
    await manager.remove_workroom(key)


async def test_bash_state_persists_across_calls(room):
    manager, key = room
    first = await manager.exec_script(key, "bash", "echo hello > /workroom/t003_state.txt", 20.0)
    assert first.exit_code == 0
    assert first.timed_out is False
    second = await manager.exec_script(key, "bash", "cat /workroom/t003_state.txt", 20.0)
    assert second.stdout == "hello\n"
    assert second.exit_code == 0


async def test_python_arithmetic(room):
    manager, key = room
    result = await manager.exec_script(key, "python3", "print(6 * 7)", 20.0)
    assert result.exit_code == 0
    assert result.stdout == "42\n"


async def test_container_has_loopback_only(room):
    """Network-none has no active/addressed/routed non-loopback interface.

    Linux may expose DOWN, addressless tunnel devices in a network-none
    namespace. Their names are not proof of an egress connection. Inspect
    actual namespace flags, both address families and routes instead.
    """
    manager, key = room
    script = '''
import fcntl, json, socket, struct
interfaces = []
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    for index, name in socket.if_nameindex():
        request = struct.pack("256s", name.encode())
        flags = struct.unpack_from("H", fcntl.ioctl(sock, 0x8913, request), 16)[0]
        try:
            address = socket.inet_ntoa(fcntl.ioctl(sock, 0x8915, request)[20:24])
        except OSError:
            address = None
        interfaces.append({"name": name, "index": index, "up": bool(flags & 1),
                           "loopback": bool(flags & 8), "ipv4": address})
with open("/proc/net/if_inet6") as file:
    addresses6 = [line.split()[-1] for line in file if line.strip()]
with open("/proc/net/route") as file:
    routes4 = [line.split()[0] for line in file.read().splitlines()[1:] if line.strip()]
with open("/proc/net/ipv6_route") as file:
    routes6 = [line.split()[-1] for line in file if line.strip()]
print(json.dumps({"interfaces": interfaces, "addresses6": addresses6,
                  "routes4": routes4, "routes6": routes6}))
'''
    result = await manager.exec_script(key, "python3", script, 20.0)
    assert result.exit_code == 0
    observed = json.loads(result.stdout)
    loopback = {item["name"] for item in observed["interfaces"] if item["loopback"]}
    assert loopback
    assert all(not item["up"] and item["ipv4"] is None
               for item in observed["interfaces"] if not item["loopback"])
    assert set(observed["addresses6"] + observed["routes4"] + observed["routes6"]) <= loopback
    assert await manager.verify_workroom_ownership(key)


async def test_sync_timeout_is_reported(room):
    manager, key = room
    result = await manager.exec_script(key, "bash", "sleep 30", 2.0)
    assert result.timed_out is True


async def test_adapter_job_cancel_against_real_workroom(room):
    """Cancel stops the wait and the container-side processes really die.

    The script proves it started (marker plus child pid) before cancel
    runs, so the test cannot pass on a not-yet-started execution.
    """
    manager, key = room
    adapter = BuiltinShellAdapter(_policy(max_jobs=1), workroom=manager)
    by_name = {d.tool_name: d for d in build_shell_descriptors(adapter.policy)}
    try:
        script = (
            "echo started > /workroom/t003_cancel_marker.txt; "
            "sleep 60 & echo $! > /workroom/t003_cancel.pid; wait"
        )
        started = await adapter.invoke(
            by_name["shell_job_start"],
            {
                "workroom_key": key,
                "language": "bash",
                "script": script,
                "timeout_seconds": 8.0,
            },
        )
        assert started.success is True
        job_id = started.output["job_id"]
        # Prove the script actually started before cancelling.
        marker = None
        for _ in range(30):
            marker = await manager.exec_script(
                key, "bash", "cat /workroom/t003_cancel_marker.txt", 8.0
            )
            if marker.exit_code == 0 and marker.stdout.strip() == "started":
                break
            await asyncio.sleep(1.0)
        assert marker is not None and marker.stdout.strip() == "started"
        pid_probe = await manager.exec_script(key, "bash", "cat /workroom/t003_cancel.pid", 8.0)
        assert pid_probe.exit_code == 0
        child_pid = pid_probe.stdout.strip()
        assert child_pid.isdigit()
        cancelled = await adapter.invoke(
            by_name["shell_job_cancel"],
            {"workroom_key": key, "job_id": job_id},
        )
        assert cancelled.success is True
        assert cancelled.output["cancelled"] is True
        assert cancelled.output["status"] == "cancelled"
        assert cancelled.metadata["container_teardown_verified"] is True
        status = await adapter.invoke(
            by_name["shell_job_status"],
            {"workroom_key": key, "job_id": job_id},
        )
        assert status.output["status"] == "cancelled"
        # The verified cancel frees the ACTIVE slot at once: the next
        # start succeeds even though the terminal record persists.
        assert (await manager.exec_script(key, "bash", "echo next", 8.0)).stdout == "next\n"
        # The child process itself is really gone (bounded death, not a
        # leak): its pid no longer signals.
        dead = None
        for _ in range(25):
            gone = await manager.exec_script(
                key, "bash", "kill -0 " + child_pid + " 2>/dev/null; echo gone=$?", 8.0,
            )
            assert gone.exit_code == 0
            if "gone=1" in gone.stdout:
                dead = True
                break
            await asyncio.sleep(1.0)
        assert dead is True
    finally:
        await adapter.aclose()


async def test_run_teardown_removes_container(room):
    manager, key = room
    await manager.exec_script(key, "bash", "echo hi", 20.0)
    assert await manager.remove_workroom(key) is True
    assert await manager.remove_workroom(key) is True


async def test_term_ignoring_script_times_out(room):
    """S1: a TERM trap escalates to KILL (137) and still reports timeout."""
    manager, key = room
    script = "trap " + chr(39) + chr(39) + " TERM; sleep 30"
    result = await manager.exec_script(key, "bash", script, 2.0)
    assert result.timed_out is True
    assert result.exit_code is None


async def test_self_exit_124_is_not_a_timeout(room):
    """S1: exit code alone never proves a timeout."""
    manager, key = room
    result = await manager.exec_script(key, "bash", "exit 124", 20.0)
    assert result.timed_out is False
    assert result.exit_code == 124


async def test_workroom_writable_exec_surface(room):
    """S2: /workroom is writable+exec; HOME/PYTHONUSERBASE point at it."""
    manager, key = room
    script = (
        "echo HOME=$HOME; echo PYTHONUSERBASE=$PYTHONUSERBASE; "
        "mkdir -p /workroom/t003sub && echo data > /workroom/t003sub/f.txt && "
        "cat /workroom/t003sub/f.txt && cp /bin/echo /workroom/t003echo && "
        "chmod +x /workroom/t003echo && /workroom/t003echo hello-surface"
    )
    result = await manager.exec_script(key, "bash", script, 20.0)
    assert result.exit_code == 0, result.stderr
    assert "HOME=/workroom" in result.stdout
    assert "PYTHONUSERBASE=/workroom/.local" in result.stdout
    assert "hello-surface" in result.stdout


async def test_tmpfs_stays_noexec(room):
    """S2: the /tmp tmpfs keeps noexec while /workroom executes."""
    manager, key = room
    script = "cp /bin/echo /tmp/t003echo && chmod +x /tmp/t003echo && /tmp/t003echo hi"
    result = await manager.exec_script(key, "bash", script, 20.0)
    assert result.exit_code != 0


async def test_restarted_manager_adopts_workroom(room):
    """S5: a fresh manager adopts the live container, state included."""
    manager, key = room
    first = await manager.exec_script(key, "bash", "echo persist > /workroom/t003_adopt.txt", 20.0)
    assert first.exit_code == 0
    fresh = ShellWorkroomManager(manager.policy)
    assert fresh.known_workrooms() == ()
    again = await fresh.exec_script(key, "bash", "cat /workroom/t003_adopt.txt", 20.0)
    assert again.stdout == "persist\n"
    assert key in fresh.known_workrooms()


async def test_stopped_workroom_is_revived_by_fresh_manager(room):
    """S5: a stopped same-name container is started and adopted."""
    manager, key = room
    await manager.exec_script(key, "bash", "echo hi", 20.0)
    stop = await asyncio.create_subprocess_exec(
        "docker", "stop", container_name_for(key),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert await stop.wait() == 0
    fresh = ShellWorkroomManager(manager.policy)
    revived = await fresh.exec_script(key, "bash", "echo back", 20.0)
    assert revived.exit_code == 0
    assert revived.stdout == "back\n"


async def test_execution_cleanup_leaves_workroom_alive(room, tmp_path):
    """Label split: execution orphan cleanup never matches shell workrooms."""
    from redteam.capability.servers.execution import ExecutionPolicy, cleanup_orphaned_containers

    manager, key = room
    await manager.exec_script(key, "bash", "echo hi", 20.0)
    policy = ExecutionPolicy(
        asset_ref="asset1",
        engagement_id="eng1",
        run_id="run1",
        workspace_root=str(tmp_path),
        image="python@sha256:" + "0" * 64,
        allowed_executables={"echo": "/usr/bin/echo"},
    )
    cleanup_orphaned_containers(policy)
    assert await manager._is_running(container_name_for(key)) is True
    alive = await manager.exec_script(key, "bash", "echo alive", 20.0)
    assert alive.stdout == "alive\n"


async def _wait_for_file(manager, key, path, content, tries=30):
    """Poll a container file until it holds the expected content."""
    probe = None
    for _ in range(tries):
        probe = await manager.exec_script(key, "bash", "cat " + path, 8.0)
        if probe.exit_code == 0 and probe.stdout.strip() == content:
            return probe
        await asyncio.sleep(1.0)
    raise AssertionError("file did not appear: " + path)


async def _wait_pid_gone(manager, key, pid, tries=25):
    """Poll until kill -0 reports the container pid dead."""
    for _ in range(tries):
        gone = await manager.exec_script(
            key, "bash", "kill -0 " + pid + " 2>/dev/null; echo gone=$?", 8.0,
        )
        assert gone.exit_code == 0
        if "gone=1" in gone.stdout:
            return True
        await asyncio.sleep(1.0)
    raise AssertionError("container pid survived: " + pid)


async def test_cancel_kills_env_scrubbed_setsid_child(room):
    """A scrubbed tag plus a new session does not escape cancel.

    The script unsets its tag BEFORE spawning, then leaves the session:
    the old environ scan could find nothing at all. The supervisor owns
    its child via subreaper plus session plus sweep, so the kill lands.
    """
    manager, key = room
    adapter = BuiltinShellAdapter(_policy(max_jobs=4), workroom=manager)
    by_name = {d.tool_name: d for d in build_shell_descriptors(adapter.policy)}
    try:
        script = (
            "echo started > /workroom/t003_scrub_marker.txt; "
            "unset WHS_SHELL_JOB; "
            "setsid sleep 45 & echo $! > /workroom/t003_scrub.pid; wait"
        )
        started = await adapter.invoke(
            by_name["shell_job_start"],
            {"workroom_key": key, "language": "bash",
             "script": script, "timeout_seconds": 60.0},
        )
        assert started.success is True
        job_id = started.output["job_id"]
        await _wait_for_file(manager, key, "/workroom/t003_scrub_marker.txt", "started")
        pid_probe = await manager.exec_script(key, "bash", "cat /workroom/t003_scrub.pid", 8.0)
        child_pid = pid_probe.stdout.strip()
        assert child_pid.isdigit()
        cancelled = await adapter.invoke(
            by_name["shell_job_cancel"],
            {"workroom_key": key, "job_id": job_id},
        )
        assert cancelled.success is True
        assert cancelled.output["cancelled"] is True
        assert cancelled.metadata["container_teardown_verified"] is True
        assert await _wait_pid_gone(manager, key, child_pid) is True
        original = await manager.supervisor_record(key, job_id)
        assert original is not None and manager.record_trusted(original)
        assert original["outcome"] == "cancelled"
    finally:
        await adapter.aclose()


async def test_self_exit_137_is_not_a_timeout(room):
    """A genuine SIGKILL self-exit carries its completed record."""
    manager, key = room
    result = await manager.exec_script(key, "bash", "kill -KILL $$", 20.0)
    assert result.timed_out is False
    assert result.exit_code == 137


async def test_retained_terminal_record_does_not_signal_reused_pgid(room):
    """An old PGID number is not authority over a later sibling job."""
    manager, key = room
    original_tag = "settled-" + uuid.uuid4().hex[:12]
    settled = await manager.exec_script(
        key, "bash", "echo settled", 8.0, exec_tag=original_tag, retain_record=True,
    )
    assert settled.exit_code == 0
    adapter = BuiltinShellAdapter(_policy(max_jobs=4), workroom=manager)
    by_name = {d.tool_name: d for d in build_shell_descriptors(adapter.policy)}
    try:
        started = await adapter.invoke(by_name["shell_job_start"], {
            "workroom_key": key, "language": "bash", "timeout_seconds": 90,
            "script": "setsid sleep 80 & echo $! > /workroom/sibling.pid; wait",
        })
        assert started.success is True
        pid = None
        for _ in range(30):
            probe = await manager.exec_script(key, "bash", "cat /workroom/sibling.pid", 8.0)
            if probe.exit_code == 0 and probe.stdout.strip().isdigit():
                pid = probe.stdout.strip()
                break
            await asyncio.sleep(0.1)
        assert pid is not None
        # Root-only fixture manipulation simulates kernel numeric PGID reuse;
        # untrusted scripts cannot write these protected records themselves.
        pgid_path = "/run/whs-shell/.whs-pgid-" + original_tag
        written = await manager._run_docker(
            ("exec", "--user", "0", "-i", container_name_for(key), "/usr/bin/tee", pgid_path),
            pid.encode(), 8.0, 1024,
        )
        assert written[0] == 0
        report = await manager.kill_tagged_exec(key, original_tag)
        assert report.verified and report.outcome == "completed"
        alive = await manager.exec_script(key, "bash", "kill -0 " + pid, 8.0)
        assert alive.exit_code == 0, "an old terminal PGID must not kill a live sibling"
    finally:
        await adapter.aclose()


async def test_background_child_is_settled_before_completion(room):
    """A live TERM-immune background child never outlives its exec.

    The parent exits 0 at once; the supervisor settles (KILL plus reap)
    the whole tree before writing the completion record, so the result
    is honestly completed and nothing leaks.
    """
    manager, key = room
    script = (
            "(trap '' TERM; exec sleep 300) & echo $! > /workroom/t003_bg.pid; "
            "exit 0"
    )
    result = await manager.exec_script(key, "bash", script, 20.0)
    assert result.timed_out is False
    assert result.exit_code == 0
    pid_probe = await manager.exec_script(key, "bash", "cat /workroom/t003_bg.pid", 8.0)
    child_pid = pid_probe.stdout.strip()
    assert child_pid.isdigit()
    assert await _wait_pid_gone(manager, key, child_pid) is True


async def test_sync_cancel_settles_container_side(room):
    """Cancelling a sync shell_run leaves no container-side survivor."""
    manager, key = room
    script = (
        "echo started > /workroom/t003_sync_marker.txt; "
        "sleep 60 & echo $! > /workroom/t003_sync.pid; wait"
    )
    task = asyncio.create_task(manager.exec_script(key, "bash", script, 60.0))
    try:
        await _wait_for_file(manager, key, "/workroom/t003_sync_marker.txt", "started")
        pid_probe = await manager.exec_script(key, "bash", "cat /workroom/t003_sync.pid", 8.0)
        child_pid = pid_probe.stdout.strip()
        assert child_pid.isdigit()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _wait_pid_gone(manager, key, child_pid) is True
    finally:
        if not task.done():
            task.cancel()


async def test_install_state_survives_stop_start(room):
    """Per-instance installs persist across container stop and start."""
    manager, key = room
    script = (
        "printf '#!/bin/sh\necho persisted-prog\n' > /workroom/t003prog && "
        "chmod +x /workroom/t003prog && /workroom/t003prog"
    )
    first = await manager.exec_script(key, "bash", script, 20.0)
    assert first.exit_code == 0
    assert "persisted-prog" in first.stdout
    stop = await asyncio.create_subprocess_exec(
        "docker", "stop", container_name_for(key),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert await stop.wait() == 0
    fresh = ShellWorkroomManager(manager.policy)
    revived = await fresh.exec_script(key, "bash", "/workroom/t003prog", 20.0)
    assert revived.exit_code == 0
    assert revived.stdout == "persisted-prog\n"


async def test_foreign_uid_writes_and_executes_in_workroom(room):
    """A non-default policy uid installs and executes in HOME/.local."""
    manager, key = room
    foreign = ShellWorkroomManager(_policy(container_user="65531:65531"))
    script = (
        "mkdir -p /workroom/.local/uid-policy-check && "
        "printf '#!/bin/sh\necho uid-prog\n' > /workroom/.local/bin/uid-prog && "
        "chmod +x /workroom/.local/bin/uid-prog && uid-prog"
    )
    try:
        result = await foreign.exec_script(key + ".uid31", "bash", script, 30.0)
        assert result.exit_code == 0, result.stderr
        assert "uid-prog" in result.stdout
    finally:
        await foreign.remove_workroom(key + ".uid31")


async def test_process_level_cap_with_two_managers(room, tmp_path):
    """Two OS processes racing a cap of 1 leave exactly one container."""
    import sys

    manager, key = room
    prefix = "t003cap." + uuid.uuid4().hex[:8]
    helper = os.path.join(os.path.dirname(__file__), "shell_cap_worker.py")
    image = manager.policy.image
    first = await asyncio.create_subprocess_exec(
        sys.executable, helper, image, prefix + ".one", "1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": os.path.join(os.getcwd(), "src")},
    )
    second = await asyncio.create_subprocess_exec(
        sys.executable, helper, image, prefix + ".two", "1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": os.path.join(os.getcwd(), "src")},
    )
    try:
        (out1, _), (out2, _) = await asyncio.gather(first.communicate(), second.communicate())
        wins = [o for o in (out1.decode(), out2.decode()) if o.startswith("OK")]
        losses = [o for o in (out1.decode(), out2.decode()) if o.startswith("FAIL")]
        assert len(wins) == 1
        assert len(losses) == 1
        assert "shell_too_many_workrooms" in losses[0]
        listed = await manager._list_managed()
        mine = [context for _name, context in listed if context.startswith(prefix)]
        assert len(mine) == 1
    finally:
        for suffix in (".one", ".two"):
            try:
                await manager.remove_workroom(prefix + suffix)
            except Exception:
                pass


KALI_IMAGE = os.environ.get("SHELL_KALI_IMAGE")
needs_kali = pytest.mark.skipif(not KALI_IMAGE, reason="SHELL_KALI_IMAGE unset")


def _kali_policy(**overrides):
    args = {
        "image": KALI_IMAGE,
        "allow_unpinned_image": True,
        "default_timeout_seconds": 60.0,
        "max_timeout_seconds": 300.0,
    }
    args.update(overrides)
    return ShellWorkroomPolicy(**args)


@needs_kali
async def test_kali_analysis_programs_run_offline():
    """Baseline programs execute locally with no network."""
    manager = ShellWorkroomManager(_kali_policy())
    key = "t3kali.gateprog"
    try:
        if not await manager.probe():
            pytest.skip("docker daemon unavailable")
        versions = await manager.exec_script(
            key, "bash",
            "for c in nmap curl jq git dig openssl whois tcpdump sqlmap; do "
            "$c --version 2>&1 || $c --help 2>&1 || $c -v 2>&1; echo \"== $c exit=$?\"; done",
            60.0)
        assert versions.exit_code == 0, versions.stderr
        assert "Nmap version" in versions.stdout
        for line in versions.stdout.splitlines():
            if line.startswith("=="):
                assert line.rstrip().endswith("exit=0"), line
        rockyou = await manager.exec_script(
            key, "bash", "ls /usr/share/wordlists/rockyou.txt.gz", 30.0)
        assert rockyou.exit_code == 0
        scan = await manager.exec_script(
            key, "bash", "nmap -sT -p 22 127.0.0.1 --host-timeout 20s", 60.0)
        assert scan.exit_code == 0
        assert "Nmap done" in scan.stdout
    finally:
        await manager.remove_workroom(key)


@needs_kali
async def test_kali_manifest_matches_configured_image():
    """The generated manifest round-trips against the configured image."""
    manager = ShellWorkroomManager(_kali_policy())
    key = "t3kali.gatemanifest"
    try:
        if not await manager.probe():
            pytest.skip("docker daemon unavailable")
        await manager.ensure_workroom(key)
        records = await manager.list_image_programs(os.environ.get("SHELL_KALI_MANIFEST"))
        by_name = {r.name: r for r in records}
        assert "dig" in by_name and "nmap" in by_name
        assert by_name["dig"].version is not None
    finally:
        await manager.remove_workroom(key)


@needs_kali
async def test_kali_venv_and_local_bin_survive_restart():
    """Venv installs and bare-name .local/bin persist across stop/start."""
    manager = ShellWorkroomManager(_kali_policy())
    key = "t3kali.gatevenv"
    try:
        if not await manager.probe():
            pytest.skip("docker daemon unavailable")
        created = await manager.exec_script(
            key, "bash",
            "python3 -m venv --system-site-packages /workroom/gatevenv && "
            "/workroom/gatevenv/bin/python -c 'print(6*7)'", 240.0)
        assert created.exit_code == 0
        assert "42" in created.stdout
        placed = await manager.exec_script(
            key, "bash",
            "printf '#!/bin/sh\\necho gate-prog\\n' > /workroom/.local/bin/gate-prog && "
            "chmod +x /workroom/.local/bin/gate-prog && gate-prog", 60.0)
        assert placed.exit_code == 0
        assert "gate-prog" in placed.stdout
        stop = await asyncio.create_subprocess_exec(
            "docker", "stop", container_name_for(key),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        assert await stop.wait() == 0
        start = await asyncio.create_subprocess_exec(
            "docker", "start", container_name_for(key),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        assert await start.wait() == 0
        revived = await manager.exec_script(
            key, "bash", "/workroom/gatevenv/bin/python -c 'print(6*7)' && gate-prog", 120.0)
        assert revived.exit_code == 0
        assert "42" in revived.stdout and "gate-prog" in revived.stdout
    finally:
        await manager.remove_workroom(key)


@needs_kali
async def test_kali_foreign_uid_installs_and_runs():
    """Policy UID 65531 writes and executes on the Kali image too."""
    manager = ShellWorkroomManager(_kali_policy(container_user="65531:65531"))
    key = "t3kali.gateuid31"
    try:
        if not await manager.probe():
            pytest.skip("docker daemon unavailable")
        result = await manager.exec_script(
            key, "bash",
            "id -u && mkdir -p /workroom/.local/uid-gate && "
            "printf '#!/bin/sh\\necho uid31-prog\\n' > /workroom/.local/bin/uid31-prog && "
            "chmod +x /workroom/.local/bin/uid31-prog && uid31-prog", 120.0)
        assert result.exit_code == 0, result.stderr
        assert "65531" in result.stdout
        assert "uid31-prog" in result.stdout
    finally:
        await manager.remove_workroom(key)


@needs_kali
async def test_kali_child_runs_demoted_and_tamper_fails():
    """Same privilege-drop and tamper guarantees on the Kali image."""
    manager = ShellWorkroomManager(_kali_policy())
    key = "t3kali.gatetamper"
    try:
        if not await manager.probe():
            pytest.skip("docker daemon unavailable")
        probe = await manager.exec_script(
            key, "bash",
            "echo \"uid=$(id -u):$(id -g)\"; "
            "grep CapEff /proc/self/status; "
            "grep NoNewPrivs /proc/self/status; "
            "kill -9 $PPID 2>/dev/null; echo \"killrc=$?\"; "
            "touch /run/whs-shell/t3poison 2>/dev/null; echo \"touchrc=$?\"; "
            "echo survived",
            60.0,
        )
        assert probe.exit_code == 0, probe.stderr
        assert "uid=65532:65532" in probe.stdout
        out_lines = probe.stdout.splitlines()
        cap_line = next(line for line in out_lines if line.startswith("CapEff:"))
        assert cap_line.rstrip().endswith("0000000000000000")
        nnp_line = next(line for line in out_lines if line.startswith("NoNewPrivs:"))
        assert nnp_line.rstrip().endswith("1")
        assert "killrc=1" in probe.stdout
        assert "touchrc=1" in probe.stdout
    finally:
        await manager.remove_workroom(key)


async def test_changed_image_is_rejected_not_reused():
    """Same key with a different policy image is rejected before mutation."""
    second_image = "python:3.12-slim-bookworm"
    if not await _image_available(second_image):
        pytest.skip("test image unavailable: " + second_image)
    manager = ShellWorkroomManager(_policy())
    key = "t003." + uuid.uuid4().hex[:12]
    try:
        if not await manager.probe():
            pytest.skip("docker daemon unavailable")
        name = await manager.ensure_workroom(key)
        before = await manager.exec_script(key, "bash", "echo marker", 20.0)
        assert before.stdout == "marker\n"
        other = ShellWorkroomManager(_policy(image=second_image))
        with pytest.raises(ShellWorkroomError) as exc:
            await other.ensure_workroom(key)
        assert exc.value.reason_code == "shell_workroom_adoption_rejected"
        kept = await manager.exec_script(key, "bash", "echo marker", 20.0)
        assert kept.stdout == "marker\n"
        assert container_name_for(key) == name
    finally:
        await manager.remove_workroom(key)


async def test_foreign_same_name_container_is_rejected_untouched():
    """A same-name container without our labels is rejected, never adopted."""
    manager = ShellWorkroomManager(_policy())
    if not await manager.probe():
        pytest.skip("docker daemon unavailable")
    key = "t003." + uuid.uuid4().hex[:12]
    name = container_name_for(key)
    created = await asyncio.create_subprocess_exec(
        "docker", "create", "--name", name, "--network", "bridge",
        IMAGE, "sleep", "infinity",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    assert await created.wait() == 0
    try:
        with pytest.raises(ShellWorkroomError) as exc:
            await manager.ensure_workroom(key)
        assert exc.value.reason_code == "shell_workroom_adoption_rejected"
        check = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Running}}", name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await check.communicate()
        assert out.strip() == b"false"
    finally:
        gone = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await gone.wait()


async def test_live_bridge_attachment_is_rejected_despite_stale_network_mode():
    """Disconnect-none/connect-bridge leaves a live route the guard must catch.

    HostConfig.NetworkMode can stay stale at none while NetworkSettings
    carries a live bridge endpoint with a default route. Adoption must
    read the live attachments and reject before any mutation.
    """
    manager = ShellWorkroomManager(_policy())
    if not await manager.probe():
        pytest.skip("docker daemon unavailable")
    key = "t003." + uuid.uuid4().hex[:12]
    name = container_name_for(key)
    await manager.ensure_workroom(key)
    try:
        disc = await asyncio.create_subprocess_exec(
            "docker", "network", "disconnect", "none", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        assert await disc.wait() == 0
        conn = await asyncio.create_subprocess_exec(
            "docker", "network", "connect", "bridge", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        assert await conn.wait() == 0
        nets = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        nets_out, _ = await nets.communicate()
        assert b"bridge" in nets_out
        fresh = ShellWorkroomManager(_policy())
        with pytest.raises(ShellWorkroomError) as exc:
            await fresh.ensure_workroom(key)
        assert exc.value.reason_code == "shell_workroom_adoption_rejected"
        assert exc.value.uncertainty is False
        state = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Running}}", name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        state_out, _ = await state.communicate()
        assert state_out.strip() == b"true"
    finally:
        await manager.remove_workroom(key)


async def test_child_runs_demoted_and_tamper_fails(room):
    """The model child runs as the policy user with zero caps and NNP;

    instance shadows cannot intercept the mechanism, and supervisor
    state resists tampering (kill fails, root dir unreadable, forged
    workdir records ignored)."""
    manager, key = room
    probe = await manager.exec_script(
        key, "bash",
        "echo \"uid=$(id -u):$(id -g)\"; "
        "grep CapEff /proc/self/status; "
        "grep NoNewPrivs /proc/self/status; "
        "printf '#!/bin/sh\\necho POISONED\\n' > /workroom/.local/bin/python3 && "
        "chmod +x /workroom/.local/bin/python3 && "
        "kill -9 $PPID 2>/dev/null; echo \"killrc=$?\"; "
        "touch /run/whs-shell/t3poison 2>/dev/null; echo \"touchrc=$?\"; "
        "printf 'FORGED' > \"/workroom/.whs-result-$WHS_RUN_ID\"; "
        "echo survived",
        60.0,
    )
    assert probe.exit_code == 0, probe.stderr
    assert probe.timed_out is False
    assert "uid=65532:65532" in probe.stdout
    cap_line = next(line for line in probe.stdout.splitlines() if line.startswith("CapEff:"))
    assert cap_line.rstrip().endswith("0000000000000000")
    nnp_line = next(line for line in probe.stdout.splitlines() if line.startswith("NoNewPrivs:"))
    assert nnp_line.rstrip().endswith("1")
    assert "killrc=1" in probe.stdout
    assert "touchrc=1" in probe.stdout
    assert "survived" in probe.stdout
    healthy = await manager.exec_script(
        key, "bash", "command -v python3 && echo mechanism-ok", 30.0)
    assert healthy.exit_code == 0
    assert "/workroom/.local/bin/python3" in healthy.stdout
    assert "mechanism-ok" in healthy.stdout


async def test_relaxed_bounds_are_rejected_untouched():
    """A labeled same-name container with looser bounds is rejected unstarted."""
    manager = ShellWorkroomManager(_policy())
    if not await manager.probe():
        pytest.skip("docker daemon unavailable")
    key = "t003." + uuid.uuid4().hex[:12]
    name = container_name_for(key)
    created = await asyncio.create_subprocess_exec(
        "docker", "create", "--name", name,
        "--label", "io.whs-redteam.shell-workroom.managed=true",
        "--label", "io.whs-redteam.shell-workroom.context=" + key,
        "--network", "none", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "65532:65532",
        "--workdir", "/workroom", "--cpus", "1.0", "--pids-limit", "128",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=67108864",
        "--memory", "256m",
        IMAGE, "sleep", "infinity",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    assert await created.wait() == 0
    try:
        with pytest.raises(ShellWorkroomError) as exc:
            await manager.ensure_workroom(key)
        assert exc.value.reason_code == "shell_workroom_adoption_rejected"
        check = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Running}}", name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await check.communicate()
        assert out.strip() == b"false"
    finally:
        gone = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await gone.wait()
