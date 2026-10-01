"""An isolated worker denial survives AGGREGATE to REPLAN (active-path gated)."""

from datetime import datetime, timedelta, timezone

import pytest

from redteam.core.enums import PolicyVerdict
from redteam.core.ids import GraphNodeId
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.dispatch import WorkerOutcome
from redteam.orchestration.nodes import aggregate_node
from redteam.orchestration.nodes.worker import worker_node
from redteam.orchestration.paths import PathPlan, PathStep
from redteam.orchestration.work.models import WorkItem, WorkStatus
from tests.fakes import FakeControlPlane, build_test_application
from tests.unit.test_fanout_approval_accounting import (
    dispatch_item,
    runtime,
    state_with,
)


async def _register_active_path_for_items(deps, items):
    path_id = "path_fanout_active"
    version = 1
    run_id = str(items[0].proposal.run_id)
    steps = []
    for index, item in enumerate(items):
        host = item.proposal.target.host
        steps.append(
            PathStep(
                step_id=f"pstep_{index}",
                objective=f"probe {host}",
                target_node_ids=[f"host:{host}"],
                allowed_operations=["recon.service_discovery"],
            )
        )
    plan = PathPlan(
        path_id=path_id,
        run_id=run_id,
        version=version,
        status="active",
        reason="fanout active path",
        goal="fanout",
        steps=steps,
    )
    await deps.path_store.register(plan)
    return path_id, version


def _bind_item_to_step(item, *, path_id, version, path_step_id):
    return item.model_copy(
        update={
            "plan_step": item.plan_step.model_copy(
                update={
                    "path_id": path_id,
                    "path_version": version,
                    "path_step_id": path_step_id,
                }
            )
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("severity", ["abort_run", "replan_goal"])
async def test_batch_preserves_highest_severity_and_redacted_reason(severity: str) -> None:
    first = dispatch_item("action_allowed", "a.local")
    denied = dispatch_item("action_denied", "b.local")
    state = state_with(
        runtime(consecutive_failures=2, remaining_budget=3),
        dispatch_items=[first, denied],
        action_proposal=first.proposal,
        worker_outcomes={
            "action_allowed": WorkerOutcome(
                action_id="action_allowed",
                work_id=first.work_id,
                policy_verdict=PolicyVerdict.ALLOW,
                executed=True,
                succeeded=True,
            ),
            "action_denied": WorkerOutcome(
                action_id="action_denied",
                work_id=denied.work_id,
                policy_verdict=PolicyVerdict.DENY,
                policy_severity=severity,
                policy_reason="denied",
                policy_budget_abort=True,
            ),
        },
    )

    result = await aggregate_node(state, deps=build_test_application())

    decision = result["policy_decision"]
    assert decision is not None
    assert decision.action_id == denied.proposal.action_id
    assert f"severity:{severity}" in decision.matched_rules
    assert "budget" in decision.matched_rules
    assert result["runtime"].consecutive_failures == 0  # sibling succeeded
    assert result["runtime"].retry_tool_calls == 0


@pytest.mark.asyncio
async def test_batch_abort_takes_precedence_over_goal_denial() -> None:
    goal = dispatch_item("action_goal", "a.local")
    abort = dispatch_item("action_abort", "b.local")
    result = await aggregate_node(
        state_with(
            runtime(),
            dispatch_items=[goal, abort],
            worker_outcomes={
                "action_goal": WorkerOutcome(
                    action_id="action_goal",
                    work_id=goal.work_id,
                    policy_verdict=PolicyVerdict.DENY,
                    policy_severity="replan_goal",
                ),
                "action_abort": WorkerOutcome(
                    action_id="action_abort",
                    work_id=abort.work_id,
                    policy_verdict=PolicyVerdict.DENY,
                    policy_severity="abort_run",
                ),
            },
        ),
        deps=build_test_application(),
    )
    decision = result["policy_decision"]
    assert decision is not None and decision.action_id == abort.proposal.action_id


@pytest.mark.asyncio
async def test_parallel_workers_create_only_one_approval_and_keep_sibling_runnable() -> None:
    plane = FakeControlPlane(default_verdict=PolicyVerdict.REQUIRE_APPROVAL)
    deps = build_test_application(control_plane=plane)
    raw_first = dispatch_item("action_first", "a.local")
    raw_sibling = dispatch_item("action_sibling", "b.local")
    path_id, version = await _register_active_path_for_items(deps, [raw_first, raw_sibling])
    first = _bind_item_to_step(raw_first, path_id=path_id, version=version, path_step_id="pstep_0")
    sibling = _bind_item_to_step(
        raw_sibling, path_id=path_id, version=version, path_step_id="pstep_1"
    )
    now = datetime.now(timezone.utc)
    for item in (first, sibling):
        await deps.work_store.add(
            WorkItem(
                work_id=item.work_id, run_id=item.proposal.run_id,
                candidate_key=item.work_id, agent_id=item.proposal.agent_id,
                target_node_ids=[GraphNodeId(f"host:{item.proposal.target.host}")],
                priority=PriorityScore(total=1), created_at=now, updated_at=now,
            )
        )
        await deps.work_store.acquire(
            item.work_id, owner=str(item.proposal.run_id), now=now,
            ttl=timedelta(seconds=60),
        )

    first_outcome = await worker_node(
        {**state_with(runtime()), "dispatch_item": first}, deps=deps
    )
    second_outcome = await worker_node(
        {**state_with(runtime()), "dispatch_item": sibling}, deps=deps
    )

    assert len(plane.evaluated) == 1
    assert len(first_outcome["worker_outcomes"]) == 1
    assert second_outcome["worker_outcomes"]["action_sibling"].approval_id is None
    pending_sibling = await deps.work_store.get(sibling.work_id)
    assert pending_sibling is not None and pending_sibling.status is WorkStatus.PENDING
