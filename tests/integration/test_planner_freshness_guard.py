"""Planner admission checks reuse the captured readset without fencing siblings.

The local provider double mutates canonical state at the boundary between the
two split-planner requests. These tests exercise the production structured
client and metered request ledger; they make no provider or target calls.
"""

from __future__ import annotations

import json

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_candidate_subject_repair import _atomize
from tests.integration.test_central_development import TARGET
from tests.integration.test_hypothesis_planner import candidate
from tests.integration.test_probe_workers import (
    PROFILE,
    register,
)
from tests.integration.test_probe_workers import selection as probe_selection


def _plan(captured_candidate: dict, *, include_update: bool) -> dict:
    update = candidate(current=captured_candidate)
    candidate_id = captured_candidate["hypothesis_id"]
    return {
        "updates": [update] if include_update else [],
        "questions": [{
            "question_id": "q1",
            "objective": "Inspect one candidate premise",
            "target_node_ids": [TARGET],
            "completion_criteria": [{
                "kind": "observation",
                "description": "Inspect the preserved response",
            }],
            "candidate_refs": [candidate_id],
        }],
        "reason": "One scoped observation can resolve the candidate uncertainty",
        "termination": "continue",
    }


class _BoundaryMutationLLM(FakeLLMClient):
    """Run a test-owned mutation just before a scripted reply reaches the caller."""

    def __init__(
        self, *, include_update=True, mutate_after_plan=None, mutate_after_atomization=None,
    ):
        super().__init__()
        self.include_update = include_update
        self.mutate_after_plan = mutate_after_plan
        self.mutate_after_atomization = mutate_after_atomization
        self.plan_dispatches = 0
        self.atomizer_dispatches = 0

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        properties = response_schema.get("properties", {})
        if "questions" in properties:
            self.plan_dispatches += 1
            await self._wait_response("complete_structured", messages)
            if self.mutate_after_plan is not None:
                await self.mutate_after_plan()
            payload = json.loads(messages[-1]["content"].split("\n\nPrevious attempt failed")[0])
            captured = next(
                item for item in payload.get("candidates", [])
                if item["hypothesis_id"] == "local"
            )
            return _plan(captured, include_update=self.include_update)
        if "tasks" in properties:
            self.atomizer_dispatches += 1
            await self._wait_response("complete_structured", messages)
            if self.mutate_after_atomization is not None:
                await self.mutate_after_atomization()
            payload = json.loads(messages[-1]["content"].split("\n\nPrevious attempt failed")[0])
            return _atomize(payload)
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )


async def _update_candidate(session, identity: str, status: str = "held") -> str:
    graph = AttackGraphService(session.deps.graph_store)
    current = await graph.get_hypothesis(identity)
    assert current is not None
    task_id = await register(
        session,
        f"mutation-{identity}-{current.revision + 1}",
        updates=[candidate(current=current.model_dump(mode="json"), status=status)],
        hypotheses={identity: current.revision + 1},
    )
    return task_id


async def _invalidations(state):
    def read(tx):
        rows = tx.db.execute(
            "SELECT payload FROM state_outbox WHERE run_id=? ORDER BY sequence",
            (tx.run_id,),
        )
        events = [json.loads(raw)["envelope"] for (raw,) in rows]
        return [item["payload"] for item in events
                if item["event_type"] == "HypothesisPlanningInvalidated"]

    return await state.transaction(read, write=False)


@pytest.mark.parametrize("include_update", [True, False], ids=["with-update", "question-only"])
@pytest.mark.parametrize("mutation_boundary", ["after_plan", "after_atomizer"])
async def test_related_candidate_change_is_invalidated_without_partial_task_write(
    tmp_path, mutation_boundary, include_update,
):
    llm = _BoundaryMutationLLM(include_update=include_update)
    async with probe_selection(tmp_path, llm=llm, profile=PROFILE) as (session, llm, _):
        await register(session, "seed", updates=[candidate()], hypotheses={"local": 1})
        if mutation_boundary == "after_plan":
            llm.mutate_after_plan = lambda: _update_candidate(session, "local")
        else:
            llm.mutate_after_atomization = lambda: _update_candidate(session, "local")

        identities = await SpecialistPlanner(session.deps).advance(session.runtime)

        assert identities == []
        assert llm.plan_dispatches == 1
        assert llm.atomizer_dispatches == (0 if mutation_boundary == "after_plan" else 1)
        assert (await session.status())["usage"]["spent"] == (
            1 if mutation_boundary == "after_plan" else 2
        )
        # The seed Task and the explicit candidate-mutation Task remain; the proposed
        # atomized Task is not registered on either early invalidation or final CAS loss.
        assert len(await session.deps.central_state.list_tasks()) == 2
        invalidations = await _invalidations(session.deps.central_state)
        assert len(invalidations) == 1
        assert invalidations[0]["reason_code"] == "related_planning_inputs_changed"
        assert invalidations[0]["cause_code"] == "candidate_related_graph_changed"


@pytest.mark.parametrize("include_update", [True, False], ids=["with-update", "question-only"])
async def test_visible_unreferenced_sibling_does_not_block_atomizer_or_commit(
    tmp_path, include_update,
):
    llm = _BoundaryMutationLLM(include_update=include_update)
    async with probe_selection(tmp_path, llm=llm, profile=PROFILE) as (session, llm, _):
        await register(session, "seed", updates=[candidate()], hypotheses={"local": 1})
        await register(
            session,
            "sibling-seed",
            updates=[candidate(key="sibling")],
            hypotheses={"sibling": 1},
        )
        llm.mutate_after_plan = lambda: _update_candidate(session, "sibling")

        identities = await SpecialistPlanner(session.deps).advance(session.runtime)

        assert llm.plan_dispatches == 1
        assert llm.atomizer_dispatches == 1
        assert (await session.status())["usage"]["spent"] == 2
        assert len(identities) == 1
        assert len(await session.deps.central_state.list_tasks()) == 4
        assert await _invalidations(session.deps.central_state) == []
