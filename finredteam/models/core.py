"""공통 기본 타입 — Verdict, Principal, Endpoint."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel


class Verdict(str, Enum):
    """판정 4종 (§instruction 설계원칙 4). VIOLATION 만 Verified Finding 이 된다."""

    PASS = "PASS"
    VIOLATION = "VIOLATION"
    ANOMALY = "ANOMALY"
    UNKNOWN = "UNKNOWN"


class Principal(BaseModel):
    """시험 주체(계정). 부트스트랩에서 생성되고 이후 모든 요청의 신원으로 쓰인다."""

    id: str  # "attacker_A" 등 논리 이름
    username: str
    password: str
    account_number: str | None = None
    token: str | None = None
    role: Literal["user", "merchant", "admin"] = "user"
    extra: dict[str, str] = {}  # api_key 등 어댑터별 부가 인증 정보


class Endpoint(BaseModel):
    """정찰로 확인된 엔드포인트 하나."""

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str  # 템플릿 포함 가능, 예: "/check_balance/{account_number}"
    auth_required: bool | None = None  # None = 미확인
    layer: Literal["generic", "finance"] | None = None
    source: Literal["seed", "openapi", "graphql", "crawl", "config"] = "seed"
    tags: list[str] = []
