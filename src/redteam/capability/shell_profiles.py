"""Repository-owned Kali image profiles.

The profile registry is data shipped beside the Docker recipe.  This module
only resolves and validates that data; it never installs a package or probes
Docker.  A profile selects one package input, which is shared by the image
build and the generated program inventory.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

PROFILE_REGISTRY_FILENAME = "profiles.json"
DEFAULT_SHELL_IMAGE_PROFILE = "baseline"
_PROFILE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_PACKAGE_FILENAME = re.compile(r"^packages(?:-[a-z0-9-]+)?\.txt$")


class ShellProfileError(ValueError):
    """Profile data is absent, malformed, or names an unsafe input."""


@dataclass(frozen=True)
class ShellImageProfile:
    name: str
    package_input: str
    inventory_dependencies: bool
    description: str


def load_shell_image_profiles(asset_dir: Path) -> dict[str, ShellImageProfile]:
    path = Path(asset_dir) / PROFILE_REGISTRY_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload["profiles"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ShellProfileError("shell image profile registry is invalid") from exc
    if payload.get("version") != 1 or not isinstance(rows, dict) or not rows:
        raise ShellProfileError("shell image profile registry is invalid")
    profiles: dict[str, ShellImageProfile] = {}
    try:
        for name, raw in rows.items():
            if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name):
                raise ShellProfileError("shell image profile name is invalid")
            if not isinstance(raw, dict):
                raise ShellProfileError("shell image profile is invalid")
            package_input = raw["package_input"]
            description = raw["description"]
            dependencies = raw.get("inventory_dependencies", False)
            if (
                not isinstance(package_input, str)
                or not _PACKAGE_FILENAME.fullmatch(package_input)
                or not isinstance(description, str)
                or not description.strip()
                or not isinstance(dependencies, bool)
            ):
                raise ShellProfileError("shell image profile is invalid")
            if not (Path(asset_dir) / package_input).is_file():
                raise ShellProfileError("shell image profile package input is missing")
            profiles[name] = ShellImageProfile(
                name=name,
                package_input=package_input,
                inventory_dependencies=dependencies,
                description=description.strip(),
            )
    except KeyError as exc:
        raise ShellProfileError("shell image profile is invalid") from exc
    if DEFAULT_SHELL_IMAGE_PROFILE not in profiles:
        raise ShellProfileError("default shell image profile is missing")
    return profiles


def resolve_shell_image_profile(asset_dir: Path, name: str) -> ShellImageProfile:
    clean = str(name or "").strip()
    profiles = load_shell_image_profiles(asset_dir)
    try:
        return profiles[clean]
    except KeyError as exc:
        raise ShellProfileError("unknown shell image profile") from exc


def package_input_sha256(asset_dir: Path, profile: ShellImageProfile) -> str:
    return hashlib.sha256((Path(asset_dir) / profile.package_input).read_bytes()).hexdigest()
