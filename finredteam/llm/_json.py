"""벤더 응답 텍스트에서 JSON 배열을 뽑아내는 공용 헬퍼.

Anthropic/OpenAI 모두 완벽한 JSON만 반환한다고 보장할 수 없다 — 코드펜스나
설명 문장이 섞여 나올 수 있으므로 방어적으로 추출한다.
"""
from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json_array(text: str) -> list | None:
    stripped = _FENCE_RE.sub("", text).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("[")
        end = stripped.rfind("]")
        if start == -1 or end == -1 or end < start:
            return None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None

    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return None
