"""semantic + episodic → 3k 토큰 이내 압축 문자열. LLM 을 쓰지 않는다(결정적).

planning/prompts.py(Day5) 가 이 출력을 그대로 프롬프트에 넣는다. 최신 lesson·fact를
우선하고, 상한을 넘기면 오래된 것부터 자른다 — 정확한 벤더 토크나이저 대신 문자수
기반 근사치를 쓰되, 항상 상한 아래로 보수적으로 자른다.
"""
from __future__ import annotations

from finredteam.memory.episodic import EpisodicMemory
from finredteam.memory.semantic import SemanticMemory

_CHARS_PER_TOKEN_ESTIMATE = 4


def _approx_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN_ESTIMATE + 1


def summarize(semantic: SemanticMemory, episodic: EpisodicMemory, max_tokens: int = 3000) -> str:
    lines: list[str] = []

    for lesson in reversed(episodic.lessons()):
        lines.append(f"- lesson[{lesson.attack_class or '?'}]: {lesson.insight}")

    for fact in reversed(semantic.observed()):
        lines.append(f"- fact: {fact.subject} {fact.predicate}={fact.object}")

    kept: list[str] = []
    total = 0
    for line in lines:
        cost = _approx_tokens(line)
        if total + cost > max_tokens:
            break
        kept.append(line)
        total += cost

    return "\n".join(kept) if kept else "(수집된 사실/교훈 없음)"
