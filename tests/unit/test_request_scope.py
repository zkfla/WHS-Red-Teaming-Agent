"""Core-owned request scope: single ContextVar, compatibility re-export."""

from __future__ import annotations

import asyncio
import pathlib

from redteam.core import request_scope as core_scope
from redteam.core.request_scope import RequestScope, current_request_scope, llm_request_scope
from redteam.orchestration import metered_llm


def test_reexport_is_identical() -> None:
    assert metered_llm.RequestScope is RequestScope
    assert metered_llm.llm_request_scope is llm_request_scope
    assert metered_llm.current_request_scope is current_request_scope
    assert not hasattr(metered_llm, "_scope")


def test_single_contextvar_definition() -> None:
    repo = pathlib.Path(__file__).resolve().parents[2] / "src"
    hits = sorted(
        path for path in repo.rglob("*.py")
        if b'ContextVar("llm_request_scope"' in path.read_bytes()
    )
    assert hits == [repo / "redteam" / "core" / "request_scope.py"]


def test_core_module_has_no_redteam_imports() -> None:
    source = pathlib.Path(core_scope.__file__).read_text(encoding="utf-8").splitlines()
    imports = [line for line in source if line.startswith(("import ", "from "))]
    assert imports
    assert all("redteam" not in line for line in imports)


def test_scope_set_and_reset() -> None:
    assert current_request_scope() is None
    with llm_request_scope("run-1", "conversation"):
        assert current_request_scope() == RequestScope(run_id="run-1", owner="conversation")
    assert current_request_scope() is None


def test_nested_scope_restores_outer() -> None:
    with llm_request_scope("run-outer", "conversation"):
        with llm_request_scope("run-inner", "specialist_planning"):
            inner = current_request_scope()
            assert inner is not None and inner.run_id == "run-inner"
        outer = current_request_scope()
        assert outer is not None and outer.run_id == "run-outer"
    assert current_request_scope() is None


async def test_parallel_tasks_are_isolated() -> None:
    async def read(run_id: str, owner: str) -> str:
        with llm_request_scope(run_id, owner):
            await asyncio.sleep(0)
            scope = current_request_scope()
            assert scope is not None
            return scope.run_id + "|" + scope.owner

    first, second = await asyncio.gather(
        read("run-a", "conversation"),
        read("run-b", "specialist_planning"),
    )
    assert first == "run-a|conversation"
    assert second == "run-b|specialist_planning"
    assert current_request_scope() is None
