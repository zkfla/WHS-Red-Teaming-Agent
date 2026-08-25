"""Engagement 정책 위반 예외. 의존성 없음 — `probing/session.py` 와
`engagement/policy.py` 양쪽에서 순환 임포트 없이 쓰기 위해 별도 모듈로 둔다.
"""
from __future__ import annotations


class PolicyViolation(Exception):
    """Hard Constraint 위반 — 실제 네트워크 요청이 나가기 전에 여기서 막는다.

    `rule`: 어떤 제약을 위반했는지 (예: "OUT_OF_SCOPE_HOST", "METHOD_FORBIDDEN",
    "STATE_CHANGE_FORBIDDEN", "AMOUNT_EXCEEDS_CEILING",
    "ACCOUNT_CREATION_FORBIDDEN"). `replay.py`/report 등에서 사유를 구분해
    보여줄 때 쓴다.
    """

    def __init__(self, rule: str, detail: str) -> None:
        self.rule = rule
        self.detail = detail
        super().__init__(f"[{rule}] {detail}")
