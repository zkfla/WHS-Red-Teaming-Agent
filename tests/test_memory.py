from finredteam.memory.episodic import EpisodicMemory
from finredteam.memory.semantic import SemanticMemory
from finredteam.memory.summarizer import _approx_tokens, summarize
from finredteam.memory.working import WorkingMemory
from finredteam.models.core import Verdict
from finredteam.models.finding import CandidateFinding, ExecutionResult, Finding
from finredteam.models.knowledge import Fact, Lesson


def _candidate(i: int) -> CandidateFinding:
    return CandidateFinding(
        id=f"c{i}",
        layer="finance",
        attack_class="F2",
        invariants=["INV-1B"],
        principal_id="attacker_A",
        method="POST",
        path="/transfer",
    )


def test_working_memory_set_get_clear():
    wm = WorkingMemory()
    wm.set("round", 1)
    assert wm.get("round") == 1
    assert wm.get("missing", "default") == "default"
    wm.clear()
    assert wm.get("round") is None


def test_semantic_memory_observed_filters_inferred():
    sm = SemanticMemory()
    sm.add_fact(Fact(subject="/dashboard", predicate="requires_auth", object="true", confidence="observed"))
    sm.add_fact(Fact(subject="/x", predicate="guess", object="maybe", confidence="inferred"))
    assert len(sm.observed()) == 1
    assert sm.observed()[0].subject == "/dashboard"
    assert len(sm.all()) == 2


def test_episodic_memory_records_and_computes_success_rate():
    em = EpisodicMemory()
    cand = _candidate(1)
    result = ExecutionResult(candidate=cand, status_codes=[200])
    violation_finding = Finding(candidate=cand, result=result, verdict=Verdict.VIOLATION)
    pass_finding = Finding(candidate=cand, result=result, verdict=Verdict.PASS)

    em.record(cand, violation_finding, Lesson(candidate_summary="c1", outcome="VIOLATION", insight="works"))
    em.record(cand, pass_finding, Lesson(candidate_summary="c1", outcome="PASS", insight="no luck"))

    assert em.success_rate("F2") == 0.5
    assert "F2" in em.tried_attack_classes()
    assert len(em.lessons()) == 2


def test_episodic_memory_consecutive_failures():
    em = EpisodicMemory()
    cand = _candidate(1)
    result = ExecutionResult(candidate=cand, status_codes=[200])
    for _ in range(3):
        em.record(cand, Finding(candidate=cand, result=result, verdict=Verdict.PASS))
    assert em.consecutive_failures() == 3

    em.record(cand, Finding(candidate=cand, result=result, verdict=Verdict.VIOLATION))
    assert em.consecutive_failures() == 0


def test_summarizer_stays_within_3k_token_budget():
    sm = SemanticMemory()
    em = EpisodicMemory()
    cand = _candidate(1)
    result = ExecutionResult(candidate=cand, status_codes=[200])

    # 3천 토큰을 훌쩍 넘는 양의 사실/교훈을 밀어넣는다.
    for i in range(2000):
        sm.add_fact(
            Fact(subject=f"/endpoint/{i}", predicate="requires_auth", object="true", confidence="observed")
        )
        em.record(
            cand,
            Finding(candidate=cand, result=result, verdict=Verdict.PASS),
            Lesson(candidate_summary=f"c{i}", outcome="PASS", insight=f"시도 {i}: 별다른 발견 없음" * 3),
        )

    summary = summarize(sm, em, max_tokens=3000)
    assert _approx_tokens(summary) <= 3000


def test_summarizer_prefers_most_recent_when_truncating():
    sm = SemanticMemory()
    em = EpisodicMemory()
    for i in range(500):
        sm.add_fact(
            Fact(subject=f"/e/{i}", predicate="requires_auth", object="true", confidence="observed")
        )

    summary = summarize(sm, em, max_tokens=50)
    assert "/e/499" in summary  # 가장 최근 사실이 우선 포함된다
    assert "/e/0 " not in summary


def test_summarizer_empty_memory():
    sm = SemanticMemory()
    em = EpisodicMemory()
    assert summarize(sm, em) == "(수집된 사실/교훈 없음)"
