"""그래프 관계(relation) 이름 규칙 — entitled_* / observed_* 접두어를 코드로 강제한다.

이 파일이 없으면 `knowledge/graph.py` 여기저기서 "entitled_" 문자열을 직접
비교하게 되어, 오타 하나로 observed 관측이 entitled 로 새는 사고가 나기 쉽다(§5).
"""
from __future__ import annotations

ENTITLED_RELATIONS = (
    "entitled_own",
    "entitled_read",
    "entitled_write",
    "entitled_transfer_from",
    "entitled_approve",
)

# observed_* 는 entitled_* 와 같은 kind 집합을 공유하되, 접두어만 다르다.
_KINDS = ("own", "read", "write", "transfer_from", "approve")


def entitled_relation(kind: str) -> str:
    rel = f"entitled_{kind}"
    if rel not in ENTITLED_RELATIONS:
        raise ValueError(f"알 수 없는 entitled 관계: {rel!r} (허용: {ENTITLED_RELATIONS})")
    return rel


def observed_relation(kind: str) -> str:
    if kind not in _KINDS:
        raise ValueError(f"알 수 없는 관계 종류: {kind!r} (허용: {_KINDS})")
    return f"observed_{kind}"


def is_entitled_relation(relation: str) -> bool:
    return relation.startswith("entitled_")


def is_observed_relation(relation: str) -> bool:
    return relation.startswith("observed_")
