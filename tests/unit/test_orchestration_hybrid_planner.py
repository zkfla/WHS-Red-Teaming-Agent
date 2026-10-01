"""H4 retired: HybridPlanner auto-selection is gone.

The old hybrid (single/clear-margin auto pick, ambiguous rerank, deterministic
top-1 fallback) assumed rule-based ranking could stand in for a decision.
That contract is retired: HybridPlanner.plan() raises ValueError and never
calls the model or auto-picks. LLMPlanner with an explicit client is the
only supported chooser; real LLM flows are covered by the new 43 tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from redteam.attack_graph.observation import GraphObservation
from redteam.core.contracts.target import HostTarget
from redteam.core.ids import AgentId, EngagementId, RunId, ToolId
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.agents.base import AgentSpec, PlanningContext, PlanStep
from redteam.orchestration.agents.planners import HybridPlanner, LLMPlanner, build_planner
from redteam.orchestration.cognition import DecisionFailure


class RecordingLLM:
    """An LLMClient that records whether it was asked anything at all."""

    def __init__(self, *, reply: dict[str, Any] | None = None, raises: Exception | None = None):
        self.calls = 0
        self.last_prompt = ""
        self._reply = reply or {}
        self._raises = raises

    async def complete(self, **kwargs: Any) -> str:
        return "ack"

    async def complete_structured(
        self, *, messages: list[dict[str, Any]], response_schema: dict, system=None, **kwargs
    ) -> dict[str, Any]:
        self.calls += 1
        self.last_prompt = "\n".join(str(item.get("content", "")) for item in messages)
        if self._raises is not None:
            raise self._raises
        return dict(self._reply)


def step(step_id: str, *, host: str = "a.local") -> PlanStep:
    return PlanStep(
        step_id=step_id,
        capability="recon",
        operation="service_discovery",
        tool_id=ToolId("recon.http_probe"),
        target=HostTarget(host=host),
        arguments={"target_ref": "lab", "path": "/"},
        reason=f"probe via {step_id}",
    )


def context(steps: list[PlanStep], *, llm=None) -> PlanningContext:
    return PlanningContext(
        agent=AgentSpec(agent_id=AgentId("recon-agent"), name="recon", planner_kind="llm"),
        observation=GraphObservation(step=1),
        candidates=steps,
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        step=1,
        llm=llm,
    )


# -- retirement: no auto pick, no rerank, no deterministic fallback ----------


async def test_hybrid_plan_is_retired() -> None:
    planner = HybridPlanner(RecordingLLM(reply={"option_id": "second"}))

    with pytest.raises(ValueError, match="retired"):
        await planner.plan(context([step("first"), step("second")]))


async def test_hybrid_never_calls_llm_or_auto_picks_on_clear_margin() -> None:
    llm = RecordingLLM(raises=AssertionError("retired planner must not call the model"))
    planner = HybridPlanner(llm, score_margin=0.15)

    with pytest.raises(ValueError, match="retired"):
        await planner.plan(context([step("second"), step("first")], llm=llm))

    assert llm.calls == 0


async def test_hybrid_fallback_helper_is_retired() -> None:
    planner = HybridPlanner(None)

    with pytest.raises(ValueError, match="retired"):
        planner._fall_back_to_top1("no LLM client available", [])


# -- LLMPlanner with an explicit client stays --------------------------------


async def test_llm_planner_honours_offered_choice_with_explicit_client() -> None:
    llm = FakeLLMClient(canned_structured={"step_id": "second"})
    planner = LLMPlanner(llm)

    picked = await planner.plan(context([step("first"), step("second")], llm=llm))

    assert picked is not None and picked.step_id == "second"
    assert planner.last_fallback_reason is None


async def test_llm_planner_refuses_unoffered_choice() -> None:
    llm = FakeLLMClient(canned_structured={"step_id": "rm -rf /"})
    planner = LLMPlanner(llm)

    with pytest.raises(DecisionFailure, match="was not offered"):
        await planner.plan(context([step("first")], llm=llm))


async def test_llm_planner_transport_failure_raises_without_leaking_secret() -> None:
    llm = RecordingLLM(raises=RuntimeError("gateway down secret-token"))
    planner = LLMPlanner(llm)

    with pytest.raises(DecisionFailure) as exc_info:
        await planner.plan(context([step("first"), step("second")], llm=llm))

    assert "RuntimeError" in str(exc_info.value)
    assert "secret-token" not in str(exc_info.value)


async def test_llm_planner_no_candidates_declines() -> None:
    planner = LLMPlanner(FakeLLMClient())

    assert await planner.plan(context([])) is None


async def test_llm_planner_without_client_raises_instead_of_top1_fallback() -> None:
    planner = LLMPlanner(None)

    with pytest.raises(DecisionFailure, match="no LLM client"):
        await planner.plan(context([step("first"), step("second")]))


# -- wiring -----------------------------------------------------------------


def test_build_planner_returns_llm_planner_for_llm_kind() -> None:
    planner = build_planner("llm", FakeLLMClient())

    assert isinstance(planner, LLMPlanner)
    assert not isinstance(planner, HybridPlanner)


def test_build_planner_rejects_non_llm_kinds() -> None:
    with pytest.raises(ValueError, match="only an explicitly configured LLM"):
        build_planner("playbook", None)
    with pytest.raises(ValueError, match="only an explicitly configured LLM"):
        build_planner("unknown", None)
