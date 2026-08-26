"""Evaluator Isolation Test(§13) — Agent 런타임 코드는 ground truth(evaluator/)를
절대 읽지 않는다. `evaluator/`는 Agent와 완전히 분리된 채점 전용 프로세스다(§10).

이미 사실임을 확인했다(2026-08-26, grep으로 재확인) — 이 테스트는 그 사실을
회귀 가드로 고정한다."""
from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "finredteam"
_EVALUATOR_DIR = _PACKAGE_ROOT / "evaluator"


def _python_files_outside_evaluator() -> list[Path]:
    return [
        p
        for p in _PACKAGE_ROOT.rglob("*.py")
        if _EVALUATOR_DIR not in p.parents and p.name != "__pycache__"
    ]


def _imports_evaluator(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "finredteam.evaluator" or alias.name.startswith("finredteam.evaluator.") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "finredteam.evaluator" or module.startswith("finredteam.evaluator."):
                return True
    return False


def test_no_runtime_code_imports_evaluator_module():
    """`finredteam/evaluator/` 밖의 어떤 `.py` 파일도 `finredteam.evaluator`를
    `import`하지 않는다 — Agent가 ground truth(known vulnerabilities/expected
    invariants/GT finding labels)를 실행 중에 읽을 수 있는 경로가 없다는 뜻."""
    offenders = [str(p.relative_to(_PACKAGE_ROOT.parent)) for p in _python_files_outside_evaluator() if _imports_evaluator(p)]
    assert offenders == [], f"evaluator/ 를 import하는 런타임 코드 발견: {offenders}"


def test_evaluator_directory_exists_and_is_excluded_from_the_scan():
    # 스캔 로직 자체가 올바른 디렉터리를 보고 있는지 확인(거짓 통과 방지).
    assert _EVALUATOR_DIR.is_dir()
    assert any(_EVALUATOR_DIR in p.parents for p in _PACKAGE_ROOT.rglob("*.py"))
