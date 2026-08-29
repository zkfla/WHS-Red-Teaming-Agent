from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cicd_redteam_agent.application.objective import normalize_objective
from cicd_redteam_agent.domain.models import AnalysisMode, AttackerProfile
from cicd_redteam_agent.framework.auth import ModelProvider

_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?(?=\s|[/?#)\],.]|$)",
    re.IGNORECASE,
)
_RUN_ID_RE = re.compile(r"(?:run(?:\s+id)?|실행\s*id)\s*[:=#]?\s*(\d{3,})", re.IGNORECASE)
_MODEL_RE = re.compile(
    r"(?P<model>(?:gpt-[A-Za-z0-9][A-Za-z0-9._-]*|o[0-9][A-Za-z0-9._-]*))"
    r"(?P<particle>으로|로)?(?=\s|[,.)\]]|$)",
    re.IGNORECASE,
)
_DEFAULT_MODEL_RE = re.compile(r"기본\s*모델(?:으로|로)?", re.IGNORECASE)
_QUOTED_PATH_RE = re.compile(r'["\'](?P<path>(?:[A-Za-z]:[\\/]|/)[^"\']+)["\']')


@dataclass(frozen=True)
class NaturalAnalysisRequest:
    target: str
    objective: str | None
    mode: AnalysisMode
    provider: ModelProvider
    attacker_profile: AttackerProfile
    model: str | None = None
    observe_github: bool = False
    observe_aws: bool = False
    github_run_id: int | None = None


def _contains_any(text: str, values: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(value.casefold() in lowered for value in values)


def _extract_model(text: str) -> tuple[str | None, str]:
    if _DEFAULT_MODEL_RE.search(text):
        cleaned = _DEFAULT_MODEL_RE.sub(" ", text, count=1)
        return None, " ".join(cleaned.split())
    match = _MODEL_RE.search(text)
    if match is None:
        return None, text
    cleaned = (text[: match.start()] + " " + text[match.end() :]).strip()
    return match.group("model"), " ".join(cleaned.split())


def _extract_target(text: str) -> tuple[str | None, str]:
    github_match = _GITHUB_URL_RE.search(text)
    if github_match:
        target = github_match.group(0).rstrip("/).,]")
        remainder = (text[: github_match.start()] + " " + text[github_match.end() :]).strip()
        return target, " ".join(remainder.split())

    quoted = _QUOTED_PATH_RE.search(text)
    if quoted:
        target = quoted.group("path")
        remainder = (text[: quoted.start()] + " " + text[quoted.end() :]).strip()
        return target, " ".join(remainder.split())

    stripped = text.strip()
    candidate = Path(stripped).expanduser()
    if candidate.exists():
        return stripped, ""

    parts = stripped.split()
    for end in range(len(parts) - 1, 0, -1):
        prefix = " ".join(parts[:end])
        prefix_path = Path(prefix).expanduser()
        if prefix_path.exists():
            return prefix, " ".join(parts[end:])
    return None, stripped


def parse_natural_request(
    text: str, *, explicit_target: str | None = None
) -> NaturalAnalysisRequest:
    normalized = " ".join(text.replace("\r", " ").replace("\n", " ").split()).strip()
    target, remainder = _extract_target(normalized)
    if explicit_target is not None:
        target = explicit_target
        remainder = normalized
    if not target:
        raise ValueError("분석 대상 저장소 경로 또는 GitHub URL을 찾지 못했습니다.")

    deterministic = _contains_any(
        normalized,
        (
            "ai 없이",
            "llm 없이",
            "정적 분석만",
            "기본 분석만",
            "deterministic",
            "모델 없이",
        ),
    )
    mode = AnalysisMode.DETERMINISTIC if deterministic else AnalysisMode.REASONING
    provider = ModelProvider.AUTO if deterministic else ModelProvider.CODEX_CHATGPT

    attacker_profile = (
        AttackerProfile.REPO_WRITER
        if _contains_any(
            normalized,
            (
                "repo writer",
                "repository writer",
                "repo-writer",
                "쓰기 권한 가진 공격자",
                "저장소 writer",
                "저장소 쓰기 권한",
            ),
        )
        else AttackerProfile.FORK_PR
    )

    observe_aws = _contains_any(
        normalized,
        (
            "aws iam",
            "aws oidc",
            "aws trust",
            "aws 신뢰",
            "aws 실제 설정",
        ),
    )
    observe_github = observe_aws or _contains_any(
        normalized,
        (
            "github api",
            "실제 github 설정",
            "github 실제 설정",
            "실제 repository 설정",
            "실제 저장소 설정",
            "실제 권한까지",
            "github 런타임",
            "실제 actions run",
        ),
    )
    run_match = _RUN_ID_RE.search(normalized)
    github_run_id = int(run_match.group(1)) if run_match else None
    if github_run_id is not None:
        observe_github = True

    model, objective_text = _extract_model(remainder.strip())
    if mode is AnalysisMode.DETERMINISTIC:
        model = None
    if not objective_text:
        objective_text = "이 저장소의 CI/CD 취약점과 공격 경로를 폭넓게 분석해줘."
    objective = None if mode is AnalysisMode.DETERMINISTIC else normalize_objective(objective_text)

    return NaturalAnalysisRequest(
        target=target,
        objective=objective,
        mode=mode,
        provider=provider,
        attacker_profile=attacker_profile,
        model=model,
        observe_github=observe_github,
        observe_aws=observe_aws,
        github_run_id=github_run_id,
    )


def is_github_repository_url(value: str) -> bool:
    match = _GITHUB_URL_RE.fullmatch(value.rstrip("/"))
    return match is not None


def materialize_target(target: str, *, cache_root: Path) -> Path:
    expanded = Path(target).expanduser()
    if expanded.exists():
        return expanded.resolve()
    if not is_github_repository_url(target):
        raise ValueError(f"분석 대상 저장소를 찾을 수 없습니다: {target}")

    match = _GITHUB_URL_RE.fullmatch(target.rstrip("/"))
    assert match is not None
    owner = match.group("owner")
    repository = match.group("repo")
    if repository.casefold().endswith(".git"):
        repository = repository[:-4]
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:8]
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = cache_root / f"{owner}-{repository}-{digest}-{timestamp}"
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--", target, str(destination)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise ValueError("GitHub URL 분석에는 git 실행 파일이 필요합니다.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ValueError(f"GitHub 저장소를 clone하지 못했습니다{suffix}") from exc
    return destination.resolve()
