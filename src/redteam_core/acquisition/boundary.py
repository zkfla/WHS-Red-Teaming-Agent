from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from redteam_core.acquisition.models import ArtifactBoundaryViolation


@dataclass(frozen=True)
class ArtifactBoundary:
    """Immutable root confinement for untrusted analysis artifacts."""

    root: Path

    def __post_init__(self) -> None:
        root = Path(self.root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("artifact boundary root must be a directory")
        object.__setattr__(self, "root", root)

    def resolve(self, relative_path: str | Path, *, must_exist: bool = True) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute():
            raise ArtifactBoundaryViolation("absolute artifact paths are not allowed")
        try:
            target = (self.root / raw).resolve(strict=must_exist)
        except (FileNotFoundError, OSError) as exc:
            raise ArtifactBoundaryViolation(
                f"artifact path cannot be resolved: {relative_path}"
            ) from exc
        if target != self.root and self.root not in target.parents:
            raise ArtifactBoundaryViolation("artifact path escapes configured root")
        return target

    def relative_ref(self, path: Path) -> str:
        resolved = Path(path).resolve(strict=True)
        if resolved != self.root and self.root not in resolved.parents:
            raise ArtifactBoundaryViolation("resolved artifact is outside configured root")
        return resolved.relative_to(self.root).as_posix()

    def iter_files(self) -> Iterator[Path]:
        seen: set[Path] = set()
        for current, dir_names, file_names in os.walk(self.root, followlinks=False):
            current_path = Path(current)
            safe_dirs: list[str] = []
            for name in sorted(dir_names):
                child = current_path / name
                try:
                    resolved = child.resolve(strict=True)
                except OSError as exc:
                    raise ArtifactBoundaryViolation(
                        f"artifact directory cannot be resolved: {child}"
                    ) from exc
                if resolved != self.root and self.root not in resolved.parents:
                    raise ArtifactBoundaryViolation(
                        "artifact directory symlink escapes configured root"
                    )
                if child.is_symlink():
                    continue
                safe_dirs.append(name)
            dir_names[:] = safe_dirs
            for name in sorted(file_names):
                child = current_path / name
                resolved = self.resolve(child.relative_to(self.root))
                if not resolved.is_file():
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield resolved
