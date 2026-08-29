from __future__ import annotations

import json
from pathlib import Path

import pytest

from cicd_redteam_agent.application.github_event import load_github_event_locator


def _event_payload() -> dict[str, object]:
    return {
        "action": "created",
        "repository": {"full_name": "example/repo"},
        "issue": {
            "number": 17,
            "pull_request": {"url": "https://api.github.test/pulls/17"},
        },
        "comment": {
            "id": 99,
            "user": {"login": "attacker", "id": 7},
            "body": "/deploy",
        },
        "sender": {"login": "attacker", "id": 7, "type": "User"},
    }


def test_github_event_file_normalizes_only_event_locators(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(_event_payload()), encoding="utf-8")

    locator = load_github_event_locator(event_file, repository_root=repository)

    assert locator.event_name == "issue_comment"
    assert locator.action == "created"
    assert locator.repository_full_name == "example/repo"
    assert locator.issue_number == 17
    assert locator.pull_number == 17
    assert locator.comment_id == 99
    assert locator.sender_login == "attacker"
    assert locator.sender_id == 7
    assert locator.source_hash
    serialized = locator.model_dump_json()
    assert "/deploy" not in serialized
    assert "body" not in serialized


def test_github_event_file_inside_analyzed_repository_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    event_file = repository / "event.json"
    event_file.write_text(json.dumps(_event_payload()), encoding="utf-8")

    with pytest.raises(ValueError, match="trusted operator input"):
        load_github_event_locator(event_file, repository_root=repository)


def test_unsupported_event_shape_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps({"action": "created"}), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported GitHub event payload shape"):
        load_github_event_locator(event_file, repository_root=repository)
