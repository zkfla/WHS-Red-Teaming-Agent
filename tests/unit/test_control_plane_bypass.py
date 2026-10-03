"""Control Plane Bypass Test + Dependency Direction Test.

Structurally enforces instruction.md sections 4 and 9:

- `core` imports nothing else from `redteam`.
- `orchestration` (Agent Team) never imports `redteam.capability`
  or `redteam.control` internals directly.
- `intake` never reaches `orchestration`, and `capability` never
  reaches back into `app`/`control`.

This is a static/import-graph check, not a runtime mock.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).parents[2] / "src" / "redteam"


def _imported_top_level_modules(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                modules.add(node.module.split(".")[0])
    return modules


def _redteam_submodules(py_file: Path) -> set[str]:
    """Second path segment of any `redteam.<x>...` import, e.g. 'capability'."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    submodules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "redteam" and len(parts) > 1:
                    submodules.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                parts = node.module.split(".")
                if parts[0] == "redteam" and len(parts) > 1:
                    submodules.add(parts[1])
    return submodules


def _offenders_in(directory: Path, forbidden: set[str]) -> list[str]:
    offenders: list[str] = []
    for py_file in directory.rglob("*.py"):
        hit = _redteam_submodules(py_file) & forbidden
        if hit:
            offenders.append(f"{py_file}: {hit}")
    return offenders


def test_core_does_not_import_other_redteam_subpackages() -> None:
    core_dir = SRC_ROOT / "core"
    offenders = []
    for py_file in core_dir.rglob("*.py"):
        for submodule in _redteam_submodules(py_file):
            if submodule != "core":
                offenders.append(f"{py_file}: redteam.{submodule}")
    assert not offenders, f"core must not import other redteam subpackages: {offenders}"


def test_orchestration_never_imports_capability_or_control_directly() -> None:
    orchestration_dir = SRC_ROOT / "orchestration"
    forbidden = {"capability", "control"}
    offenders = []
    for py_file in orchestration_dir.rglob("*.py"):
        hit = _redteam_submodules(py_file) & forbidden
        if hit:
            offenders.append(f"{py_file}: {hit}")
    assert not offenders, (
        "orchestration must reach Capability/Control only through "
        f"redteam.core ports, never their concrete packages: {offenders}"
    )


def test_plain_import_statements_and_reverse_boundaries_hold(tmp_path: Path) -> None:
    probe = tmp_path / "probe_import.py"
    probe.write_text("import redteam.capability.foo\n", encoding="utf-8")
    assert _redteam_submodules(probe) == {"capability"}
    probe.write_text("from redteam.control import bar\n", encoding="utf-8")
    assert _redteam_submodules(probe) == {"control"}
    assert _offenders_in(SRC_ROOT / "intake", {"orchestration"}) == []
    assert _offenders_in(SRC_ROOT / "capability", {"app", "control"}) == []
    assert _offenders_in(SRC_ROOT / "orchestration", {"capability", "control"}) == []
    assert _offenders_in(SRC_ROOT / "core", {"capability", "control", "app"}) == []
