"""민감정보 중앙 통제 — 모든 응답 증거는 이 모듈을 거친다 (§10, §설계원칙 6).

CRITICAL/HIGH 등급 필드가 섞인 응답은 원문을 버리고 스키마(타입)·필드 존재여부·
해시만 남긴다. F8 열거 프로브가 타인의 잔액·이름을 실제로 받아오더라도, 그 값을
그대로 저장하는 행위 자체가 유출이므로 절대 원문을 남기지 않는다.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel

from finredteam.knowledge.sensitive import classify
from finredteam.storage.db import DB

TRUNCATE_BYTES = 4096


class CapturedResponse(BaseModel):
    """probing/session.py 등이 만들어 evidence.py 로 넘기는 원시 응답 표현."""

    status_code: int
    body: Any = None  # 파싱된 JSON (dict/list) 또는 None — money.loads_money_json 경유
    text: str = ""  # 원문 텍스트 (해시·크기 계산용)

    model_config = {"arbitrary_types_allowed": True}


class EvidenceRef(BaseModel):
    ref_id: str
    mode: Literal["full", "redacted"]
    status: int
    size: int
    body_hash: str
    body: str | None = None  # mode="full" 일 때만: 정규화된 JSON 문자열(4096바이트로 절단)
    value_schema: Any = None  # mode="redacted" 일 때만: 타입만, 값 없음
    field_presence: dict[str, bool] | None = None
    context: dict[str, str] = {}


def extract_field_names(body: Any, prefix: str = "") -> list[str]:
    names: list[str] = []
    if isinstance(body, dict):
        for key, value in body.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            names.append(path)
            names.extend(extract_field_names(value, path))
    elif isinstance(body, list):
        for item in body:
            names.extend(extract_field_names(item, prefix))
    return names


def field_schema(body: Any) -> Any:
    """값을 버리고 타입 구조만 남긴다."""
    if isinstance(body, dict):
        return {k: field_schema(v) for k, v in body.items()}
    if isinstance(body, list):
        return [field_schema(body[0])] if body else []
    return type(body).__name__


def canonical(body: Any) -> str:
    return json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def truncate(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore") + "...<truncated>"


def store_response(
    resp: CapturedResponse, context: dict[str, str], db: DB | None = None
) -> EvidenceRef:
    field_names = extract_field_names(resp.body) if resp.body is not None else []
    level = classify(field_names)
    canonical_body = canonical(resp.body)
    body_hash = _sha256(canonical_body)
    size = len(resp.text.encode("utf-8")) if resp.text else len(canonical_body.encode("utf-8"))

    if level in ("CRITICAL", "HIGH"):
        ref = EvidenceRef(
            ref_id=f"ev_{body_hash[:16]}",
            mode="redacted",
            status=resp.status_code,
            size=size,
            body_hash=body_hash,
            value_schema=field_schema(resp.body),
            field_presence={f: True for f in field_names},
            context=context,
        )
    else:
        ref = EvidenceRef(
            ref_id=f"ev_{body_hash[:16]}",
            mode="full",
            status=resp.status_code,
            size=size,
            body_hash=body_hash,
            body=truncate(canonical_body, TRUNCATE_BYTES),
            context=context,
        )

    if db is not None:
        db.append("responses", ref.model_dump())
    return ref
