"""Unit tests for Docker lifecycle and immutable environment preparation.

Drives redteam.capability.shell_preparation with every Docker call
intercepted at the _command seam (no network, no live Docker, no real
subprocess). Production is root-owned: these tests only observe it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from redteam.capability import shell_image, shell_preparation
from redteam.capability.settings import MCPConfigurationError
from redteam.capability.shell_policy import ShellWorkroomPolicy

IMAGE_ID = "sha256:" + "ab" * 32
DIGEST_REF = "example.com/kali@sha256:" + "cd" * 32
RECORDED_ID = "sha256:" + "11" * 32


@pytest.fixture(autouse=True)
def _clean_docker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("REDTEAM_SHELL_AUTO_SETUP", "false")


def _default_policy(**overrides: Any) -> ShellWorkroomPolicy:
    return ShellWorkroomPolicy(image="sha256:" + "0" * 64, **overrides)


class _DockerFake:
    """Deterministic stand-in for shell_preparation._command."""

    def __init__(
        self,
        *,
        host: str = "unix:///var/run/docker.sock",
        context: str = "desktop-linux",
        engine_queue: list[tuple[int, str]] | None = None,
        desktop_start_code: int = 0,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.host = host
        self.context = context
        self.engine_queue = list(engine_queue) if engine_queue is not None else [(0, "linux")]
        self.desktop_start_code = desktop_start_code
        self.image_ids: dict[str, str] = {}

    async def __call__(self, *argv: str, timeout: float = 15.0) -> tuple[int, str]:
        self.calls.append(tuple(argv))
        if "desktop" in argv and "start" in argv:
            return self.desktop_start_code, ""
        if "context" in argv and "show" in argv:
            return 0, self.context
        if "context" in argv and "inspect" in argv:
            return 0, self.host
        if "info" in argv:
            if self.engine_queue:
                return self.engine_queue.pop(0)
            return 0, "linux"
        if "image" in argv and "inspect" in argv:
            image = argv[list(argv).index("inspect") + 1]
            if image in self.image_ids:
                return 0, self.image_ids[image]
            return 1, ""
        raise AssertionError(f"unexpected docker call: {argv}")

    def started_desktop(self) -> bool:
        return any("desktop" in call and "start" in call for call in self.calls)


def _patch_docker(monkeypatch: pytest.MonkeyPatch, fake: _DockerFake, *, binary: str) -> None:
    monkeypatch.setattr(shell_preparation, "_command", fake)
    monkeypatch.setattr(
        shell_preparation.shutil, "which", lambda name: binary if name == "docker" else None,
    )


def _patch_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cache = tmp_path / "cache"
    monkeypatch.setattr(shell_preparation, "_cache_directory", lambda: cache)
    return cache


def _patch_build(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Path]]:
    calls: list[tuple[Any, Path]] = []

    async def fake_build(policy: Any, cache_dir: Path, progress: Any) -> tuple[str, str]:
        calls.append((policy, cache_dir))
        manifest = cache_dir / "manifests" / "built.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"image_digest": IMAGE_ID, "programs": [{"name": "python3"}]}),
            encoding="utf-8",
        )
        return IMAGE_ID, str(manifest)

    monkeypatch.setattr(shell_image, "build_default_image", fake_build)
    return calls


def _ready_docker(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _DockerFake:
    fake = _DockerFake(**kwargs)
    fake.image_ids[IMAGE_ID] = IMAGE_ID
    _patch_docker(monkeypatch, fake, binary="/usr/bin/docker")
    return fake


async def test_ready_linux_engine_returns_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    endpoint = await shell_preparation.ensure_docker_ready(_default_policy())
    assert (endpoint.binary, endpoint.context, endpoint.host) == (
        "/usr/bin/docker", "desktop-linux", "unix:///var/run/docker.sock",
    )
    assert not fake.started_desktop()


async def test_docker_not_installed_carries_install_guide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    async def refuse(*argv: str, timeout: float = 15.0) -> tuple[int, str]:
        raise AssertionError(f"no docker call expected: {argv}")

    monkeypatch.setattr(shell_preparation, "_command", refuse)
    monkeypatch.setattr(shell_preparation.shutil, "which", lambda name: None)
    with pytest.raises(MCPConfigurationError, match="docs.docker.com"):
        await shell_preparation.ensure_docker_ready(_default_policy())


async def test_docker_host_without_context_refuses_with_manual_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://192.168.1.5:2376")

    async def refuse(*argv: str, timeout: float = 15.0) -> tuple[int, str]:
        raise AssertionError(f"no docker call expected: {argv}")

    monkeypatch.setattr(shell_preparation, "_command", refuse)
    monkeypatch.setattr(
        shell_preparation.shutil, "which", lambda name: "/usr/bin/docker",
    )
    with pytest.raises(MCPConfigurationError, match="REDTEAM_SHELL_AUTO_SETUP=false"):
        await shell_preparation.ensure_docker_ready(_default_policy())


async def test_remote_context_not_ready_refuses_without_starting_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _DockerFake(host="tcp://192.168.1.5:2376", engine_queue=[(1, "")])
    _patch_docker(monkeypatch, fake, binary="/usr/bin/docker")

    def refuse_launch(binary: str, env: Any) -> None:
        raise AssertionError("remote endpoint must not start local Desktop")

    monkeypatch.setattr(shell_preparation, "_launch_desktop", refuse_launch)
    policy = _default_policy(docker_context="myremote")
    with pytest.raises(MCPConfigurationError, match="will not start local Docker Desktop"):
        await shell_preparation.ensure_docker_ready(policy)
    assert not fake.started_desktop()


async def test_non_linux_engine_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_docker(monkeypatch, engine_queue=[(0, "windows")])
    with pytest.raises(MCPConfigurationError, match="Linux container engine"):
        await shell_preparation.ensure_docker_ready(_default_policy())


class _FakeStartupInfo:
    def __init__(self) -> None:
        self.dwFlags = 0
        self.wShowWindow = 1


def _nt_os() -> SimpleNamespace:
    import os as real_os

    values = {key: getattr(real_os, key) for key in dir(real_os) if not key.startswith("__")}
    values["name"] = "nt"
    return SimpleNamespace(**values)


def _patch_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, popen: Any,
) -> tuple[Path, str]:
    """Fake an nt runtime; return the Desktop.exe path that must be launched."""
    monkeypatch.setattr(shell_preparation, "os", _nt_os())
    monkeypatch.setattr(
        shell_preparation,
        "subprocess",
        SimpleNamespace(
            STARTUPINFO=_FakeStartupInfo,
            STARTF_USESHOWWINDOW=1,
            DETACHED_PROCESS=16,
            CREATE_NEW_PROCESS_GROUP=512,
            Popen=popen,
            DEVNULL="devnull",
        ),
    )
    program_files = tmp_path / "ProgramFiles"
    desktop = program_files / "Docker/Docker/Docker Desktop.exe"
    desktop.parent.mkdir(parents=True)
    desktop.write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    local_app = tmp_path / "LocalAppData"
    local_app.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))
    docker_bin = program_files / "Docker/Docker/resources/bin/docker.exe"
    return desktop, str(docker_bin)


def _npipe_fake(**kwargs: Any) -> _DockerFake:
    return _DockerFake(host="npipe:////./pipe/docker_engine", context="desktop", **kwargs)


async def test_windows_npipe_starts_desktop_then_falls_back_to_hidden_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[tuple[Any, Any]] = []

    def recording_popen(*args: Any, **kwargs: Any) -> Any:
        launched.append((args, kwargs))
        return SimpleNamespace(pid=4242)

    desktop, docker_bin = _patch_windows(monkeypatch, tmp_path, recording_popen)
    fake = _npipe_fake(engine_queue=[(1, ""), (0, "linux")], desktop_start_code=1)
    _patch_docker(monkeypatch, fake, binary=docker_bin)
    progress: list[str] = []
    endpoint = await shell_preparation.ensure_docker_ready(
        _default_policy(), progress=progress.append,
    )
    assert endpoint.host == "npipe:////./pipe/docker_engine"
    assert fake.started_desktop()
    assert len(launched) == 1
    (popen_args, popen_kwargs) = launched[0]
    assert popen_args[0] == [str(desktop)]
    assert str(desktop).startswith(str(tmp_path / "ProgramFiles"))
    roots = (str(tmp_path / "ProgramFiles"), str(tmp_path / "LocalAppData"))
    assert popen_args[0][0].startswith(roots)
    assert "startupinfo" in popen_kwargs
    assert progress and "Docker Desktop" in progress[0]


async def test_readiness_timeout_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shell_preparation, "READINESS_TIMEOUT", 0.0)

    def refuse_popen(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Desktop launch must not run when the CLI start succeeds")

    _patch_windows(monkeypatch, tmp_path, refuse_popen)
    fake = _npipe_fake(engine_queue=[(1, "")])
    _patch_docker(monkeypatch, fake, binary="C:/docker/docker.exe")
    with pytest.raises(MCPConfigurationError, match="did not become ready within"):
        await shell_preparation.ensure_docker_ready(_default_policy())
    assert fake.started_desktop()


async def test_fresh_default_builds_publishes_and_returns_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_docker(monkeypatch)
    cache = _patch_cache(monkeypatch, tmp_path)
    build_calls = _patch_build(monkeypatch)
    result = await shell_preparation.prepare_shell_policy(None)
    assert len(result.fingerprint) == 64
    assert result.policy.image == IMAGE_ID
    assert result.manifest_path is not None
    assert Path(result.manifest_path).parent == cache / "manifests"
    assert Path(result.manifest_path).is_file()
    assert (cache / "records" / f"{result.fingerprint}.json").is_file()
    assert len(build_calls) == 1


async def test_sticky_rerun_reuses_fingerprint_without_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_docker(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    build_calls = _patch_build(monkeypatch)
    first = await shell_preparation.prepare_shell_policy(None)
    second = await shell_preparation.prepare_shell_policy(None)
    assert second.fingerprint == first.fingerprint
    assert second.policy == first.policy
    assert second.manifest_path == first.manifest_path
    assert len(build_calls) == 1


async def test_expected_fingerprint_restores_exact_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_docker(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    build_calls = _patch_build(monkeypatch)
    first = await shell_preparation.prepare_shell_policy(None)
    restored = await shell_preparation.prepare_shell_policy(
        None, expected_fingerprint=first.fingerprint,
    )
    assert restored.fingerprint == first.fingerprint
    assert restored.policy == first.policy
    assert restored.manifest_path == first.manifest_path
    assert len(build_calls) == 1


async def test_missing_record_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_docker(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    build_calls = _patch_build(monkeypatch)
    with pytest.raises(MCPConfigurationError):
        await shell_preparation.prepare_shell_policy(None, expected_fingerprint="ff" * 32)
    assert build_calls == []


async def test_wrong_shape_fingerprint_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_docker(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    build_calls = _patch_build(monkeypatch)
    with pytest.raises(MCPConfigurationError, match="fingerprint is invalid"):
        await shell_preparation.prepare_shell_policy(None, expected_fingerprint="not-hex")
    assert build_calls == []


async def test_recorded_image_gone_refuses_without_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    build_calls = _patch_build(monkeypatch)
    await shell_preparation.prepare_shell_policy(None)
    fake.image_ids.pop(IMAGE_ID)
    with pytest.raises(MCPConfigurationError, match="image is unavailable"):
        await shell_preparation.prepare_shell_policy(None)
    assert len(build_calls) == 1


async def test_manifest_digest_mismatch_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_docker(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    first = await shell_preparation.prepare_shell_policy(None)
    assert first.manifest_path is not None
    Path(first.manifest_path).write_text(
        json.dumps({"image_digest": "sha256:" + "00" * 64, "programs": []}),
        encoding="utf-8",
    )
    with pytest.raises(MCPConfigurationError, match="does not match the pinned image"):
        await shell_preparation.prepare_shell_policy(
            None, expected_fingerprint=first.fingerprint,
        )


async def test_unreadable_manifest_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_docker(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    first = await shell_preparation.prepare_shell_policy(None)
    assert first.manifest_path is not None
    Path(first.manifest_path).unlink()
    with pytest.raises(MCPConfigurationError):
        await shell_preparation.prepare_shell_policy(
            None, expected_fingerprint=first.fingerprint,
        )


async def test_changed_endpoint_refuses_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    build_calls = _patch_build(monkeypatch)
    first = await shell_preparation.prepare_shell_policy(None)
    fake.host = "unix:///var/run/other.sock"
    with pytest.raises(MCPConfigurationError, match="endpoint has changed"):
        await shell_preparation.prepare_shell_policy(
            None, expected_fingerprint=first.fingerprint,
        )
    assert len(build_calls) == 1


def _explicit_policy_file(tmp_path: Path, **overrides: Any) -> str:
    fields: dict[str, Any] = {"image": DIGEST_REF}
    fields.update(overrides)
    path = tmp_path / "shell-policy.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return str(path)


async def test_explicit_policy_differs_from_record_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[DIGEST_REF] = RECORDED_ID
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    recorded = await shell_preparation.prepare_shell_policy(_explicit_policy_file(tmp_path))
    assert recorded.policy.image == RECORDED_ID
    other = _explicit_policy_file(tmp_path, memory="1024m")
    with pytest.raises(MCPConfigurationError, match="differs from the recorded environment"):
        await shell_preparation.prepare_shell_policy(
            other, expected_fingerprint=recorded.fingerprint,
        )


async def test_cancel_while_waiting_for_lock_propagates_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_docker(monkeypatch)
    cache = _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    lock_path = cache / "prep.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    async with shell_preparation._preparation_lock(lock_path):
        task = asyncio.create_task(shell_preparation.prepare_shell_policy(None))
        await asyncio.sleep(0.6)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (cache / "active.json").exists()
    async with shell_preparation._preparation_lock(lock_path):
        pass


class _HangingProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.kill_called = 0
        self.wait_called = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(3600)
        return b"", b""

    def kill(self) -> None:
        self.kill_called += 1
        self.returncode = -9

    async def wait(self) -> int | None:
        self.wait_called += 1
        return self.returncode


def _patch_hanging_spawn(monkeypatch: pytest.MonkeyPatch) -> _HangingProcess:
    proc = _HangingProcess()

    async def fake_spawn(*argv: Any, **kwargs: Any) -> Any:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    return proc


async def test_command_timeout_kills_spawned_child(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _patch_hanging_spawn(monkeypatch)
    code, output = await shell_preparation._command("docker", "info", timeout=0.05)
    assert (code, output) == (124, "")
    assert proc.kill_called == 1
    assert proc.wait_called == 1


async def test_command_cancellation_kills_spawned_child(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _patch_hanging_spawn(monkeypatch)
    task = asyncio.create_task(shell_preparation._command("docker", "info", timeout=60))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.kill_called == 1
    assert proc.wait_called == 1


async def test_second_handle_lock_contention_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shell_preparation, "LOCK_TIMEOUT", 0.5)
    path = tmp_path / "prep.lock"
    async with shell_preparation._preparation_lock(path):
        with pytest.raises(MCPConfigurationError, match="Timed out waiting"):
            async with shell_preparation._preparation_lock(path):
                pass


@pytest.mark.parametrize("variable,suffix", [
    ("ProgramFiles", "Docker/Docker/resources/bin/docker.exe"),
    ("LOCALAPPDATA", "Programs/DockerDesktop/resources/bin/docker.exe"),
])
def test_installed_docker_missing_from_path_is_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str, suffix: str,
) -> None:
    binary = tmp_path / suffix
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fixture")
    monkeypatch.setattr(shell_preparation, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(shell_preparation.shutil, "which", lambda _name: None)
    assert shell_preparation._docker_binary("docker", {variable: str(tmp_path)}) == str(binary)


async def test_custom_local_pipe_does_not_start_unrelated_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("Custom pipe must not launch Desktop")
    _patch_windows(monkeypatch, tmp_path, forbidden)
    fake = _npipe_fake(engine_queue=[(1, "")])
    fake.host = "npipe:////./pipe/custom-dockerd"
    monkeypatch.setattr(shell_preparation, "_command", fake)
    monkeypatch.setattr(shell_preparation.shutil, "which", lambda _name: "docker")
    with pytest.raises(MCPConfigurationError, match="remote/custom"):
        await shell_preparation.ensure_docker_ready(_default_policy())
    assert not any("desktop" in call and "start" in call for call in fake.calls)


async def test_explicit_dev_tag_changes_apply_to_new_runs_but_refuse_old_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"image": "custom:dev", "allow_unpinned_image": True}))
    fake.image_ids["custom:dev"] = IMAGE_ID
    first = await shell_preparation.prepare_shell_policy(str(path))
    fake.image_ids["custom:dev"] = RECORDED_ID
    fake.image_ids[RECORDED_ID] = RECORDED_ID
    second = await shell_preparation.prepare_shell_policy(str(path))
    assert second.policy.image == RECORDED_ID
    assert second.fingerprint != first.fingerprint
    with pytest.raises(MCPConfigurationError, match="explicit shell policy differs"):
        await shell_preparation.prepare_shell_policy(
            str(path), expected_fingerprint=first.fingerprint,
        )
