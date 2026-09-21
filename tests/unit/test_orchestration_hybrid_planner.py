"""H4: the hybrid planner spends a model call only when it is genuinely needed.

The four paths the plan asks for are all here: one candidate, a clear score
lead, an ambiguous top-K that goes to the reranker, and every failure shape
that must fall back to the deterministic top-1. The negative cases matter
most -- a planner that quietly called the model anyway, or accepted a step it
was never offered, would look identical on the happy path.
"""

from __future__ import annotations

from dataclasses import replace
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


def context(steps: list[PlanStep], *, keys=None, scores=None, llm=None) -> PlanningContext:
    metadata: dict[str, Any] = {}
    if keys is not None:
        metadata["candidate_keys"] = list(keys)
    if scores is not None:
        metadata["candidate_scores"] = scores
    return PlanningContext(
        agent=AgentSpec(agent_id=AgentId("recon-agent"), name="recon", planner_kind="llm"),
        observation=GraphObservation(step=1),
        candidates=steps,
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        step=1,
        llm=llm,
        metadata=metadata,
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
    llm = RecordingLLM(reply={"option_id": "second"})
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
    llm = RecordingLLM(reply={"option_id": "second"})
    planner = HybridPlanner(llm, top_k=2)
    steps = [step("first"), step("second"), step("third"), step("fourth")]

    await planner.plan(
        context(
            steps,
            scores={"first": 1.0, "second": 0.99, "third": 0.98, "fourth": 0.97},
            llm=llm,
        )
    )

    assert "option_id=first" in llm.last_prompt
    assert "option_id=second" in llm.last_prompt
    assert "option_id=third" not in llm.last_prompt
    assert "option_id=fourth" not in llm.last_prompt


# -- every failure resolves to the deterministic top-1 ---------------------


async def test_a_step_outside_the_shortlist_is_refused() -> None:
    llm = RecordingLLM(reply={"option_id": "fourth"})
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


async def test_an_invented_option_id_is_refused() -> None:
    llm = RecordingLLM(reply={"option_id": "rm -rf /"})
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


# -- one playbook step, several targets: option identity must stay unique ---


async def test_the_same_step_id_on_two_targets_stays_two_distinct_options() -> None:
    # available_steps() keeps one step_id for a playbook entry bound to
    # several targets. Keying scores or the shortlist by step_id would
    # collapse them, and the last target scored would silently win.
    llm = RecordingLLM(raises=AssertionError("a clear winner needs no model call"))
    planner = HybridPlanner(llm)
    steps = [step("probe-host", host="a.local"), step("probe-host", host="b.local")]

    picked = await planner.plan(
        context(
            steps,
            keys=["cand_a", "cand_b"],
            scores={"cand_a": 0.2, "cand_b": 1.9},
            llm=llm,
        )
    )

    assert picked is not None
    assert picked.step_id == "probe-host"
    assert picked.target is not None and picked.target.host == "b.local"
    assert planner.last_option_id == "cand_b"
    assert llm.calls == 0


async def test_an_ambiguous_pair_sharing_a_step_id_is_still_distinguishable() -> None:
    llm = RecordingLLM(reply={"option_id": "cand_b"})
    planner = HybridPlanner(llm)
    steps = [step("probe-host", host="a.local"), step("probe-host", host="b.local")]

    picked = await planner.plan(
        context(
            steps,
            keys=["cand_a", "cand_b"],
            scores={"cand_a": 1.20, "cand_b": 1.10},
            llm=llm,
        )
    )

    assert picked is not None
    assert picked.target is not None and picked.target.host == "b.local"
    assert planner.last_decision == "reranked"
    assert planner.last_option_id == "cand_b"


async def test_the_ambiguity_prompt_addresses_options_by_their_unique_id() -> None:
    llm = RecordingLLM(reply={"option_id": "cand_b"})
    planner = HybridPlanner(llm)
    steps = [step("probe-host", host="a.local"), step("probe-host", host="b.local")]

    await planner.plan(
        context(
            steps,
            keys=["cand_a", "cand_b"],
            scores={"cand_a": 1.20, "cand_b": 1.10},
            llm=llm,
        )
    )

    assert "option_id=cand_a" in llm.last_prompt
    assert "option_id=cand_b" in llm.last_prompt
    # The shared step_id is still shown for context, but it is not the handle.
    assert llm.last_prompt.count("step_id=probe-host") == 2


async def test_the_ambiguity_prompt_shows_each_option_target() -> None:
    # Two bindings of one playbook step are indistinguishable unless the
    # prompt says where each would run.
    llm = RecordingLLM(reply={"option_id": "cand_b"})
    planner = HybridPlanner(llm)
    steps = [step("probe-host", host="a.local"), step("probe-host", host="b.local")]

    await planner.plan(
        context(
            steps,
            keys=["cand_a", "cand_b"],
            scores={"cand_a": 1.20, "cand_b": 1.10},
            llm=llm,
        )
    )

    assert "target=a.local" in llm.last_prompt
    assert "target=b.local" in llm.last_prompt


async def test_the_ambiguity_prompt_keeps_the_bounded_context() -> None:
    llm = RecordingLLM(reply={"option_id": "k1"})
    planner = HybridPlanner(llm)
    planning = context(
        [step("first"), step("second")],
        keys=["k1", "k2"],
        scores={"k1": 1.0, "k2": 0.99},
        llm=llm,
    )
    planning = replace(
        planning,
        observation=GraphObservation(
            step=3,
            open_hypotheses=["hyp_1: is /admin exposed?"],
            findings=["find_1 [high/verified]"],
            attempted=["recon.service_discovery@host:a.local"],
        ),
        metadata={
            **planning.metadata,
            "evidence_excerpts": [
                {
                    "evidence_id": "ev_1",
                    "target_node_id": "endpoint:a.local GET /",
                    "source_id": "recon.http_probe",
                    "summary": "status 200",
                }
            ],
        },
    )

    await planner.plan(planning)

    # The rerank prompt must not have regressed relative to the plain
    # LLMPlanner prompt it replaces.
    assert "hyp_1: is /admin exposed?" in llm.last_prompt
    assert "find_1 [high/verified]" in llm.last_prompt
    assert "recon.service_discovery@host:a.local" in llm.last_prompt
    assert "evidence_id=ev_1" in llm.last_prompt


async def test_the_ambiguity_prompt_never_leaks_step_arguments() -> None:
    # Arguments can carry request bodies and references; the prompt is built
    # from the target and the step's own prose instead.
    llm = RecordingLLM(reply={"option_id": "k1"})
    planner = HybridPlanner(llm)
    leaky = step("first").model_copy(
        update={
            "arguments": {
                "target_ref": "lab",
                "path": "/",
                "session_ref": "must-not-leak",
                "json_body": {"password": "hunter2"},
            }
        }
    )

    await planner.plan(
        context([leaky, step("second")], keys=["k1", "k2"], scores={"k1": 1.0, "k2": 0.99}, llm=llm)
    )

    assert "hunter2" not in llm.last_prompt
    assert "must-not-leak" not in llm.last_prompt


async def test_a_step_id_answer_cannot_resolve_an_ambiguous_option_set() -> None:
    # The model answered with the shared step_id, which names two options.
    # There is no safe way to pick one, so the deterministic top-1 stands.
    llm = RecordingLLM(reply={"step_id": "probe-host"})
    planner = HybridPlanner(llm)
    steps = [step("probe-host", host="a.local"), step("probe-host", host="b.local")]

    picked = await planner.plan(
        context(
            steps,
            keys=["cand_a", "cand_b"],
            scores={"cand_a": 1.20, "cand_b": 1.10},
            llm=llm,
        )
    )

    assert picked is not None
    assert picked.target is not None and picked.target.host == "a.local"
    assert planner.last_decision == "fallback"
    assert "was not offered" in (planner.last_fallback_reason or "")


async def test_an_invalid_unique_option_id_falls_back_to_the_top_candidate() -> None:
    llm = RecordingLLM(reply={"option_id": "cand_missing"})
    planner = HybridPlanner(llm)
    steps = [step("probe-host", host="a.local"), step("probe-host", host="b.local")]

    picked = await planner.plan(
        context(
            steps,
            keys=["cand_a", "cand_b"],
            scores={"cand_a": 1.20, "cand_b": 1.10},
            llm=llm,
        )
    )

    assert picked is not None
    assert picked.target is not None and picked.target.host == "a.local"
    assert planner.last_decision == "fallback"
    assert planner.last_option_id == "cand_a"


# -- wiring ----------------------------------------------------------------


def test_build_planner_returns_the_hybrid_planner_for_the_llm_kind() -> None:
    planner = build_planner("llm", FakeLLMClient(), score_margin=0.5, top_k=4)

    # Still an LLMPlanner, so existing callers and tests keep their contract.
    assert isinstance(planner, LLMPlanner)
    assert isinstance(planner, HybridPlanner)


def test_build_planner_keeps_the_playbook_planner_deterministic() -> None:
    assert not isinstance(build_planner("playbook", None), HybridPlanner)
    assert not isinstance(build_planner("unknown", None), HybridPlanner)
