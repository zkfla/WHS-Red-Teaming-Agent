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
from unittest.mock import AsyncMock

import pytest

from redteam.capability import shell_image, shell_preparation, shell_workroom
from redteam.capability.gated_network_policy import (
    GatedLimits,
    ManagedSidecarRequest,
    build_gated_network_policy,
    policy_content_hash,
)
from redteam.capability.settings import MCPConfigurationError
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_runtime import prepare_shell_runtime

IMAGE_ID = "sha256:" + "ab" * 32
DIGEST_REF = "example.com/kali@sha256:" + "cd" * 32
RECORDED_ID = "sha256:" + "11" * 32
SIDECAR_ID = "sha256:" + "ef" * 32
SIDECAR_REF = "registry.example/gate-sidecar@sha256:" + "99" * 32


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
        self.build_calls: list[tuple[str, ...]] = []
        self.nft_calls: list[tuple[tuple[str, ...], bytes | None]] = []
        self.nft_code = 0

    async def __call__(
        self, *argv: str, timeout: float = 15.0, stdin_bytes: bytes | None = None,
    ) -> tuple[int, str]:
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
        if "build" in argv:
            self.build_calls.append(tuple(argv))
            tag = argv[argv.index("--tag") + 1]
            self.image_ids[tag] = SIDECAR_ID
            self.image_ids[SIDECAR_ID] = SIDECAR_ID
            return 0, ""
        if "run" in argv and "nft" in argv:
            self.nft_calls.append((tuple(argv), stdin_bytes))
            return self.nft_code, ""
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
            json.dumps({
                "image_digest": IMAGE_ID,
                "programs": [{
                    "version": "1", "origin": "fixture",
                    "executables": [{"command": "python3", "path": "/usr/bin/python3"}],
                }],
            }),
            encoding="utf-8",
        )
        return IMAGE_ID, str(manifest)

    monkeypatch.setattr(shell_image, "build_default_image", fake_build)
    return calls


def _program_manifest(tmp_path: Path, image: str, *, name: str = "nmap") -> Path:
    path = tmp_path / f"program-manifest-{image[-8:]}.json"
    path.write_text(
        json.dumps({
            "image_digest": image,
            "programs": [{
                "version": "1",
                "origin": "fixture",
                "executables": [{"command": name, "path": f"/usr/bin/{name}"}],
            }],
        }),
        encoding="utf-8",
    )
    return path


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


def _gated_policy_file(
    tmp_path: Path,
    *,
    sidecar: str | ManagedSidecarRequest = SIDECAR_ID,
    limits: GatedLimits | None = None,
    name: str = "gated-policy.json",
) -> tuple[str, Any]:
    selected = build_gated_network_policy(
        targets={"web": ["192.0.2.10"]}, sidecar=sidecar,
        limits=limits or GatedLimits(
            max_flow_init_deliveries=1, max_output_bytes=4096, duration_seconds=10.0,
        ),
        endpoints={"web": [(443, "tcp")]},
    )
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(selected.model_dump(mode="json")), encoding="utf-8")
    return str(path), selected


async def test_pinned_sidecar_preparation_checks_rules_on_selected_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch, context="selected-context", host="unix:///selected/docker.sock")
    fake.image_ids[SIDECAR_REF] = SIDECAR_ID
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    gate_path, selection = _gated_policy_file(tmp_path, sidecar=SIDECAR_REF)

    prepared = await shell_preparation.prepare_shell_policy(
        None, network_policy_path=gate_path,
    )

    assert prepared.network_policy is not None
    assert prepared.network_policy.sidecar_image == SIDECAR_ID
    assert prepared.network_policy_hash == policy_content_hash(selection)
    assert prepared.status == "network_prerequisites_ready"
    assert prepared.readiness_checked_at is not None
    assert prepared.endpoint.context == "selected-context"
    assert len(fake.nft_calls) == 1
    argv, stdin_bytes = fake.nft_calls[0]
    assert stdin_bytes is not None and b"\r" not in stdin_bytes
    assert stdin_bytes.startswith(b"table inet ")
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--cap-add" in argv and argv[argv.index("--cap-add") + 1] == "NET_ADMIN"
    assert "-v" not in argv and "--mount" not in argv
    assert "--pull=never" in argv
    assert "--context" in argv and argv[argv.index("--context") + 1] == "selected-context"
    record_path = tmp_path / "cache" / "records" / f"{prepared.fingerprint}.json"
    assert record_path.exists()


async def test_managed_sidecar_builds_recipe_and_resolves_inspected_immutable_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    gate_path, _selection = _gated_policy_file(
        tmp_path, sidecar=ManagedSidecarRequest(),
    )

    prepared = await shell_preparation.prepare_shell_policy(
        None, network_policy_path=gate_path,
    )

    assert prepared.network_policy is not None
    assert prepared.network_policy.sidecar_image == SIDECAR_ID
    assert len(fake.build_calls) == 1
    build = fake.build_calls[0]
    assert "--file" in build and "deploy" in build[build.index("--file") + 1]
    assert not any("pull" in call for call in fake.calls)
    restored = await shell_preparation.prepare_shell_policy(
        None, network_policy_path=gate_path, expected_fingerprint=prepared.fingerprint,
    )
    assert restored.fingerprint == prepared.fingerprint
    assert len(fake.build_calls) == 1


async def test_managed_restore_does_not_require_current_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    gate_path, _selection = _gated_policy_file(
        tmp_path, sidecar=ManagedSidecarRequest(),
    )
    prepared = await shell_preparation.prepare_shell_policy(None, network_policy_path=gate_path)
    assert prepared.network_policy is not None
    recorded_sidecar = prepared.network_policy.sidecar_image
    assert recorded_sidecar == SIDECAR_ID

    def recipe_missing() -> str:
        raise shell_preparation.ShellPreparationError(
            "gate_sidecar_recipe_unavailable", "fixture recipe unavailable",
        )

    monkeypatch.setattr(shell_preparation, "_gate_sidecar_recipe_hash", recipe_missing)
    restored = await shell_preparation.prepare_shell_policy(
        None, network_policy_path=gate_path, expected_fingerprint=prepared.fingerprint,
    )

    assert restored.fingerprint == prepared.fingerprint
    assert restored.network_policy is not None
    assert restored.network_policy.sidecar_image == recorded_sidecar
    assert len(fake.build_calls) == 1


async def test_missing_pinned_sidecar_is_not_replaced_or_pulled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    gate_path, _selection = _gated_policy_file(tmp_path, sidecar=SIDECAR_REF)

    with pytest.raises(shell_preparation.ShellPreparationError) as caught:
        await shell_preparation.prepare_shell_policy(None, network_policy_path=gate_path)

    assert caught.value.code == "sidecar_image_missing"
    assert not fake.build_calls
    assert not any("pull" in call for call in fake.calls)


async def test_policy_path_relocation_restores_same_identity_but_content_change_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[SIDECAR_ID] = SIDECAR_ID
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    first_path, selected = _gated_policy_file(tmp_path, sidecar=SIDECAR_ID)
    first = await shell_preparation.prepare_shell_policy(None, network_policy_path=first_path)
    record_path = tmp_path / "cache" / "records" / f"{first.fingerprint}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["readiness_checked_at"] = "display-only-old-timestamp"
    record["preparation_status"] = "display-only-old-status"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    relocated = tmp_path / "relocated" / "gate.json"
    relocated.parent.mkdir()
    relocated.write_text(Path(first_path).read_text(encoding="utf-8"), encoding="utf-8")

    restored = await shell_preparation.prepare_shell_policy(
        None, network_policy_path=str(relocated), expected_fingerprint=first.fingerprint,
    )

    assert restored.fingerprint == first.fingerprint
    assert restored.network_policy_hash == policy_content_hash(selected)
    assert len(fake.nft_calls) == 2
    assert restored.status == "network_prerequisites_ready"
    changed_path, _changed = _gated_policy_file(
        tmp_path / "changed",
        sidecar=SIDECAR_ID,
        limits=GatedLimits(
            max_flow_init_deliveries=1, max_output_bytes=8192, duration_seconds=10.0,
        ),
    )
    with pytest.raises(shell_preparation.ShellPreparationError) as changed:
        await shell_preparation.prepare_shell_policy(
            None, network_policy_path=changed_path, expected_fingerprint=first.fingerprint,
        )
    assert changed.value.code == "network_policy_mismatch"
    changed_new = await shell_preparation.prepare_shell_policy(
        None, network_policy_path=changed_path,
    )
    assert changed_new.fingerprint != first.fingerprint
    with pytest.raises(shell_preparation.ShellPreparationError) as omitted:
        await shell_preparation.prepare_shell_policy(
            None, expected_fingerprint=first.fingerprint,
        )
    assert omitted.value.code == "network_policy_mismatch"

    fake.image_ids.pop(SIDECAR_ID)
    with pytest.raises(shell_preparation.ShellPreparationError) as missing_sidecar:
        await shell_preparation.prepare_shell_policy(
            None, network_policy_path=str(relocated), expected_fingerprint=first.fingerprint,
        )
    assert missing_sidecar.value.code == "sidecar_image_missing"


async def test_sidecar_and_endpoint_changes_do_not_restore_same_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[SIDECAR_ID] = SIDECAR_ID
    fake.image_ids[RECORDED_ID] = RECORDED_ID
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    first_path, _ = _gated_policy_file(tmp_path, sidecar=SIDECAR_ID)
    first = await shell_preparation.prepare_shell_policy(None, network_policy_path=first_path)
    other_path, _ = _gated_policy_file(
        tmp_path / "sidecar-change", sidecar=RECORDED_ID,
    )
    other = await shell_preparation.prepare_shell_policy(None, network_policy_path=other_path)
    assert other.fingerprint != first.fingerprint

    fake.host = "unix:///var/run/changed-docker.sock"
    with pytest.raises(shell_preparation.ShellPreparationError) as endpoint:
        await shell_preparation.prepare_shell_policy(
            None, network_policy_path=first_path, expected_fingerprint=first.fingerprint,
        )
    assert endpoint.value.code == "prepared_environment_mismatch"


async def test_kernel_failure_and_timeout_have_stable_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[SIDECAR_ID] = SIDECAR_ID
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    gate_path, _ = _gated_policy_file(tmp_path, sidecar=SIDECAR_ID)

    fake.nft_code = 1
    with pytest.raises(shell_preparation.ShellPreparationError) as unsupported:
        await shell_preparation.prepare_shell_policy(None, network_policy_path=gate_path)
    assert unsupported.value.code == "kernel_feature_unsupported"
    assert unsupported.value.action_condition

    fake.nft_code = 124
    with pytest.raises(shell_preparation.ShellPreparationError) as timeout:
        await shell_preparation.prepare_shell_policy(None, network_policy_path=gate_path)
    assert timeout.value.code == "preparation_timeout"
    assert timeout.value.action_condition


async def test_nft_cancellation_propagates_without_recording_prepared_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[SIDECAR_ID] = SIDECAR_ID
    cache = _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    gate_path, _ = _gated_policy_file(tmp_path, sidecar=SIDECAR_ID)
    original = fake.__call__
    nft_started = asyncio.Event()

    async def wait_during_nft(*argv: str, timeout: float = 15.0,
                              stdin_bytes: bytes | None = None) -> tuple[int, str]:
        if "run" in argv and "nft" in argv:
            nft_started.set()
            await asyncio.Event().wait()
        return await original(*argv, timeout=timeout, stdin_bytes=stdin_bytes)

    monkeypatch.setattr(shell_preparation, "_command", wait_during_nft)
    task = asyncio.create_task(shell_preparation.prepare_shell_policy(
        None, network_policy_path=gate_path,
    ))
    await asyncio.wait_for(nft_started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list((cache / "records").glob("*.json"))


async def test_shell_runtime_exposes_preparation_and_resolved_network_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[SIDECAR_ID] = SIDECAR_ID
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    gate_path, selected = _gated_policy_file(tmp_path, sidecar=SIDECAR_ID)

    runtime = await prepare_shell_runtime(None, network_policy_path=gate_path)

    assert runtime is not None
    assert runtime.preparation_status == "network_prerequisites_ready"
    assert runtime.network_policy is not None
    assert runtime.network_policy.sidecar_image == SIDECAR_ID
    assert runtime.network_policy_hash == policy_content_hash(selected)
    assert runtime.environment_fingerprint is not None
    assert runtime.docker_endpoint is not None


async def test_manual_runtime_cannot_drop_a_selected_gate_policy(tmp_path: Path) -> None:
    gate_path, _ = _gated_policy_file(tmp_path)
    with pytest.raises(shell_preparation.ShellPreparationError) as caught:
        await prepare_shell_runtime(None, auto_setup=False, network_policy_path=gate_path)
    assert caught.value.code == "network_policy_requires_preparation"


async def test_daemon_failure_is_not_reported_as_local_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _DockerFake(engine_queue=[(1, "")])
    _patch_docker(monkeypatch, fake, binary="/usr/bin/docker")
    with pytest.raises(shell_preparation.ShellPreparationError) as caught:
        await shell_preparation.prepare_shell_policy(None)
    assert caught.value.code == "docker_daemon_unavailable"


async def test_explicit_policy_differs_from_record_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[DIGEST_REF] = RECORDED_ID
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    manifest = _program_manifest(tmp_path, RECORDED_ID)
    recorded = await shell_preparation.prepare_shell_policy(
        _explicit_policy_file(tmp_path), program_manifest_path=str(manifest),
    )
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
    first_manifest = _program_manifest(tmp_path, IMAGE_ID)
    first = await shell_preparation.prepare_shell_policy(
        str(path), program_manifest_path=str(first_manifest),
    )
    fake.image_ids["custom:dev"] = RECORDED_ID
    fake.image_ids[RECORDED_ID] = RECORDED_ID
    second_manifest = _program_manifest(tmp_path, RECORDED_ID)
    second = await shell_preparation.prepare_shell_policy(
        str(path), program_manifest_path=str(second_manifest),
    )
    assert second.policy.image == RECORDED_ID
    assert second.fingerprint != first.fingerprint
    with pytest.raises(MCPConfigurationError, match="explicit shell policy differs"):
        await shell_preparation.prepare_shell_policy(
            str(path), expected_fingerprint=first.fingerprint,
        )


async def test_program_manifest_override_requires_explicit_shell_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _patch_cache(monkeypatch, tmp_path)
    manifest = _program_manifest(tmp_path, IMAGE_ID)

    with pytest.raises(shell_preparation.ShellPreparationError) as prepare_error:
        await shell_preparation.prepare_shell_policy(
            None, program_manifest_path=str(manifest),
        )
    assert prepare_error.value.code == "program_manifest_requires_explicit_policy"
    assert not cache.exists()

    with pytest.raises(shell_preparation.ShellPreparationError) as runtime_error:
        await prepare_shell_runtime(None, program_manifest_path=str(manifest))
    assert runtime_error.value.code == "program_manifest_requires_explicit_policy"


async def test_explicit_image_binds_cached_catalog_used_by_no_argument_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[DIGEST_REF] = RECORDED_ID
    fake.image_ids[RECORDED_ID] = RECORDED_ID
    fake.image_ids[SIDECAR_ID] = SIDECAR_ID
    cache = _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    manifest = _program_manifest(tmp_path, RECORDED_ID, name="nmap")
    original_bytes = manifest.read_bytes()
    policy_path = _explicit_policy_file(tmp_path)
    gate_path, _selection = _gated_policy_file(tmp_path, sidecar=SIDECAR_ID)

    runtime = await prepare_shell_runtime(
        policy_path,
        network_policy_path=gate_path,
        program_manifest_path=str(manifest),
    )

    assert runtime is not None
    assert runtime.policy.image == RECORDED_ID
    assert runtime.preparation_status == "network_prerequisites_ready"
    assert runtime.network_policy is not None
    assert runtime.program_source._program_manifest_path == str(
        cache / "manifests" / f"{runtime.environment_fingerprint}.json"
    )
    assert Path(runtime.program_source._program_manifest_path).read_bytes() == original_bytes
    monkeypatch.setattr(
        runtime.program_source, "_resolve_image_id", AsyncMock(return_value=RECORDED_ID),
    )
    rows = await runtime.program_source.list_image_programs()
    assert [(row.name, row.executable) for row in rows] == [("nmap", "/usr/bin/nmap")]
    record = json.loads(
        (cache / "records" / f"{runtime.environment_fingerprint}.json").read_text()
    )
    assert record["manifest_path"] == runtime.program_source._program_manifest_path


async def test_explicit_image_uses_matching_default_catalog_when_path_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[DIGEST_REF] = RECORDED_ID
    fake.image_ids[RECORDED_ID] = RECORDED_ID
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    manifest = _program_manifest(tmp_path, RECORDED_ID)
    monkeypatch.setattr(shell_workroom, "default_program_manifest_path", lambda: str(manifest))

    prepared = await shell_preparation.prepare_shell_policy(_explicit_policy_file(tmp_path))

    assert prepared.manifest_path is not None
    assert Path(prepared.manifest_path).read_bytes() == manifest.read_bytes()


async def test_explicit_image_missing_catalog_fails_before_record_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[DIGEST_REF] = RECORDED_ID
    cache = _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    missing = tmp_path / "missing-program-manifest.json"

    with pytest.raises(shell_preparation.ShellPreparationError) as caught:
        await shell_preparation.prepare_shell_policy(
            _explicit_policy_file(tmp_path), program_manifest_path=str(missing),
        )

    assert caught.value.code == "program_catalog_unavailable"
    assert not list((cache / "records").glob("*.json"))


async def test_explicit_image_mismatched_catalog_fails_before_record_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[DIGEST_REF] = RECORDED_ID
    cache = _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    manifest = _program_manifest(tmp_path, IMAGE_ID)

    with pytest.raises(shell_preparation.ShellPreparationError) as caught:
        await shell_preparation.prepare_shell_policy(
            _explicit_policy_file(tmp_path), program_manifest_path=str(manifest),
        )

    assert caught.value.code == "program_catalog_mismatch"
    assert not list((cache / "records").glob("*.json"))


async def test_explicit_image_malformed_catalog_fails_before_record_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_docker(monkeypatch).image_ids[DIGEST_REF] = RECORDED_ID
    cache = _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    manifest = tmp_path / "malformed-program-manifest.json"
    manifest.write_text(
        json.dumps({"image_digest": RECORDED_ID, "programs": [{"executables": ["bad"]}]}),
        encoding="utf-8",
    )

    with pytest.raises(shell_preparation.ShellPreparationError) as caught:
        await shell_preparation.prepare_shell_policy(
            _explicit_policy_file(tmp_path), program_manifest_path=str(manifest),
        )

    assert caught.value.code == "program_catalog_mismatch"
    assert not list((cache / "records").glob("*.json"))


async def test_fresh_managed_selection_still_requires_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_docker(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    gate_path, _selection = _gated_policy_file(
        tmp_path, sidecar=ManagedSidecarRequest(),
    )

    def recipe_missing() -> str:
        raise shell_preparation.ShellPreparationError(
            "gate_sidecar_recipe_unavailable", "fixture recipe unavailable",
        )

    monkeypatch.setattr(shell_preparation, "_gate_sidecar_recipe_hash", recipe_missing)
    with pytest.raises(shell_preparation.ShellPreparationError) as caught:
        await shell_preparation.prepare_shell_policy(None, network_policy_path=gate_path)

    assert caught.value.code == "gate_sidecar_recipe_unavailable"


async def test_catalog_content_is_part_of_identity_and_restore_uses_cached_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[DIGEST_REF] = RECORDED_ID
    fake.image_ids[RECORDED_ID] = RECORDED_ID
    cache = _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    policy_path = _explicit_policy_file(tmp_path)
    manifest = _program_manifest(tmp_path, RECORDED_ID, name="nmap")
    first = await shell_preparation.prepare_shell_policy(
        policy_path, program_manifest_path=str(manifest),
    )
    cached_first = Path(first.manifest_path or "")
    manifest.write_bytes(_program_manifest(tmp_path, RECORDED_ID, name="python3").read_bytes())
    second = await shell_preparation.prepare_shell_policy(
        policy_path, program_manifest_path=str(manifest),
    )
    assert second.fingerprint != first.fingerprint
    assert cached_first.read_bytes() != Path(second.manifest_path or "").read_bytes()

    restored = await shell_preparation.prepare_shell_policy(
        policy_path, expected_fingerprint=first.fingerprint,
    )
    assert restored.manifest_path == str(cached_first)
    assert cached_first.read_text(encoding="utf-8").find("nmap") >= 0

    record_path = cache / "records" / f"{first.fingerprint}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    Path(record["manifest_path"]).write_text(
        json.dumps({
            "image_digest": RECORDED_ID,
            "programs": [{
                "version": "1", "origin": "fixture",
                "executables": [{"command": "changed", "path": "/usr/bin/changed"}],
            }],
        }),
        encoding="utf-8",
    )
    with pytest.raises(shell_preparation.ShellPreparationError) as mutated:
        await shell_preparation.prepare_shell_policy(
            policy_path, expected_fingerprint=first.fingerprint,
        )
    assert mutated.value.code == "prepared_environment_mismatch"


async def test_explicit_resume_rejects_changed_supplied_catalog_but_uses_recorded_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[DIGEST_REF] = RECORDED_ID
    fake.image_ids[RECORDED_ID] = RECORDED_ID
    cache = _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    policy_path = _explicit_policy_file(tmp_path)
    selected_manifest = _program_manifest(tmp_path, RECORDED_ID, name="nmap")
    first = await shell_preparation.prepare_shell_policy(
        policy_path, program_manifest_path=str(selected_manifest),
    )
    recorded = Path(first.manifest_path or "")
    changed_manifest = _program_manifest(tmp_path, RECORDED_ID, name="python3")

    with pytest.raises(shell_preparation.ShellPreparationError) as changed:
        await shell_preparation.prepare_shell_policy(
            policy_path,
            program_manifest_path=str(changed_manifest),
            expected_fingerprint=first.fingerprint,
        )

    assert changed.value.code == "program_catalog_mismatch"
    assert json.loads(recorded.read_text(encoding="utf-8"))["programs"][0]["executables"][0][
        "command"
    ] == "nmap"
    cache_record = json.loads(
        (cache / "records" / f"{first.fingerprint}.json").read_text(encoding="utf-8")
    )
    assert cache_record["manifest_path"] == str(recorded)


async def test_legacy_record_without_catalog_does_not_adopt_current_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ready_docker(monkeypatch)
    fake.image_ids[DIGEST_REF] = RECORDED_ID
    cache = _patch_cache(monkeypatch, tmp_path)
    _patch_build(monkeypatch)
    policy_path = _explicit_policy_file(tmp_path)
    manifest = _program_manifest(tmp_path, RECORDED_ID)
    first = await shell_preparation.prepare_shell_policy(
        policy_path, program_manifest_path=str(manifest),
    )
    record_path = cache / "records" / f"{first.fingerprint}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["manifest_path"] = None
    record_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(shell_workroom, "default_program_manifest_path", lambda: str(manifest))

    with pytest.raises(shell_preparation.ShellPreparationError) as caught:
        await shell_preparation.prepare_shell_policy(
            policy_path, expected_fingerprint=first.fingerprint,
        )

    assert caught.value.code == "program_catalog_unavailable"
