"""Capture final application requests with explicit doubles, never provider traffic.

These are JSON envelope bytes, not tokens. Stage 8a's measured before values are
preserved below; 8b now dispatches bounded views through the real consumer.
"""

import json
from dataclasses import asdict

import pytest

from redteam.core.enums import TaskStatus
from redteam.core.ports.llm import NativeCompletion
from redteam.orchestration.investigation_context import (
    InvestigationContextReader,
    capture_investigation_context,
    context_bytes,
)
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_probe_workers import drive, selection
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
        current = json.loads(messages[-1]["content"].split("\n\nPrevious attempt failed")[0])
        self.envelopes.append({
            "stage": stage, "total_bytes": context_bytes(envelope),
            "parts": {key: context_bytes(value) for key, value in envelope.items()},
            "fields": {key: context_bytes(value) for key, value in current.items()},
            "task_count": len(current.get("investigation_tasks", [])),
        })


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
