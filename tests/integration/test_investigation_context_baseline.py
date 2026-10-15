"""Capture final application requests with explicit doubles, never provider traffic.

These are JSON envelope bytes, not tokens. Stage 8a's measured before values are
preserved below; 8b now dispatches bounded views through the real consumer.
"""

import json
from dataclasses import asdict, replace
from hashlib import sha256

import pytest

from redteam.core.enums import TaskStatus
from redteam.core.ports.llm import LLMUsage, NativeCompletion
from redteam.orchestration.investigation_context import (
    InvestigationContextReader,
    capture_investigation_context,
    context_bytes,
)
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.fakes.worker_input import worker_input
from tests.integration.test_central_development import TOOL, discover, gateway, inspect, turn
from tests.integration.test_probe_workers import drive, register, selection
from tests.integration.test_v3_atomization import (
    SplitDouble,
    _new_candidate_atom,
    _new_candidate_plan,
)
from tests.unit.test_central_state import task


class MeasuredDouble(SplitDouble):
    def __init__(self, *, lookups=False):
        super().__init__([_new_candidate_plan()], [_new_candidate_atom])
        if lookups:
            self.plans[:0] = [{"reason": "Find a past purpose", "context_request": {
                "context": {"kind": "task", "mode": "list", "text": "Historical observation",
                            "count": 1}}},
                {"reason": "Read its original conditions", "context_request": {"context": {
                    "kind": "task", "mode": "read",
                    "refs": [{"kind": "task", "identity": "history-0"}]}}}]
        self.envelopes = []
        self._previous_envelopes = {}
        self.native_responses = [NativeCompletion(text="Synthetic probe has no more actions.")]

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        stage = "planner" if "questions" in response_schema.get("properties", {}) else (
            "atomizer" if "tasks" in response_schema.get("properties", {}) else "review"
        )
        self.measure(stage, messages, system, response_schema=response_schema)
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )

    async def complete_native(self, messages, tools, system=None, **kwargs):
        self.measure("worker", messages, system, tools=[asdict(tool) for tool in tools])
        return await super().complete_native(messages, tools, system=system, **kwargs)

    def measure(self, stage, messages, system, **schemas):
        envelope = {"messages": messages, "system": system, **schemas}
        previous = self._previous_envelopes.get(stage)
        comparison = _compare_requests(envelope, previous)
        # Only the in-memory double keeps request text. Artifacts remain scalar.
        self._previous_envelopes[stage] = json.loads(json.dumps(envelope))
        current = worker_input(messages) if stage == "worker" else json.loads(
            messages[-1]["content"].split("\n\nPrevious attempt failed")[0],
        )
        self.envelopes.append({
            "stage": stage, "total_bytes": context_bytes(envelope),
            "parts": {key: context_bytes(value) for key, value in envelope.items()},
            "fields": {key: context_bytes(value) for key, value in current.items()},
            "task_count": len(current.get("investigation_tasks", [])),
            **comparison,
        })


def _compare_requests(envelope, previous):
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()

    def ordered(value):
        return {"system": value.get("system"), "tools": value.get("tools"),
                "response_schema": value.get("response_schema"), "messages": value["messages"]}

    current_bytes = encode(ordered(envelope))
    old_bytes = encode(ordered(previous)) if previous else b""
    prefix = next((index for index, (left, right) in enumerate(
        zip(old_bytes, current_bytes, strict=False))
                   if left != right), min(len(old_bytes), len(current_bytes)))
    old_messages = previous["messages"] if previous else []
    common_messages = next((index for index, (left, right) in enumerate(
        zip(old_messages, envelope["messages"], strict=False)) if left != right),
        min(len(old_messages), len(envelope["messages"])))
    windows = []
    for message in envelope["messages"]:
        if message.get("role") == "tool":
            body = json.loads(message["content"])
            windows.extend(body.get("original_results", []))
    return {
        "message_count": len(envelope["messages"]), "previous_message_count": len(old_messages),
        "common_message_prefix": common_messages,
        "first_changed_previous_message": (
            common_messages if common_messages < len(old_messages) else None),
        "common_prefix_bytes": prefix, "previous_bytes": len(old_bytes),
        "system_hash": sha256(encode(envelope.get("system"))).hexdigest(),
        "tools_hash": sha256(encode(envelope.get("tools"))).hexdigest(),
        "schema_hash": sha256(encode(envelope.get("response_schema"))).hexdigest(),
        "original_window_limits": [window["limit"] for window in windows],
        "original_window_text_chars": [len(window.get("text", "")) for window in windows],
    }


async def test_multi_turn_worker_final_input_baseline(tmp_path):
    """Compare 10c append-only inputs against the recorded 10a mutation baseline."""
    llm = MeasuredDouble()
    inner = gateway()
    for response in inner.responses:
        response.output = {"status": 200, "text": "synthetic-long-output " * 500}
    from tests.integration.test_central_completion import OriginalProtocolDouble

    async with selection(tmp_path, llm=llm, tools=OriginalProtocolDouble(inner)) as (session, _, _):
        identity = await register(session, "multi-turn-baseline", actions=4, requests=8)
        llm.native_responses_by_task[identity] = [
            discover(TOOL), replace(turn(inspect("first", account="a")),
                                    usage=LLMUsage(8, 2, 10, 4)),
            turn(inspect("second", account="b")), turn(inspect("third", account="c")),
            NativeCompletion(text="Return the bounded observations for planner assessment."),
        ]
        result, = await drive(session)
        assert result.kind == "completed", result.error
        assert len(inner.invocations) == 3
        usage = await session.deps.central_state.request_ledger().snapshot(
            str(session.runtime.run_id),
        )
        assert usage["spent"] == len(llm.envelopes) == 5
        assert usage["by_task"][identity]["spent"] == 5
        assert usage["by_task"][identity]["usage"]["cache_read_tokens"] == {
            "known": 4, "unknown": 4,
        }
        assert all(item["parts"]["system"] > 0 and item["parts"]["tools"] > 0
                   for item in llm.envelopes)
        assert all(item["first_changed_previous_message"] is None for item in llm.envelopes)
        assert all(all(limit == 4000 for limit in item["original_window_limits"])
                   for item in llm.envelopes)
    scalar = {"requests": llm.envelopes, "model_calls": usage["spent"],
              "unknown_usage": usage["unknown_usage"],
              "cumulative_bytes": sum(item["total_bytes"] for item in llm.envelopes)}
    (tmp_path / "worker-session-baseline.json").write_text(json.dumps(scalar, indent=2))
    print(json.dumps({"multi_turn_baseline": scalar}, sort_keys=True))


@pytest.mark.parametrize("lookups", [False, True])
async def test_final_planner_atomizer_and_worker_baseline(tmp_path, lookups):
    measurements = []
    for historical in (10, 100, 1000):
        llm = MeasuredDouble(lookups=lookups)
        async with selection(tmp_path / str(historical), llm=llm) as (session, _, tools):
            state = session.deps.central_state
            # Historical fixture rows only. They assert no executed effects and
            # are deliberately seeded separately from the live admission path.
            def seed(tx, historical=historical):
                mission_id, _ = tx.binding()
                for index in range(historical):
                    item = task(f"history-{index}", mission_id=mission_id).model_copy(update={
                        "status": TaskStatus.COMPLETED, "required": False,
                        "objective": "Historical observation " + "x" * 200,
                    })
                    tx.db.execute("INSERT INTO state_tasks VALUES(?,?,?)", (
                        tx.run_id, str(item.task_id), item.model_dump_json(),
                    ))
            await state.transaction(seed)
            identities = await SpecialistPlanner(session.deps).advance(session.runtime)
            assert len(identities) == 1
            view = InvestigationContextReader(await capture_investigation_context(state)).overview(
                active_task_id=identities[0], max_bytes=16384,
            )
            assert view["status"] == "ok"
            await drive(session)
            assert not tools.inner.invocations  # No target call to the synthetic provider.
            planner = next(item for item in llm.envelopes if item["stage"] == "planner")
            atomizer = next(item for item in llm.envelopes if item["stage"] == "atomizer")
            worker = next(item for item in llm.envelopes if item["stage"] == "worker")
            assert worker["task_count"] == 1
            assert planner["fields"]["investigations"] > 1000
            assert atomizer["fields"]["investigations"] == 2  # Empty related history, JSON [].
            assert all(item["parts"]["system"] > 0 for item in llm.envelopes)
            assert worker["parts"]["tools"] > 0 and planner["parts"]["response_schema"] > 0
            ledger = await state.request_ledger().snapshot(state.run_id)
            assert ledger["spent"] == len(llm.envelopes)
            assert ledger["spent"] == (5 if lookups else 3)
            measurements.append({
                "historical_tasks": historical,
                "requests": llm.envelopes,
                "comparison_view_bytes": context_bytes(view),
                "planner_context_is_dispatched": True,
                "ledger_requests": ledger["spent"],
                "usage_unknown": ledger["unknown_usage"],
            })
    short, _, long = measurements
    for stage in ("planner", "atomizer"):
        first = next(item for item in short["requests"] if item["stage"] == stage)
        last = next(item for item in long["requests"] if item["stage"] == stage)
        assert abs(last["total_bytes"] - first["total_bytes"]) < 2048
    assert long["comparison_view_bytes"] <= 16384
    assert abs(long["comparison_view_bytes"] - short["comparison_view_bytes"]) < 2048
    assert abs(sum(item["total_bytes"] for item in long["requests"])
               - sum(item["total_bytes"] for item in short["requests"])) < 4096
    # Only scalar counts/sizes are saved, and only in the test's temporary directory.
    (tmp_path / "baseline.json").write_text(json.dumps(measurements, indent=2))
    print(json.dumps({"lookups": lookups, "baseline": [{
        "historical_tasks": row["historical_tasks"],
        "stages": {stage: next(item["total_bytes"] for item in row["requests"]
                               if item["stage"] == stage)
                   for stage in ("planner", "atomizer", "worker")},
        "comparison_view_bytes": row["comparison_view_bytes"],
        "requests": row["ledger_requests"], "unknown_usage": row["usage_unknown"],
        "cumulative_bytes": sum(item["total_bytes"] for item in row["requests"]),
    } for row in measurements]}, sort_keys=True))
