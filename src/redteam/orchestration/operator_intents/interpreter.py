"""Deterministic Korean/English interpretation of operator instructions.

This is intentionally *not* an LLM: an operator instruction is a control
input, so classifying it must be reproducible, inspectable and cheap.
The classifier maps free text onto the frozen ``OperatorIntentType``
vocabulary, extracts at most a known security area vocabulary (never an
arbitrary target), and returns every keyword that fired so the decision is
explainable after the fact.

Anything that does not map to a known, safe instruction raises
``IntentInterpretationError`` — the service records the rejection rather
than guessing.
"""

from __future__ import annotations

import re
from datetime import datetime

from redteam.core.contracts.mission import OperatorIntent, Provenance
from redteam.core.enums import OperatorIntentType
from redteam.core.ids import MissionId, OperatorIntentId, TaskId, new_id
from redteam.orchestration.operator_intents.models import (
    IntentClassification,
    IntentInterpretationError,
)
from redteam.orchestration.operator_intents.redaction import redact_text, secret_parameters

#: Read-only questions win over actions: if an operator asks "why did you
#: stop?", answering is safer than re-interpreting the word "stop".
_EXPLANATION_KEYWORDS = (
    "explain",
    "why",
    "how did",
    "how come",
    "reason",
    "설명",
    "이유",
    "왜",
    "어떻게",
)
_STATUS_KEYWORDS = (
    "status",
    "progress",
    "state",
    "summary",
    "현황",
    "상태",
    "진행",
    "요약",
)

#: Actions, in precedence order ("일시정지" must read as pause, not stop).
_PAUSE_KEYWORDS = (
    "pause",
    "hold on",
    "hold",
    "wait",
    "일시정지",
    "일시 정지",
    "잠시",
    "잠깐",
    "멈춰",
    "멈춤",
)
_RESUME_KEYWORDS = (
    "resume",
    "unpause",
    "continue",
    "carry on",
    "재개",
    "계속",
    "이어서",
    "다시 시작",
)
_STOP_KEYWORDS = (
    "stop",
    "abort",
    "terminate",
    "halt",
    "중지",
    "정지",
    "중단",
    "그만",
    "종료",
)
_EXCLUDE_KEYWORDS = (
    "exclude",
    "skip",
    "ignore",
    "avoid",
    "omit",
    "cancel",
    "no longer look",
    "don't look",
    "dont look",
    "stop looking",
    "제외",
    "무시",
    "건너뛰",
    "빼고",
    "빼줘",
    "취소",
    # "이 endpoint는 더 이상 보지 마" / "그 경로 안 봐" — a natural
    # suppression that must not fall through to the unknown-instruction
    # rejection. "안 봐" is only read as EXCLUDE when no explanation keyword
    # ("왜") fired first, so "왜 /api/docs 안 봐?" stays a question.
    "더 이상 보지",
    "더이상 보지",
    "보지 마",
    "보지마",
    "안 봐",
    "안봐",
)
_REPRIORITIZE_KEYWORDS = (
    "reprioritize",
    "re-prioritize",
    "reorder",
    "우선순위",
    "순서",
    "priority order",
)
_FOCUS_KEYWORDS = ("focus", "concentrate", "prioritize", "emphasis", "집중", "중점", "우선")

#: Only canonical, already-understood workstreams are extractable. This is
#: what keeps FOCUS/EXCLUDE from being an implicit scope expansion: they can
#: only re-rank or drop work the Mission already has, never name a new target.
_AREA_SYNONYMS: dict[str, tuple[str, ...]] = {
    "reconnaissance": ("reconnaissance", "recon", "scan", "정찰", "스캔"),
    "web": ("web", "http", "웹"),
    "api": ("api", "endpoint", "엔드포인트"),
    "authentication": ("authentication", "authn", "login", "로그인", "인증"),
    "authorization": ("authorization", "authz", "access control", "권한", "인가"),
    "mobile": ("mobile", "android", "ios", "모바일", "앱"),
}

_NUMBER_RE = re.compile(r"\b(\d{1,4})\b")

_QUERY_TYPES = (OperatorIntentType.EXPLANATION_QUERY, OperatorIntentType.STATUS_QUERY)

#: What an operator can ask *about* in an explanation. This only selects
#: which structured section ``OperatorIntentService.explain`` emphasizes;
#: it never widens scope.
_SUBJECT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "candidate": ("candidate", "후보", "candidate_key"),
    "action": ("action", "chosen", "choice", "selected", "동작", "행동", "선택"),
    "blocked": ("blocked", "block", "blocking", "막힌", "차단", "블록"),
    "endpoint": ("endpoint", "엔드포인트", "path", "경로"),
    "task": ("task", "subtask", "태스크", "작업"),
}

#: A bounded endpoint selector: an absolute path (``/api/docs``) or an
#: absolute http(s) URL. It is deliberately narrow — it can only name an
#: endpoint that already exists in the Task Graph / Attack Graph, so an
#: EXCLUDE/FOCUS/explanation built on it cannot expand scope.
_ENDPOINT_TOKEN_RE = re.compile(
    r"(?<![\w/])("
    r"https?://[A-Za-z0-9.\-]+(?::\d+)?(?:/[A-Za-z0-9._~%\-]+)*"
    r"|/[A-Za-z0-9._~%\-]+(?:/[A-Za-z0-9._~%\-]+)*"
    r")"
)
_PATH_SELECTOR_RE = re.compile(r"/[A-Za-z0-9._~%\-]+(?:/[A-Za-z0-9._~%\-]+)*")
_URL_SELECTOR_RE = re.compile(r"https?://[A-Za-z0-9.\-]+(?::\d+)?(?:/[A-Za-z0-9._~%\-]+)*")

#: The only supported task selector syntax — ``task:<id>`` / ``task#<id>``.
#: It is resolved against *existing* Task ids by the service; an unknown
#: selector simply matches nothing rather than creating work.
_TASK_SELECTOR_RE = re.compile(
    r"(?i)\b(?:task|subtask|태스크)\s*[:#]\s*([A-Za-z0-9][A-Za-z0-9._:%-]{0,127})"
)

_MAX_SELECTOR_LENGTH = 200
_MAX_SELECTORS = 8


def _validate_endpoint_selector(raw: str) -> str | None:
    """Return a canonical selector, or ``None`` when it is not bounded/safe."""
    value = raw.strip().rstrip(".,;:!?)]}\"'")
    if not value or len(value) > _MAX_SELECTOR_LENGTH:
        return None
    if any(character.isspace() for character in value) or ".." in value:
        return None
    if value.startswith(("http://", "https://")):
        return value if _URL_SELECTOR_RE.fullmatch(value) else None
    if not value.startswith("/") or value == "/" or "//" in value:
        return None
    return value if _PATH_SELECTOR_RE.fullmatch(value) else None


def extract_endpoint_selectors(text: str) -> list[str]:
    """Canonical, bounded endpoint selectors named in ``text`` (stable order).

    Only path-like/URL-like tokens that pass :func:`_validate_endpoint_selector`
    are returned, at most :data:`_MAX_SELECTORS` of them. The list is a
    *reference* to already-observed endpoints, not new target scope.
    """
    found: list[str] = []
    for match in _ENDPOINT_TOKEN_RE.finditer(text or ""):
        candidate = _validate_endpoint_selector(match.group(1))
        if candidate and candidate not in found:
            found.append(candidate)
        if len(found) >= _MAX_SELECTORS:
            break
    return found


def extract_task_selectors(text: str) -> list[str]:
    """Bounded ``task:<id>`` selectors named in ``text`` (stable order)."""
    found: list[str] = []
    for match in _TASK_SELECTOR_RE.finditer(text or ""):
        token = match.group(1).strip().rstrip(".,;:!?)]}\"'")
        if token and len(token) <= 128 and token not in found:
            found.append(token)
        if len(found) >= _MAX_SELECTORS:
            break
    return found


def extract_subjects(text: str) -> list[str]:
    """Explanation subjects named in ``text`` (stable vocabulary order)."""
    lowered = (text or "").lower()
    found: list[str] = []
    for subject, synonyms in _SUBJECT_SYNONYMS.items():
        if any(synonym in lowered for synonym in synonyms):
            found.append(subject)
    return found


def _first_match(keywords: tuple[str, ...], lowered: str) -> str | None:
    for keyword in keywords:
        if keyword in lowered:
            return keyword
    return None


def _action_family(keyword: str) -> str:
    if keyword in _PAUSE_KEYWORDS:
        return "pause"
    if keyword in _RESUME_KEYWORDS:
        return "resume"
    if keyword in _STOP_KEYWORDS:
        return "stop"
    if keyword in _EXCLUDE_KEYWORDS:
        return "exclude"
    if keyword in _REPRIORITIZE_KEYWORDS:
        return "reprioritize"
    if keyword in _FOCUS_KEYWORDS:
        return "focus"
    return "query"


def extract_areas(text: str) -> list[str]:
    """Canonical security areas named in ``text`` (stable vocabulary order)."""
    lowered = text.lower()
    found: list[str] = []
    for area, synonyms in _AREA_SYNONYMS.items():
        if any(synonym in lowered for synonym in synonyms):
            found.append(area)
    return found


def classify_instruction(instruction: str) -> IntentClassification:
    """Deterministically classify one instruction, or reject it."""
    redacted = redact_text(instruction).strip()
    if not redacted:
        raise IntentInterpretationError("empty operator instruction")
    lowered = redacted.lower()

    matched: list[str] = []
    action_keyword: str | None = None

    # Order matters. A question ("explain why you stopped") is read before
    # any verb, and an explicit control verb ("계속 진행해" / "resume") is
    # read before a bare status noun ("진행"/"progress") so a resume is not
    # mistaken for a status query.
    explanation = _first_match(_EXPLANATION_KEYWORDS, lowered)
    if explanation is not None:
        intent_type = OperatorIntentType.EXPLANATION_QUERY
        matched.append(explanation)
    else:
        for keywords, candidate in (
            (_PAUSE_KEYWORDS, OperatorIntentType.PAUSE),
            (_RESUME_KEYWORDS, OperatorIntentType.RESUME),
            (_STOP_KEYWORDS, OperatorIntentType.STOP),
            (_EXCLUDE_KEYWORDS, OperatorIntentType.EXCLUDE),
            (_REPRIORITIZE_KEYWORDS, OperatorIntentType.REPRIORITIZE),
            (_FOCUS_KEYWORDS, OperatorIntentType.FOCUS),
        ):
            action_keyword = _first_match(keywords, lowered)
            if action_keyword is not None:
                intent_type = candidate
                matched.append(action_keyword)
                break
        else:
            status = _first_match(_STATUS_KEYWORDS, lowered)
            if status is not None:
                intent_type = OperatorIntentType.STATUS_QUERY
                matched.append(status)
            else:
                raise IntentInterpretationError(f"unrecognized operator instruction: {redacted!r}")

    endpoint_selectors = extract_endpoint_selectors(redacted)
    task_selectors = extract_task_selectors(redacted)
    subjects = extract_subjects(redacted)
    # Areas are read from the instruction with exact selectors removed:
    # ``/api/docs`` must not be widened to the whole ``api`` area, or an
    # "exclude this endpoint" would suppress unrelated api work.
    selector_free = redacted
    for selector in (*endpoint_selectors, *task_selectors):
        selector_free = selector_free.replace(selector, " ")
    areas = extract_areas(selector_free)
    matched.extend(areas)
    matched.extend(endpoint_selectors)
    matched.extend(task_selectors)

    # More than one action family firing ("focus web but exclude mobile") is
    # still deterministic — the precedence table decides — but it is worth
    # surfacing as lower confidence.
    family_hits = {
        family
        for family, keywords in (
            ("pause", _PAUSE_KEYWORDS),
            ("resume", _RESUME_KEYWORDS),
            ("stop", _STOP_KEYWORDS),
            ("exclude", _EXCLUDE_KEYWORDS),
            ("reprioritize", _REPRIORITIZE_KEYWORDS),
            ("focus", _FOCUS_KEYWORDS),
        )
        if _first_match(keywords, lowered) is not None
    }
    ambiguous_action = len(family_hits) > 1 and action_keyword is not None
    confidence = 0.5 if ambiguous_action else 1.0
    note = None
    if ambiguous_action:
        note = f"multiple action keywords: {sorted(family_hits)}"

    return IntentClassification(
        intent_type=intent_type,
        redacted_instruction=redacted,
        areas=areas,
        endpoint_selectors=endpoint_selectors,
        task_selectors=task_selectors,
        subjects=subjects,
        matched_keywords=list(dict.fromkeys(matched)),
        confidence=confidence,
        note=note,
    )


def _string_list(value: object) -> list[str]:
    """Coerce an existing parameter value to a list of strings."""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def interpret_instruction(
    instruction: str,
    *,
    mission_id: MissionId,
    now: datetime,
    task_ids: list[TaskId] | None = None,
    parameters: dict[str, object] | None = None,
    source: str = "operator",
) -> OperatorIntent:
    """Build a validated, secret-free ``OperatorIntent`` from free text.

    Raises ``IntentInterpretationError`` when the text is empty or maps to
    no known instruction, and ``pydantic.ValidationError`` when a caller
    supplies an executable parameter (``tool_id``, ...) — both fail closed.
    """
    classification = classify_instruction(instruction)
    params: dict[str, object] = secret_parameters(parameters)  # type: ignore[arg-type]

    if classification.areas and classification.intent_type not in _QUERY_TYPES:
        key = (
            "exclude_areas"
            if classification.intent_type is OperatorIntentType.EXCLUDE
            else "focus_areas"
        )
        existing = _string_list(params.get(key))
        params[key] = list(dict.fromkeys([*existing, *classification.areas]))

    # Exact selectors are recorded for *every* intent type, including query
    # types: ``explain`` reads the latest explanation/status intent's
    # selectors so "why /api/docs?" can answer about that endpoint without a
    # separate call-site change. They are references, never new scope.
    for key, values in (
        ("endpoint_selectors", classification.endpoint_selectors),
        ("task_selectors", classification.task_selectors),
        ("subjects", classification.subjects),
    ):
        if values:
            existing = _string_list(params.get(key))
            params[key] = list(dict.fromkeys([*existing, *[str(item) for item in values]]))

    if (
        classification.intent_type is OperatorIntentType.REPRIORITIZE
        and "priority" not in params
        and "priorities" not in params
    ):
        # A path like ``/api/v2`` must not be mistaken for a priority number.
        number_source = classification.redacted_instruction
        for selector in classification.endpoint_selectors:
            number_source = number_source.replace(selector, " ")
        number = _NUMBER_RE.search(number_source)
        if number is not None:
            params["priority"] = int(number.group(1))

    return OperatorIntent(
        intent_id=OperatorIntentId(new_id("intent")),
        mission_id=mission_id,
        intent_type=classification.intent_type,
        instruction=classification.redacted_instruction,
        task_ids=sorted(set(task_ids or []), key=str),
        parameters=params,
        provenance=Provenance(source=source),
        created_at=now,
    )
