from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from redteam_core.acquisition import (
    AcquisitionBudget,
    AcquisitionBudgetExceeded,
    AcquisitionLimits,
    AnalysisHint,
    ArtifactBoundary,
    ArtifactBoundaryViolation,
    ArtifactOversizeError,
    inventory_artifacts,
    rank_hints,
    read_artifact_text,
    search_artifact_text,
)


def _boundary(tmp_path: Path) -> ArtifactBoundary:
    root = tmp_path / "root"
    root.mkdir()
    (root / "b.txt").write_text("second needle\n", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "a.txt").write_text("first needle\nother\n", encoding="utf-8")
    return ArtifactBoundary(root)


def test_boundary_rejects_parent_and_absolute_escape(tmp_path: Path):
    boundary = _boundary(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(ArtifactBoundaryViolation, match="escapes"):
        boundary.resolve("../outside.txt")
    with pytest.raises(ArtifactBoundaryViolation, match="absolute"):
        boundary.resolve(outside)


def test_boundary_rejects_directory_link_escape(tmp_path: Path):
    boundary = _boundary(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.txt").write_text("hidden", encoding="utf-8")
    link = boundary.root / "escape"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("Windows junction creation is unavailable in this environment")
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactBoundaryViolation, match="symlink escapes"):
        tuple(boundary.iter_files())


def test_inventory_is_deterministic_and_bounded(tmp_path: Path):
    boundary = _boundary(tmp_path)
    budget = AcquisitionBudget()
    first = inventory_artifacts(boundary, budget, suffix="txt", limit=1)
    assert first.truncated
    assert len(first.entries) == 1
    assert first.entries[0].relative_path == "b.txt"

    second = inventory_artifacts(boundary, AcquisitionBudget(), suffix="txt", limit=10)
    assert [item.relative_path for item in second.entries] == ["b.txt", "sub/a.txt"]


def test_read_rejects_oversize_and_enforces_output_cap(tmp_path: Path):
    boundary = _boundary(tmp_path)
    limits = AcquisitionLimits(max_single_file_bytes=5)
    with pytest.raises(ArtifactOversizeError, match="max_single_file_bytes"):
        read_artifact_text(boundary, AcquisitionBudget(limits=limits), "b.txt")

    result = read_artifact_text(
        boundary,
        AcquisitionBudget(),
        "sub/a.txt",
        max_characters=5,
    )
    assert result.content == "first"
    assert result.truncated
    assert result.content_trust == "UNTRUSTED_ARTIFACT"
    with pytest.raises(ValueError, match="positive"):
        read_artifact_text(boundary, AcquisitionBudget(), "b.txt", max_characters=0)


def test_search_skips_oversize_without_treating_it_as_negative_evidence(tmp_path: Path):
    boundary = _boundary(tmp_path)
    limits = AcquisitionLimits(max_single_file_bytes=15, max_search_results=10)
    result = search_artifact_text(
        boundary,
        AcquisitionBudget(limits=limits),
        "needle",
    )
    assert [item.relative_path for item in result.hits] == ["b.txt"]
    assert result.hits[0].content_trust == "UNTRUSTED_ARTIFACT"
    assert result.skipped_oversize_files == ("sub/a.txt",)


def test_budget_exhaustion_fails_closed(tmp_path: Path):
    boundary = _boundary(tmp_path)
    budget = AcquisitionBudget(limits=AcquisitionLimits(max_scanned_files=1))
    with pytest.raises(AcquisitionBudgetExceeded, match="scanned-file"):
        inventory_artifacts(boundary, budget, limit=10)

    byte_budget = AcquisitionBudget(
        limits=AcquisitionLimits(max_total_bytes_read=5, max_single_file_bytes=100)
    )
    with pytest.raises(AcquisitionBudgetExceeded, match="byte budget"):
        read_artifact_text(boundary, byte_budget, "b.txt")


def test_explicit_zero_limits_are_rejected_instead_of_becoming_defaults(tmp_path: Path):
    boundary = _boundary(tmp_path)
    with pytest.raises(ValueError, match="positive"):
        inventory_artifacts(boundary, AcquisitionBudget(), limit=0)
    with pytest.raises(ValueError, match="positive"):
        search_artifact_text(boundary, AcquisitionBudget(), "needle", limit=0)


def test_hints_are_advisory_and_only_define_priority(tmp_path: Path):
    _boundary(tmp_path)
    low = AnalysisHint.create(
        hint_type_id="reference.hint.text@1",
        subject_ref="sub/a.txt",
        score=10,
        reason="weak signal",
    )
    high = AnalysisHint.create(
        hint_type_id="reference.hint.text@1",
        subject_ref="b.txt",
        score=100,
        reason="strong signal",
    )
    assert all(item.advisory_only for item in (low, high))
    assert rank_hints((low, high)) == (high, low)
