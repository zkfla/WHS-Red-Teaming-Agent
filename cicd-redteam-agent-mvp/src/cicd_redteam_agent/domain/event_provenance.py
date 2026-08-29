from __future__ import annotations

import json

from cicd_redteam_agent.domain.investigation import WorkflowRunEventLinkageEvidence
from cicd_redteam_agent.domain.models import EventPrincipalRole


def principal_role_for_reference(reference: str, event_name: str) -> EventPrincipalRole | None:
    name = reference.casefold()
    event = event_name.casefold()
    if name.startswith("github.event.sender."):
        return EventPrincipalRole.EVENT_SENDER
    if name.startswith("github.event.comment."):
        if event == "pull_request_review_comment":
            return EventPrincipalRole.REVIEW_COMMENT_AUTHOR
        return EventPrincipalRole.COMMENT_AUTHOR
    if name.startswith("github.event.issue."):
        return EventPrincipalRole.ISSUE_AUTHOR
    if name.startswith("github.event.pull_request."):
        return EventPrincipalRole.PULL_REQUEST_AUTHOR
    if name.startswith("github.event.review."):
        return EventPrincipalRole.REVIEW_AUTHOR
    if name in {"github.event.label.name", "github.event.label.color"}:
        return EventPrincipalRole.LABEL_ACTOR
    return None


def _principal_locator(
    role: EventPrincipalRole,
    linkage: WorkflowRunEventLinkageEvidence,
) -> dict[str, object] | None:
    base: dict[str, object] = {"role": role.value, "run_id": linkage.run_id}
    if role in {EventPrincipalRole.EVENT_SENDER, EventPrincipalRole.LABEL_ACTOR}:
        if linkage.sender_login is None and linkage.sender_id is None:
            return None
        if (
            role is EventPrincipalRole.LABEL_ACTOR
            and linkage.action not in {"labeled", "unlabeled"}
        ):
            return None
        return base
    if role is EventPrincipalRole.COMMENT_AUTHOR:
        if linkage.comment_id is None:
            return None
        return {**base, "comment_id": linkage.comment_id}
    if role is EventPrincipalRole.ISSUE_AUTHOR:
        if linkage.issue_number is None:
            return None
        return {**base, "issue_number": linkage.issue_number}
    if role is EventPrincipalRole.PULL_REQUEST_AUTHOR:
        if linkage.pull_number is None:
            return None
        return {**base, "pull_number": linkage.pull_number}
    if role is EventPrincipalRole.REVIEW_AUTHOR:
        if linkage.pull_number is None or linkage.review_id is None:
            return None
        return {
            **base,
            "pull_number": linkage.pull_number,
            "review_id": linkage.review_id,
        }
    if role is EventPrincipalRole.REVIEW_COMMENT_AUTHOR:
        if linkage.review_comment_id is None:
            return None
        return {**base, "review_comment_id": linkage.review_comment_id}
    return None


def event_principal_qualifier(
    role: EventPrincipalRole,
    linkage: WorkflowRunEventLinkageEvidence,
) -> str | None:
    locator = _principal_locator(role, linkage)
    if locator is None:
        return None
    return json.dumps(locator, sort_keys=True, separators=(",", ":"))


def parse_event_principal_qualifier(value: str | None) -> dict[str, object]:
    if not value:
        raise ValueError("event principal requirement has no locator")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("event principal requirement locator is invalid") from exc
    if not isinstance(decoded, dict):
        raise ValueError("event principal requirement locator must be an object")
    return decoded


def event_value_from_linkage(
    reference: str,
    linkage: WorkflowRunEventLinkageEvidence | None,
) -> str | int | None:
    if linkage is None:
        return None
    name = reference.casefold()
    if name == "github.event.action":
        return linkage.action
    if name == "github.event.label.name":
        return linkage.label_name
    if name == "github.event.issue.number":
        return linkage.issue_number
    if name == "github.event.pull_request.number":
        return linkage.pull_number
    return None
