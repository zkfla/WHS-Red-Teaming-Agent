from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from cicd_redteam_agent.domain.investigation import GitHubEventLocator

MAX_GITHUB_EVENT_BYTES = 1024 * 1024


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def infer_github_event_name(payload: dict[str, object]) -> str:
    if isinstance(payload.get("review"), dict) and isinstance(payload.get("pull_request"), dict):
        return "pull_request_review"
    if isinstance(payload.get("comment"), dict) and isinstance(payload.get("pull_request"), dict):
        return "pull_request_review_comment"
    if isinstance(payload.get("comment"), dict) and isinstance(payload.get("issue"), dict):
        return "issue_comment"
    if isinstance(payload.get("pull_request"), dict):
        return "pull_request"
    if isinstance(payload.get("issue"), dict):
        return "issues"
    raise ValueError("unsupported GitHub event payload shape for principal provenance")


def normalize_github_event_payload(
    payload: dict[str, object],
    *,
    source_hash: str,
) -> GitHubEventLocator:
    event_name = infer_github_event_name(payload)
    action = _text(payload.get("action"))
    repository = _mapping(payload.get("repository"))
    sender = _mapping(payload.get("sender"))
    issue = _mapping(payload.get("issue"))
    pull_request = _mapping(payload.get("pull_request"))
    comment = _mapping(payload.get("comment"))
    review = _mapping(payload.get("review"))
    label = _mapping(payload.get("label"))

    issue_number = _positive_int(issue.get("number"))
    pull_number = _positive_int(pull_request.get("number"))
    if (
        pull_number is None
        and issue_number is not None
        and isinstance(issue.get("pull_request"), dict)
    ):
        pull_number = issue_number

    raw_comment_id = _positive_int(comment.get("id"))
    comment_id = raw_comment_id if event_name == "issue_comment" else None
    review_comment_id = (
        raw_comment_id if event_name == "pull_request_review_comment" else None
    )

    return GitHubEventLocator(
        event_name=event_name,
        action=action,
        repository_full_name=_text(repository.get("full_name")),
        issue_number=issue_number,
        pull_number=pull_number,
        comment_id=comment_id,
        review_id=_positive_int(review.get("id")),
        review_comment_id=review_comment_id,
        label_name=_text(label.get("name")),
        sender_login=_text(sender.get("login")),
        sender_id=_positive_int(sender.get("id")),
        sender_type=_text(sender.get("type")),
        source_hash=source_hash,
    )


def load_github_event_locator(
    event_file: str | Path,
    *,
    repository_root: str | Path,
) -> GitHubEventLocator:
    raw_path = Path(event_file).expanduser()
    lexical_path = Path(os.path.abspath(raw_path))
    repository = Path(repository_root).expanduser().resolve(strict=False)
    try:
        resolved_path = raw_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"unable to read GitHub event file: {raw_path}") from exc
    if _is_within(lexical_path, repository) or _is_within(resolved_path, repository):
        raise ValueError(
            "GitHub event file must be trusted operator input outside the analyzed repository"
        )
    if not resolved_path.is_file():
        raise ValueError(f"GitHub event file is not a regular file: {raw_path}")
    try:
        raw = resolved_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unable to read GitHub event file: {raw_path}") from exc
    if len(raw) > MAX_GITHUB_EVENT_BYTES:
        raise ValueError("GitHub event file exceeds the 1 MiB limit")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("GitHub event file must contain one UTF-8 JSON object") from exc
    if not isinstance(decoded, dict):
        raise ValueError("GitHub event file must contain one JSON object")
    return normalize_github_event_payload(
        decoded,
        source_hash=hashlib.sha256(raw).hexdigest(),
    )
