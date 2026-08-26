"""후보(CandidateFinding)를 실제로 실행한다 — 관측(전) → 요청(N회) → 관측(후).

레이어별 분기(§13 pseudocode `executor.run`)는 Day7 에서 `probing/generic/*` 이
붙으면서 완성된다. Day5 범위는 finance 레이어 실행 경로다.
"""
from __future__ import annotations

from finredteam.config import MAX_PROBES
from finredteam.knowledge.sensitive import classify
from finredteam.models.core import Principal
from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.probing.finance.state import compute_delta, observe_many
from finredteam.probing.session import Session, SessionError
from finredteam.storage.db import DB
from finredteam.storage.evidence import extract_field_names, store_response
from finredteam.targets.loader import TargetConfig


def run(
    session: Session,
    target: TargetConfig,
    candidate: CandidateFinding,
    principal: Principal,
    watch_accounts: list[str],
    *,
    db: DB | None = None,
    repeats: int = 1,
) -> ExecutionResult:
    """`watch_accounts`: 이 시험으로 델타를 관측해야 하는 계좌 번호 목록
    (예: 송금이면 [발신 계좌, 수신 계좌]) — candidates.py/planner.py 가 채운다.
    `repeats`: F3(멱등성) 처럼 동일 후보를 여러 번 실행해야 할 때 쓴다 (MAX_PROBES 상한).
    """
    repeats = max(1, min(repeats, MAX_PROBES))

    before = observe_many(session, target, watch_accounts, principal_id=principal.id)

    status_codes: list[int] = []
    evidence_refs: list[str] = []
    latencies_ms: list[float] = []
    sensitive_levels: list[str] = []
    response_texts: list[str] = []
    for _ in range(repeats):
        try:
            ex = session.authed_request(
                candidate.method, candidate.path, principal, json_body=candidate.params
            )
        except SessionError:
            status_codes.append(0)
            continue
        status_codes.append(ex.status_code)
        ref = store_response(
            session.capture(ex), {"candidate_id": candidate.id, "principal": principal.id}, db
        )
        evidence_refs.append(ref.ref_id)
        latencies_ms.append(ex.elapsed_ms)
        field_names = extract_field_names(ex.response_body) if ex.response_body is not None else []
        sensitive_levels.append(classify(field_names))
        response_texts.append(ex.response_text or "")

    after = observe_many(session, target, watch_accounts, principal_id=principal.id)
    delta = compute_delta(before, after)

    return ExecutionResult(
        candidate=candidate,
        status_codes=status_codes,
        before=before,
        after=after,
        delta=delta,
        evidence_refs=evidence_refs,
        latencies_ms=latencies_ms,
        sensitive_levels=sensitive_levels,
        response_texts=response_texts,
    )
