"""plan8c: context.query runtime-local tool, real central state, no model/target.

Reuses test_hypothesis_probes.py's fixtures (temporary SQLite, synthetic originals)
so this exercises the same registration path the Planner/atomizer use, then calls
the handler exactly as the native tool-call loop would.
"""

from __future__ import annotations

from types import SimpleNamespace

from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.native_local_tools import context_query_handler
from tests.unit.test_hypothesis_probes import admit, candidate, probe, setup


def _context(deps, task_id):
    runtime = SimpleNamespace(active_task_id=task_id)
    return LocalToolContext(deps=deps, runtime=runtime, purpose="test")


async def test_worker_can_list_and_read_its_own_task(tmp_path):
    deps, service = await setup(tmp_path)
    await admit(deps, service, "seed", updates=[candidate(revision=None, status="open")])
    receipt = await admit(deps, service, "p1", tasks=[probe("probe-a")])
    task_id = receipt["task_ids"][0]

    listed = await context_query_handler(
        {"kind": "task", "mode": "list"}, _context(deps, task_id),
    )
    assert listed.ok is True
    assert listed.payload["status"] == "ok"
    assert {record["identity"] for record in listed.payload["records"]} == {task_id}
    assert listed.refs == (f"task:{task_id}",)

    detail = await context_query_handler(
        {"kind": "task", "mode": "read", "refs": [{"kind": "task", "identity": task_id}],
         "section": "completion_criteria"},
        _context(deps, task_id),
    )
    assert detail.ok is True
    assert detail.payload["view_kind"] == "detail"


async def test_worker_cannot_read_a_sibling_task_with_no_shared_ancestry(tmp_path):
    deps, service = await setup(tmp_path)
    await admit(deps, service, "seed", updates=[candidate(revision=None, status="open")])
    receipt = await admit(deps, service, "p1", tasks=[probe("probe-a")])
    sibling = await admit(deps, service, "p2", tasks=[probe("probe-b")])
    task_id, sibling_id = receipt["task_ids"][0], sibling["task_ids"][0]

    result = await context_query_handler(
        {"kind": "task", "mode": "read",
         "refs": [{"kind": "task", "identity": sibling_id}]},
        _context(deps, task_id),
    )
    assert result.ok is False
    assert result.payload["status"] == "error"
    assert result.payload["code"] == "context_reference_outside_read_scope"


async def test_malformed_query_returns_a_model_input_diagnostic_not_a_crash(tmp_path):
    deps, service = await setup(tmp_path)
    await admit(deps, service, "seed", updates=[candidate(revision=None, status="open")])
    receipt = await admit(deps, service, "p1", tasks=[probe("probe-a")])
    task_id = receipt["task_ids"][0]

    result = await context_query_handler(
        {"kind": "task", "mode": "related"},  # related needs refs; none supplied
        _context(deps, task_id),
    )
    assert result.ok is False
    assert result.payload["diagnostic"]["cause_scope"] == "model_input"


async def test_no_active_task_is_refused_cleanly(tmp_path):
    deps, _service = await setup(tmp_path)
    runtime = SimpleNamespace(active_task_id=None)
    result = await context_query_handler(
        {"kind": "task", "mode": "list"},
        LocalToolContext(deps=deps, runtime=runtime, purpose="test"),
    )
    assert result.ok is False
    assert "active task" in result.error
