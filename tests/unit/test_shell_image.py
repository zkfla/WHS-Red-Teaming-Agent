from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import sysconfig
from pathlib import Path
from types import SimpleNamespace

import pytest

from redteam.capability import shell_image
from redteam.capability.shell_image import (
    ShellImageError,
    image_tag_for,
    manifest_command,
    recipe_hash,
    resolve_asset_dir,
    validate_generated_manifest,
)
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_profiles import package_input_sha256, resolve_shell_image_profile

FAKE_ID = "sha256:" + "ab" * 32
OTHER_ID = "sha256:" + "cd" * 32
def make_policy(**overrides):
    data: dict[str, object] = {"image": FAKE_ID}
    data.update(overrides)
    return ShellWorkroomPolicy(**data)
def make_assets(base: Path, marker: str = "one") -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "Dockerfile").write_text("FROM x " + marker, encoding="utf-8")
    (base / "packages.txt").write_text("bash " + marker, encoding="utf-8")
    (base / "packages-kali-default.txt").write_text("kali-linux-default", encoding="utf-8")
    (base / "profiles.json").write_text(json.dumps({
        "version": 1,
        "profiles": {
            "baseline": {"package_input": "packages.txt",
                         "inventory_dependencies": False,
                         "description": "fixture baseline"},
            "kali-default": {"package_input": "packages-kali-default.txt",
                             "inventory_dependencies": True,
                             "description": "fixture broad"},
        },
    }), encoding="utf-8")
    (base / "generate-manifest.py").write_text("# gen " + marker, encoding="utf-8")
    return base


def manifest_payload(assets: Path, image_id: str) -> dict:
    return {
        "inventory_schema_version": 2,
        "image_digest": image_id,
        "profile": "baseline",
        "package_input": "packages.txt",
        "package_input_sha256": hashlib.sha256(
            (assets / "packages.txt").read_bytes()
        ).hexdigest(),
        "programs": [],
    }


def profile_manifest_payload(assets: Path, image_id: str, profile_name: str) -> dict:
    profile = resolve_shell_image_profile(assets, profile_name)
    return {
        "inventory_schema_version": 2,
        "image_digest": image_id,
        "profile": profile.name,
        "package_input": profile.package_input,
        "package_input_sha256": package_input_sha256(assets, profile),
        "programs": [],
    }
class FakeRunner:
    def __init__(self, image_id: str, write_manifest: bool = True, digest: str | None = None):
        self.calls: list[tuple[str, ...]] = []
        self.image_id = image_id
        self.write_manifest = write_manifest
        self.digest = digest if digest is not None else image_id
    async def __call__(self, cmd: tuple[str, ...], timeout_seconds: float):
        self.calls.append(tuple(cmd))
        first = cmd[0] if len(cmd) > 0 else ""
        if first == sys.executable:
            out_index = list(cmd).index("--output")
            out_path = Path(cmd[out_index + 1])
            img_index = list(cmd).index("--image")
            img = cmd[img_index + 1]
            _ = img
            if self.write_manifest:
                assets = Path(cmd[1]).parent
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(
                    json.dumps(manifest_payload(assets, self.digest)), encoding="utf-8"
                )
            return (0, "", "")
        if "inspect" in cmd:
            return (0, self.image_id + chr(10), "")
        if "build" in cmd:
            return (0, "built", "")
        return (1, "", "nope")
async def test_cache_hit_reuses_tag_without_build(tmp_path, monkeypatch):
    assets = make_assets(tmp_path / "assets")
    monkeypatch.setattr(shell_image, "resolve_asset_dir", lambda: assets)
    policy = make_policy()
    cache = tmp_path / "cache"
    runner = FakeRunner(FAKE_ID)
    image_id, manifest = await shell_image.build_default_image(policy, cache, runner=runner)
    assert image_id == FAKE_ID
    assert manifest.endswith(".json")
    kinds = ["inspect" in c for c in runner.calls]
    assert any(kinds)
    assert not any(["build" in c for c in runner.calls])
async def test_build_when_absent_then_resolve_id(tmp_path, monkeypatch):
    assets = make_assets(tmp_path / "assets")
    monkeypatch.setattr(shell_image, "resolve_asset_dir", lambda: assets)
    policy = make_policy()
    cache = tmp_path / "cache"
    calls: list[tuple[str, ...]] = []
    async def flaky(cmd: tuple[str, ...], timeout_seconds: float):
        calls.append(tuple(cmd))
        if sys.executable == cmd[0]:
            idx = list(cmd).index("--output")
            outp = Path(cmd[idx + 1])
            outp.parent.mkdir(parents=True, exist_ok=True)
            outp.write_text(json.dumps(manifest_payload(assets, FAKE_ID)), encoding="utf-8")
            return (0, "", "")
        if "inspect" in cmd and len(calls) <= 2:
            return (1, "", "missing")
        if "build" in cmd:
            return (0, "built", "")
        if "inspect" in cmd:
            return (0, FAKE_ID + chr(10), "")
        return (1, "", "nope")
    image_id, _mp = await shell_image.build_default_image(policy, cache, runner=flaky)
    assert image_id == FAKE_ID
    assert any(["build" in c for c in calls])
def test_recipe_hash_changes_when_file_changes(tmp_path):
    first = make_assets(tmp_path / "first", marker="one")
    second = make_assets(tmp_path / "second", marker="one")
    assert recipe_hash(first) == recipe_hash(second)
    assert image_tag_for(recipe_hash(first)) == image_tag_for(recipe_hash(second))
    third = make_assets(tmp_path / "third", marker="two")
    assert recipe_hash(first) != recipe_hash(third)
    assert image_tag_for(recipe_hash(first)) != image_tag_for(recipe_hash(third))


def test_profiles_have_independent_reproducible_inputs(tmp_path):
    assets = make_assets(tmp_path / "assets")
    baseline = resolve_shell_image_profile(assets, "baseline")
    broad = resolve_shell_image_profile(assets, "kali-default")
    assert not baseline.inventory_dependencies
    assert broad.inventory_dependencies
    assert baseline.package_input != broad.package_input
    assert package_input_sha256(assets, baseline) != package_input_sha256(assets, broad)
    assert recipe_hash(assets, "baseline") != recipe_hash(assets, "kali-default")


@pytest.mark.parametrize("mutation,message", [
    ("duplicate", "duplicate"),
    ("missing_executable_status", "status"),
    ("bad_shadow", "shadowing"),
])
def test_v2_manifest_rejects_inventory_mismatches(tmp_path, mutation, message):
    assets = make_assets(tmp_path / "assets")
    profile = resolve_shell_image_profile(assets, "baseline")
    payload = manifest_payload(assets, FAKE_ID)
    executable = {
        "command": "tool",
        "path": "/usr/bin/tool",
        "resolved_path": "/usr/local/bin/tool",
        "path_status": "shadowed",
    }
    payload["programs"] = [{
        "package": "fixture", "inventory_status": "enumerated",
        "executables": [executable],
    }]
    if mutation == "duplicate":
        payload["programs"][0]["executables"].append(dict(executable))
    elif mutation == "missing_executable_status":
        payload["programs"][0]["inventory_status"] = "no_executable"
    else:
        payload["programs"][0]["executables"][0]["resolved_path"] = "/usr/bin/tool"
    with pytest.raises(ShellImageError, match=message):
        validate_generated_manifest(
            payload,
            image_id=FAKE_ID,
            profile=profile,
            package_sha256=package_input_sha256(assets, profile),
        )
def test_manifest_command_args_and_context(tmp_path):
    assets = make_assets(tmp_path / "assets")
    plain = make_policy()
    cmd = manifest_command(plain, assets, FAKE_ID, tmp_path / "out.json")
    assert cmd[0] == sys.executable
    assert "--offline" in cmd
    assert "--image" in cmd
    assert FAKE_ID in cmd
    assert "--docker-binary" in cmd
    assert cmd[cmd.index("--profile") + 1] == "baseline"
    assert "--docker-context" not in cmd
    ctx = make_policy(docker_context="myctx")
    cmd2 = manifest_command(ctx, assets, FAKE_ID, tmp_path / "out2.json")
    assert "--docker-context" in cmd2
    assert "myctx" in cmd2


async def test_broad_profile_is_explicit_in_build_and_manifest_commands(tmp_path, monkeypatch):
    assets = make_assets(tmp_path / "assets")
    monkeypatch.setattr(shell_image, "resolve_asset_dir", lambda: assets)
    calls: list[tuple[str, ...]] = []
    inspect_count = 0

    async def runner(cmd: tuple[str, ...], timeout_seconds: float):
        nonlocal inspect_count
        calls.append(cmd)
        if cmd[0] == sys.executable:
            output = Path(cmd[cmd.index("--output") + 1])
            profile_name = cmd[cmd.index("--profile") + 1]
            output.write_text(json.dumps(
                profile_manifest_payload(assets, FAKE_ID, profile_name)
            ), encoding="utf-8")
            return (0, "", "")
        if "inspect" in cmd:
            inspect_count += 1
            return ((1, "", "missing") if inspect_count == 1
                    else (0, FAKE_ID + "\n", ""))
        if "build" in cmd:
            return (0, "built", "")
        return (1, "", "unexpected")

    await shell_image.build_default_image(
        make_policy(), tmp_path / "cache", runner=runner, profile_name="kali-default"
    )
    build = next(call for call in calls if "build" in call)
    assert "SHELL_IMAGE_PROFILE=kali-default" in build
    assert "PACKAGE_INPUT=packages-kali-default.txt" in build
    generated = next(call for call in calls if call[0] == sys.executable)
    assert generated[generated.index("--profile") + 1] == "kali-default"
async def test_manifest_digest_mismatch_raises(tmp_path, monkeypatch):
    assets = make_assets(tmp_path / "assets")
    monkeypatch.setattr(shell_image, "resolve_asset_dir", lambda: assets)
    policy = make_policy()
    cache = tmp_path / "cache"
    runner = FakeRunner(FAKE_ID, digest=OTHER_ID)
    try:
        await shell_image.build_default_image(policy, cache, runner=runner)
        raise AssertionError("expected mismatch")
    except ShellImageError as exc:
        assert "mismatch" in str(exc).lower()
async def test_missing_manifest_raises(tmp_path, monkeypatch):
    assets = make_assets(tmp_path / "assets")
    monkeypatch.setattr(shell_image, "resolve_asset_dir", lambda: assets)
    policy = make_policy()
    cache = tmp_path / "cache"
    runner = FakeRunner(FAKE_ID, write_manifest=False)
    try:
        await shell_image.build_default_image(policy, cache, runner=runner)
        raise AssertionError("expected missing")
    except ShellImageError as exc:
        text = str(exc).lower()
        assert "unreadable" in text or "missing" in text or "manifest" in text
def test_asset_resolution_source_first():
    resolved = resolve_asset_dir()
    assert (resolved / "Dockerfile").is_file()
    assert (resolved / "packages.txt").is_file()
    assert (resolved / "generate-manifest.py").is_file()
def test_asset_resolution_installed_prefix(tmp_path, monkeypatch):
    fake_data = tmp_path / "data"
    target = fake_data / "share" / "redteam" / "shell-workroom"
    target.mkdir(parents=True)
    for name in shell_image.RECIPE_FILES:
        (target / name).write_text("x", encoding="utf-8")
    monkeypatch.setattr(sysconfig, "get_path", lambda *a, **k: str(fake_data))
    orig = Path.is_file
    def fake_is_file(self):
        text = str(self)
        if "deploy" in text and "shell-workroom" in text:
            return False
        return orig(self)
    monkeypatch.setattr(Path, "is_file", fake_is_file)
    resolved = resolve_asset_dir()
    assert resolved == target
async def test_context_propagation_in_docker_commands(tmp_path, monkeypatch):
    assets = make_assets(tmp_path / "assets")
    monkeypatch.setattr(shell_image, "resolve_asset_dir", lambda: assets)
    policy = make_policy(docker_context="myctx", docker_binary="docker")
    seen: list[tuple[str, ...]] = []
    async def rec(cmd: tuple[str, ...], timeout_seconds: float):
        seen.append(tuple(cmd))
        if sys.executable == cmd[0]:
            idx = list(cmd).index("--output")
            outp = Path(cmd[idx + 1])
            outp.parent.mkdir(parents=True, exist_ok=True)
            outp.write_text(json.dumps(manifest_payload(assets, FAKE_ID)), encoding="utf-8")
            return (0, "", "")
        if "inspect" in cmd:
            return (0, FAKE_ID + chr(10), "")
        return (0, "", "")
    await shell_image.build_default_image(policy, tmp_path / "cache", runner=rec)
    docker_calls = [c for c in seen if len(c) > 0 and c[0] == "docker"]
    assert len(docker_calls) > 0
    for call in docker_calls:
        assert "--context" in call
        assert "myctx" in call
    plain = make_policy()
    seen2: list[tuple[str, ...]] = []
    async def rec2(cmd: tuple[str, ...], timeout_seconds: float):
        seen2.append(tuple(cmd))
        if sys.executable == cmd[0]:
            idx = list(cmd).index("--output")
            outp = Path(cmd[idx + 1])
            outp.parent.mkdir(parents=True, exist_ok=True)
            outp.write_text(json.dumps(manifest_payload(assets, FAKE_ID)), encoding="utf-8")
            return (0, "", "")
        return (0, FAKE_ID + chr(10), "")
    await shell_image.build_default_image(plain, tmp_path / "cache2", runner=rec2)
    docker_calls2 = [c for c in seen2 if len(c) > 0 and c[0] == "docker"]
    assert len(docker_calls2) > 0
    for call in docker_calls2:
        assert "--context" not in call
async def test_cancellation_terminates_child():
    cmd = (sys.executable, "-c", "import time; time.sleep(30)")
    task = asyncio.create_task(shell_image._default_runner(cmd, 60.0))
    await asyncio.sleep(0.5)
    task.cancel()
    try:
        await task
        raise AssertionError("expected cancel")
    except asyncio.CancelledError:
        pass
    assert task.cancelled() or task.done()
async def test_build_failure_raises(tmp_path, monkeypatch):
    assets = make_assets(tmp_path / "assets")
    monkeypatch.setattr(shell_image, "resolve_asset_dir", lambda: assets)
    policy = make_policy()
    async def fail(cmd: tuple[str, ...], timeout_seconds: float):
        if "inspect" in cmd:
            return (1, "", "missing")
        if "build" in cmd:
            return (1, "", "build broke")
        return (1, "", "nope")
    try:
        await shell_image.build_default_image(policy, tmp_path / "cache", runner=fail)
        raise AssertionError("expected build fail")
    except ShellImageError as exc:
        assert "build" in str(exc).lower()
async def test_progress_messages_are_short(tmp_path, monkeypatch):
    assets = make_assets(tmp_path / "assets")
    monkeypatch.setattr(shell_image, "resolve_asset_dir", lambda: assets)
    policy = make_policy()
    seen: list[str] = []
    runner = FakeRunner(FAKE_ID)
    cache_dir = tmp_path / "cache"
    await shell_image.build_default_image(policy, cache_dir, progress=seen.append, runner=runner)
    assert len(seen) > 0
    for msg in seen:
        assert len(msg) < 200
        assert "sha256:" not in msg
def test_asset_resolution_user_and_site_candidates(tmp_path, monkeypatch):
    defaultbase = tmp_path / "datamissing"
    defaultbase.mkdir(parents=True)
    userbase = tmp_path / "userbase"
    usertarget = userbase / "share" / "redteam" / "shell-workroom"
    usertarget.mkdir(parents=True)
    for name in shell_image.RECIPE_FILES:
        (usertarget / name).write_text("u", encoding="utf-8")
    purelib = tmp_path / "venv" / "Lib" / "site-packages"
    purelib.mkdir(parents=True)
    def fake_get(name, scheme=None):
        if name == "purelib":
            return str(purelib)
        if scheme is not None:
            return str(userbase)
        return str(defaultbase)
    monkeypatch.setattr(sysconfig, "get_path", fake_get)
    orig = Path.is_file
    def fake_is_file(self):
        text = str(self)
        if "deploy" in text and "shell-workroom" in text:
            return False
        return orig(self)
    monkeypatch.setattr(Path, "is_file", fake_is_file)
    assert resolve_asset_dir() == usertarget
    for name in shell_image.RECIPE_FILES:
        (usertarget / name).unlink()
    sitetarget = tmp_path / "venv" / "share" / "redteam" / "shell-workroom"
    sitetarget.mkdir(parents=True)
    for name in shell_image.RECIPE_FILES:
        (sitetarget / name).write_text("s", encoding="utf-8")
    assert resolve_asset_dir() == sitetarget
def test_terminate_tree_terminates_job_even_when_returncode_set():
    calls: list[str] = []
    class FakeJob:
        def terminate(self) -> None:
            calls.append("terminate")
    class Proc:
        returncode = 0
        pid = 4242
        def kill(self) -> None:
            calls.append("kill")
    shell_image._terminate_tree(Proc(), FakeJob())
    assert calls == ["terminate", "kill"]


def test_terminate_tree_windows_terminates_job_and_kills_unjoined_parent():
    calls: list[str] = []
    class FakeJob:
        def terminate(self) -> None:
            calls.append("terminate")
    class Proc:
        def kill(self) -> None:
            calls.append("kill")
            raise ProcessLookupError("already gone")
    shell_image._terminate_tree(Proc(), FakeJob())
    assert calls == ["terminate", "kill"]
def test_terminate_tree_posix_uses_killpg(monkeypatch):
    seen: list[tuple] = []
    class Proc:
        returncode = None
        pid = 5678
    proxy = SimpleNamespace(
        name="posix",
        killpg=lambda pgid, sig: seen.append((pgid, int(sig))),
    )
    monkeypatch.setattr(shell_image, "os", proxy)
    monkeypatch.setattr(shell_image, "signal", SimpleNamespace(SIGKILL=9))
    shell_image._terminate_tree(Proc())
    assert seen == [(5678, 9)]
def _grandchild_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            done = subprocess.run(
                ["tasklist", "/FI", "PID eq " + str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            text = done.stdout.decode("utf-8", errors="replace")
            return str(pid) in text
        except Exception:
            pytest.skip("no tasklist")
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            stat = handle.read()
        state = stat.rsplit(") ", 1)[-1].split(" ", 1)[0]
        if state == "Z":
            return False
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        pytest.skip("no pid check")
    return True
async def test_runner_cancel_kills_tree(tmp_path):
    helper = tmp_path / "tree_helper.py"
    pidfile = tmp_path / "grand.pid"
    script = [
        "import subprocess",
        "import sys",
        "import time",
        "from pathlib import Path",
        "pidfile = Path(sys.argv[1])",
        'grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])',
        'pidfile.write_text(str(grand.pid), encoding="utf-8")',
        "time.sleep(30)",
    ]
    helper.write_text(chr(10).join(script), encoding="utf-8")
    cmd = (sys.executable, str(helper), str(pidfile))
    task = asyncio.create_task(shell_image._default_runner(cmd, 60.0))
    grand_pid = 0
    for _i in range(100):
        await asyncio.sleep(0.2)
        if pidfile.is_file():
            try:
                grand_pid = int(pidfile.read_text(encoding="utf-8").strip())
            except ValueError:
                continue
            break
    if grand_pid == 0:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        pytest.skip("helper did not start")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    gone = False
    for _i in range(50):
        await asyncio.sleep(0.2)
        if not _grandchild_alive(grand_pid):
            gone = True
            break
    assert gone, "grandchild survived cancel"

def _force_kill_tree(pid: int) -> None:
    """Best-effort cleanup so a failure never leaves the grandchild running."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


async def test_runner_timeout_kills_orphaned_grandchild(tmp_path):
    """A dead parent must not leave a live descendant behind."""
    helper = tmp_path / "orphan_helper.py"
    pidfile = tmp_path / "orphan.pid"
    script = [
        "import subprocess",
        "import sys",
        "from pathlib import Path",
        "pidfile = Path(sys.argv[1])",
        "grand = subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(300)\"],",
        "                         stdout=sys.stdout, stderr=sys.stderr, close_fds=False)",
        "pidfile.write_text(str(grand.pid), encoding=\"utf-8\")",
    ]
    helper.write_text(chr(10).join(script), encoding="utf-8")
    # POSIX: the grandchild retains the inherited pipe, so communicate() never sees
    # EOF and the runner must time out. Windows (Proactor asyncio): grandchildren do
    # not retain the pipe, so communicate() may see EOF and return normally; the
    # guarantee under test there is JobObject descendant teardown, not the timeout.
    if os.name == "nt":
        try:
            await shell_image._default_runner((sys.executable, str(helper), str(pidfile)), 10.0)
        except ShellImageError as exc:
            assert "timed out" in str(exc)
    else:
        with pytest.raises(ShellImageError, match="timed out"):
            await shell_image._default_runner((sys.executable, str(helper), str(pidfile)), 10.0)
    assert pidfile.is_file(), "helper exited without recording the grandchild pid"
    grand_pid = int(pidfile.read_text(encoding="utf-8").strip())
    try:
        gone = False
        for _i in range(75):
            if not _grandchild_alive(grand_pid):
                gone = True
                break
            await asyncio.sleep(0.2)
        assert gone, "grandchild survived parent exit and runner timeout"
    finally:
        if _grandchild_alive(grand_pid):
            _force_kill_tree(grand_pid)
        try:
            os.waitpid(grand_pid, os.WNOHANG)
        except Exception:
            pass


async def test_default_runner_captures_normal_output():
    code, out, _err = await shell_image._default_runner(
        (sys.executable, "-c", "print('MARKER-OK')"), 60.0,
    )
    assert code == 0
    assert "MARKER-OK" in out
