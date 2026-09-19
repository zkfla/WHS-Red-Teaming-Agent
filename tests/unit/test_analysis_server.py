"""Policy validation and zip-level behavior for the analysis fallback server."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from mcp import Client
from pydantic import ValidationError

from redteam.capability.servers import analysis


def _make_apk(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


@pytest.fixture
def apk_path(tmp_path: Path) -> Path:
    return _make_apk(
        tmp_path / "sample.apk",
        {
            "AndroidManifest.xml": b"\x03\x00\x08\x00binary-manifest-placeholder",
            "res/values/strings.xml": b"<resources><string>hunter2-lab-secret</string></resources>",
            "classes.dex": b"dex\n" + b"\x00" * 64,
        },
    )


@pytest.fixture
def policy(apk_path: Path) -> analysis.AnalysisServerPolicy:
    return analysis.AnalysisServerPolicy.model_validate(
        {"apks": {"sample": {"path": str(apk_path)}}}
    )


def test_apk_profile_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="existing .apk file"):
        analysis.AnalysisServerPolicy.model_validate(
            {"apks": {"sample": {"path": str(tmp_path / "missing.apk")}}}
        )


def test_apk_profile_rejects_wrong_extension(tmp_path: Path) -> None:
    other = tmp_path / "sample.zip"
    other.write_bytes(b"not-an-apk")
    with pytest.raises(ValidationError, match="existing .apk file"):
        analysis.AnalysisServerPolicy.model_validate({"apks": {"sample": {"path": str(other)}}})


def test_empty_apk_registry_rejected() -> None:
    with pytest.raises(ValidationError):
        analysis.AnalysisServerPolicy.model_validate({"apks": {}})


async def test_file_listing_reports_entries_without_content(
    policy: analysis.AnalysisServerPolicy,
) -> None:
    async with Client(analysis.create_server(policy)) as client:
        result = await client.call_tool("apk_file_listing", {"apk_ref": "sample"})
    assert result.structured_content is not None
    names = {entry["name"] for entry in result.structured_content["entries"]}
    assert names == {"AndroidManifest.xml", "res/values/strings.xml", "classes.dex"}
    assert result.structured_content["may_be_truncated"] is False


async def test_file_listing_truncates_at_configured_limit(apk_path: Path) -> None:
    limited = analysis.AnalysisServerPolicy.model_validate(
        {"apks": {"sample": {"path": str(apk_path)}}, "max_listing_entries": 2}
    )
    async with Client(analysis.create_server(limited)) as client:
        result = await client.call_tool("apk_file_listing", {"apk_ref": "sample"})
    assert result.structured_content is not None
    assert len(result.structured_content["entries"]) == 2
    assert result.structured_content["may_be_truncated"] is True


async def test_unknown_apk_ref_rejected(policy: analysis.AnalysisServerPolicy) -> None:
    async with Client(analysis.create_server(policy)) as client:
        result = await client.call_tool("apk_file_listing", {"apk_ref": "does-not-exist"})
    assert result.is_error is True


async def test_entry_digest_matches_sha256_and_never_returns_raw_bytes(
    policy: analysis.AnalysisServerPolicy,
) -> None:
    content = b"dex\n" + b"\x00" * 64
    async with Client(analysis.create_server(policy)) as client:
        result = await client.call_tool(
            "apk_entry_digest", {"apk_ref": "sample", "entry_name": "classes.dex"}
        )
    assert result.structured_content is not None
    assert result.structured_content["sha256"] == hashlib.sha256(content).hexdigest()
    assert "raw" not in result.structured_content
    assert content not in str(result.structured_content).encode()


async def test_entry_digest_unknown_entry_rejected(policy: analysis.AnalysisServerPolicy) -> None:
    async with Client(analysis.create_server(policy)) as client:
        result = await client.call_tool(
            "apk_entry_digest", {"apk_ref": "sample", "entry_name": "no/such/file"}
        )
    assert result.is_error is True


async def test_string_search_finds_pattern_and_hides_full_content(
    policy: analysis.AnalysisServerPolicy,
) -> None:
    async with Client(analysis.create_server(policy)) as client:
        result = await client.call_tool(
            "apk_string_search",
            {
                "apk_ref": "sample",
                "entry_name": "res/values/strings.xml",
                "pattern": "hunter2-lab-secret",
            },
        )
    assert result.structured_content is not None
    assert result.structured_content["match_count"] == 1
    assert result.structured_content["match_offsets"] == [
        "<resources><string>".__len__(),
    ]


async def test_string_search_rejects_short_pattern(policy: analysis.AnalysisServerPolicy) -> None:
    async with Client(analysis.create_server(policy)) as client:
        result = await client.call_tool(
            "apk_string_search",
            {"apk_ref": "sample", "entry_name": "classes.dex", "pattern": "x"},
        )
    assert result.is_error is True


async def test_string_search_allows_entries_within_size_limit(apk_path: Path) -> None:
    tight = analysis.AnalysisServerPolicy.model_validate(
        {"apks": {"sample": {"path": str(apk_path)}}, "max_search_bytes": 1024}
    )
    async with Client(analysis.create_server(tight)) as client:
        result = await client.call_tool(
            "apk_string_search",
            {"apk_ref": "sample", "entry_name": "classes.dex", "pattern": "dex"},
        )
    assert result.is_error is False


async def test_string_search_rejects_oversized_entry(tmp_path: Path) -> None:
    apk = _make_apk(tmp_path / "big.apk", {"big.bin": b"a" * 2048})
    tight = analysis.AnalysisServerPolicy.model_validate(
        {"apks": {"sample": {"path": str(apk)}}, "max_search_bytes": 1024}
    )
    async with Client(analysis.create_server(tight)) as client:
        result = await client.call_tool(
            "apk_string_search",
            {"apk_ref": "sample", "entry_name": "big.bin", "pattern": "aaa"},
        )
    assert result.is_error is True


async def test_bad_zip_file_surfaces_as_tool_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.apk"
    broken.write_bytes(b"not a real zip")
    policy = analysis.AnalysisServerPolicy.model_validate(
        {"apks": {"sample": {"path": str(broken)}}}
    )
    async with Client(analysis.create_server(policy)) as client:
        result = await client.call_tool("apk_file_listing", {"apk_ref": "sample"})
    assert result.is_error is True
