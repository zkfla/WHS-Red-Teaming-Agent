"""Operator-only reference provisioning. No MCP Tool or Skill auto-install.

Agent reads selected artifacts via the existing approved workspace read port.
Pinned hashes detect drift, not correctness or prompt-injection safety.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from redteam.capability.servers.workspace import WorkspaceIO, WorkspacePolicy

Identity = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Domain = Literal["general", "web", "api", "recon", "mobile", "cloud_identity", "ai_llm", "vdb"]
_MAX_BYTES = 65_536
_DATA = files("redteam.capability").joinpath("reference_data")
_TEMPLATES = ("NOTICE.md", "hypothesis-log.md", "evidence-report.md", "session-notes.md")


class MethodologyError(ValueError):
    """Fixed messages; do not echo checkout content or host paths to consumers."""


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {".", ".."} for part in path.parts)
        or "\\" in value
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("invalid relative reference path")
    return value


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: Identity
    repository: Annotated[
        str, Field(pattern=r"^https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    ]
    commit: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    license: Literal["MIT"]
    license_path: Literal["LICENSE"]
    license_sha256: Digest


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reference_id: Identity
    source_id: Identity
    path: str
    sha256: Digest
    domains: tuple[Domain, ...] = Field(min_length=1, max_length=8)
    title: str = Field(min_length=1, max_length=200)
    review_caveat: str = Field(min_length=1, max_length=500)
    excluded_support: tuple[Literal["scripts", "unreviewed_detail_references"], ...]

    @field_validator("path")
    @classmethod
    def path_check(cls, value: str) -> str:
        safe_path(value)
        if not value.endswith(".md"):
            raise ValueError("reference must be Markdown data")
        return value

    @property
    def bundle_path(self) -> str:
        # Do not place SKILL.md in an auto-discovery directory.
        return f"references/{self.reference_id}.md"


class ReferenceCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    trust: Literal["untrusted_reference"]
    execution_permission: Literal[False]
    source_scripts_included: Literal[False]
    review_status: Literal["reviewed_reference_only"]
    sources: tuple[Source, ...] = Field(min_length=1, max_length=16)
    references: tuple[Reference, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def bindings(self):
        source_ids = {s.source_id for s in self.sources}
        reference_ids = {r.reference_id for r in self.references}
        if len(source_ids) != len(self.sources) or len(reference_ids) != len(self.references):
            raise ValueError("duplicate source/reference identifiers")
        if any(r.source_id not in source_ids for r in self.references):
            raise ValueError("reference uses an unknown source")
        paths = {(r.source_id, r.path) for r in self.references}
        if len(paths) != len(self.references):
            raise ValueError("duplicate source paths")
        return self

    def list_for_domain(self, domain: Domain) -> tuple[Reference, ...]:
        return tuple(r for r in self.references if domain in r.domains)


def load_catalog() -> ReferenceCatalog:
    return ReferenceCatalog.model_validate_json(_DATA.joinpath("methodologies.json").read_bytes())


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_source(root: Path, path: str) -> bytes:
    safe_path(path)
    if not root.is_absolute() or root.resolve() != root:
        raise MethodologyError("reference_invalid_checkout")
    descriptors = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for component in PurePosixPath(path).parts[:-1]:
            fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        fd = os.open(
            PurePosixPath(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > _MAX_BYTES:
            raise MethodologyError("reference_unsafe_source")
        chunks = []
        total = 0
        while chunk := os.read(fd, min(8192, _MAX_BYTES + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_BYTES:
                raise MethodologyError("reference_unsafe_source")
        after = os.fstat(fd)
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise MethodologyError("reference_source_changed")
        data = b"".join(chunks)
        data.decode("utf-8")
        return data
    except (OSError, UnicodeError):
        raise MethodologyError("reference_unsafe_source") from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _git(root: Path, *args: str) -> str:
    # Explicit argv, no shell/network/install/hooks or inherited GIT_* overrides.
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.worktree=.",
                *args,
            ],
            cwd=root,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LC_ALL": "C",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
            capture_output=True,
            check=True,
            timeout=10,
            text=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise MethodologyError("reference_checkout_unavailable") from None


def _local_data(catalog: ReferenceCatalog) -> dict[str, bytes]:
    payload = {"catalog.json": catalog.model_dump_json(indent=2).encode("utf-8")}
    for name in _TEMPLATES:
        path = "NOTICE.md" if name == "NOTICE.md" else f"templates/{name}"
        payload[path] = _DATA.joinpath("templates").joinpath(name).read_bytes()
    return payload


def _index(policy: WorkspacePolicy, catalog: ReferenceCatalog) -> dict:
    digests = {name: _digest(data) for name, data in _local_data(catalog).items()}
    digests.update({r.bundle_path: r.sha256 for r in catalog.references})
    digests.update({f"licenses/{s.source_id}.txt": s.license_sha256 for s in catalog.sources})
    return {
        "schema_version": 1,
        "asset_ref": policy.asset_ref,
        "engagement_id": policy.engagement_id,
        "run_id": policy.run_id,
        "trust": "untrusted_reference",
        "execution_permission": False,
        "files": dict(sorted(digests.items())),
    }


def provision_bundle(
    checkouts: dict[str, Path],
    destination: Path,
    *,
    asset_ref: str,
    engagement_id: str,
    run_id: str,
    catalog: ReferenceCatalog | None = None,
) -> WorkspacePolicy:
    """Trusted operator call only. All source checks precede output creation.

    Custom catalog is for reviewed deployments/tests, never an Agent argument.
    Existing destinations are never modified. Unexpected IO leaves a partial
    directory for operator inspection; it is not published as a valid bundle.
    """
    catalog = catalog or load_catalog()
    if set(checkouts) != {s.source_id for s in catalog.sources}:
        raise MethodologyError("reference_checkout_set_mismatch")
    if not destination.is_absolute() or destination.resolve() != destination:
        raise MethodologyError("reference_invalid_destination")
    policy = WorkspacePolicy(
        root=str(destination),
        writable=False,
        asset_ref=asset_ref,
        engagement_id=engagement_id,
        run_id=run_id,
        max_file_bytes=_MAX_BYTES,
        max_return_chars=16_000,
    )
    payload = _local_data(catalog)
    for source in catalog.sources:
        root = checkouts[source.source_id]
        if not root.is_absolute() or root.resolve() != root:
            raise MethodologyError("reference_invalid_checkout")
        # Git status may invoke a clean filter. Reject local executable drivers
        # and included/worktree configs before HEAD/status inspection. Global
        # and system configs are already disabled by _git.
        config = _git(root, "config", "--local", "--no-includes", "--list")
        if any(
            line.split("=", 1)[0]
            .lower()
            .startswith(("filter.", "include.", "includeif.", "extensions."))
            for line in config.splitlines()
        ):
            raise MethodologyError("reference_checkout_config_unsupported")
        if _git(root, "rev-parse", "HEAD") != source.commit:
            raise MethodologyError("reference_revision_mismatch")
        if _git(root, "status", "--porcelain", "--untracked-files=all"):
            raise MethodologyError("reference_dirty_checkout")
        data = _read_source(root, source.license_path)
        if _digest(data) != source.license_sha256:
            raise MethodologyError("reference_license_mismatch")
        payload[f"licenses/{source.source_id}.txt"] = data
    for reference in catalog.references:
        data = _read_source(checkouts[reference.source_id], reference.path)
        if _digest(data) != reference.sha256:
            raise MethodologyError("reference_digest_mismatch")
        payload[reference.bundle_path] = data
    payload["index.json"] = json.dumps(_index(policy, catalog), indent=2).encode("utf-8")
    try:
        destination.mkdir(mode=0o700)  # Exclusive, no parents/overwrite.
        # Private new directory, generated paths only, no imported scripts.
        for relative, data in payload.items():
            target = destination / relative
            target.parent.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        # Index is emitted last. Seal data directories after all writes.
        for directory in ("references", "licenses", "templates"):
            (destination / directory).chmod(0o500)
        destination.chmod(0o500)
    except OSError:
        raise MethodologyError("reference_destination_exists_or_provision_incomplete") from None
    verify_bundle(policy, catalog=catalog)
    return policy


def verify_bundle(policy: WorkspacePolicy, *, catalog: ReferenceCatalog | None = None) -> dict:
    """Operator preflight. Compare index AND every file to trusted packaged metadata.

    It does not install a loader, intercept Agent reads, or provide an OS sandbox.
    Agent must check workspace FileOutput.sha256 against the trusted index.
    """
    if policy.writable:
        raise MethodologyError("reference_readonly_policy_required")
    catalog = catalog or load_catalog()
    io = WorkspaceIO(policy)
    expected = _index(policy, catalog)

    def read(path):
        return io.read_file(
            policy.asset_ref, path, 0, policy.max_return_chars, policy.engagement_id, policy.run_id
        )

    try:
        index = read("index.json")
        if index.truncated or json.loads(index.content) != expected:
            raise MethodologyError("reference_index_mismatch")
        for path, digest in expected["files"].items():
            if read(path).sha256 != digest:
                raise MethodologyError("reference_bundle_digest_mismatch")
        # Unknown files/symlink dirs cannot silently join the reference corpus.
        observed = set()
        for directory, directories, filenames in os.walk(policy.root, followlinks=False):
            for name in directories:
                child = Path(directory) / name
                relative = child.relative_to(policy.root).as_posix()
                if relative not in {"references", "licenses", "templates"} or child.is_symlink():
                    raise MethodologyError("reference_unexpected_bundle_entry")
            for name in filenames:
                observed.add((Path(directory) / name).relative_to(policy.root).as_posix())
            if len(observed) > 100:
                raise MethodologyError("reference_unexpected_bundle_entry")
        if observed != set(expected["files"]) | {"index.json"}:
            raise MethodologyError("reference_unexpected_bundle_entry")
        return expected
    except (ValueError, OSError) as exc:
        if isinstance(exc, MethodologyError):
            raise
        raise MethodologyError("reference_bundle_invalid") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", action="append", default=[], help="source-id=/absolute/checkout"
    )
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--asset-ref", required=True)
    parser.add_argument("--engagement-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--policy-output", required=True, type=Path)
    args = parser.parse_args()
    pairs = [value.split("=", 1) for value in args.source]
    if any(len(pair) != 2 for pair in pairs) or len({p[0] for p in pairs}) != len(pairs):
        raise MethodologyError("reference_checkout_set_mismatch")
    output = args.policy_output
    if (
        not output.is_absolute()
        or output.resolve() != output
        or output.exists()
        or output == args.destination
        or args.destination in output.parents
    ):
        raise MethodologyError("reference_invalid_policy_output")
    policy = provision_bundle(
        {key: Path(value) for key, value in pairs},
        args.destination,
        asset_ref=args.asset_ref,
        engagement_id=args.engagement_id,
        run_id=args.run_id,
    )
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(policy.model_dump_json(indent=2))
    print(
        json.dumps(
            {
                "asset_ref": policy.asset_ref,
                "references": len(load_catalog().references),
                "trust": "untrusted_reference",
                "execution_permission": False,
            }
        )
    )


if __name__ == "__main__":
    main()
