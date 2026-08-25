"""engagement/loader.py — `--engagement <ref>` 해석 규칙 + 대화형 선택용 목록."""
from __future__ import annotations

from pathlib import Path

from finredteam.engagement import loader as loader_module


def _write_minimal_engagement(dir_path: Path, engagement_id: str) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "engagement.yaml").write_text(
        f"""\
engagement_id: {engagement_id}
target:
  name: {engagement_id}
  base_url: http://localhost:5000
""",
        encoding="utf-8",
    )


def test_resolve_engagement_ref_by_direct_directory(tmp_path, monkeypatch):
    eng_dir = tmp_path / "my-engagement"
    _write_minimal_engagement(eng_dir, "my-engagement")
    resolved = loader_module.resolve_engagement_ref(str(eng_dir))
    assert resolved == eng_dir / "engagement.yaml"


def test_resolve_engagement_ref_by_direct_file(tmp_path):
    eng_dir = tmp_path / "my-engagement"
    _write_minimal_engagement(eng_dir, "my-engagement")
    resolved = loader_module.resolve_engagement_ref(str(eng_dir / "engagement.yaml"))
    assert resolved == eng_dir / "engagement.yaml"


def test_resolve_engagement_ref_by_examples_name(tmp_path, monkeypatch):
    monkeypatch.setattr(loader_module, "_ENGAGEMENTS_ROOT", tmp_path / "engagements")
    monkeypatch.setattr(loader_module, "_EXAMPLES_ROOT", tmp_path / "engagements" / "examples")
    _write_minimal_engagement(tmp_path / "engagements" / "examples" / "vulnbank", "vulnbank")

    resolved = loader_module.resolve_engagement_ref("vulnbank")
    assert resolved == tmp_path / "engagements" / "examples" / "vulnbank" / "engagement.yaml"


def test_resolve_engagement_ref_by_own_name(tmp_path, monkeypatch):
    monkeypatch.setattr(loader_module, "_ENGAGEMENTS_ROOT", tmp_path / "engagements")
    monkeypatch.setattr(loader_module, "_EXAMPLES_ROOT", tmp_path / "engagements" / "examples")
    _write_minimal_engagement(tmp_path / "engagements" / "my-bank", "my-bank")

    resolved = loader_module.resolve_engagement_ref("my-bank")
    assert resolved == tmp_path / "engagements" / "my-bank" / "engagement.yaml"


def test_resolve_engagement_ref_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(loader_module, "_ENGAGEMENTS_ROOT", tmp_path / "engagements")
    monkeypatch.setattr(loader_module, "_EXAMPLES_ROOT", tmp_path / "engagements" / "examples")
    assert loader_module.resolve_engagement_ref("no-such-thing") is None


def test_list_available_engagements_merges_examples_and_own(tmp_path, monkeypatch):
    root = tmp_path / "engagements"
    monkeypatch.setattr(loader_module, "_ENGAGEMENTS_ROOT", root)
    monkeypatch.setattr(loader_module, "_EXAMPLES_ROOT", root / "examples")
    _write_minimal_engagement(root / "examples" / "vulnbank", "vulnbank")
    _write_minimal_engagement(root / "my-bank", "my-bank")

    found = loader_module.list_available_engagements()
    names = sorted(p.parent.name for p in found)
    assert names == ["my-bank", "vulnbank"]


def test_list_available_engagements_excludes_examples_directory_itself(tmp_path, monkeypatch):
    root = tmp_path / "engagements"
    monkeypatch.setattr(loader_module, "_ENGAGEMENTS_ROOT", root)
    monkeypatch.setattr(loader_module, "_EXAMPLES_ROOT", root / "examples")
    _write_minimal_engagement(root / "examples" / "vulnbank", "vulnbank")

    found = loader_module.list_available_engagements()
    assert len(found) == 1  # examples/ 자기 자신이 engagements/*/engagement.yaml 로 이중 집계 안 됨


def test_list_available_engagements_empty_when_nothing_exists(tmp_path, monkeypatch):
    root = tmp_path / "engagements"
    monkeypatch.setattr(loader_module, "_ENGAGEMENTS_ROOT", root)
    monkeypatch.setattr(loader_module, "_EXAMPLES_ROOT", root / "examples")
    assert loader_module.list_available_engagements() == []
