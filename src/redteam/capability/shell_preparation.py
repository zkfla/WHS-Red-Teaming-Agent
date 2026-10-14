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
from typing import Any, Literal

from redteam.capability.gated_network_policy import (
    GatedNetworkPolicy,
    GatedNetworkPolicyRequest,
    policy_content_hash,
    resolve_managed_sidecar,
)
from redteam.capability.gated_window import GATED_QUEUE_NUM, build_ruleset
from redteam.capability.settings import MCPConfigurationError
from redteam.capability.shell_policy import ShellWorkroomPolicy, load_shell_workroom_policy
from redteam.core.environment import env_snapshot

READINESS_TIMEOUT = 180.0
LOCK_TIMEOUT = 900.0
INSTALL_GUIDE = "https://docs.docker.com/desktop/setup/install/"
PreparedShellStatus = Literal["local_only", "network_prerequisites_ready"]
_IMAGE_ID_PREFIX = "sha256:"


class ShellPreparationError(MCPConfigurationError):
    """Safe, stable preparation failure for configuration and availability callers."""

    def __init__(self, code: str, action_condition: str) -> None:
        self.code = code
        self.action_condition = action_condition
        super().__init__(f"{code}: {action_condition}")


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
    endpoint: DockerEndpoint
    network_policy: GatedNetworkPolicy | None = None
    network_policy_hash: str | None = None
    status: PreparedShellStatus = "local_only"
    readiness_checked_at: str | None = None


async def _command(
    *argv: str, timeout: float = 15.0, stdin_bytes: bytes | None = None,
) -> tuple[int, str]:
    # Shield creation: cancellation must not abandon a child before ownership.
    spawning = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )
    process = None
    try:
        process = await asyncio.shield(spawning)
        communicate = (
            process.communicate(input=stdin_bytes)
            if stdin_bytes is not None else process.communicate()
        )
        output, _ = await asyncio.wait_for(communicate, timeout)
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
    raise ShellPreparationError(
        "docker_daemon_unavailable",
        f"Docker is unavailable. Install and start Docker Desktop, then retry: {INSTALL_GUIDE}",
    )


async def resolve_docker_endpoint(policy: ShellWorkroomPolicy) -> DockerEndpoint:
    env = env_snapshot()
    binary = _docker_binary(policy.docker_binary, env)
    context = policy.docker_context or env.get("DOCKER_CONTEXT", "").strip()
    if not context and env.get("DOCKER_HOST", "").strip():
        raise ShellPreparationError(
            "docker_daemon_unavailable",
            "Select the intended Docker context when DOCKER_HOST is set or use "
            "REDTEAM_SHELL_AUTO_SETUP=false with a manual policy.",
        )
    if not context:
        code, context = await _command(binary, "context", "show")
        if code or not context:
            raise ShellPreparationError(
                "docker_daemon_unavailable", "Select an available Docker context and retry."
            )
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
        raise ShellPreparationError(
            "docker_daemon_unavailable", "Restore the selected Docker endpoint and retry."
        )
    return DockerEndpoint(binary, context, host)


def _launch_desktop(binary: str, env: Mapping[str, str]) -> None:
    desktop = next((path for path in _desktop_paths(binary, env) if path.is_file()), None)
    if desktop is None:
        raise ShellPreparationError(
            "docker_daemon_unavailable",
            f"Docker Desktop could not be started. Complete its first-start setup: {INSTALL_GUIDE}",
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
            raise ShellPreparationError(
                "docker_daemon_unavailable",
                "Kali requires Docker's Linux container engine; select it and retry.",
            )
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
        raise ShellPreparationError(
            "docker_daemon_unavailable",
            "The selected Docker engine is unavailable. Start that engine and rerun; "
            "an unavailable remote/custom endpoint will not start local Docker Desktop.",
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
    raise ShellPreparationError(
        "docker_daemon_unavailable",
        "Docker Desktop did not become ready within 180 seconds. Complete its setup, "
        "select Linux containers, then rerun.",
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


def _load_network_policy(
    path: str | None,
) -> tuple[GatedNetworkPolicy | GatedNetworkPolicyRequest | None, str | None]:
    if path is None:
        return None, None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        selected: GatedNetworkPolicy | GatedNetworkPolicyRequest
        if "managed_sidecar" in data:
            selected = GatedNetworkPolicyRequest.model_validate(data)
        else:
            selected = GatedNetworkPolicy.model_validate(data)
        if isinstance(selected, GatedNetworkPolicy):
            try:
                selected.require_sidecar_image()
            except ValueError as exc:
                raise ShellPreparationError(
                    "sidecar_image_missing", "Select an available pinned gate-sidecar image."
                ) from exc
        return selected, policy_content_hash(selected)
    except ShellPreparationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ShellPreparationError(
            "network_policy_mismatch",
            "Select a valid immutable gated policy and retry preparation.",
        ) from exc


def _gate_sidecar_recipe() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[3]
    dockerfile = root / "deploy" / "docker" / "gate-sidecar" / "Dockerfile"
    required = (
        dockerfile,
        root / "deploy" / "docker" / "gate-sidecar" / "gate-daemon",
        root / "src" / "redteam" / "capability" / "gate_daemon.py",
        root / "src" / "redteam" / "capability" / "gate_wire.py",
        root / "src" / "redteam" / "capability" / "gate_nfqueue.py",
    )
    if not all(path.is_file() for path in required):
        raise ShellPreparationError(
            "gate_sidecar_recipe_unavailable",
            "Restore the repository gate-sidecar recipe before selecting managed preparation.",
        )
    return root, dockerfile


def _gate_sidecar_recipe_hash() -> str:
    root, _dockerfile = _gate_sidecar_recipe()
    files = (
        "deploy/docker/gate-sidecar/Dockerfile",
        "deploy/docker/gate-sidecar/gate-daemon",
        "src/redteam/capability/gate_daemon.py",
        "src/redteam/capability/gate_wire.py",
        "src/redteam/capability/gate_nfqueue.py",
    )
    return _json_hash({name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                       for name in files})


def _valid_image_id(value: str) -> bool:
    return (len(value) == 71 and value.startswith(_IMAGE_ID_PREFIX)
            and all(char in "0123456789abcdef" for char in value[7:]))


async def _inspect_image(
    docker_policy: ShellWorkroomPolicy,
    image: str,
    *,
    failure_code: str,
    action_condition: str,
) -> str:
    code, image_id = await _command(
        *docker_policy.docker_command(
            "image", "inspect", image, "--format", "{{.Id}}",
        ),
        timeout=30.0,
    )
    if code == 124:
        raise ShellPreparationError(
            "preparation_timeout", "Restore the selected Docker daemon and retry preparation."
        )
    if code or not _valid_image_id(image_id):
        raise ShellPreparationError(failure_code, action_condition)
    return image_id


async def _resolve_network_policy(
    selected: GatedNetworkPolicy | GatedNetworkPolicyRequest | None,
    selected_hash: str | None,
    docker_policy: ShellWorkroomPolicy,
    *,
    recipe_hash: str | None,
) -> GatedNetworkPolicy | None:
    if selected is None:
        return None
    if isinstance(selected, GatedNetworkPolicyRequest):
        if recipe_hash is None:
            raise ShellPreparationError(
                "gate_sidecar_recipe_unavailable",
                "Restore the repository gate-sidecar recipe before selecting managed preparation.",
            )
        root, dockerfile = _gate_sidecar_recipe()
        tag = f"whs-gate-sidecar-managed:{recipe_hash[:16]}-{(selected_hash or '')[:12]}"
        code, _output = await _command(
            *docker_policy.docker_command(
                "build", "--quiet", "--tag", tag, "--file", str(dockerfile), str(root),
            ),
            timeout=600.0,
        )
        if code == 124:
            raise ShellPreparationError(
                "preparation_timeout",
                "Retry the selected managed sidecar build on the same daemon.",
            )
        if code:
            raise ShellPreparationError(
                "gate_sidecar_build_failed",
                "Inspect the repository gate-sidecar build prerequisites and retry.",
            )
        image_id = await _inspect_image(
            docker_policy, tag, failure_code="sidecar_image_missing",
            action_condition="Restore the managed gate-sidecar image on the selected daemon.",
        )
        try:
            return resolve_managed_sidecar(selected, image_id)
        except ValueError as exc:
            raise ShellPreparationError(
                "network_policy_mismatch", "Select a valid managed gate-sidecar request."
            ) from exc

    try:
        reference = selected.require_sidecar_image()
    except ValueError as exc:
        raise ShellPreparationError(
            "sidecar_image_missing", "Select an available pinned gate-sidecar image."
        ) from exc
    image_id = await _inspect_image(
        docker_policy, reference, failure_code="sidecar_image_missing",
        action_condition="Restore the explicitly selected pinned gate-sidecar image locally.",
    )
    return GatedNetworkPolicy.model_validate({
        **selected.model_dump(mode="python"), "sidecar_image": image_id,
    })


async def _check_network_readiness(
    docker_policy: ShellWorkroomPolicy, policy: GatedNetworkPolicy,
) -> str:
    quota_bytes = policy.default_target_caps.max_output_bytes
    try:
        rules = build_ruleset(queue_num=GATED_QUEUE_NUM, allowed=(), quota_bytes=quota_bytes)
    except ValueError as exc:
        raise ShellPreparationError(
            "network_policy_mismatch", "Select a valid finite target quota and retry preparation."
        ) from exc
    rules_bytes = rules.replace("\r\n", "\n").encode("utf-8")
    if b"\r" in rules_bytes:
        raise ShellPreparationError(
            "network_policy_mismatch", "The generated gate readiness rules are not LF encoded."
        )
    code, _output = await _command(
        *docker_policy.docker_command(
            "run", "--rm", "--pull=never", "--network", "none",
            "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
            "--entrypoint", "nft", "-i", policy.require_sidecar_image(),
            "--check", "-f", "-",
        ),
        timeout=60.0,
        stdin_bytes=rules_bytes,
    )
    if code == 124:
        raise ShellPreparationError(
            "preparation_timeout", "Retry the bounded nft readiness check on the selected daemon."
        )
    if code:
        raise ShellPreparationError(
            "kernel_feature_unsupported",
            "Select a Linux Docker daemon with nftables quota and NFQUEUE support.",
        )
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _manifest_hash(path: str | None, image: str) -> str | None:
    if path is None:
        return None
    raw = _validated_manifest_bytes(path, image)
    return hashlib.sha256(raw).hexdigest()


def _validated_manifest_bytes(path: str, image: str) -> bytes:
    try:
        raw = Path(path).read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ShellPreparationError(
            "program_catalog_unavailable",
            "Restore the Kali program manifest and retry preparation.",
        ) from exc
    if not isinstance(data, dict) or data.get("image_digest") != image:
        raise ShellPreparationError(
            "program_catalog_mismatch",
            "Prepared program manifest does not match the pinned image; "
            "restore a matching catalog.",
        )
    programs = data.get("programs")
    if not isinstance(programs, list) or any(
        not isinstance(program, dict)
        or not isinstance(program.get("executables", []), list)
        or any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("command"), str)
            or not isinstance(entry.get("path"), str)
            for entry in program.get("executables", [])
        )
        for program in programs
    ):
        raise ShellPreparationError(
            "program_catalog_mismatch", "Repair the Kali program manifest for the pinned image."
        )
    return raw


def _atomic_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
    selected_program_manifest_path: str | None,
    selected_network_policy: GatedNetworkPolicy | GatedNetworkPolicyRequest | None,
    selected_network_policy_hash: str | None,
) -> PreparedShellEnvironment:
    if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
        raise ShellPreparationError(
            "prepared_environment_mismatch",
            "Prepared environment fingerprint is invalid; "
            "select the exact recorded Run environment.",
        )
    try:
        record = json.loads(
            (cache / "records" / f"{fingerprint}.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ShellPreparationError(
            "prepared_environment_missing", "Restore the recorded environment before resuming."
        ) from exc
    policy = ShellWorkroomPolicy.model_validate(record["policy"])
    policy.validate_image()
    manifest = record.get("manifest_path")
    if not isinstance(manifest, str) or not manifest:
        raise ShellPreparationError(
            "program_catalog_unavailable",
            "This Run has no recorded Kali program catalog; prepare a new environment to resume.",
        )
    expected_manifest = (cache / "manifests" / f"{fingerprint}.json").resolve()
    if Path(manifest).resolve() != expected_manifest:
        raise ShellPreparationError(
            "prepared_environment_mismatch",
            "The recorded Kali program catalog is not the immutable catalog for this Run.",
        )
    identity: dict[str, Any] = {
        "policy": policy.model_dump(exclude_none=True),
        "endpoint": record["endpoint"],
        "manifest_hash": _manifest_hash(manifest, policy.image),
    }
    if explicit_policy is not None and selected_program_manifest_path is not None:
        selected_manifest_hash = _manifest_hash(selected_program_manifest_path, policy.image)
        if selected_manifest_hash != identity["manifest_hash"]:
            raise ShellPreparationError(
                "program_catalog_mismatch",
                "The supplied Kali program catalog differs from the recorded Run catalog.",
            )
    recorded_policy_hash = record.get("network_policy_hash")
    recorded_resolved_policy_hash = record.get("resolved_network_policy_hash")
    if recorded_policy_hash is not None:
        identity["network_policy_hash"] = recorded_policy_hash
        identity["resolved_network_policy_hash"] = recorded_resolved_policy_hash
    if _json_hash(identity) != fingerprint or record["endpoint"] != asdict(endpoint):
        raise ShellPreparationError(
            "prepared_environment_mismatch",
            "The recorded Docker environment or endpoint has changed.",
        )
    if selected_network_policy_hash != recorded_policy_hash:
        raise ShellPreparationError(
            "network_policy_mismatch",
            "Select the recorded gated policy content to resume this Run.",
        )
    if explicit_policy is not None:
        selected_image = await _inspect_image(
            explicit_policy.model_copy(update={
                "docker_binary": endpoint.binary, "docker_context": endpoint.context,
            }),
            explicit_policy.image, failure_code="kali_image_missing",
            action_condition="Restore the explicitly selected Kali image locally.",
        )
        normalized = explicit_policy.model_copy(
            update={
                "image": selected_image,
                "allow_unpinned_image": False,
            }
        )
        if normalized != policy:
            raise ShellPreparationError(
                "prepared_environment_mismatch",
                "The explicit shell policy differs from the recorded environment.",
            )
    docker_policy = policy.model_copy(update={
        "docker_binary": endpoint.binary, "docker_context": endpoint.context,
    })
    image = await _inspect_image(
        docker_policy, policy.image, failure_code="kali_image_missing",
        action_condition="The recorded Kali image is unavailable; restore it to resume.",
    )
    if image != policy.image:
        raise ShellPreparationError(
            "kali_image_missing", "The recorded Kali image is unavailable; restore it to resume."
        )

    resolved_network_policy = None
    checked_at = None
    status: PreparedShellStatus = "local_only"
    if recorded_policy_hash is not None:
        try:
            resolved_network_policy = GatedNetworkPolicy.model_validate(record["network_policy"])
            resolved_network_policy.require_sidecar_image()
        except (KeyError, ValueError, TypeError) as exc:
            raise ShellPreparationError(
                "network_policy_mismatch", "Restore the recorded immutable gated policy."
            ) from exc
        if policy_content_hash(resolved_network_policy) != recorded_resolved_policy_hash:
            raise ShellPreparationError(
                "network_policy_mismatch", "The recorded gated policy content has changed."
            )
        if isinstance(selected_network_policy, GatedNetworkPolicy):
            reference = selected_network_policy.require_sidecar_image()
            selected_image = await _inspect_image(
                docker_policy, reference, failure_code="sidecar_image_missing",
                action_condition=(
                    "Restore the explicitly selected pinned gate-sidecar image locally."
                ),
            )
            if selected_image != resolved_network_policy.sidecar_image:
                raise ShellPreparationError(
                    "network_policy_mismatch",
                    "The selected pinned gate-sidecar no longer matches the recorded Run.",
                )
        sidecar_image = await _inspect_image(
            docker_policy, resolved_network_policy.require_sidecar_image(),
            failure_code="sidecar_image_missing",
            action_condition="Restore the recorded immutable gate-sidecar image locally.",
        )
        if sidecar_image != resolved_network_policy.sidecar_image:
            raise ShellPreparationError(
                "sidecar_image_missing",
                "Restore the recorded immutable gate-sidecar image locally.",
            )
        checked_at = await _check_network_readiness(docker_policy, resolved_network_policy)
        status = "network_prerequisites_ready"
        record["readiness_checked_at"] = checked_at
        record["preparation_status"] = status
        _atomic_json(cache / "records" / f"{fingerprint}.json", record)
    return PreparedShellEnvironment(
        policy=policy, manifest_path=manifest, fingerprint=fingerprint, endpoint=endpoint,
        network_policy=resolved_network_policy, network_policy_hash=recorded_policy_hash,
        status=status, readiness_checked_at=checked_at,
    )


async def prepare_shell_policy(
    policy_path: str | None,
    *,
    program_manifest_path: str | None = None,
    network_policy_path: str | None = None,
    progress: Callable[[str], None] | None = None,
    expected_fingerprint: str | None = None,
) -> PreparedShellEnvironment:
    """Prepare once, or restore the immutable environment recorded by a Run."""
    if policy_path is None and program_manifest_path is not None:
        raise ShellPreparationError(
            "program_manifest_requires_explicit_policy",
            "Select an explicit Kali image policy before supplying a program manifest.",
        )
    try:
        explicit = load_shell_workroom_policy(policy_path) if policy_path is not None else None
        initial = explicit or ShellWorkroomPolicy(image="sha256:" + "0" * 64)
        selected_network_policy, selected_network_policy_hash = _load_network_policy(
            network_policy_path
        )
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
                    program_manifest_path,
                    selected_network_policy,
                    selected_network_policy_hash,
                )
            # Recipe state belongs to fresh managed selection. A durable
            # expected-fingerprint restore uses the recorded immutable sidecar
            # in _restore and must not require today's build recipe.
            recipe_hash = (
                _gate_sidecar_recipe_hash()
                if isinstance(selected_network_policy, GatedNetworkPolicyRequest) else None
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
                    "network_policy_hash": selected_network_policy_hash,
                    "gate_recipe_hash": recipe_hash,
                }
            )
            if explicit is None and request in active:
                return await _restore(
                    cache, active[request], endpoint, initial if explicit else None,
                    program_manifest_path, selected_network_policy,
                    selected_network_policy_hash,
                )
            manifest: str | None = None
            if explicit is None:
                from redteam.capability.shell_image import build_default_image

                image, built_manifest = await build_default_image(initial, cache, progress)
                manifest = program_manifest_path or built_manifest
            else:
                image = await _inspect_image(
                    initial, initial.image, failure_code="kali_image_missing",
                    action_condition=(
                        "The configured Kali image is unavailable; restore it locally."
                    ),
                )
                if program_manifest_path is not None:
                    manifest = program_manifest_path
                else:
                    from redteam.capability.shell_workroom import default_program_manifest_path

                    manifest = default_program_manifest_path()
            policy = initial.model_copy(update={"image": image, "allow_unpinned_image": False})
            policy.validate_image()
            docker_policy = policy.model_copy(update={
                "docker_binary": endpoint.binary, "docker_context": endpoint.context,
            })
            resolved_network_policy = await _resolve_network_policy(
                selected_network_policy, selected_network_policy_hash, docker_policy,
                recipe_hash=recipe_hash,
            )
            checked_at = (
                await _check_network_readiness(docker_policy, resolved_network_policy)
                if resolved_network_policy is not None else None
            )
            status: PreparedShellStatus = (
                "network_prerequisites_ready"
                if resolved_network_policy is not None else "local_only"
            )
            resolved_network_policy_hash = (
                policy_content_hash(resolved_network_policy)
                if resolved_network_policy is not None else None
            )
            manifest_content = _validated_manifest_bytes(manifest, image)
            manifest_hash = hashlib.sha256(manifest_content).hexdigest()
            identity: dict[str, Any] = {
                "policy": policy.model_dump(exclude_none=True),
                "endpoint": asdict(endpoint),
                "manifest_hash": manifest_hash,
            }
            if selected_network_policy_hash is not None:
                identity["network_policy_hash"] = selected_network_policy_hash
                identity["resolved_network_policy_hash"] = resolved_network_policy_hash
            fingerprint = _json_hash(identity)
            destination = cache / "manifests" / f"{fingerprint}.json"
            _atomic_bytes(destination, manifest_content)
            manifest = str(destination)
            _atomic_json(
                cache / "records" / f"{fingerprint}.json",
                {
                    "policy": policy.model_dump(exclude_none=True),
                    "endpoint": asdict(endpoint),
                    "manifest_path": manifest,
                    **({
                        "network_policy": resolved_network_policy.model_dump(mode="json"),
                        "network_policy_hash": selected_network_policy_hash,
                        "resolved_network_policy_hash": resolved_network_policy_hash,
                        "preparation_status": status,
                        "readiness_checked_at": checked_at,
                    } if resolved_network_policy is not None else {}),
                },
            )
            if explicit is None:
                active[request] = fingerprint
                _atomic_json(active_path, active)
            return PreparedShellEnvironment(
                policy=policy, manifest_path=manifest, fingerprint=fingerprint,
                endpoint=endpoint, network_policy=resolved_network_policy,
                network_policy_hash=selected_network_policy_hash,
                status=status, readiness_checked_at=checked_at,
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, MCPConfigurationError):
            raise
        raise MCPConfigurationError(
            "Kali preparation or recorded environment is unavailable "
            f"({type(exc).__name__}); no replacement environment was started."
        ) from None
