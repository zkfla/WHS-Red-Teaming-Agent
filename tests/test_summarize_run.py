"""실행 결과 자연어 요약(§ reporting/summarize_run.py).

★ 판정과 무관한 순수 부가 기능이라는 걸 확인한다 — 실패해도(LLMError, 빈
응답) 예외를 던지지 않고 조용히 None 을 돌려줘야 한다(호출부인 __main__.py
가 이미 저장된 결과를 보여주는 걸 막으면 안 되므로).
"""
from __future__ import annotations

from finredteam.llm.base import LLMError, Usage
from finredteam.llm.fake import FakeLLMClient
from finredteam.reporting.summarize_run import RunNarrative, summarize


class _StubLLM:
    provider = "stub"
    model = "stub"

    def __init__(self, response: str | None = None, raise_error: bool = False, empty: bool = False):
        self._response = response
        self._raise_error = raise_error
        self._empty = empty
        self.last_user_prompt: str | None = None

    def complete_json(self, system, user, schema, temperature=0.3):
        self.last_user_prompt = user
        if self._raise_error:
            raise LLMError("boom")
        if self._empty:
            return []
        return [schema.model_validate({"narrative": self._response})]

    @property
    def usage(self) -> Usage:
        return Usage()


_REPORT = {"run_id": "r1", "target": "vulnbank", "verified_count": 2, "termination_reason": "max_rounds"}
_FINDINGS = [
    {
        "verdict": "VIOLATION",
        "invariant": "INV-2",
        "reason": "entitlement 없이 접근 성공",
        "candidate": {"operation": "read_balance", "principal_id": "attacker_A", "method": "GET", "path": "/x"},
    }
]


def test_summarize_returns_narrative_from_llm():
    llm = _StubLLM(response="테스트 결과 요약입니다.")
    result = summarize(llm, _REPORT, _FINDINGS)
    assert result == "테스트 결과 요약입니다."


def test_summarize_returns_none_on_llm_error():
    llm = _StubLLM(raise_error=True)
    assert summarize(llm, _REPORT, _FINDINGS) is None


def test_summarize_returns_none_on_empty_response():
    llm = _StubLLM(empty=True)
    assert summarize(llm, _REPORT, _FINDINGS) is None


def test_summarize_prompt_does_not_include_raw_evidence_refs():
    # 프롬프트를 불필요하게 키우지 않는지 — evidence_refs 같은 세부 필드는 뺀다.
    llm = _StubLLM(response="ok")
    findings = [{**_FINDINGS[0], "evidence_refs": ["ev_should_not_leak_into_prompt"]}]
    summarize(llm, _REPORT, findings)
    assert "ev_should_not_leak_into_prompt" not in llm.last_user_prompt
    assert "INV-2" in llm.last_user_prompt


def test_fake_llm_client_handles_narrative_schema_without_crashing():
    llm = FakeLLMClient()
    results = llm.complete_json("sys", "user", RunNarrative)
    assert len(results) == 1
    assert isinstance(results[0].narrative, str)
    assert results[0].narrative
