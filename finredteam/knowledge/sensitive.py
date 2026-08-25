"""민감정보 등급 사전 — storage/evidence.py 가 이 등급으로 원문 저장 여부를 결정한다.

여기는 정적 분류 규칙만 둔다. Day3 이후 knowledge/graph.py 등이 커져도 이 파일은
storage/ 가 Day1부터 참조할 수 있도록 의존성 없이 독립적으로 유지한다.
"""
from __future__ import annotations

from typing import Literal

Level = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]

# 필드 이름(소문자, 부분일치)에 따른 등급. 위에서부터 우선순위가 높다.
_CRITICAL_MARKERS = ("password", "cvv", "card_number", "pan", "ssn", "ssn_number", "secret", "api_key", "private_key")
_HIGH_MARKERS = ("token", "balance", "account_number", "card_id", "email", "phone")
_MEDIUM_MARKERS = ("amount", "limit", "username", "name", "address")


def _classify_field(field_name: str) -> Level:
    lowered = field_name.lower()
    if any(marker in lowered for marker in _CRITICAL_MARKERS):
        return "CRITICAL"
    if any(marker in lowered for marker in _HIGH_MARKERS):
        return "HIGH"
    if any(marker in lowered for marker in _MEDIUM_MARKERS):
        return "MEDIUM"
    return "LOW"


_LEVEL_RANK: dict[Level, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def classify(field_names: list[str]) -> Level:
    """필드 이름 목록 중 가장 높은 등급을 응답 전체의 등급으로 삼는다."""
    if not field_names:
        return "LOW"
    return max((_classify_field(f) for f in field_names), key=lambda lv: _LEVEL_RANK[lv])
