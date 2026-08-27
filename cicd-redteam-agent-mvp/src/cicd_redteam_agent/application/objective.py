from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

MAX_OBJECTIVE_CHARACTERS = 16_000
OBJECTIVE_COMPILER_VERSION = "1.0"


@dataclass(frozen=True)
class CompiledObjectiveGoal:
    compiler_version: str = OBJECTIVE_COMPILER_VERSION
    source_hints: tuple[str, ...] = ()
    mechanism_hints: tuple[str, ...] = ()
    sink_hints: tuple[str, ...] = ()
    investigation_preferences: tuple[str, ...] = ()

    @property
    def focused(self) -> bool:
        return bool(self.source_hints or self.mechanism_hints or self.sink_hints)

    def as_dict(self) -> dict[str, object]:
        return {
            "compiler_version": self.compiler_version,
            "source_hints": list(self.source_hints),
            "mechanism_hints": list(self.mechanism_hints),
            "sink_hints": list(self.sink_hints),
            "investigation_preferences": list(self.investigation_preferences),
            "focused": self.focused,
        }


_SOURCE_ALIASES = {
    "fork_pr": ("fork pr", "fork-pr", "forked pr", "포크 pr", "포크된 pr"),
    "pull_request": ("pull request", "pull_request", "pr에서", "pr 공격"),
    "repository_writer": ("repo writer", "repository writer", "저장소 writer", "저장소 쓰기 권한"),
    "workflow_dispatch": ("workflow_dispatch", "workflow dispatch", "수동 실행"),
}

_MECHANISM_ALIASES = {
    "artifact": ("artifact", "아티팩트"),
    "cache": ("cache", "캐시"),
    "local_action": ("local action", "로컬 액션"),
    "reusable_workflow": ("reusable workflow", "재사용 workflow", "재사용 워크플로"),
    "secret": ("secret", "시크릿", "비밀값"),
    "oidc": ("oidc", "id-token", "id token"),
    "self_hosted_runner": (
        "self-hosted runner",
        "self hosted runner",
        "셀프호스티드",
        "자체 runner",
    ),
    "mutable_dependency": (
        "mutable action",
        "mutable dependency",
        "unpinned action",
        "외부 action",
        "외부 의존성",
    ),
    "shell_execution": ("shell injection", "command injection", "명령 주입", "쉘 인젝션", "shell"),
    "permissions": ("permission", "permissions", "권한"),
}

_SINK_ALIASES = {
    "github_token_write": (
        "write token",
        "github token",
        "github_token",
        "contents:write",
        "쓰기 token",
    ),
    "oidc_token": ("oidc token", "id-token", "id token", "oidc"),
    "cloud_identity": (
        "cloud identity",
        "클라우드 identity",
        "클라우드 아이덴티티",
        "role assumption",
        "assume role",
    ),
    "secret_exposure": (
        "secret leak",
        "secret exposure",
        "시크릿 유출",
        "secret 유출",
        "비밀 유출",
    ),
    "privileged_execution": (
        "privileged workflow",
        "privileged execution",
        "특권 workflow",
        "특권 워크플로",
        "특권 실행",
    ),
}

_PREFERENCE_ALIASES = {
    "preserve_unknown": ("unknown으로", "unknown 유지", "unknown을 유지", "preserve unknown"),
    "external_unknown_candidate": (
        "외부 설정이 필요하면",
        "외부 evidence",
        "외부 정보",
        "external unknown",
        "external evidence",
        "candidate로",
        "candidate로 남겨",
    ),
    "prioritize_requested_scope": ("집중 조사", "우선 조사", "위주로", "focus on", "prioritize"),
}

_GOAL_KEYWORDS = {
    "fork_pr": ("pull_request", "pull-request", "fork", "head_ref", "pr"),
    "pull_request": ("pull_request", "pull-request", "pr", "head_ref"),
    "repository_writer": ("repository", "repo", "writer", "write"),
    "workflow_dispatch": ("workflow_dispatch", "dispatch"),
    "artifact": ("artifact",),
    "cache": ("cache",),
    "local_action": ("local_action", "repository_file", "action.yml"),
    "reusable_workflow": ("reusable", "workflow_call"),
    "secret": ("secret",),
    "oidc": ("oidc", "id_token", "id-token"),
    "self_hosted_runner": ("runner", "self_hosted", "self-hosted"),
    "mutable_dependency": ("external", "mutable", "dependency", "container", "install"),
    "shell_execution": ("command", "shell", "run", "execute", "execution"),
    "permissions": ("permission", "write", "credential"),
    "github_token_write": ("github_token", "token", "contents_write", "write"),
    "oidc_token": ("oidc", "id_token", "id-token"),
    "cloud_identity": ("cloud", "identity", "role", "aws", "gcp", "azure"),
    "secret_exposure": ("secret", "output", "artifact", "external"),
    "privileged_execution": ("privileged", "credential", "write", "execution"),
}


def normalize_objective(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("analysis objective must not be empty")
    if len(normalized) > MAX_OBJECTIVE_CHARACTERS:
        raise ValueError(
            f"analysis objective exceeds {MAX_OBJECTIVE_CHARACTERS} characters"
        )
    return normalized


def objective_hash(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _matched_hints(text: str, aliases: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    lowered = text.casefold()
    return tuple(
        name
        for name, terms in aliases.items()
        if any(term.casefold() in lowered for term in terms)
    )


def compile_objective_goal(value: str | None) -> CompiledObjectiveGoal:
    if value is None:
        return CompiledObjectiveGoal()
    text = normalize_objective(value)
    return CompiledObjectiveGoal(
        source_hints=_matched_hints(text, _SOURCE_ALIASES),
        mechanism_hints=_matched_hints(text, _MECHANISM_ALIASES),
        sink_hints=_matched_hints(text, _SINK_ALIASES),
        investigation_preferences=_matched_hints(text, _PREFERENCE_ALIASES),
    )


def objective_relation_score(
    goal: CompiledObjectiveGoal,
    *,
    source_id: str,
    relation_type: str,
    target_id: str,
    category: str | None,
) -> int:
    """Score relation relevance without changing Fact or Validator semantics."""
    if not goal.focused:
        return 0
    haystack = " ".join((source_id, relation_type, target_id, category or "")).casefold()
    score = 0
    for hint in goal.source_hints:
        if any(keyword.casefold() in haystack for keyword in _GOAL_KEYWORDS.get(hint, ())):
            score += 6
    for hint in goal.mechanism_hints:
        if any(keyword.casefold() in haystack for keyword in _GOAL_KEYWORDS.get(hint, ())):
            score += 5
    for hint in goal.sink_hints:
        if any(keyword.casefold() in haystack for keyword in _GOAL_KEYWORDS.get(hint, ())):
            score += 7
    return score


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read_limited(stream: TextIO) -> str:
    # CRLF normalization can halve the character count, so permit up to 2x the
    # canonical limit before normalization and fail closed beyond that.
    value = stream.read(MAX_OBJECTIVE_CHARACTERS * 2 + 1)
    if len(value) > MAX_OBJECTIVE_CHARACTERS * 2:
        raise ValueError("analysis objective input is too large")
    return value


def load_analysis_objective(
    *,
    objective: str | None,
    objective_file: str | Path | None,
    repository_root: str | Path,
    stdin: TextIO,
) -> str | None:
    if objective is not None and objective_file is not None:
        raise ValueError("--objective and --objective-file are mutually exclusive")
    if objective is not None:
        return normalize_objective(objective)
    if objective_file is None:
        return None
    if str(objective_file) == "-":
        return normalize_objective(_read_limited(stdin))

    raw_path = Path(objective_file).expanduser()
    lexical_path = Path(os.path.abspath(raw_path))
    repository = Path(repository_root).expanduser().resolve(strict=False)
    try:
        resolved_path = raw_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"unable to read analysis objective file: {raw_path}") from exc
    if _is_within(lexical_path, repository) or _is_within(resolved_path, repository):
        raise ValueError(
            "analysis objective file must be trusted operator input outside the analyzed repository"
        )
    if not resolved_path.is_file():
        raise ValueError(f"analysis objective file is not a regular file: {raw_path}")
    try:
        with resolved_path.open("r", encoding="utf-8") as handle:
            text = _read_limited(handle)
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"unable to read UTF-8 analysis objective file: {raw_path}") from exc
    return normalize_objective(text)
