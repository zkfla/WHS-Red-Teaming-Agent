"""Bounded UTF-8 workspace MCP. No host shell, network, or arbitrary roots.

The operator provisions one private root for one engagement/run/asset. POSIX
descriptor-relative IO prevents symlink traversal; writes are atomic and
digest-conditional. Read and write servers expose disjoint tool sets.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import secrets
import stat
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, Field, field_validator

_PRIVATE_PREFIX = ".redteam-"
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class WorkspaceError(ValueError):
    """Only fixed messages/codes: never echo paths, file content, or OS errors."""


class WorkspacePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_ref: str
    engagement_id: str
    run_id: str
    root: str
    writable: bool = False
    max_file_bytes: int = Field(default=1_048_576, ge=1, le=8_388_608)
    max_return_chars: int = Field(default=16_000, ge=1, le=64_000)
    max_entries: int = Field(default=200, ge=1, le=1000)
    max_scan_entries: int = Field(default=2000, ge=1, le=10_000)
    max_search_files: int = Field(default=100, ge=1, le=1000)
    max_search_bytes: int = Field(default=2_097_152, ge=1, le=16_777_216)
    max_matches: int = Field(default=50, ge=1, le=500)
    max_depth: int = Field(default=8, ge=1, le=32)
    max_workspace_files: int = Field(default=256, ge=1, le=10_000)
    max_workspace_bytes: int = Field(default=16_777_216, ge=1, le=134_217_728)

    @field_validator("asset_ref", "engagement_id", "run_id")
    @classmethod
    def valid_identity(cls, value: str) -> str:
        if not _IDENTITY.fullmatch(value):
            raise ValueError("workspace identities must be bounded opaque identifiers")
        return value

    @classmethod
    def load(cls, path: str) -> WorkspacePolicy:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


class WorkspaceEntry(BaseModel):
    path: str
    kind: Literal["file", "directory", "blocked"]
    size_bytes: int | None = None


class DirectoryOutput(BaseModel):
    asset_ref: str
    path: str
    entries: list[WorkspaceEntry]
    truncated: bool


class FileOutput(BaseModel):
    asset_ref: str
    path: str
    encoding: Literal["utf-8"] = "utf-8"
    content: str
    sha256: str
    size_bytes: int
    offset_chars: int
    next_offset_chars: int | None
    truncated: bool


class SearchMatch(BaseModel):
    path: str
    line_number: int | None = None
    excerpt: str | None = None


class SearchOutput(BaseModel):
    asset_ref: str
    path: str
    mode: Literal["name", "content"]
    matches: list[SearchMatch]
    files_checked: int
    bytes_checked: int
    skipped_entries: int
    truncated: bool


class MutationOutput(BaseModel):
    asset_ref: str
    path: str
    sha256: str
    size_bytes: int
    created: bool


def _parts(path: str, *, directory: bool = False) -> tuple[str, ...]:
    if directory and path == ".":
        return ()
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 512
        or path.startswith("/")
        or any(char in path for char in "\\:\x00\r\n")
    ):
        raise WorkspaceError("workspace_invalid_path: use a bounded relative path")
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} or part.startswith(_PRIVATE_PREFIX) for part in parts):
        raise WorkspaceError("workspace_invalid_path: ambiguous or reserved path")
    return parts


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class WorkspaceIO:
    def __init__(self, policy: WorkspacePolicy) -> None:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            raise WorkspaceError("workspace_platform_unsupported: POSIX safe IO required")
        self.policy = policy
        root = Path(policy.root)
        if not root.is_absolute() or root.is_symlink():
            raise WorkspaceError("workspace_invalid_root: provision a private absolute directory")
        self._root_path = root.resolve(strict=True)
        if self._root_path in {Path("/"), Path.home().resolve()}:
            raise WorkspaceError("workspace_invalid_root: broad system roots are forbidden")
        info = self._root_path.stat()
        self._validate_directory(info)
        if info.st_mode & 0o077:
            raise WorkspaceError("workspace_unsafe_root: require private mode 0700 root")
        self._root_identity = info.st_dev, info.st_ino

    @staticmethod
    def _validate_directory(info: os.stat_result) -> None:
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise WorkspaceError("workspace_unsafe_directory: require owned non-shared directory")

    def _check_context(self, asset_ref: str, engagement_id: str, run_id: str) -> None:
        if (asset_ref, engagement_id, run_id) != (
            self.policy.asset_ref,
            self.policy.engagement_id,
            self.policy.run_id,
        ):
            raise WorkspaceError("workspace_context_mismatch: asset/engagement/run not authorized")

    @contextmanager
    def _directory(self, parts: tuple[str, ...]) -> Iterator[int]:
        fd: int | None = None
        try:
            fd = os.open(self._root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(fd)
            self._validate_directory(info)
            if info.st_mode & 0o077:
                raise WorkspaceError("workspace_unsafe_root: require private mode 0700 root")
            if (info.st_dev, info.st_ino) != self._root_identity:
                raise WorkspaceError("workspace_root_changed: provisioned root was replaced")
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    info = os.fstat(child)
                    self._validate_directory(info)
                    if info.st_dev != self._root_identity[0]:
                        raise WorkspaceError(
                            "workspace_unsafe_directory: mounted directories refused"
                        )
                except BaseException:
                    os.close(child)
                    raise
                os.close(fd)
                fd = child
            yield fd
        except FileNotFoundError as exc:
            raise WorkspaceError("workspace_not_found: entry does not exist") from exc
        except OSError as exc:
            raise WorkspaceError("workspace_access_denied: unsafe or inaccessible entry") from exc
        finally:
            if fd is not None:
                os.close(fd)

    def _read_at(self, directory_fd: int, name: str, byte_budget: int | None = None) -> bytes:
        limit = self.policy.max_file_bytes
        if byte_budget is not None:
            limit = min(limit, byte_budget)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.getuid()
                or before.st_mode & 0o022
                or before.st_dev != self._root_identity[0]
            ):
                raise WorkspaceError(
                    "workspace_unsafe_file: require private regular single-link file"
                )
            if before.st_size > limit:
                raise WorkspaceError("workspace_file_too_large: configured file limit exceeded")
            chunks: list[bytes] = []
            total = 0
            while total <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > limit:
                raise WorkspaceError("workspace_file_too_large: configured file limit exceeded")
            if _fingerprint(before) != _fingerprint(os.fstat(fd)):
                raise WorkspaceError("workspace_file_changed: retry a fresh read")
            return b"".join(chunks)
        finally:
            os.close(fd)

    @staticmethod
    def _decode(data: bytes) -> str:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError("workspace_not_text: only UTF-8 text is supported") from exc
        if "\x00" in text:
            raise WorkspaceError("workspace_not_text: binary content is refused")
        return text

    def list_directory(
        self, asset_ref: str, path: str, engagement_id: str, run_id: str
    ) -> DirectoryOutput:
        self._check_context(asset_ref, engagement_id, run_id)
        parts = _parts(path, directory=True)
        entries: list[WorkspaceEntry] = []
        truncated = False
        with self._directory(parts) as fd, os.scandir(fd) as iterator:
            scanned = 0
            for entry in iterator:
                scanned += 1
                if scanned > self.policy.max_scan_entries:
                    truncated = True
                    break
                if entry.name.startswith(_PRIVATE_PREFIX):
                    continue
                if len(entries) == self.policy.max_entries:
                    truncated = True
                    break
                info = entry.stat(follow_symlinks=False)
                kind: Literal["file", "directory", "blocked"] = "blocked"
                if entry.name not in {".", ".."} and not any(
                    c in entry.name for c in "\\:\x00\r\n"
                ):
                    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        kind = "file"
                    elif stat.S_ISDIR(info.st_mode):
                        kind = "directory"
                entries.append(
                    WorkspaceEntry(
                        path="/".join((*parts, entry.name)),
                        kind=kind,
                        size_bytes=info.st_size if kind == "file" else None,
                    )
                )
        return DirectoryOutput(
            asset_ref=asset_ref,
            path=path,
            entries=sorted(entries, key=lambda entry: entry.path),
            truncated=truncated,
        )

    def read_file(
        self,
        asset_ref: str,
        path: str,
        offset_chars: int,
        max_chars: int,
        engagement_id: str,
        run_id: str,
    ) -> FileOutput:
        self._check_context(asset_ref, engagement_id, run_id)
        if offset_chars < 0 or not 1 <= max_chars <= self.policy.max_return_chars:
            raise WorkspaceError("workspace_invalid_range: invalid character range")
        parts = _parts(path)
        with self._directory(parts[:-1]) as fd:
            data = self._read_at(fd, parts[-1])
        text = self._decode(data)
        if offset_chars > len(text):
            raise WorkspaceError("workspace_invalid_range: offset is beyond end of file")
        end = min(offset_chars + max_chars, len(text))
        return FileOutput(
            asset_ref=asset_ref,
            path=path,
            content=text[offset_chars:end],
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            offset_chars=offset_chars,
            next_offset_chars=end if end < len(text) else None,
            truncated=end < len(text),
        )

    def _walk(self, parts: tuple[str, ...]) -> Iterator[tuple[str, os.stat_result | None]]:
        """Bounded traversal. A None sentinel means the traversal was cut short."""
        pending = deque([(parts, 0)])
        scanned = 0
        while pending:
            directory, depth = pending.popleft()
            with self._directory(directory) as fd, os.scandir(fd) as iterator:
                for entry in iterator:
                    scanned += 1
                    if scanned > self.policy.max_scan_entries:
                        yield "", None
                        return
                    if entry.name.startswith(_PRIVATE_PREFIX):
                        continue
                    name = "/".join((*directory, entry.name))
                    try:
                        _parts(name)
                        info = entry.stat(follow_symlinks=False)
                    except (WorkspaceError, OSError):
                        yield "", None
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if depth < self.policy.max_depth:
                            pending.append(((*directory, entry.name), depth + 1))
                        else:
                            yield "", None
                    else:
                        yield name, info

    def search_files(
        self,
        asset_ref: str,
        pattern: str,
        path: str,
        mode: Literal["name", "content"],
        engagement_id: str,
        run_id: str,
    ) -> SearchOutput:
        self._check_context(asset_ref, engagement_id, run_id)
        if not 1 <= len(pattern) <= 200 or mode not in {"name", "content"}:
            raise WorkspaceError("workspace_invalid_search: use a bounded literal pattern")
        parts = _parts(path, directory=True)
        matches: list[SearchMatch] = []
        files = total_bytes = skipped = 0
        truncated = False
        for name, info in self._walk(parts):
            if info is None:
                truncated = True
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                skipped += 1
                continue
            if files == self.policy.max_search_files:
                truncated = True
                break
            files += 1
            if mode == "name":
                if pattern in name:
                    matches.append(SearchMatch(path=name))
            else:
                if info.st_size > self.policy.max_file_bytes:
                    skipped += 1
                    truncated = True
                    continue
                if total_bytes + info.st_size > self.policy.max_search_bytes:
                    truncated = True
                    break
                try:
                    file_parts = _parts(name)
                    with self._directory(file_parts[:-1]) as fd:
                        data = self._read_at(
                            fd,
                            file_parts[-1],
                            self.policy.max_search_bytes - total_bytes,
                        )
                    total_bytes += len(data)
                    if total_bytes > self.policy.max_search_bytes:
                        truncated = True
                        break
                    text = self._decode(data)
                except WorkspaceError:
                    skipped += 1
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if pattern in line:
                        matches.append(
                            SearchMatch(path=name, line_number=number, excerpt=line[:200])
                        )
                        if len(matches) >= self.policy.max_matches:
                            break
            if len(matches) >= self.policy.max_matches:
                truncated = True
                break
        return SearchOutput(
            asset_ref=asset_ref,
            path=path,
            mode=mode,
            matches=matches,
            files_checked=files,
            bytes_checked=total_bytes,
            skipped_entries=skipped,
            truncated=truncated,
        )

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        if not self.policy.writable:
            raise WorkspaceError("workspace_read_only: writing is disabled by operator policy")
        fcntl = importlib.import_module("fcntl")
        with self._directory(()) as root_fd:
            lock_fd = os.open(
                f"{_PRIVATE_PREFIX}lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=root_fd,
            )
            try:
                info = os.fstat(lock_fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise WorkspaceError("workspace_unsafe_lock: invalid internal lock file")
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise WorkspaceError("workspace_busy: another write is in progress") from exc
                try:
                    yield
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _quota(self, old_size: int | None, new_size: int) -> None:
        total = files = 0
        for _name, info in self._walk(()):
            if info is None:
                raise WorkspaceError(
                    "workspace_quota_unknown: traversal limit prevents quota check"
                )
            if stat.S_ISREG(info.st_mode):
                files += 1
                total += info.st_size
        if (
            files + int(old_size is None) > self.policy.max_workspace_files
            or total - (old_size or 0) + new_size > self.policy.max_workspace_bytes
        ):
            raise WorkspaceError("workspace_quota_exceeded: workspace file/byte limit exceeded")

    def _replace(
        self,
        path: str,
        data: bytes,
        expected_sha256: str | None,
    ) -> MutationOutput:
        if len(data) > self.policy.max_file_bytes:
            raise WorkspaceError("workspace_file_too_large: configured file limit exceeded")
        if expected_sha256 is not None and not _DIGEST.fullmatch(expected_sha256):
            raise WorkspaceError("workspace_invalid_digest: expected lowercase SHA-256")
        parts = _parts(path)
        with self._directory(parts[:-1]) as fd:
            try:
                previous = self._read_at(fd, parts[-1])
            except FileNotFoundError:
                previous = None
            if previous is None:
                if expected_sha256 is not None:
                    raise WorkspaceError("workspace_conflict: expected file no longer exists")
            elif expected_sha256 is None or hashlib.sha256(previous).hexdigest() != expected_sha256:
                raise WorkspaceError("workspace_conflict: overwrite requires current SHA-256")
            self._quota(len(previous) if previous is not None else None, len(data))
            temporary = f"{_PRIVATE_PREFIX}tmp-{secrets.token_hex(16)}"
            tmp_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=fd,
            )
            try:
                with os.fdopen(tmp_fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if previous is None:
                    # Atomic no-clobber publication (rename alone overwrites).
                    os.link(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
                else:
                    if self._read_at(fd, parts[-1]) != previous:
                        raise WorkspaceError("workspace_conflict: file changed during write")
                    os.replace(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
        return MutationOutput(
            asset_ref=self.policy.asset_ref,
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            created=previous is None,
        )

    def write_file(
        self,
        asset_ref: str,
        path: str,
        content: str,
        expected_sha256: str | None,
        engagement_id: str,
        run_id: str,
    ) -> MutationOutput:
        self._check_context(asset_ref, engagement_id, run_id)
        if len(content) > self.policy.max_file_bytes:
            raise WorkspaceError("workspace_file_too_large: configured file limit exceeded")
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise WorkspaceError("workspace_not_text: invalid UTF-8 input") from exc
        self._decode(data)
        with self._write_lock():
            return self._replace(path, data, expected_sha256)

    def apply_patch(
        self,
        asset_ref: str,
        path: str,
        old_text: str,
        new_text: str,
        expected_sha256: str,
        engagement_id: str,
        run_id: str,
    ) -> MutationOutput:
        self._check_context(asset_ref, engagement_id, run_id)
        if not old_text or len(old_text) + len(new_text) > self.policy.max_file_bytes:
            raise WorkspaceError(
                "workspace_invalid_patch: bounded unique text replacement required"
            )
        parts = _parts(path)
        with self._write_lock():
            with self._directory(parts[:-1]) as fd:
                data = self._read_at(fd, parts[-1])
            text = self._decode(data)
            if text.count(old_text) != 1:
                raise WorkspaceError("workspace_conflict: old_text must match exactly once")
            try:
                updated = text.replace(old_text, new_text, 1).encode("utf-8")
            except UnicodeEncodeError as exc:
                raise WorkspaceError("workspace_not_text: invalid UTF-8 input") from exc
            self._decode(updated)
            return self._replace(path, updated, expected_sha256)


def create_server(policy: WorkspacePolicy, mode: Literal["read", "write"] = "read") -> MCPServer:
    io = WorkspaceIO(policy)
    if mode not in {"read", "write"} or (mode == "write" and not policy.writable):
        raise WorkspaceError("workspace_read_only: write server requires explicit writable policy")
    server = MCPServer(f"workspace-{mode}", log_level="WARNING")

    def list_directory(
        asset_ref: str,
        path: str = ".",
        engagement_id: str = "",
        run_id: str = "",
    ) -> DirectoryOutput:
        """List an approved run workspace; no absolute paths or link traversal."""
        return io.list_directory(asset_ref, path, engagement_id, run_id)

    def read_file(
        asset_ref: str,
        path: str,
        offset_chars: int = 0,
        max_chars: int = policy.max_return_chars,
        engagement_id: str = "",
        run_id: str = "",
    ) -> FileOutput:
        """Read bounded approved UTF-8 text. sha256 covers the complete bounded file."""
        return io.read_file(asset_ref, path, offset_chars, max_chars, engagement_id, run_id)

    def search_files(
        asset_ref: str,
        pattern: str,
        path: str = ".",
        mode: Literal["name", "content"] = "content",
        engagement_id: str = "",
        run_id: str = "",
    ) -> SearchOutput:
        """Bounded literal filename/content search; regex and unsafe entries are excluded."""
        return io.search_files(asset_ref, pattern, path, mode, engagement_id, run_id)

    def write_file(
        asset_ref: str,
        path: str,
        content: str,
        expected_sha256: str | None = None,
        engagement_id: str = "",
        run_id: str = "",
    ) -> MutationOutput:
        """Atomically create text; overwrite only with the current file SHA-256."""
        return io.write_file(asset_ref, path, content, expected_sha256, engagement_id, run_id)

    def apply_patch(
        asset_ref: str,
        path: str,
        old_text: str,
        new_text: str,
        expected_sha256: str,
        engagement_id: str = "",
        run_id: str = "",
    ) -> MutationOutput:
        """Replace exactly one literal text occurrence using a current SHA-256 precondition."""
        return io.apply_patch(
            asset_ref,
            path,
            old_text,
            new_text,
            expected_sha256,
            engagement_id,
            run_id,
        )

    functions = (
        (list_directory, read_file, search_files)
        if mode == "read"
        else (
            write_file,
            apply_patch,
        )
    )
    for function in functions:
        # Literal is reflected by actual MCP discovery, not invented by Registry.
        function.__annotations__["asset_ref"] = Literal[policy.asset_ref]
        if "path" in function.__annotations__:
            function.__annotations__["path"] = Annotated[str, Field(min_length=1, max_length=512)]
        if "max_chars" in function.__annotations__:
            function.__annotations__["max_chars"] = Annotated[
                int,
                Field(ge=1, le=policy.max_return_chars),
            ]
            function.__annotations__["offset_chars"] = Annotated[int, Field(ge=0)]
        if "pattern" in function.__annotations__:
            function.__annotations__["pattern"] = Annotated[
                str, Field(min_length=1, max_length=200)
            ]
        for name in ("content", "old_text", "new_text"):
            if name in function.__annotations__:
                function.__annotations__[name] = Annotated[
                    str, Field(max_length=policy.max_file_bytes)
                ]
        server.tool()(function)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--mode", choices=["read", "write"], default="read")
    args = parser.parse_args()
    # Trusted local stdio only; do not expose an unauthenticated filesystem HTTP service.
    create_server(WorkspacePolicy.load(args.policy), args.mode).run(transport="stdio")


if __name__ == "__main__":
    main()
