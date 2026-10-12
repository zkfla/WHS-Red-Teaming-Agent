"""Durable resume: recover a checkpoint in a fresh process and resume safely.

Prompt 6 split from the in-process ``reconcile_once`` path:

    process 1: run -> APPROVAL_PENDING (durable sqlite checkpoint + Approval)
    process 2: recover_runtime(run_id) -> resume_recovered(runtime)

Only an explicitly re-linked checkpointed proposal may resume; a checkpoint
whose next super-step is EXECUTE/WORKER is never replayed; and a resolved
denial goes back through the REPLAN path rather than executing.

New durable contract (this migration):

- Every model answer comes from the in-file scripted model below, which
  speaks the genuine native ReAct contract (``complete_native`` with one
  real mobile tool call on the first turn, empty completions to hold)
  and is injected via an explicit ``llm_client``. No fake model with
  canned ``{}``, no rule fallback: an unscripted route raises instead
  of completing a turn.
- Every domain store is a SQLite file under ``tmp_path``: investigation
  paths, the work ledger, the mission/task graph, and the attack graph.
  The LangGraph checkpoint itself is SQLite too. Memory-only checkpointing
  alone does not satisfy this contract.
  owning engagement, plus the target proof (the declared asset node id in
  the path steps).
- The checkpointed proposal is checked against graph provenance that names
  the owning engagement, plus the target proof (the declared asset node id
  in the work ledger row and the proposal target) and the original provider
  call id shared by the native envelope, the plan step, and the work row.
  Native runs keep no investigation path; historical path durability is
  covered separately by a genuinely legacy PathPlan fixture below.
"""

from __future__ import annotations

from pathlib import Path

from redteam.orchestration.paths.models import (
    CompletionCriterion,
    PathPlan,
    PathStep,
    is_active_status,
)
from redteam.orchestration.paths.sqlite import SQLitePathStore

RUN_ID = "run_durable"
ASSET_ID = "evil.apk"
ASSET_NODE_ID = "asset:evil.apk"
MOBILE_TOOL = "mobile.artifact_analyze"












def _files(directory) -> dict[str, str]:  # noqa: ANN001
    """Task and path state share one transaction; other durable stores are adjacent."""
    directory = Path(directory)
    return {
        "checkpoint": str(directory / "checkpoints.sqlite3"),
        "paths": str(directory / "task_graph.sqlite3"),
        "work": str(directory / "work.sqlite3"),
        "tasks": str(directory / "task_graph.sqlite3"),
        "graph": str(directory / "attack_graph.sqlite3"),
    }






































def _legacy_path_plan() -> PathPlan:
    """Genuinely legacy investigation path: an accepted/active PathPlan aimed
    at the declared asset, of the kind historical strategy-loop runs stored."""
    return PathPlan(
        path_id="path_legacy_durable",
        run_id="run_legacy_path",
        mission_id="mission_legacy",
        version=1,
        parent_version=None,
        status="active",
        reason="historical accepted investigation path",
        goal="analyze the declared artifact",
        steps=[
            PathStep(
                step_id="step-1",
                objective="analyze the declared APK artifact",
                agent_id="mobile-agent",
                target_node_ids=[ASSET_NODE_ID],
                completion_criteria=[
                    CompletionCriterion(
                        kind="evidence", description="static analysis evidence recorded",
                        min_evidence=1,
                    )
                ],
            )
        ],
    )


async def test_sqlite_legacy_path_plan_survives_reopen(tmp_path) -> None:
    """Historical path durability, independent of the native loop: a legacy
    active PathPlan registered on SQLite is still active after reopen, and
    step evidence recorded on it survives one more reopen."""
    files = _files(tmp_path)
    registered = await SQLitePathStore(files["paths"]).register(_legacy_path_plan())
    assert is_active_status(registered.status)

    reopened = await SQLitePathStore(files["paths"]).get_active("run_legacy_path")
    assert reopened is not None
    assert reopened.path_id == registered.path_id
    assert is_active_status(reopened.status)
    assert any(
        ASSET_NODE_ID in [str(t) for t in step.target_node_ids]
        for step in reopened.steps
    )

    progressed = await SQLitePathStore(files["paths"]).update_step(
        "run_legacy_path",
        registered.path_id,
        registered.version,
        "step-1",
        status="completed",
        outcome="static analysis evidence recorded",
        evidence_ids=["ev_legacy_1"],
    )
    assert progressed is not None

    again = await SQLitePathStore(files["paths"]).get_active("run_legacy_path")
    assert again is not None
    assert again.path_id == registered.path_id
    assert [(s.step_id, s.status, list(s.evidence_ids)) for s in again.steps] == [
        (s.step_id, s.status, list(s.evidence_ids)) for s in progressed.steps
    ]
    assert again.steps[0].evidence_ids == ["ev_legacy_1"]
