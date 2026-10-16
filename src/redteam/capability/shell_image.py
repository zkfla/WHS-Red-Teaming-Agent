from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
import sysconfig
from collections.abc import Awaitable, Callable
from pathlib import Path

from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_profiles import (
    DEFAULT_SHELL_IMAGE_PROFILE,
    PROFILE_REGISTRY_FILENAME,
    ShellImageProfile,
    package_input_sha256,
    resolve_shell_image_profile,
)
from redteam.capability.windows_process_job import WindowsProcessJob

RECIPE_FILES = (
    "Dockerfile",
    "packages.txt",
    "packages-kali-default.txt",
    PROFILE_REGISTRY_FILENAME,
    "generate-manifest.py",
)
IMAGE_TAG_PREFIX = "whs-shell-workroom"
MANIFESTS_DIRNAME = "manifests"
GENERATOR_FILENAME = "generate-manifest.py"
DOCKER_INSPECT_FORMAT = "{{.Id}}"
BUILD_TIMEOUT_SECONDS = 600.0
INSPECT_TIMEOUT_SECONDS = 60.0
MANIFEST_TIMEOUT_SECONDS = 180.0


class ShellImageError(ValueError):
    pass


def _dir_has_all(path: Path) -> bool:
    for name in RECIPE_FILES:
        if not (path / name).is_file():
            return False
    return True


def _user_data_dir() -> Path | None:
    try:
        scheme_fn = getattr(sysconfig, "get_preferred_scheme", None)
        if scheme_fn is not None:
            scheme = scheme_fn("user")
        elif os.name == "nt":
            scheme = "nt_user"
        else:
            scheme = "posix_user"
        return Path(sysconfig.get_path("data", scheme=scheme))
    except Exception:
        return None


def resolve_asset_dir() -> Path:
    here = Path(__file__).resolve()
    repo_root = here.parents[3]
    source = repo_root / "deploy" / "docker" / "shell-workroom"
    if _dir_has_all(source):
        return source
    default_data = Path(sysconfig.get_path("data")) / "share" / "redteam" / "shell-workroom"
    if _dir_has_all(default_data):
        return default_data
    user_base = _user_data_dir()
    if user_base is not None:
        user_data = user_base / "share" / "redteam" / "shell-workroom"
        if _dir_has_all(user_data):
            return user_data
    try:
        pure = Path(sysconfig.get_path("purelib"))
    except Exception:
        pure = None
    if pure is not None:
        for anc in (pure.parent.parent, pure.parent.parent.parent):
            cand = anc / "share" / "redteam" / "shell-workroom"
            if _dir_has_all(cand):
                return cand
    prefix_cand = Path(sys.prefix) / "share" / "redteam" / "shell-workroom"
    if _dir_has_all(prefix_cand):
        return prefix_cand
    return default_data


def recipe_hash(
    asset_dir: Path, profile_name: str = DEFAULT_SHELL_IMAGE_PROFILE
) -> str:
    profile = resolve_shell_image_profile(asset_dir, profile_name)
    digest = hashlib.sha256()
    base = Path(asset_dir)
    # A broad profile input does not invalidate the baseline cache and vice versa.
    for name in ("Dockerfile", PROFILE_REGISTRY_FILENAME, "generate-manifest.py",
                 profile.package_input):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((base / name).read_bytes())
        digest.update(b"|")
    return digest.hexdigest()


def image_tag_for(
    recipe_digest: str, profile_name: str = DEFAULT_SHELL_IMAGE_PROFILE
) -> str:
    short = recipe_digest[:16]
    return f"{IMAGE_TAG_PREFIX}-{profile_name}:{short}"


def _terminate_tree(proc, job: WindowsProcessJob | None = None) -> None:
    if job is not None:
        job.terminate()
        # Cancellation can arrive before the shim has joined the job.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return
    try:
        pid = proc.pid
    except Exception:
        return
    if pid is None:
        return
    if os.name == "nt":
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    else:
        try:
            # start_new_session makes pid the stable group id, even after exit.
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        except Exception:
            pass


async def _default_runner(cmd: tuple[str, ...], timeout_seconds: float,
                          *, stdin: bytes | None = None) -> tuple[int, str, str]:
    job = WindowsProcessJob() if os.name == "nt" else None
    if os.name == "nt":
        assert job is not None
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *job.command(cmd),
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )
    else:
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        )
    proc = None
    try:
        proc = await asyncio.shield(spawning)
        stdout, stderr = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        if proc is None:
            proc = await spawning
        _terminate_tree(proc, job)
        try:
            await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except Exception:
            pass
        raise ShellImageError("command timed out") from exc
    except asyncio.CancelledError:
        if proc is None:
            proc = await spawning
        _terminate_tree(proc, job)
        try:
            await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except Exception:
            pass
        raise
    finally:
        try:
            if proc is None:
                proc = await spawning
            if proc.returncode is None:
                _terminate_tree(proc, job)
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=5.0)
                except Exception:
                    pass
        finally:
            if job is not None:
                job.close()
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    code = proc.returncode
    if code is None:
        code = 1
    return (code, out, err)


async def run_preparation_command(cmd: tuple[str, ...], timeout_seconds: float,
                                  *, stdin: bytes | None = None) -> tuple[int, str, str]:
    """Bounded process lifecycle for provider preparation; never a model execution tool."""
    if stdin is None:
        return await _default_runner(cmd, timeout_seconds)
    return await _default_runner(cmd, timeout_seconds, stdin=stdin)


def _is_image_id(value: str) -> bool:
    text = value.strip()
    if not text.startswith("sha256:"):
        return False
    rest = text[len("sha256:") :]
    if len(rest) != 64:
        return False
    for ch in rest:
        if ch not in "0123456789abcdef":
            return False
    return True


def _manifest_path_for(cache_dir: Path, recipe_digest: str) -> Path:
    return Path(cache_dir) / MANIFESTS_DIRNAME / f"{recipe_digest}.json"


def _generator_path_for(asset_dir: Path) -> Path:
    return Path(asset_dir) / GENERATOR_FILENAME


def manifest_command(
    policy: ShellWorkroomPolicy,
    asset_dir: Path,
    image_id: str,
    output: Path,
    profile_name: str = DEFAULT_SHELL_IMAGE_PROFILE,
) -> tuple[str, ...]:
    gen = str(_generator_path_for(asset_dir))
    cmd: list[str] = [sys.executable, gen, "--offline", "--image", image_id]
    cmd.extend(["--output", str(output)])
    cmd.extend(["--profile", profile_name])
    cmd.extend(["--docker-binary", policy.docker_binary])
    if policy.docker_context is not None:
        cmd.extend(["--docker-context", policy.docker_context])
    return tuple(cmd)


def validate_generated_manifest(
    data: object,
    *,
    image_id: str,
    profile: ShellImageProfile,
    package_sha256: str,
) -> None:
    """Validate the v2 live-image inventory before it can be published."""
    if not isinstance(data, dict) or data.get("image_digest") != image_id:
        raise ShellImageError("manifest image digest mismatch")
    if data.get("inventory_schema_version") != 2:
        raise ShellImageError("manifest inventory schema mismatch")
    if data.get("profile") != profile.name:
        raise ShellImageError("manifest image profile mismatch")
    if data.get("package_input") != profile.package_input:
        raise ShellImageError("manifest package input mismatch")
    if data.get("package_input_sha256") != package_sha256:
        raise ShellImageError("manifest package input digest mismatch")
    programs = data.get("programs")
    if not isinstance(programs, list):
        raise ShellImageError("manifest program inventory is invalid")
    identities: set[tuple[str, str]] = set()
    for program in programs:
        if not isinstance(program, dict):
            raise ShellImageError("manifest program inventory is invalid")
        executables = program.get("executables")
        status = program.get("inventory_status")
        if not isinstance(executables, list):
            raise ShellImageError("manifest program inventory is invalid")
        if status != ("enumerated" if executables else "no_executable"):
            raise ShellImageError("manifest package executable status mismatch")
        for executable in executables:
            if not isinstance(executable, dict):
                raise ShellImageError("manifest executable inventory is invalid")
            command = executable.get("command")
            path = executable.get("path")
            path_status = executable.get("path_status")
            resolved = executable.get("resolved_path")
            if (
                not isinstance(command, str)
                or not command
                or not isinstance(path, str)
                or not path.startswith("/")
                or path_status not in {"effective", "shadowed", "off_path"}
            ):
                raise ShellImageError("manifest executable inventory is invalid")
            identity = (command, path)
            if identity in identities:
                raise ShellImageError("manifest duplicate executable identity")
            identities.add(identity)
            if path_status == "effective" and resolved != path:
                raise ShellImageError("manifest effective path mismatch")
            if path_status == "shadowed" and (
                not isinstance(resolved, str) or not resolved or resolved == path
            ):
                raise ShellImageError("manifest shadowing status mismatch")
            if path_status == "off_path" and resolved is not None:
                raise ShellImageError("manifest off-path status mismatch")


async def build_default_image(
    policy: ShellWorkroomPolicy,
    cache_dir: Path,
    progress: Callable[[str], None] | None = None,
    runner: Callable[[tuple[str, ...], float], Awaitable[tuple[int, str, str]]] | None = None,
    profile_name: str = DEFAULT_SHELL_IMAGE_PROFILE,
) -> tuple[str, str]:
    asset_dir = resolve_asset_dir()
    for name in RECIPE_FILES:
        if not (asset_dir / name).is_file():
            raise ShellImageError("canonical recipe file missing")
    profile = resolve_shell_image_profile(asset_dir, profile_name)
    package_sha256 = package_input_sha256(asset_dir, profile)
    digest = recipe_hash(asset_dir, profile.name)
    tag = image_tag_for(digest, profile.name)
    run = runner if runner is not None else _default_runner
    if progress is not None:
        progress("Checking cached image")
    inspect_cmd = policy.docker_command("inspect", "--format", DOCKER_INSPECT_FORMAT, tag)
    code, out, _err = await run(inspect_cmd, INSPECT_TIMEOUT_SECONDS)
    image_id = out.strip() if code == 0 else ""
    if code == 0 and _is_image_id(image_id):
        if progress is not None:
            progress("Reusing cached image")
    else:
        if progress is not None:
            progress("Building workroom image")
        build_cmd = policy.docker_command(
            "build",
            "--build-arg", f"SHELL_IMAGE_PROFILE={profile.name}",
            "--build-arg", f"PACKAGE_INPUT={profile.package_input}",
            "-t", tag, str(asset_dir),
        )
        bcode, _bout, _berr = await run(build_cmd, BUILD_TIMEOUT_SECONDS)
        if bcode != 0:
            raise ShellImageError("docker build failed")
        code2, out2, _err2 = await run(inspect_cmd, INSPECT_TIMEOUT_SECONDS)
        image_id = out2.strip() if code2 == 0 else ""
        if code2 != 0 or not _is_image_id(image_id):
            raise ShellImageError("image id resolve failed")
    manifests = Path(cache_dir) / MANIFESTS_DIRNAME
    manifests.mkdir(parents=True, exist_ok=True)
    manifest_path = _manifest_path_for(cache_dir, digest)
    if progress is not None:
        progress("Generating program manifest")
    gen_cmd = manifest_command(
        policy, asset_dir, image_id, manifest_path, profile.name
    )
    gcode, _gout, _gerr = await run(gen_cmd, MANIFEST_TIMEOUT_SECONDS)
    if gcode != 0:
        raise ShellImageError("manifest generation failed")
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ShellImageError("manifest unreadable") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ShellImageError("manifest is not valid JSON") from exc
    validate_generated_manifest(
        data, image_id=image_id, profile=profile, package_sha256=package_sha256
    )
    if progress is not None:
        progress("Validating program manifest")
    return (image_id, str(manifest_path))
