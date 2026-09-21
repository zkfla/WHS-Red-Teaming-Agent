"""H4: the hybrid planner spends a model call only when it is genuinely needed.

The four paths the plan asks for are all here: one candidate, a clear score
lead, an ambiguous top-K that goes to the reranker, and every failure shape
that must fall back to the deterministic top-1. The negative cases matter
most -- a planner that quietly called the model anyway, or accepted a step it
was never offered, would look identical on the happy path.
"""

from __future__ import annotations

from typing import Any

from redteam.attack_graph.observation import GraphObservation
from redteam.core.contracts.target import HostTarget
from redteam.core.ids import AgentId, EngagementId, RunId, ToolId
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.agents.base import AgentSpec, PlanningContext, PlanStep
from redteam.orchestration.agents.planners import HybridPlanner, LLMPlanner, build_planner


class RecordingLLM:
    """An LLMClient that records whether it was asked anything at all."""

    def __init__(self, *, reply: dict[str, Any] | None = None, raises: Exception | None = None):
        self.calls = 0
        self.last_prompt = ""
        self._reply = reply or {}
        self._raises = raises

    async def complete(self, **kwargs: Any) -> str:  # pragma: no cover - unused
        return "ack"

    async def complete_structured(
        self, *, messages: list[dict[str, Any]], response_schema: dict, system=None, **kwargs
    ) -> dict[str, Any]:
        self.calls += 1
        self.last_prompt = "\n".join(str(item.get("content", "")) for item in messages)
        if self._raises is not None:
            raise self._raises
        return dict(self._reply)


def step(step_id: str) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        capability="recon",
        operation="service_discovery",
        tool_id=ToolId("recon.http_probe"),
        target=HostTarget(host="a.local"),
        arguments={"target_ref": "lab", "path": "/"},
        reason=f"probe via {step_id}",
    )


def context(steps: list[PlanStep], *, scores=None, llm=None) -> PlanningContext:
    return PlanningContext(
        agent=AgentSpec(agent_id=AgentId("recon-agent"), name="recon", planner_kind="llm"),
        observation=GraphObservation(step=1),
        candidates=steps,
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        step=1,
        llm=llm,
        metadata={"candidate_scores": scores} if scores is not None else {},
    )


# -- deterministic paths ---------------------------------------------------


async def test_a_single_candidate_is_chosen_without_asking_anyone() -> None:
    llm = RecordingLLM(raises=AssertionError("the model must not be called"))
    planner = HybridPlanner(llm)

    picked = await planner.plan(context([step("only")], scores={"only": 1.4}, llm=llm))

    assert picked is not None and picked.step_id == "only"
    assert planner.last_decision == "single"
    assert llm.calls == 0


async def test_a_clear_score_lead_is_chosen_without_asking_anyone() -> None:
    llm = RecordingLLM(raises=AssertionError("the model must not be called"))
    planner = HybridPlanner(llm, score_margin=0.15)

    picked = await planner.plan(
        context(
            [step("second"), step("first")],
            scores={"first": 2.0, "second": 1.5},
            llm=llm,
        )
    )

    assert picked is not None and picked.step_id == "first"
    assert planner.last_decision == "clear_margin"
    assert llm.calls == 0


async def test_scores_decide_the_order_not_the_input_order() -> None:
    llm = RecordingLLM()
    planner = HybridPlanner(llm)

    picked = await planner.plan(
        context(
            [step("low"), step("high")],
            scores={"high": 9.0, "low": 0.1},
            llm=llm,
        )
    )

    assert picked is not None and picked.step_id == "high"
    assert llm.calls == 0


async def test_candidates_without_scores_keep_their_given_order() -> None:
    # No comparable numbers: the caller already ranked them, so the choice
    # stays deterministic instead of being replaced by a guess.
    llm = RecordingLLM(raises=AssertionError("the model must not be called"))
    planner = HybridPlanner(llm)

    picked = await planner.plan(context([step("first"), step("second")], llm=llm))

    assert picked is not None and picked.step_id == "first"
    assert llm.calls == 0


# -- the ambiguous path ----------------------------------------------------


async def test_a_close_score_gap_goes_to_the_reranker() -> None:
    llm = RecordingLLM(reply={"step_id": "second"})
    planner = HybridPlanner(llm, score_margin=0.15)

    picked = await planner.plan(
        context(
            [step("first"), step("second")],
            scores={"first": 1.20, "second": 1.10},
            llm=llm,
        )
    )

    assert picked is not None and picked.step_id == "second"
    assert planner.last_decision == "reranked"
    assert planner.last_fallback_reason is None
    assert llm.calls == 1


async def test_the_reranker_only_sees_the_bounded_top_k() -> None:
    llm = RecordingLLM(reply={"step_id": "second"})
    planner = HybridPlanner(llm, top_k=2)
    steps = [step("first"), step("second"), step("third"), step("fourth")]

    await planner.plan(
        context(
            steps,
            scores={"first": 1.0, "second": 0.99, "third": 0.98, "fourth": 0.97},
            llm=llm,
        )
    )

    assert "step_id=first" in llm.last_prompt
    assert "step_id=second" in llm.last_prompt
    assert "step_id=third" not in llm.last_prompt
    assert "step_id=fourth" not in llm.last_prompt


# -- every failure resolves to the deterministic top-1 ---------------------


async def test_a_step_outside_the_shortlist_is_refused() -> None:
    llm = RecordingLLM(reply={"step_id": "fourth"})
    planner = HybridPlanner(llm, top_k=2)

    picked = await planner.plan(
        context(
            [step("first"), step("second"), step("third"), step("fourth")],
            scores={"first": 1.0, "second": 0.99, "third": 0.98, "fourth": 0.97},
            llm=llm,
        )
    )

    assert picked is not None and picked.step_id == "first"
    assert planner.last_decision == "fallback"
    assert "was not offered" in (planner.last_fallback_reason or "")


async def test_an_invented_step_id_is_refused() -> None:
    llm = RecordingLLM(reply={"step_id": "rm -rf /"})
    planner = HybridPlanner(llm)

    picked = await planner.plan(
        context(
            [step("first"), step("second")],
            scores={"first": 1.0, "second": 0.99},
            llm=llm,
        )
    )

    assert picked is not None and picked.step_id == "first"
    assert planner.last_decision == "fallback"


async def test_a_failing_reranker_keeps_the_deterministic_top1() -> None:
    llm = RecordingLLM(raises=RuntimeError("token=secret-token"))
    planner = HybridPlanner(llm)

    picked = await planner.plan(
        context(
            [step("first"), step("second")],
            scores={"first": 1.0, "second": 0.99},
            llm=llm,
        )
    )

    assert picked is not None and picked.step_id == "first"
    assert planner.last_decision == "fallback"
    reason = planner.last_fallback_reason or ""
    assert "RuntimeError" in reason
    # Provider messages may carry prompt fragments or credentials; only the
    # exception class is ever recorded.
    assert "secret-token" not in reason


async def test_no_client_means_the_deterministic_top1() -> None:
    planner = HybridPlanner(None)

    picked = await planner.plan(
        context([step("first"), step("second")], scores={"first": 1.0, "second": 0.99})
    )

    assert picked is not None and picked.step_id == "first"
    assert planner.last_decision == "fallback"
    assert "no LLM client" in (planner.last_fallback_reason or "")


async def test_no_candidates_declines() -> None:
    planner = HybridPlanner(FakeLLMClient())

    assert await planner.plan(context([], scores={})) is None
    assert planner.last_decision is None


# -- wiring ----------------------------------------------------------------


def test_build_planner_returns_the_hybrid_planner_for_the_llm_kind() -> None:
    planner = build_planner("llm", FakeLLMClient(), score_margin=0.5, top_k=4)

    # Still an LLMPlanner, so existing callers and tests keep their contract.
    assert isinstance(planner, LLMPlanner)
    assert isinstance(planner, HybridPlanner)


def test_build_planner_keeps_the_playbook_planner_deterministic() -> None:
    assert not isinstance(build_planner("playbook", None), HybridPlanner)
    assert not isinstance(build_planner("unknown", None), HybridPlanner)
