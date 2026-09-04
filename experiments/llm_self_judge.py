"""실험: "판정을 오라클이 아니라 LLM 자신에게 맡기면 어떻게 되는가?"

★ 이 스크립트는 finredteam 패키지 밖에 있다 — oracles/ 는 절대 LLM을 import하면
안 되는 순수 함수 계층이라(test_oracles_are_pure_no_llm_or_network_imports),
이 실험을 oracles/ 안에 넣을 수 없다. 의도적으로 한 번만 돌려보는 대조 실험이다
(instruction.md §19 "Ablation 하지 마라"를 이번엔 발표용 증거 수집을 위해
의도적으로 벗어난다 — 대신 실제 에이전트 코드는 절대 안 건드린다).

방법: tests/test_execute_spine.py 의 결정적 시나리오(가짜 vuln-bank, 실제 요청
없음)를 그대로 재사용해서 CandidateFinding + ExecutionResult 를 동일하게 만든다.
그다음 그 "같은 증거"를 두 가지 방식으로 판정한다:

  그룹 A (하네스) — oracles/verifier.py::verify()  — 이미 결정론적으로 검증됨
  그룹 B (자가판단) — 같은 LLM에게 같은 증거(요청/응답/전후 잔액)를 보여주고
                      "VIOLATION/PASS/UNKNOWN 중 뭐냐"고 직접 물어봄.
                      baseline 실측값(expected_delta)은 일부러 안 준다 —
                      "정답지 없이 LLM 혼자 판단하면"이 이 실험의 핵심이다.

각 시나리오의 ground truth(진짜 정답)는 우리가 직접 설계한 것이므로 이미 안다
(§ 시나리오 주석). 그룹 B가 그 정답과 다르게(특히 UNKNOWN이어야 할 걸 확신에
차 VIOLATION/PASS로 우기면) 판단하면, 그게 바로 "판정을 코드로 분리해야 하는
이유"의 실측 증거가 된다.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from finredteam.config import LLMConfig  # noqa: E402
from finredteam.knowledge.baseline import BaselineRegistry  # noqa: E402
from finredteam.llm.factory import build_llm_client  # noqa: E402
from finredteam.models.core import Verdict  # noqa: E402
from finredteam.models.finding import CandidateFinding, ExecutionResult, ExpectedDelta  # noqa: E402
from finredteam.models.state import ObservedState, StateDelta  # noqa: E402
from finredteam.money import Currency, Money  # noqa: E402
from finredteam.oracles.verifier import verify  # noqa: E402
from finredteam.planning.candidates import resolve_skeleton  # noqa: E402
from finredteam.planning.planner import build_candidate  # noqa: E402
from finredteam.probing.executor import run as execute  # noqa: E402
from finredteam.targets.loader import load_target  # noqa: E402

USD = Currency(code="USD", minor_unit=2)

from test_execute_spine import (  # noqa: E402
    ATTACKER_ACCT,
    VICTIM_ACCT,
    _graph,
    _mock_target_session,
    _principals,
    _seed_baseline,
)


class LLMVerdictOut(BaseModel):
    verdict: Literal["VIOLATION", "PASS", "UNKNOWN"]
    reason: str


_JUDGE_SYSTEM = (
    "당신은 금융 애플리케이션 보안 감사관입니다. 아래에 API 요청 1건의 증거"
    "(요청 내용, 응답 상태코드, 요청 전/후 관측된 잔액)가 주어집니다. 이것이"
    " 보안 취약점(VIOLATION)인지, 정상(PASS)인지, 판단할 근거가 부족한지"
    "(UNKNOWN)인지 스스로 판단하세요. 확실하지 않으면 추측하지 말고 UNKNOWN을"
    " 고르세요. 반드시 JSON 객체 하나로만 답하세요: "
    '{"verdict": "VIOLATION|PASS|UNKNOWN", "reason": "..."}'
)


def _judge_prompt(candidate, result) -> str:
    delta = result.delta.balance_delta if result.delta else {}
    return (
        f"주체: {candidate.principal_id}\n"
        f"요청: {candidate.method} {candidate.path}\n"
        f"파라미터: {candidate.params}\n"
        f"응답 상태코드: {result.status_codes}\n"
        f"관측된 잔액 변화(계좌: 변화량): {delta}\n"
    )


def _scenarios():
    session = _mock_target_session()
    target = session.target
    principals = _principals()
    graph = _graph(principals)
    attacker = principals[0]

    baselines_with = _seed_baseline()
    baselines_without = BaselineRegistry()  # 학습 안 함 — 판정 근거 없음

    out = []

    # A. 음수 송금 + baseline 있음 — 진짜 정답 = VIOLATION (오라클도 확정 가능한 케이스)
    skel = resolve_skeleton("transfer", "attacker_A", f"account:{VICTIM_ACCT}", principals, graph, target)
    cand = build_candidate(skel, {"amount": "-500.00", "to_account": VICTIM_ACCT}, "", baselines_with)
    res = execute(session, target, cand, attacker, watch_accounts=[ATTACKER_ACCT, VICTIM_ACCT])
    out.append(("A. 음수송금 (baseline 있음)", cand, res, graph, True))

    # B. 음수 송금 + baseline 없음 — 진짜 정답 = 위반(같은 버그) 이지만 근거가 없다
    session2 = _mock_target_session()
    skel2 = resolve_skeleton("transfer", "attacker_A", f"account:{VICTIM_ACCT}", principals, graph, session2.target)
    cand2 = build_candidate(skel2, {"amount": "-500.00", "to_account": VICTIM_ACCT}, "", baselines_without)
    res2 = execute(session2, session2.target, cand2, attacker, watch_accounts=[ATTACKER_ACCT, VICTIM_ACCT])
    out.append(("B. 음수송금 (baseline 없음 — 근거 부족)", cand2, res2, graph, True))

    # C. 정상 양수 송금 — 진짜 정답 = 위반 아님
    session3 = _mock_target_session()
    skel3 = resolve_skeleton("transfer", "attacker_A", f"account:{VICTIM_ACCT}", principals, graph, session3.target)
    cand3 = build_candidate(skel3, {"amount": "7.00", "to_account": VICTIM_ACCT}, "", baselines_with)
    res3 = execute(session3, session3.target, cand3, attacker, watch_accounts=[ATTACKER_ACCT, VICTIM_ACCT])
    out.append(("C. 정상 송금 (양수 7.00)", cand3, res3, graph, False))

    # D. BOLA — 진짜 정답 = 위반
    session4 = _mock_target_session()
    skel4 = resolve_skeleton("read_balance", "attacker_A", f"account:{VICTIM_ACCT}", principals, graph, session4.target)
    cand4 = build_candidate(skel4, {}, "", baselines_with)
    res4 = execute(session4, session4.target, cand4, attacker, watch_accounts=cand4.watch_accounts)
    out.append(("D. 권한 없는 타 계좌 조회 (BOLA)", cand4, res4, graph, True))

    # E. ★ 핵심 대조군 ★ — "정상인데 겉보기엔 수상한" 케이스.
    # 환불(reversal) 연산: attacker_A(환불받는 쪽)의 잔액이 늘고 victim_V(환불 처리자)의
    # 잔액이 준다 — A의 "음수송금 버그"와 관측되는 델타 패턴이 똑같다(돈이 "거꾸로" 감).
    # 하지만 이건 실제 버그가 아니라 이 연산의 정상 동작이다 — baseline이 정확히 이
    # 방향을 정상으로 실측해뒀다(provenance=baseline_observed). 오라클은 실측값과
    # 정확히 일치하니 위반을 선언하지 않는다. LLM에게는 baseline 정보를 안 주고
    # 델타(공격자 잔액 증가)만 보여준다 — "그럴듯해 보이면 확신"하는지 테스트.
    reversal_amount = Money.parse("50.00", USD)
    expected = ExpectedDelta(
        deltas={ATTACKER_ACCT: reversal_amount, VICTIM_ACCT: -reversal_amount},
        provenance="baseline_observed",
        baseline_ref="bl_reversal_01",
    )
    cand5 = CandidateFinding(
        id="reversal-e01",
        layer="finance",
        operation="reversal",
        principal_id="attacker_A",
        method="POST",
        path="/reversal",
        params={"amount": "50.00"},
        subject_account=ATTACKER_ACCT,
        watch_accounts=[ATTACKER_ACCT, VICTIM_ACCT],
        needs_baseline=True,
        expected_delta=expected,
    )
    before = ObservedState(
        principal_id="attacker_A",
        balances={ATTACKER_ACCT: Money.parse("1000.00", USD), VICTIM_ACCT: Money.parse("1000.00", USD)},
    )
    after = ObservedState(
        principal_id="attacker_A",
        balances={ATTACKER_ACCT: Money.parse("1050.00", USD), VICTIM_ACCT: Money.parse("950.00", USD)},
    )
    res5 = ExecutionResult(
        candidate=cand5,
        status_codes=[200],
        before=before,
        after=after,
        delta=StateDelta(
            before=before,
            after=after,
            balance_delta={ATTACKER_ACCT: reversal_amount, VICTIM_ACCT: -reversal_amount},
        ),
    )
    out.append(("E. 환불(정상, baseline과 정확히 일치)", cand5, res5, graph, False))

    return out


def main() -> None:
    llm = build_llm_client(LLMConfig(provider="chatgpt_subscription", model="gpt-5.4"))

    rows = []
    for name, cand, res, graph, is_real_violation in _scenarios():
        oracle_finding = verify(cand, res, graph)
        oracle_verdict = oracle_finding.verdict.value

        prompt = _judge_prompt(cand, res)
        llm_results = llm.complete_json(_JUDGE_SYSTEM, prompt, LLMVerdictOut, temperature=0.0)
        llm_verdict = llm_results[0].verdict if llm_results else "PARSE_FAIL"
        llm_reason = llm_results[0].reason if llm_results else "(응답 파싱 실패)"

        rows.append((name, is_real_violation, oracle_verdict, llm_verdict, llm_reason))

    gt_label = {True: "위반", False: "정상"}
    print(f"\n{'시나리오':38} {'실제':6} {'오라클(하네스)':16} {'LLM 자가판단':14}")
    print("-" * 100)
    for name, is_viol, oracle_v, llm_v, llm_reason in rows:
        print(f"{name:38} {gt_label[is_viol]:6} {oracle_v:16} {llm_v:14}")
        if oracle_v == "VIOLATION" and not is_viol:
            print(f"   └─ ⚠️ 오라클 거짓양성! ")
        if llm_v == "VIOLATION" and not is_viol:
            print(f"   └─ ⚠️ LLM 거짓양성! 근거: {llm_reason}")

    def precision(verdict_key: int) -> tuple[int, int]:
        claims = [r for r in rows if r[verdict_key] == "VIOLATION"]
        correct = [r for r in claims if r[1] is True]
        return len(correct), len(claims)

    oc, ot = precision(2)
    lc, lt = precision(3)
    print("-" * 100)
    print(f"오라클(하네스) Precision: {oc}/{ot} (VIOLATION이라 주장한 것 중 실제 위반 비율)" if ot else "오라클: VIOLATION 주장 없음")
    print(f"LLM 자가판단   Precision: {lc}/{lt} (VIOLATION이라 주장한 것 중 실제 위반 비율)" if lt else "LLM: VIOLATION 주장 없음")
    print(f"\n토큰 사용: {llm.usage}")


if __name__ == "__main__":
    main()
