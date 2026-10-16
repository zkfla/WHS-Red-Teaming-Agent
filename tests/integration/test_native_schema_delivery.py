"""Schema-sensitive explicit double; real admission, review, storage and metering."""

import json
from dataclasses import replace

import pytest

from redteam.capability.development import DevelopmentToolGateway
from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.core.enums import ApprovalStatus, TaskStatus, ToolHealthStatus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.nodes.react import model_node
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.state import initial_graph_state
from redteam.orchestration.tool_discovery import (
    NATIVE_INPUT_LIMIT,
    NativeInputLimit,
    tool_get_detail_handler,
)
from redteam.orchestration.worker_session import WorkerSession
from tests.fakes.worker_input import worker_input
from tests.integration.test_central_completion import OriginalProtocolDouble
from tests.integration.test_central_development import TOOL, call, gateway, inspect, turn
from tests.integration.test_probe_workers import drive, register, selection


class SchemaSensitiveProbe(FakeLLMClient):
    """Chooses discovery only when its intended operation's schema is absent."""

    def __init__(self, *, read_extra=False, raw_detail_name=False):
        super().__init__()
        self.envelopes = []
        self.read_extra = read_extra
        self.raw_detail_name = raw_detail_name
        self.read_issued = False

    async def complete_native(self, messages, tools, system=None, **kwargs):
        self.envelopes.append({"messages": messages, "tools": tools, "system": system})
        payload = worker_input(messages)
        originals = payload["probe"]["original_evidence_ids"]
        names = {item.name for item in tools}
        if originals and exposed_tool_name("task.settle") not in names:
            response = turn(call("return-detail", "tool.get_detail", {"tool_id": "task.settle"}))
        elif originals and self.read_extra and not self.read_issued:
            self.read_issued = True
            response = turn(call("extra-window", "evidence.read", {
                "evidence_id": originals[0], "offset": 10, "limit": 300,
            }))
        elif originals:
            task = payload["active_task"]
            self._canned_structured = {
                "accepted": True, "reason": "Response assessed", "claims": [{
                    "kind": "observation", "description": "Response observed",
                    "evidence_ids": originals,
                }], "criterion_results": [{
                    "criterion_id": criterion_id(CompletionCriterion.model_validate(item)),
                    "met": True, "evidence_ids": originals, "reason": "Response observed",
                } for item in task["completion_criteria"]],
            }
            response = turn(call("return", "task.settle", {
                "outcome": "success", "reason": "Observed response", "evidence_ids": originals,
            }))
        elif exposed_tool_name(TOOL) in names:
            response = turn(inspect("observe"))
        else:
            detail = call("detail", "tool.get_detail", {"tool_id": TOOL})
            if self.raw_detail_name:
                detail = replace(detail, name="tool.get_detail")
            response = turn(detail)
        self.native_responses = [response]
        return await super().complete_native(messages, tools, system=system, **kwargs)

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        if "updates" in response_schema["properties"]:
            self._canned_structured = {
                "updates": [], "questions": [],
                "reason": "Observation returned; await authorized follow-up",
                "termination": "held", "resume_conditions": ["Additional premise arrives"],
            }
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )


def provider(*, large=False, risk="low"):
    original = gateway(risk=risk)
    descriptor = original.catalog.all_descriptors()[0]
    eligible = [descriptor]
    if large:
        eligible = [descriptor.model_copy(update={"description": "Contract " * 3000})]
        eligible.extend(descriptor.model_copy(update={
            "tool_id": f"web.additional-{i}", "tool_name": f"additional-{i}",
            "description": "Contract " * 3000,
        }) for i in range(14))
    return OriginalProtocolDouble(DevelopmentToolGateway([
        *eligible,
        descriptor.model_copy(update={"tool_id": "cloud.hidden", "capability_tags": ["cloud"]}),
        descriptor.model_copy(update={"tool_id": "web.unhealthy",
                                      "health_status": ToolHealthStatus.UNHEALTHY}),
    ], original.responses))


@pytest.mark.parametrize("large", [False, True])
async def test_size_based_whole_catalog_or_lazy_schema_reaches_review_and_measured_return(tmp_path,
                                                                                      large):
    async with selection(tmp_path, tools=provider(large=large), llm=SchemaSensitiveProbe()) \
            as (session, llm, tools):
        identity = await register(session, "input-delivery", actions=2)
        outcome, = await drive(session)
        assert outcome.kind == "completed", outcome.error
        task = await session.deps.central_state.get_task(identity)
        assert task.status is TaskStatus.COMPLETED and task.result.outcome == "confirmed"
        assert task.result.completion_receipt_id and len(tools.inner.invocations) == 1
        first_names = {spec.name for spec in llm.envelopes[0]["tools"]}
        assert {exposed_tool_name(item) for item in ["evidence.read", "task.settle",
                "task.publish_result", "tool.list_cards", "tool.get_detail"]} <= first_names
        publication_name = exposed_tool_name("task.publish_result")
        publication_spec = next(
            spec for spec in llm.envelopes[0]["tools"] if spec.name == publication_name
        )
        publication_input = publication_spec.input_schema
        publication_result = publication_input["$defs"]["PublicationResultInput"]
        assert publication_result["properties"]["contract_version"]["enum"] == [1, 2]
        assert publication_result["properties"]["contract_version"]["default"] == 2
        detail = await tool_get_detail_handler(
            {"tool_id": "task.publish_result"},
            LocalToolContext(session.deps, session.runtime),
        )
        assert detail.ok, detail.error
        assert detail.payload["input_schema"] == publication_input
        assert exposed_tool_name("goal.settle") not in first_names
        assert exposed_tool_name("cloud.hidden") not in first_names
        assert exposed_tool_name("web.unhealthy") not in first_names
        expected_remote = {exposed_tool_name(str(item.tool_id))
                           for item in tools.inner.catalog.all_descriptors()
                           if str(item.tool_id) not in {"cloud.hidden", "web.unhealthy"}}
        assert (first_names & expected_remote) == (set() if large else expected_remote)
        assert all(NATIVE_INPUT_LIMIT.fits(**envelope) for envelope in llm.envelopes)
        for previous, current in zip(llm.envelopes, llm.envelopes[1:], strict=False):
            assert current["tools"][:len(previous["tools"])] == previous["tools"]
            assert current["messages"][:len(previous["messages"])] == previous["messages"]
        await SpecialistPlanner(session.deps).advance(session.runtime)
        ledger = await session.deps.central_state.request_ledger().snapshot(session.runtime.run_id)
        native_calls = len(llm.envelopes)
        assert native_calls == (3 if large else 2)
        # Includes independent review and the actual Planner call, not just worker turns.
        assert ledger["spent"] == len(llm.requests) == native_calls + 2
        assert ledger["known_tokens"] == native_calls * 10
        assert ledger["unknown_usage"] == 2 and ledger["reserved"] == 0
        print({"mode": "lazy" if large else "eager", "worker_requests": native_calls,
               "total_requests": ledger["spent"], "known_tokens": ledger["known_tokens"],
               "unknown_usage": ledger["unknown_usage"],
               "native_input_bytes": [NATIVE_INPUT_LIMIT.size(**x) for x in llm.envelopes]})


async def test_raw_exact_detail_name_resolves_then_advertises_only_selected_schema(tmp_path):
    async with selection(
        tmp_path, tools=provider(large=True),
        llm=SchemaSensitiveProbe(raw_detail_name=True),
    ) as (session, llm, tools):
        identity = await register(session, "raw-detail-name", actions=2)
        outcome, = await drive(session)

        task = await session.deps.central_state.get_task(identity)
        assert outcome.kind == "completed", outcome.error
        assert task.status is TaskStatus.COMPLETED and task.result.outcome == "confirmed"
        assert len(tools.inner.invocations) == 1

        first_names = {item.name for item in llm.envelopes[0]["tools"]}
        second_names = {item.name for item in llm.envelopes[1]["tools"]}
        assert exposed_tool_name("tool.get_detail") in first_names
        assert exposed_tool_name(TOOL) not in first_names
        assert exposed_tool_name(TOOL) in second_names
        assert exposed_tool_name("cloud.hidden") not in second_names
        assert exposed_tool_name("web.unhealthy") not in second_names

        ledger = await session.deps.central_state.request_ledger().snapshot(
            session.runtime.run_id,
        )
        assert ledger["outcomes"]["unknown_tool"] == 0


async def test_initial_schema_does_not_bypass_approval_or_replay_success(tmp_path):
    async with selection(tmp_path, tools=provider(risk="high"), llm=SchemaSensitiveProbe()) \
            as (session, llm, tools):
        identity = await register(session, "approval", actions=2)
        outcome, = await drive(session)
        task = await session.deps.central_state.get_task(identity)
        assert task.status is TaskStatus.BLOCKED and not tools.inner.invocations
        assert len(llm.requests) == 1  # No schema/read/model polling while approval is pending.
        original, = await session.deps.central_state.list_executions()
        await session.deps.control_plane.resolve_approval(
            outcome.runtime.pending_approval_id, ApprovalStatus.APPROVED, "fixture:operator",
        )
        recovered, recovery = await session.driver.recover_central_instance(outcome.runtime)
        assert recovery.ready
        returned = await session.driver.runner_for(recovered).resume_recovered(recovered)
        task = await session.deps.central_state.get_task(identity)
        assert task.result.outcome == "confirmed" and returned.model_requests_used > 0
        assert len(tools.inner.invocations) == 1
        assert tools.inner.invocations[0].invocation_id == original.invocation_id
        ledger = await session.deps.central_state.request_ledger().snapshot(session.runtime.run_id)
        assert ledger["spent"] == len(llm.requests) == 3  # Two worker turns + one review.


async def test_overlarge_mandatory_input_stops_before_provider_dispatch(tmp_path, monkeypatch):
    from redteam.orchestration.central_runtime import ready_workers
    from redteam.orchestration.nodes import react

    monkeypatch.setattr(react, "NATIVE_INPUT_LIMIT", NativeInputLimit(max_bytes=1))
    async with selection(tmp_path, llm=SchemaSensitiveProbe()) as (session, llm, _):
        await register(session, "overlarge", actions=2)
        runtime, = await ready_workers(session.deps, session.runtime)
        state = await model_node(initial_graph_state(runtime), deps=session.deps)
        assert state["runtime"].planning_diagnostic.code == "native_input_limit_exceeded"
        ledger = await session.deps.central_state.request_ledger().snapshot(session.runtime.run_id)
        assert ledger["spent"] == 0 and not llm.requests


async def test_growing_signed_conversation_holds_without_evicting_or_rewriting(
    tmp_path, monkeypatch,
):
    from redteam.orchestration.nodes import react

    class BoundedProbe(SchemaSensitiveProbe):
        async def complete_native(self, messages, tools, system=None, **kwargs):
            result = await super().complete_native(messages, tools, system=system, **kwargs)
            size = NATIVE_INPUT_LIMIT.size(messages=messages, tools=tools, system=system)
            monkeypatch.setattr(react, "NATIVE_INPUT_LIMIT", NativeInputLimit(max_bytes=size + 1))
            return replace(result, continuation=({"type": "thinking", "thinking": "",
                                                 "signature": "opaque-boundary-fixture"},))

    async with selection(tmp_path, tools=provider(), llm=BoundedProbe()) as (session, llm, tools):
        await register(session, "context-boundary", actions=2)
        outcome, = await drive(session)
        assert len(llm.envelopes) == len(tools.inner.invocations) == 1
        assert outcome.runtime.planning_diagnostic.code == "worker_session_compaction_held"
        runner = session.driver.runner_for(outcome.runtime)
        snapshot = await runner._graph.aget_state(runner.config())
        saved = WorkerSession.model_validate_json(json.dumps(snapshot.values["worker_session"]))
        assert saved.conversation()[0] == llm.envelopes[0]["messages"][0]
        assert saved.tools() == llm.envelopes[0]["tools"]
        assert saved.generation == 0  # No silent compaction, eviction or extra summary call.


async def test_same_probe_compares_prior_lazy_preconditions_and_current_delivery(
    tmp_path, monkeypatch,
):
    """Reproduce only the old advertisement predicates, keeping the same schema-sensitive model.

    This proves removed lookup prerequisites and accounts for costs. It cannot
    estimate a live model's voluntary choices, reasoning quality or token usage.
    """
    from redteam.orchestration.nodes import react
    from redteam.orchestration.tool_discovery import build_native_bindings, detail_fetched_tool_ids

    class PriorLazyExposure(NativeInputLimit):
        def fits(self, **envelope):
            remote_present = any(spec.name == exposed_tool_name(TOOL) for spec in envelope["tools"])
            bindings = build_native_bindings(catalog_tool_ids=[TOOL],
                                             local_tool_ids=["tool.get_detail"])
            if remote_present and TOOL not in detail_fetched_tool_ids(
                envelope["messages"], bindings=bindings,
            ):
                return False
            return super().fits(**envelope)

    measured = []
    for prior in (True, False):
        with monkeypatch.context() as changes:
            selected = selection(
                tmp_path / str(prior), tools=provider(), llm=SchemaSensitiveProbe(),
            )
            async with selected as (session, llm, tools):
                if prior:
                    changes.setattr(react, "NATIVE_INPUT_LIMIT", PriorLazyExposure())
                    descriptors = list(session.deps.local_tools.descriptors())
                    changes.setattr(
                        session.deps.local_tools, "descriptors", lambda values=descriptors: [
                            replace(item, initial_exposure=False) for item in values
                        ],
                    )
                identity = await register(session, "same-probe", actions=2)
                outcome, = await drive(session)
                assert outcome.kind == "completed", outcome.error
                task = await session.deps.central_state.get_task(identity)
                assert task.result.outcome == "confirmed" and len(tools.inner.invocations) == 1
                await SpecialistPlanner(session.deps).advance(session.runtime)
                ledger = await session.deps.central_state.request_ledger().snapshot(
                    session.runtime.run_id,
                )
                assert ledger["spent"] == len(llm.requests) == len(llm.envelopes) + 2
                measured.append({"prior": prior, "worker_requests": len(llm.envelopes),
                                 "total_requests": ledger["spent"],
                                 "known_tokens": ledger["known_tokens"],
                                 "unknown_usage": ledger["unknown_usage"],
                                 "native_input_bytes": [NATIVE_INPUT_LIMIT.size(**envelope)
                                                        for envelope in llm.envelopes]})
    before, after = measured
    assert after["worker_requests"] < before["worker_requests"]
    assert before["unknown_usage"] == after["unknown_usage"] == 2
    print({"same_probe_comparison": measured})


async def test_extra_reader_window_is_delivered_and_checkpoint_keeps_exact_display(tmp_path):
    async with selection(tmp_path, tools=provider(), llm=SchemaSensitiveProbe(read_extra=True)) \
            as (session, llm, _):
        await register(session, "extra-window", actions=2)
        outcome, = await drive(session)
        assert outcome.kind == "completed", outcome.error
        delivered = [json.loads(message["content"])
                     for message in llm.envelopes[-1]["messages"] if message["role"] == "tool"]
        read, = [body for body in delivered if body.get("evidence_read_request")]
        assert read["payload"]["text"] and read["payload"]["offset"] == 10
        assert read["payload"]["provenance"]["invocation_id"]
        runner = session.driver.runner_for(outcome.runtime)
        snapshot = await runner._graph.aget_state(runner.config())
        messages = WorkerSession.model_validate_json(json.dumps(
            snapshot.values["worker_session"],
        )).conversation()
        assert messages[0]["role"] == "user"
        calls = {call["id"] for message in messages for call in message.get("tool_calls", [])}
        answers = {message["tool_call_id"] for message in messages if message["role"] == "tool"}
        assert calls == answers
        bodies = [json.loads(message["content"]) for message in messages
                  if message["role"] == "tool"]
        saved, = [body for body in bodies if body.get("evidence_read_request")]
        assert saved["payload"]["text"] == read["payload"]["text"]
        assert saved["evidence_read_request"] == read["evidence_read_request"]
        for message in messages:
            if message["role"] == "user" and message["content"].startswith("{"):
                context = json.loads(message["content"])
                assert len(context.get("investigation_tasks", [])) <= 1
