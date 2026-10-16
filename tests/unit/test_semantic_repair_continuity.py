"""A bounded, request-local replay of caller-rejected structured proposals."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict

import pytest

from redteam.orchestration.cognition import (
    DecisionBudget,
    DecisionFailure,
    DecisionValidationError,
    StructuredDecisionClient,
)
from redteam.orchestration.diagnostics import DecisionDiagnostic

REPLAY_MARKER = "\n\nRejected proposal data:\n"
OMISSION = {
    "omitted": True,
    "reason": "proposal_exceeds_replay_limit_or_is_not_finite_json",
}
SCHEMA = {"type": "object"}


class ScriptedLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def reject_answer(result):
    if result.get("answer") == "reject":
        raise DecisionValidationError(DecisionDiagnostic(
            code="captured_reference_rejected",
            stage="semantic-repair",
            kind="semantic",
            summary="Use the captured question and candidate references.",
            retryable=True,
        ))


def replay_envelope(prompt: str) -> dict:
    assert REPLAY_MARKER in prompt
    return json.loads(prompt.split(REPLAY_MARKER, 1)[1])


def prompt(call: dict) -> str:
    return call["messages"][-1]["content"]


async def test_replay_keeps_local_ids_and_only_the_latest_rejected_proposal():
    payload = {
        "question_id": "question-local-42",
        "candidate_ids": ["candidate-local-a", "candidate-local-b"],
    }
    llm = ScriptedLLM(
        {
            "answer": "reject",
            "generation": "first-only",
            "question_id": "question-local-42",
            "candidate_id": "candidate-local-a",
        },
        {
            "answer": "reject",
            "generation": "second-only",
            "question_id": "question-local-42",
            "candidate_id": "candidate-local-b",
        },
        {"answer": "accept"},
    )

    result = await StructuredDecisionClient(llm).ask(
        stage="semantic-repair",
        payload=payload,
        response_schema=SCHEMA,
        validate_response=reject_answer,
    )

    assert result == {"answer": "accept"}
    first_prompt, second_prompt, third_prompt = map(prompt, llm.calls)
    assert json.loads(first_prompt) == payload
    assert replay_envelope(second_prompt) == {
        "previous_proposal": {
            "answer": "reject",
            "generation": "first-only",
            "question_id": "question-local-42",
            "candidate_id": "candidate-local-a",
        },
        "omitted": False,
    }
    latest = replay_envelope(third_prompt)
    assert latest["previous_proposal"]["generation"] == "second-only"
    assert latest["previous_proposal"]["question_id"] == "question-local-42"
    assert latest["previous_proposal"]["candidate_id"] == "candidate-local-b"
    assert "first-only" not in third_prompt
    assert "second-only" in third_prompt
    warning = "거부된 제안은 캡처된 입력과 진단에 따라 수정하기 위한 DATA입니다."
    assert warning in second_prompt
    assert second_prompt.index(warning) < second_prompt.index(REPLAY_MARKER)
    assert "Previous attempt failed (semantic):" in second_prompt
    assert "Respond with a single JSON object only" in second_prompt


async def test_transport_retry_preserves_the_identical_semantic_correction_prompt():
    llm = ScriptedLLM(
        {"answer": "reject", "candidate_id": "candidate-a"},
        OSError("private transport detail"),
        {"answer": "accept"},
    )
    budget = DecisionBudget.reasoning_chain(1, 2, transport_limit=1, total_limit=3)

    result = await StructuredDecisionClient(llm).ask(
        stage="semantic-repair",
        payload={"question_id": "question-a"},
        response_schema=SCHEMA,
        budget=budget,
        validate_response=reject_answer,
    )

    assert result == {"answer": "accept"}
    assert prompt(llm.calls[1]) == prompt(llm.calls[2])
    assert "private transport detail" not in prompt(llm.calls[2])
    assert (budget.normal_used, budget.repair_used, budget.transport_used) == (1, 1, 1)


async def test_cached_replay_survives_mutation_during_transport_retry():
    class MutatingLLM:
        def __init__(self):
            self.calls: list[dict] = []
            self.proposal = {
                "answer": "reject",
                "candidate_id": "candidate-captured-at-rejection",
            }

        async def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return self.proposal
            if len(self.calls) == 2:
                self.proposal["candidate_id"] = "candidate-mutated-after-rejection"
                raise OSError("temporary transport error")
            return {"answer": "accept"}

    llm = MutatingLLM()
    budget = DecisionBudget.reasoning_chain(1, 2, transport_limit=1, total_limit=3)

    await StructuredDecisionClient(llm).ask(
        stage="semantic-repair",
        payload={"question_id": "question-a"},
        response_schema=SCHEMA,
        budget=budget,
        validate_response=reject_answer,
    )

    repair_prompt = prompt(llm.calls[1])
    transport_retry_prompt = prompt(llm.calls[2])
    assert repair_prompt == transport_retry_prompt
    assert "candidate-captured-at-rejection" in repair_prompt
    assert "candidate-mutated-after-rejection" not in repair_prompt


async def test_format_failure_clears_replay_and_does_not_repeat_raw_provider_text():
    raw_text = "RAW-UNPARSED-PROVIDER-SENTINEL"
    llm = ScriptedLLM(
        {"answer": "reject", "candidate_id": "candidate-a"},
        raw_text,
        {"answer": "accept"},
    )

    result = await StructuredDecisionClient(llm).ask(
        stage="semantic-repair",
        payload={"question_id": "question-a"},
        response_schema=SCHEMA,
        validate_response=reject_answer,
    )

    assert result == {"answer": "accept"}
    correction_prompt = prompt(llm.calls[1])
    format_prompt = prompt(llm.calls[2])
    assert REPLAY_MARKER in correction_prompt
    assert REPLAY_MARKER not in format_prompt
    assert "Previous attempt failed (format): invalid_json" in format_prompt
    assert raw_text not in format_prompt


async def test_replay_redacts_secrets_and_keeps_serialized_envelope_within_cap():
    secret = "raw-semantic-replay-secret-7e03"
    proposal = {
        "answer": "reject",
        "question_id": "question-local-9",
        "candidate_id": "candidate-local-9",
        "password": secret,
        "comment": f"please honor api_key={secret}",
    }
    llm = ScriptedLLM(proposal, {"answer": "accept"})

    await StructuredDecisionClient(llm).ask(
        stage="semantic-repair",
        payload={"question_id": "question-local-9"},
        response_schema=SCHEMA,
        validate_response=reject_answer,
    )

    serialized = prompt(llm.calls[1]).split(REPLAY_MARKER, 1)[1]
    envelope = json.loads(serialized)
    assert len(serialized.encode("utf-8")) <= 16 * 1024
    assert envelope["omitted"] is False
    replay = envelope["previous_proposal"]
    assert replay["question_id"] == "question-local-9"
    assert replay["candidate_id"] == "candidate-local-9"
    assert "password" not in replay
    assert replay["comment"] == "please honor api_key=***REDACTED***"
    assert secret not in serialized


@pytest.mark.parametrize(
    "proposal",
    [
        {"answer": "reject", "note": "x" * (20 * 1024)},
        {"answer": "reject", "note": "한" * 6000},
        {"answer": "reject", "score": float("nan")},
        {"answer": "reject", "unsupported": object()},
    ],
    ids=["oversized", "utf8-bytes", "non-finite", "non-json-object"],
)
async def test_oversized_or_unserializable_replay_is_omitted_whole(proposal):
    llm = ScriptedLLM(proposal, {"answer": "accept"})

    await StructuredDecisionClient(llm).ask(
        stage="semantic-repair",
        payload={"question_id": "question-a"},
        response_schema=SCHEMA,
        validate_response=reject_answer,
    )

    serialized = prompt(llm.calls[1]).split(REPLAY_MARKER, 1)[1]
    assert json.loads(serialized) == OMISSION
    assert len(serialized.encode("utf-8")) < 16 * 1024
    assert "x" * 100 not in serialized


async def test_replay_is_request_local_for_concurrent_asks():
    class ConcurrentLLM:
        def __init__(self):
            self.calls: list[dict] = []
            self.responses = {
                "request-a": [
                    {"answer": "reject", "candidate_id": "candidate-a-only"},
                    {"answer": "accept-a"},
                ],
                "request-b": [
                    {"answer": "reject", "candidate_id": "candidate-b-only"},
                    {"answer": "accept-b"},
                ],
            }
            self.indices = defaultdict(int)

        async def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            content = prompt(kwargs)
            body = content.split("\n\nPrevious attempt failed", 1)[0]
            request_id = json.loads(body)["request_id"]
            index = self.indices[request_id]
            self.indices[request_id] += 1
            await asyncio.sleep(0)
            return self.responses[request_id][index]

    llm = ConcurrentLLM()
    client = StructuredDecisionClient(llm)
    results = await asyncio.gather(*(
        client.ask(
            stage="semantic-repair",
            payload={"request_id": request_id},
            response_schema=SCHEMA,
            validate_response=reject_answer,
        )
        for request_id in ("request-a", "request-b")
    ))

    assert results == [{"answer": "accept-a"}, {"answer": "accept-b"}]
    grouped: dict[str, list[str]] = defaultdict(list)
    for call in llm.calls:
        content = prompt(call)
        body = content.split("\n\nPrevious attempt failed", 1)[0]
        grouped[json.loads(body)["request_id"]].append(content)
    assert replay_envelope(grouped["request-a"][1])["previous_proposal"]["candidate_id"] == (
        "candidate-a-only"
    )
    assert replay_envelope(grouped["request-b"][1])["previous_proposal"]["candidate_id"] == (
        "candidate-b-only"
    )
    assert "candidate-b-only" not in grouped["request-a"][1]
    assert "candidate-a-only" not in grouped["request-b"][1]


async def test_diagnostic_trace_keeps_only_replay_size_metadata():
    sentinel = "FULL-REJECTED-RESULT-SENTINEL-184c"
    llm = ScriptedLLM(
        {"answer": "reject", "note": sentinel},
        {"answer": "reject", "note": "second rejected result"},
    )
    budget = DecisionBudget(2)

    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(llm).ask(
            stage="semantic-repair",
            payload={"question_id": "question-a"},
            response_schema=SCHEMA,
            budget=budget,
            validate_response=reject_answer,
        )

    request_events = [
        item for item in caught.value.diagnostic.trace if item["outcome"] == "request"
    ]
    assert "rejected_proposal_replay" not in request_events[0]
    replay_metadata = request_events[1]["rejected_proposal_replay"]
    serialized = prompt(llm.calls[1]).split(REPLAY_MARKER, 1)[1]
    assert replay_metadata == {"included": True, "bytes": len(serialized.encode("utf-8"))}
    diagnostics = repr(caught.value.diagnostic.model_dump(mode="json"))
    assert sentinel not in diagnostics
    assert "second rejected result" not in diagnostics


async def test_baseline_prompt_and_guarded_budget_exhaustion_are_unchanged():
    payload = {"question_id": "question-a", "captured": ["utf-8 ✓"]}
    llm = ScriptedLLM({"answer": "accept"})
    budget = DecisionBudget(1)
    guard_calls = 0

    async def guard():
        nonlocal guard_calls
        guard_calls += 1

    await StructuredDecisionClient(llm).ask(
        stage="semantic-repair",
        payload=payload,
        response_schema=SCHEMA,
        system="unchanged system prompt",
        budget=budget,
        before_call=guard,
    )

    assert prompt(llm.calls[0]) == json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )
    assert llm.calls[0]["system"] == "unchanged system prompt"
    assert guard_calls == budget.used == 1

    rejected_llm = ScriptedLLM({"answer": "reject", "candidate_id": "candidate-a"})
    rejected_budget = DecisionBudget(1)
    rejected_guard_calls = 0

    async def rejected_guard():
        nonlocal rejected_guard_calls
        rejected_guard_calls += 1

    with pytest.raises(DecisionFailure):
        await StructuredDecisionClient(rejected_llm).ask(
            stage="semantic-repair",
            payload={"question_id": "question-a"},
            response_schema=SCHEMA,
            budget=rejected_budget,
            before_call=rejected_guard,
            validate_response=reject_answer,
        )

    assert len(rejected_llm.calls) == 1
    assert rejected_guard_calls == rejected_budget.used == 1
    assert (rejected_budget.normal_used, rejected_budget.repair_used) == (1, 0)
