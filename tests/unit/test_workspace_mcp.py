"""File boundary regressions; no host credentials, shell or external target."""

from __future__ import annotations

import hashlib
import os
import shlex
from pathlib import Path

import pytest

from redteam.capability.servers.workspace import (
    WorkspaceError,
    WorkspaceIO,
    WorkspacePolicy,
    create_server,
)
from redteam.capability.settings import MCPConfigurationError, MCPServerSettings
from redteam.control.scope import check_scope
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import AssetTarget
from redteam.core.ids import AssetId
from redteam.orchestration.action_naming import canonical_action_key
from tests.unit.test_scope import _proposal_for

_CONTEXT = {"asset_ref": "workspace-test", "engagement_id": "eng-test", "run_id": "run-test"}


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspaceIO:
    root = tmp_path / "private-workspace"
    root.mkdir(mode=0o700)
    return WorkspaceIO(WorkspacePolicy(root=str(root), writable=True, **_CONTEXT))


def _read(workspace: WorkspaceIO, path: str, **kwargs):
    return workspace.read_file(path=path, offset_chars=0, max_chars=16000, **_CONTEXT, **kwargs)


def _write(workspace: WorkspaceIO, path: str, content: str, digest: str | None = None):
    return workspace.write_file(
        path=path,
        content=content,
        expected_sha256=digest,
        **_CONTEXT,
    )


def test_create_read_patch_and_digest_conflict(workspace: WorkspaceIO) -> None:
    created = _write(workspace, "notes.txt", "금융 시험용\nmarker")
    assert created.created
    assert created.sha256 == hashlib.sha256("금융 시험용\nmarker".encode()).hexdigest()
    assert (Path(workspace.policy.root) / "notes.txt").stat().st_mode & 0o777 == 0o600
    assert _read(workspace, "notes.txt").content == "금융 시험용\nmarker"
    for digest in (None, "0" * 64):
        with pytest.raises(WorkspaceError, match="workspace_conflict"):
            _write(workspace, "notes.txt", "lost change", digest)
    changed = workspace.apply_patch(
        path="notes.txt",
        old_text="marker",
        new_text="verified",
        expected_sha256=created.sha256,
        **_CONTEXT,
    )
    assert not changed.created
    assert _read(workspace, "notes.txt").content.endswith("verified")
    with pytest.raises(WorkspaceError, match="workspace_conflict"):
        _write(workspace, "notes.txt", "stale", created.sha256)
    assert _write(workspace, "notes.txt", "current", changed.sha256).size_bytes == 7
    assert not list(Path(workspace.policy.root).glob(".redteam-tmp-*"))


def test_unicode_character_pagination(workspace: WorkspaceIO) -> None:
    _write(workspace, "text.txt", "가나다😀")
    result = workspace.read_file(
        path="text.txt",
        offset_chars=1,
        max_chars=2,
        **_CONTEXT,
    )
    assert result.content == "나다" and result.next_offset_chars == 3 and result.truncated
    assert result.size_bytes == 13
    assert (
        workspace.read_file(
            path="text.txt",
            offset_chars=3,
            max_chars=2,
            **_CONTEXT,
        ).content
        == "😀"
    )


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "a/../b",
        "/etc/passwd",
        "a//b",
        "./a",
        "a/",
        "a\\b",
        "C:secrets",
        "a\x00b",
        "a\nb",
        ".redteam-lock",
        "a/.redteam-temp",
        "a" * 513,
    ],
)
def test_read_and_write_refuse_ambiguous_paths(workspace: WorkspaceIO, path: str) -> None:
    with pytest.raises(WorkspaceError, match="workspace_invalid_path"):
        _read(workspace, path)
    with pytest.raises(WorkspaceError, match="workspace_invalid_path"):
        _write(workspace, path, "safe")


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_unsafe_files_cannot_be_read_or_replaced(
    workspace: WorkspaceIO,
    tmp_path: Path,
    kind: str,
) -> None:
    outside = tmp_path / "outside-secret"
    outside.write_text("must-not-return-this", encoding="utf-8")
    target = Path(workspace.policy.root) / "unsafe"
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    with pytest.raises(WorkspaceError):
        _read(workspace, "unsafe")
    with pytest.raises(WorkspaceError):
        _write(workspace, "unsafe", "overwrite")
    assert outside.read_text() == "must-not-return-this"


def test_symlink_parent_and_replaced_root_are_refused(
    workspace: WorkspaceIO,
    tmp_path: Path,
) -> None:
    root = Path(workspace.policy.root)
    (root / "alias").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WorkspaceError):
        _write(workspace, "alias/outside", "no")
    root.rename(tmp_path / "old-root")
    root.mkdir(mode=0o700)
    with pytest.raises(WorkspaceError, match="workspace_root_changed"):
        workspace.list_directory(path=".", **_CONTEXT)
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_public_root_rejected(tmp_path: Path, mode: int) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    root.chmod(mode)
    with pytest.raises(WorkspaceError, match="workspace_unsafe"):
        WorkspaceIO(WorkspacePolicy(root=str(root), **_CONTEXT))


def test_root_permissions_rechecked(workspace: WorkspaceIO) -> None:
    Path(workspace.policy.root).chmod(0o755)
    with pytest.raises(WorkspaceError, match="workspace_unsafe_root"):
        workspace.list_directory(path=".", **_CONTEXT)


@pytest.mark.parametrize("field", ["asset_ref", "engagement_id", "run_id"])
def test_context_cannot_select_another_workspace(workspace: WorkspaceIO, field: str) -> None:
    context = {**_CONTEXT, field: "other"}
    with pytest.raises(WorkspaceError, match="workspace_context_mismatch"):
        workspace.list_directory(path=".", **context)


def test_read_only_policy_refuses_both_mutations(workspace: WorkspaceIO) -> None:
    reader = WorkspaceIO(workspace.policy.model_copy(update={"writable": False}))
    with pytest.raises(WorkspaceError, match="workspace_read_only"):
        _write(reader, "new.txt", "no")
    with pytest.raises(WorkspaceError, match="workspace_read_only"):
        reader.apply_patch(
            path="new.txt",
            old_text="a",
            new_text="b",
            expected_sha256="0" * 64,
            **_CONTEXT,
        )
    with pytest.raises(WorkspaceError, match="workspace_read_only"):
        create_server(reader.policy, "write")


@pytest.mark.parametrize("content", [b"\xff\xfe", b"a\x00b"])
def test_binary_read_rejected(workspace: WorkspaceIO, content: bytes) -> None:
    (Path(workspace.policy.root) / "binary").write_bytes(content)
    with pytest.raises(WorkspaceError, match="workspace_not_text"):
        _read(workspace, "binary")


@pytest.mark.parametrize("offset,size", [(-1, 10), (0, 0), (0, 16001), (10, 1)])
def test_bad_range_rejected(workspace: WorkspaceIO, offset: int, size: int) -> None:
    _write(workspace, "small.txt", "small")
    with pytest.raises(WorkspaceError, match="workspace_invalid_range"):
        workspace.read_file(path="small.txt", offset_chars=offset, max_chars=size, **_CONTEXT)


def test_bounded_search_and_listing(workspace: WorkspaceIO) -> None:
    root = Path(workspace.policy.root)
    (root / "nested").mkdir()
    _write(workspace, "nested/a.txt", "marker one\nmarker two\nother")
    _write(workspace, "b.txt", "normal")
    (root / "link").symlink_to(root / "nested/a.txt")
    result = workspace.search_files(pattern="marker", path=".", mode="content", **_CONTEXT)
    assert {match.line_number for match in result.matches} == {1, 2}
    assert result.skipped_entries == 1
    by_name = workspace.search_files(pattern="a.txt", path=".", mode="name", **_CONTEXT)
    assert [match.path for match in by_name.matches] == ["nested/a.txt"]
    assert by_name.bytes_checked == 0
    listed = workspace.list_directory(path=".", **_CONTEXT)
    assert {item.path: item.kind for item in listed.entries} == {
        "nested": "directory",
        "b.txt": "file",
        "link": "blocked",
    }
    limited = WorkspaceIO(workspace.policy.model_copy(update={"max_matches": 1, "max_entries": 1}))
    assert limited.list_directory(path=".", **_CONTEXT).truncated
    found = limited.search_files(pattern="marker", path=".", mode="content", **_CONTEXT)
    assert found.truncated and len(found.matches) == 1


def test_limits_fail_closed_and_preserve_files(workspace: WorkspaceIO) -> None:
    limited = WorkspaceIO(
        workspace.policy.model_copy(
            update={
                "max_file_bytes": 8,
                "max_workspace_files": 1,
                "max_workspace_bytes": 8,
            }
        )
    )
    _write(limited, "one.txt", "12345678")
    with pytest.raises(WorkspaceError, match="workspace_file_too_large"):
        _write(limited, "one.txt", "123456789")
    with pytest.raises(WorkspaceError, match="workspace_quota_exceeded"):
        _write(limited, "two.txt", "2")
    assert _read(limited, "one.txt").content == "12345678"
    assert not (Path(limited.policy.root) / "two.txt").exists()


def test_unknown_quota_and_concurrent_writer_fail_closed(workspace: WorkspaceIO) -> None:
    _write(workspace, "a.txt", "a")
    _write(workspace, "b.txt", "b")
    limited = WorkspaceIO(workspace.policy.model_copy(update={"max_scan_entries": 1}))
    with pytest.raises(WorkspaceError, match="workspace_quota_unknown"):
        _write(limited, "c.txt", "c")
    with workspace._write_lock():
        with pytest.raises(WorkspaceError, match="workspace_busy"):
            _write(workspace, "c.txt", "c")


def test_invalid_entry_prevents_partial_quota_approval(workspace: WorkspaceIO) -> None:
    (Path(workspace.policy.root) / "unsupported:name").write_text("existing", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="workspace_quota_unknown"):
        _write(workspace, "new.txt", "must not be written")
    assert workspace.search_files(
        pattern="existing", path=".", mode="content", **_CONTEXT
    ).truncated


def test_search_byte_and_file_budget(workspace: WorkspaceIO) -> None:
    _write(workspace, "a.txt", "marker")
    _write(workspace, "b.txt", "marker")
    limited = WorkspaceIO(workspace.policy.model_copy(update={"max_search_bytes": 6}))
    result = limited.search_files(pattern="marker", path=".", mode="content", **_CONTEXT)
    assert result.truncated and result.bytes_checked == 6 and len(result.matches) == 1
    limited = WorkspaceIO(workspace.policy.model_copy(update={"max_search_files": 1}))
    assert limited.search_files(pattern="marker", path=".", mode="content", **_CONTEXT).truncated


@pytest.mark.parametrize("digest", ["bad", "A" * 64])
def test_invalid_digest_preserves_existing_file(workspace: WorkspaceIO, digest: str) -> None:
    _write(workspace, "old.txt", "preserved")
    with pytest.raises(WorkspaceError, match="workspace_invalid_digest"):
        _write(workspace, "old.txt", "replace", digest)
    assert _read(workspace, "old.txt").content == "preserved"


@pytest.mark.parametrize("mode", ["read", "write"])
def test_documented_profile_has_exact_contract(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    path = Path(__file__).parents[2] / "deploy" / "mcp" / "workspace" / f"{mode}.env.example"
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw and not raw.startswith("#"):
            name, value = raw.split("=", 1)
            monkeypatch.setenv(name, shlex.split(value)[0])
    settings = MCPServerSettings.from_environment(f"workspace-{mode}")
    assert settings.profile == f"workspace_{mode}"
    assert set(settings.allowed_tools) == set(settings.tool_operations)
    for operations in settings.tool_operations.values():
        for operation in operations:
            assert canonical_action_key("analysis", operation) == f"analysis.{operation}"


@pytest.mark.parametrize("old,new", [("", "b"), ("missing", "b"), ("a", "b")])
def test_patch_requires_unique_match(workspace: WorkspaceIO, old: str, new: str) -> None:
    result = _write(workspace, "patch.txt", "a a")
    with pytest.raises(WorkspaceError, match="workspace_invalid_patch|workspace_conflict"):
        workspace.apply_patch(
            path="patch.txt",
            old_text=old,
            new_text=new,
            expected_sha256=result.sha256,
            **_CONTEXT,
        )
    assert _read(workspace, "patch.txt").content == "a a"


def test_control_refuses_asset_argument_different_from_approved_target() -> None:
    proposal = _proposal_for(AssetTarget(asset_id=AssetId("workspace-test")))
    proposal.capability = "analysis"
    proposal.operation = "file_read"
    proposal.arguments = {"asset_ref": "other", "path": "notes.txt"}
    scope = EngagementScope(allowed_assets=["workspace-test", "other"])
    assert not check_scope(proposal, scope)[0]
    proposal.arguments["asset_ref"] = "workspace-test"
    assert check_scope(proposal, scope)[0]


@pytest.mark.parametrize("kind,transport", [("web_api", "stdio"), ("analysis", "streamable_http")])
def test_workspace_profile_rejects_wrong_kind_or_transport(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    transport: str,
) -> None:
    prefix = "REDTEAM_MCP_WORKSPACE_TEST"
    for suffix, value in {
        "KIND": kind,
        "TRANSPORT": transport,
        "COMMAND": "python",
        "ENDPOINT": "http://127.0.0.1:9999/mcp",
        "PROFILE": "workspace_read",
    }.items():
        monkeypatch.setenv(f"{prefix}_{suffix}", value)
    with pytest.raises(MCPConfigurationError):
        MCPServerSettings.from_environment("workspace-test")
