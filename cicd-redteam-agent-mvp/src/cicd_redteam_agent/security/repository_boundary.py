from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path

from cicd_redteam_agent.security.redaction import redact

MAX_FILE_BYTES = 1024 * 1024
MAX_SLICE_LINES = 200
MAX_SEARCH_RESULTS = 50
EXCLUDED_DIRS = {".git", ".venv", "node_modules", "vendor", "dist", "build", "target"}


class RepositoryBoundaryError(ValueError):
    pass


class RepositoryBoundary:
    def __init__(self, root: str | Path) -> None:
        candidate = Path(root).expanduser()
        if not candidate.exists() or not candidate.is_dir():
            raise RepositoryBoundaryError(f"Repository path is not a directory: {candidate}")
        self.root = candidate.resolve(strict=True)

    def resolve(self, relative_path: str | Path, *, must_exist: bool = True) -> Path:
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
            raise RepositoryBoundaryError("Absolute, parent, and .git paths are forbidden")
        candidate = (self.root / path).resolve(strict=must_exist)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise RepositoryBoundaryError("Path escapes the repository boundary") from exc
        return candidate

    def read_text(self, relative_path: str | Path) -> str:
        path = self.resolve(relative_path)
        if not path.is_file():
            raise RepositoryBoundaryError("Only regular files can be read")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise RepositoryBoundaryError("File exceeds the 1 MiB limit")
        data = path.read_bytes()
        if b"\x00" in data:
            raise RepositoryBoundaryError("Binary files are forbidden")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RepositoryBoundaryError("File is not UTF-8 text") from exc

    def read_slice(self, relative_path: str, start_line: int, end_line: int) -> str:
        if start_line < 1 or end_line < start_line or end_line - start_line + 1 > MAX_SLICE_LINES:
            raise RepositoryBoundaryError("Line range must contain 1-200 lines")
        lines = self.read_text(relative_path).splitlines()
        return redact("\n".join(lines[start_line - 1 : end_line]))

    def search_literal(
        self, query: str, include_globs: list[str] | None = None, max_results: int = 50
    ) -> list[dict[str, object]]:
        if not query:
            raise RepositoryBoundaryError("Search query must not be empty")
        limit = min(max(max_results, 1), MAX_SEARCH_RESULTS)
        globs = include_globs or ["*", "**/*"]
        results: list[dict[str, object]] = []
        for path in sorted(self.root.rglob("*")):
            if len(results) >= limit:
                break
            relative = path.relative_to(self.root).as_posix()
            if not path.is_file() or any(part in EXCLUDED_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in {".lock", ".png", ".jpg", ".gif", ".zip", ".exe"}:
                continue
            if not any(fnmatch.fnmatch(relative, pattern) for pattern in globs):
                continue
            try:
                text = self.read_text(relative)
            except RepositoryBoundaryError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query in line:
                    results.append(
                        {
                            "path": relative,
                            "line": number,
                            "excerpt": redact(line),
                            "content_hash": hashlib.sha256(line.encode()).hexdigest(),
                        }
                    )
                    if len(results) >= limit:
                        break
        return results
