"""Execution-only Docker lifecycle and immutable environment preparation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from redteam.capability.settings import MCPConfigurationError
from redteam.capability.shell_policy import ShellWorkroomPolicy, load_shell_workroom_policy
from redteam.core.environment import env_snapshot

READINESS_TIMEOUT = 180.0
LOCK_TIMEOUT = 900.0
INSTALL_GUIDE = "https://docs.docker.com/desktop/setup/install/"


@dataclass(frozen=True)
class DockerEndpoint:
    binary: str
    context: str
    host: str


@dataclass(frozen=True)
class PreparedShellEnvironment:
    policy: ShellWorkroomPolicy
    manifest_path: str | None
    fingerprint: str


async def _command(*argv: str, timeout: float = 15.0) -> tuple[int, str]:
    # Shield creation: cancellation must not abandon a child before ownership.
    spawning = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )
    process = None
    try:
        process = await asyncio.shield(spawning)
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        return process.returncode or 0, output.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        return 124, ""
    finally:
        if process is None:
            process = await spawning
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.shield(process.wait())


def _desktop_paths(binary: str | None, env: Mapping[str, str]) -> list[Path]:
    paths: list[Path] = []
    if binary:
        parents = Path(binary).parents
        if len(parents) > 2:
            paths.append(parents[2] / "Docker Desktop.exe")
    for variable, suffix in (
        ("ProgramFiles", "Docker/Docker/Docker Desktop.exe"),
        ("LOCALAPPDATA", "Programs/DockerDesktop/Docker Desktop.exe"),
    ):
        root = env.get(variable) or env.get(variable.upper())
        if root:
            paths.append(Path(root) / suffix)
    return paths


def _docker_binary(requested: str, env: Mapping[str, str]) -> str:
    found = shutil.which(requested)
    if found:
        return found
    if os.name == "nt" and requested == "docker":
        for desktop in _desktop_paths(None, env):
            candidate = desktop.parent / "resources/bin/docker.exe"
            if candidate.is_file():
                return str(candidate)
    raise MCPConfigurationError(
        f"Docker is not installed or its executable is unavailable. Install Docker "
        f"Desktop, complete its first-start setup, then rerun the CLI: {INSTALL_GUIDE}"
    )


async def resolve_docker_endpoint(policy: ShellWorkroomPolicy) -> DockerEndpoint:
    env = env_snapshot()
    binary = _docker_binary(policy.docker_binary, env)
    context = policy.docker_context or env.get("DOCKER_CONTEXT", "").strip()
    if not context and env.get("DOCKER_HOST", "").strip():
        raise MCPConfigurationError(
            "Automatic setup requires a named Docker context when DOCKER_HOST is set. "
            "Select DOCKER_CONTEXT or use REDTEAM_SHELL_AUTO_SETUP=false with a manual policy."
        )
    if not context:
        code, context = await _command(binary, "context", "show")
        if code or not context:
            raise MCPConfigurationError("Could not resolve the active Docker context.")
    pinned = policy.model_copy(update={"docker_binary": binary, "docker_context": context})
    # Validate the resolved context before it enters a command or durable policy.
    pinned = ShellWorkroomPolicy.model_validate(pinned.model_dump())
    code, host = await _command(
        *pinned.docker_command(
            "context",
            "inspect",
            context,
            "--format",
            "{{.Endpoints.docker.Host}}",
        )
    )
    if code or not host:
        raise MCPConfigurationError("Could not resolve the selected Docker endpoint.")
    return DockerEndpoint(binary, context, host)


def _launch_desktop(binary: str, env: Mapping[str, str]) -> None:
    desktop = next((path for path in _desktop_paths(binary, env) if path.is_file()), None)
    if desktop is None:
        raise MCPConfigurationError(
            "Docker Desktop could not be started. Install/start Docker Desktop and "
            f"complete its initial setup: {INSTALL_GUIDE}"
        )
    windows_subprocess: Any = subprocess  # Windows-only attributes, called only on Windows.
    startup = windows_subprocess.STARTUPINFO()
    startup.dwFlags |= windows_subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    subprocess.Popen(
        [str(desktop)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        startupinfo=startup,
        creationflags=(
            windows_subprocess.DETACHED_PROCESS | windows_subprocess.CREATE_NEW_PROCESS_GROUP
        ),
    )


async def ensure_docker_ready(
    policy: ShellWorkroomPolicy,
    *,
    progress: Callable[[str], None] | None = None,
) -> DockerEndpoint:
    endpoint = await resolve_docker_endpoint(policy)
    pinned = policy.model_copy(
        update={
            "docker_binary": endpoint.binary,
            "docker_context": endpoint.context,
        }
    )

    async def engine_ready() -> bool:
        code, engine = await _command(*pinned.docker_command("info", "--format", "{{.OSType}}"))
        if code:
            return False
        if engine != "linux":
            raise MCPConfigurationError("Kali requires Docker's Linux container engine.")
        return True

    if await engine_ready():
        return endpoint
    # Docker Desktop's documented engine pipes, not arbitrary local named pipes.
    desktop_pipes = {
        "npipe:////./pipe/docker_engine",
        "npipe:////./pipe/dockerdesktoplinuxengine",
        "npipe:////./pipe/dockerdesktopwindowsengine",
    }
    local_desktop = (
        os.name == "nt"
        and endpoint.host.lower() in desktop_pipes
        and any(path.is_file() for path in _desktop_paths(endpoint.binary, env_snapshot()))
    )
    if not local_desktop:
        raise MCPConfigurationError(
            "The selected Docker engine is unavailable. Start that engine and rerun; "
            "an unavailable remote/custom endpoint will not start local Docker Desktop."
        )
    if progress:
        progress("Starting Docker Desktop; waiting for its Linux engine...")
    code, _ = await _command(endpoint.binary, "desktop", "start", "--detach", timeout=30)
    if code:
        _launch_desktop(endpoint.binary, env_snapshot())
    deadline = time.monotonic() + READINESS_TIMEOUT
    while time.monotonic() < deadline:
        if await engine_ready():
            return endpoint
        await asyncio.sleep(2)
    raise MCPConfigurationError(
        "Docker Desktop did not become ready within 180 seconds. Complete any Desktop "
        "license, WSL or permission prompt, select Linux containers, then rerun."
    )


def _cache_directory() -> Path:
    env = env_snapshot()
    root = env.get("LOCALAPPDATA") if os.name == "nt" else env.get("XDG_CACHE_HOME")
    return (Path(root) if root else Path.home() / ".cache") / "redteam/shell-workroom-prep"


@asynccontextmanager
async def _preparation_lock(path: Path):
    handle = path.open("a+b")
    locked = False
    deadline = time.monotonic() + LOCK_TIMEOUT
    try:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while not locked:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    windows_lock: Any = msvcrt
                    windows_lock.locking(handle.fileno(), windows_lock.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise MCPConfigurationError(
                        "Timed out waiting for Kali environment preparation."
                    ) from None
                await asyncio.sleep(0.2)
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                windows_unlock: Any = msvcrt
                windows_unlock.locking(handle.fileno(), windows_unlock.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _json_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _manifest_hash(path: str | None, image: str) -> str | None:
    if path is None:
        return None
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("image_digest") != image:
        raise MCPConfigurationError("Prepared program manifest does not match the pinned image.")
    if not isinstance(data.get("programs"), list):
        raise MCPConfigurationError("Prepared program manifest is invalid.")
    return hashlib.sha256(raw).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def _restore(
    cache: Path,
    fingerprint: str,
    endpoint: DockerEndpoint,
    explicit_policy: ShellWorkroomPolicy | None,
) -> PreparedShellEnvironment:
    if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
        raise MCPConfigurationError("Prepared environment fingerprint is invalid.")
    record = json.loads((cache / "records" / f"{fingerprint}.json").read_text(encoding="utf-8"))
    policy = ShellWorkroomPolicy.model_validate(record["policy"])
    policy.validate_image()
    manifest = record["manifest_path"]
    identity = {
        "policy": policy.model_dump(exclude_none=True),
        "endpoint": record["endpoint"],
        "manifest_hash": _manifest_hash(manifest, policy.image),
    }
    if _json_hash(identity) != fingerprint or record["endpoint"] != asdict(endpoint):
        raise MCPConfigurationError("The recorded Docker environment or endpoint has changed.")
    if explicit_policy is not None:
        code, selected_image = await _command(
            *explicit_policy.docker_command(
                "image",
                "inspect",
                explicit_policy.image,
                "--format",
                "{{.Id}}",
            )
        )
        normalized = explicit_policy.model_copy(
            update={
                "image": selected_image,
                "allow_unpinned_image": False,
            }
        )
        if code or normalized != policy:
            raise MCPConfigurationError(
                "The explicit shell policy differs from the recorded environment."
            )
    code, image = await _command(
        *policy.docker_command(
            "image",
            "inspect",
            policy.image,
            "--format",
            "{{.Id}}",
        )
    )
    if code or image != policy.image:
        raise MCPConfigurationError("The recorded Kali image is unavailable; restore it to resume.")
    return PreparedShellEnvironment(policy, manifest, fingerprint)


async def prepare_shell_policy(
    policy_path: str | None,
    *,
    progress: Callable[[str], None] | None = None,
    expected_fingerprint: str | None = None,
) -> PreparedShellEnvironment:
    """Prepare once, or restore the immutable environment recorded by a Run."""
    try:
        explicit = load_shell_workroom_policy(policy_path) if policy_path is not None else None
        initial = explicit or ShellWorkroomPolicy(image="sha256:" + "0" * 64)
        endpoint = await ensure_docker_ready(initial, progress=progress)
        initial = initial.model_copy(
            update={
                "docker_binary": endpoint.binary,
                "docker_context": endpoint.context,
            }
        )
        cache = _cache_directory()
        for directory in (cache, cache / "records", cache / "manifests"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        async with _preparation_lock(cache / "prep.lock"):
            if expected_fingerprint is not None:
                return await _restore(
                    cache,
                    expected_fingerprint,
                    endpoint,
                    initial if explicit is not None else None,
                )
            active_path = cache / "active.json"
            active = (
                json.loads(active_path.read_text(encoding="utf-8"))
                if explicit is None and active_path.exists()
                else {}
            )
            if not isinstance(active, dict):
                raise MCPConfigurationError("The prepared environment index is invalid.")
            request = _json_hash(
                {
                    "endpoint": asdict(endpoint),
                    "selection": initial.model_dump() if explicit is not None else "default",
                }
            )
            if explicit is None and request in active:
                return await _restore(
                    cache, active[request], endpoint, initial if explicit else None
                )
            manifest: str | None = None
            if explicit is None:
                from redteam.capability.shell_image import build_default_image

                image, manifest = await build_default_image(initial, cache, progress)
            else:
                code, image = await _command(
                    *initial.docker_command(
                        "image",
                        "inspect",
                        initial.image,
                        "--format",
                        "{{.Id}}",
                    )
                )
                if code:
                    raise MCPConfigurationError("The configured shell image is unavailable.")
            policy = initial.model_copy(update={"image": image, "allow_unpinned_image": False})
            policy.validate_image()
            identity = {
                "policy": policy.model_dump(exclude_none=True),
                "endpoint": asdict(endpoint),
                "manifest_hash": _manifest_hash(manifest, image),
            }
            fingerprint = _json_hash(identity)
            if manifest is not None:
                destination = cache / "manifests" / f"{fingerprint}.json"
                if Path(manifest) != destination:
                    shutil.copyfile(manifest, destination)
                manifest = str(destination)
            _atomic_json(
                cache / "records" / f"{fingerprint}.json",
                {
                    "policy": policy.model_dump(exclude_none=True),
                    "endpoint": asdict(endpoint),
                    "manifest_path": manifest,
                },
            )
            if explicit is None:
                active[request] = fingerprint
                _atomic_json(active_path, active)
            return PreparedShellEnvironment(policy, manifest, fingerprint)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, MCPConfigurationError):
            raise
        raise MCPConfigurationError(
            "Kali preparation or recorded environment is unavailable "
            f"({type(exc).__name__}); no replacement environment was started."
        ) from None
