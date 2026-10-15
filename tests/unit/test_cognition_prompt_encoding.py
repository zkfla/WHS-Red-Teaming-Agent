"""Decision prompt bodies use the compact UTF-8 encoding that the planner size guard measures."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from redteam.llm.litellm import LiteLLMClient
from redteam.orchestration.cognition import DecisionBudget, StructuredDecisionClient
from redteam.orchestration.investigation_context import context_bytes

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

FIXTURE = {
    "investigations": [
        {
            "task_id": "task-1",
            "purpose": "한글 검색 결과 확인 — ünïcödé ✓ 🔐",
            "status": "pending",
            "evidence_ids": ["ev-1", "ev-2"],
        },
        {
            "task_id": "task-2",
            "purpose": 'quote " backslash \\ newline\n tab\t',
            "status": "ready",
            "evidence_ids": [],
        },
    ],
    "control": "\u0001",
    "nested": {"ratio": 0.5, "count": 3, "flag": True, "empty": None},
}

# Byte counts for FIXTURE: legacy json.dumps(default) versus the compact UTF-8 body.
LEGACY_BYTES = 421
WIRE_BYTES = 339


def provider(monkeypatch, replies, calls):
    async def completion(**kwargs):
        calls.append(kwargs)
        text = replies.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=None,
        )

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            get_llm_provider=lambda model: (model, "openai", None, None),
            acompletion=completion,
        ),
    )
    return LiteLLMClient("unit-model")


def compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


async def test_wire_body_roundtrips_unicode_and_special_characters(monkeypatch):
    calls = []
    llm = provider(monkeypatch, ['{"answer":"ok"}'], calls)

    await StructuredDecisionClient(llm).ask(
        stage="encoding",
        payload=FIXTURE,
        response_schema=SCHEMA,
        budget=DecisionBudget(1),
    )

    wire = calls[0]["messages"][-1]["content"]
    assert json.loads(wire) == FIXTURE
    assert wire == compact(FIXTURE)
    assert "한글 검색 결과 확인" in wire
    assert "\\uac80" not in wire
    assert "\\u0001" in wire  # control characters stay escaped, so the body remains valid JSON


async def test_wire_bytes_match_guard_measure_and_shrink(monkeypatch):
    calls = []
    llm = provider(monkeypatch, ['{"answer":"ok"}'], calls)

    await StructuredDecisionClient(llm).ask(
        stage="encoding",
        payload=FIXTURE,
        response_schema=SCHEMA,
        budget=DecisionBudget(1),
    )

    wire = calls[0]["messages"][-1]["content"]
    legacy = len(json.dumps(FIXTURE).encode("utf-8"))
    assert context_bytes(FIXTURE) == len(wire.encode("utf-8")) == WIRE_BYTES
    assert legacy == LEGACY_BYTES
    assert WIRE_BYTES < LEGACY_BYTES


async def test_retry_body_keeps_compact_body_and_feedback(monkeypatch):
    calls = []
    llm = provider(monkeypatch, ["not json", '{"answer":"ok"}'], calls)

    await StructuredDecisionClient(llm).ask(
        stage="encoding",
        payload=FIXTURE,
        response_schema=SCHEMA,
        budget=DecisionBudget(3),
    )

    assert len(calls) == 2
    body, feedback = calls[1]["messages"][-1]["content"].split("\n\nPrevious attempt failed", 1)
    assert body == compact(FIXTURE)
    assert "Respond with a single JSON object only" in feedback


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
async def test_non_finite_number_fails_before_any_request(monkeypatch, bad):
    calls = []
    llm = provider(monkeypatch, [], calls)
    budget = DecisionBudget(1)

    with pytest.raises(ValueError, match="not finite JSON"):
        await StructuredDecisionClient(llm).ask(
            stage="encoding",
            payload={"score": bad},
            response_schema=SCHEMA,
            budget=budget,
        )

    assert calls == []
    assert budget.used == 0
