"""Bounded input contracts and reference-only evidence delivery, without providers."""

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import EvidenceSourceType
from redteam.core.ports.llm import NativeToolSpec
from redteam.orchestration.checkpointing import build_checkpointer
from redteam.orchestration.local_tools import LocalToolDescriptor
from redteam.orchestration.native_local_tools import (
    evidence_read_descriptor,
    goal_settle_descriptor,
    task_settle_descriptor,
)
from redteam.orchestration.nodes.react import _result_messages
from redteam.orchestration.tool_discovery import (
    NativeInputLimit,
    bootstrap_local_ids,
    build_native_bindings,
    detail_fetched_tool_ids,
    discovery_tool_contributions,
)
from tests.fakes.evidence_store import FakeEvidenceStore


def test_initial_read_return_declarations_do_not_change_bootstrap_or_goal_ownership():
    declarations = [item.factory(None)[0] for item in discovery_tool_contributions()]
    reads = [evidence_read_descriptor(), task_settle_descriptor(), goal_settle_descriptor()]
    assert all(not item.initial_exposure for item in declarations)
    assert {item.tool_id for item in reads if item.initial_exposure} == {
        "evidence.read", "task.settle",
    }
    assert bootstrap_local_ids(reads) == []
    assert set(bootstrap_local_ids(declarations)) == {"tool.list_cards", "tool.get_detail"}
    descriptor = LocalToolDescriptor(tool_id="other", description="Other helper")
    assert descriptor.initial_exposure is False


@pytest.mark.parametrize("limit", [0, True, 256 * 1024 + 1])
def test_native_input_limit_is_a_validated_byte_budget(limit):
    with pytest.raises(ValidationError):
        NativeInputLimit(max_bytes=limit)


def test_envelope_size_includes_full_descriptions_schemas_utf8_and_other_context():
    budget = NativeInputLimit()
    arguments = {"system": "한글", "messages": [{"role": "user", "content": "Context"}],
                 "tools": [NativeToolSpec(name="helper", description="Description", input_schema={
                     "type": "object", "properties": {"argument": {"type": "string"}},
                 })]}
    size = budget.size(**arguments)
    assert NativeInputLimit(max_bytes=size).fits(**arguments)
    assert not NativeInputLimit(max_bytes=size - 1).fits(**arguments)
    expanded = {**arguments, "system": "한글" * 50000}
    assert not budget.fits(**expanded)


def round_messages(identity, body, *, name="tool", arguments=None):
    return [{"role": "user", "content": "Current state"},
            {"role": "assistant", "tool_calls": [{"id": identity, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments or {})}}]},
            {"role": "tool", "tool_call_id": identity, "content": json.dumps(body)}]


async def test_both_originals_survive_local_round_and_restoration_without_checkpoint_bodies():
    store = FakeEvidenceStore()
    for index in range(2):
        await store.append(EvidenceRecord.model_validate({
            "evidence_id": f"ev-{index}", "run_id": "run", "engagement_id": "eng",
            "action_id": f"action-{index}", "invocation_id": f"inv-{index}",
            "source_type": EvidenceSourceType.TOOL, "source_id": f"provider-{index}",
            "content_type": "application/json", "digest": "a" * 64,
            "created_at": datetime.now(timezone.utc), "summary": "Response observed",
            "metadata": {"tool_result": {
                "success": True, "output": {"actual_session_ref": None,
                "observed": f"response-marker-{index}", "large": "x" * 20000,
                "api_key": "fixture-private-value"},
            }},
        }))
    history = [*round_messages("first", {"executed": True, "evidence_ids": ["ev-0"]}),
               *round_messages("second", {"executed": True, "evidence_ids": ["ev-1"]}),
               *round_messages("detail", {"ok": True}, name="get_detail",
                               arguments={"tool_id": "registered.tool"})]
    original_history = copy.deepcopy(history)
    saver = build_checkpointer(enabled=True)
    restored = saver.serde.loads_typed(saver.serde.dumps_typed({"native_messages": history}))
    restored = restored["native_messages"]
    bindings = build_native_bindings(catalog_tool_ids=["registered.tool"],
                                     local_tool_ids=["tool.get_detail"])
    # The same successful detail proof remains usable after checkpoint restore.
    restored[-2]["tool_calls"][0]["function"]["name"] = next(
        name for name, identity in bindings.items() if identity == "tool.get_detail"
    )
    assert detail_fetched_tool_ids(restored, bindings=bindings) == ["registered.tool"]
    delivered = await _result_messages(
        restored, deps=SimpleNamespace(evidence_store=store),
        runtime=SimpleNamespace(run_id="run", engagement_id="eng"),
    )
    views = [view for message in delivered if message["role"] == "tool"
             for view in json.loads(message["content"]).get("original_results", [])]
    assert {view["evidence_id"] for view in views} == {"ev-0", "ev-1"}
    assert sum(len(view["text"]) for view in views) <= 8000
    assert all(view["truncated"] and view["execution_original"] for view in views)
    for index, view in enumerate(views):
        assert view["provenance"]["action_id"] == f"action-{index}"
        assert f"response-marker-{index}" in view["text"]
    assert "fixture-private-value" not in json.dumps(delivered)
    assert history == original_history and "response-marker" not in json.dumps(restored)
