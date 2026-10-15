"""Non-secret session registrations and evidence-backed HTTP comparisons."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.target import UrlTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.ids import EvidenceId
from redteam.core.ports.evidence_store import EvidenceStore


class AuthSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_ref: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    paths: list[str] = Field(min_length=1)
    expected_access: Literal["allow", "deny", "unspecified"] = "unspecified"

    @field_validator("paths")
    @classmethod
    def literal_paths(cls, values: list[str]) -> list[str]:
        if any(
            not path.startswith("/")
            or any(c in path for c in "?#%\\\r\n*")
            or "//" in path
            or any(part in {".", ".."} for part in path.split("/"))
            for path in values
        ):
            raise ValueError("auth session paths must be exact literal paths")
        return sorted(set(values))


class AuthenticationCheck(BaseModel):
    baseline_evidence_id: EvidenceId
    session_ref: str
    expected_access: Literal["allow", "deny", "unspecified"] = "unspecified"


def request_key(method: str, path: str, session_ref: str | None) -> str:
    encoded = json.dumps([method, path, session_ref], separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def _safe_response_metadata(output: dict[str, Any]) -> dict[str, Any]:
    """Copy only already-normalized response metadata into an observation.

    The current web capability intentionally does not expose response headers.
    This seam accepts safe metadata if a compatible ToolResult producer supplies
    it later, without ever copying raw headers, cookie values, or credentials.
    """

    metadata: dict[str, Any] = {}
    redirect = output.get("redirect_metadata")
    if isinstance(redirect, dict) and redirect.get("detected") is True:
        location = redirect.get("location_path")
        if (
            isinstance(location, str)
            and location.startswith("/")
            and len(location) <= 512
            and not any(ord(char) < 32 for char in location)
            and "\\" not in location
        ):
            metadata["redirect_metadata"] = {
                "detected": True,
                "location_path": location,
                "query_omitted": redirect.get("query_omitted") is True,
                "fragment_omitted": redirect.get("fragment_omitted") is True,
            }
        else:
            metadata["redirect_metadata"] = {"detected": True}

    challenge = output.get("auth_challenge")
    if isinstance(challenge, dict) and challenge.get("present") is True:
        scheme = challenge.get("scheme")
        metadata["auth_challenge"] = {
            "present": True,
            "scheme": scheme if isinstance(scheme, str) and len(scheme) <= 32 else "unknown",
            "realm_present": challenge.get("realm_present") is True,
        }

    cookies = output.get("cookie_metadata")
    if isinstance(cookies, dict) and cookies.get("present") is True:
        safe_cookies: list[dict[str, Any]] = []
        raw_cookies = cookies.get("cookies", [])
        if not isinstance(raw_cookies, list):
            raw_cookies = []
        for item in raw_cookies[:50]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            safe_cookies.append(
                {
                    "name": name if isinstance(name, str) and len(name) <= 128 else "unknown",
                    "secure": item.get("secure") is True,
                    "http_only": item.get("http_only") is True,
                    "same_site": item.get("same_site")
                    if item.get("same_site") in {"strict", "lax", "none", "unspecified", "invalid"}
                    else "unspecified",
                    "domain_present": item.get("domain_present") is True,
                    "path_present": item.get("path_present") is True,
                    "persistent": item.get("persistent") is True,
                }
            )
        metadata["cookie_metadata"] = {
            "present": True,
            "cookies": safe_cookies,
            "truncated": cookies.get("truncated") is True,
            "values_omitted": True,
        }
    return metadata


def http_observation(arguments: dict[str, Any], result: ToolResult) -> dict[str, Any] | None:
    """Project HTTP data for its registered mapper or explicit auth assessment.

    Physical ToolId spelling is not an HTTP contract. Generic evidence
    consumers obtain this projection from the selected mapper instead.
    """
    if not result.success:
        return None
    output = result.output
    if not isinstance(output, dict):
        return None
    status = output.get("status_code")
    path, method = arguments.get("path"), arguments.get("method", "GET")
    target_ref, session_ref = arguments.get("target_ref"), arguments.get("session_ref")
    if (
        type(status) is not int
        or not 100 <= status <= 599
        or not isinstance(path, str)
        or not path.startswith("/")
        or not isinstance(method, str)
        or method not in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
        or not isinstance(target_ref, str)
        or not target_ref
        or (session_ref is not None and not isinstance(session_ref, str))
    ):
        return None
    digest = output.get("body_sha256")
    return {
        "kind": "http_response",
        "context_key": request_key(method, path, session_ref),
        "method": method,
        "path": path,
        "target_ref": target_ref,
        "session_ref": session_ref,
        "status_code": status,
        # Both come straight out of CapturedResponse.summary; recording them
        # lets a later comparison ask "same body?" without the body ever
        # leaving the Capability Layer.
        "redirect": output.get("redirect") is True,
        "body_sha256": digest if isinstance(digest, str) else None,
        **({"actual_session_ref": output["actual_session_ref"]}
           if "actual_session_ref" in output else {}),
        **_safe_response_metadata(output),
    }


#: Confirmed Finding: an operator-declared deny path the session still reached.
ACCESS_CONTROL_VIOLATION = "access_control_violation"


def comparison_outcome(status: int | None, expected_access: str) -> str:
    if status is None:
        return "request_failed_or_uninterpretable"
    if 200 <= status < 300:
        return (
            ACCESS_CONTROL_VIOLATION
            if expected_access == "deny"
            else "session_access_observed"
        )
    if status == 401:
        return "session_not_accepted"
    if status == 403:
        return "access_denied"
    return "inconclusive_response"


#: Fixed codes naming the first baseline or request condition that failed.
#: They never carry provided values, prompts, or credential material.
BaselineMismatchReason = Literal[
    "baseline_missing",
    "baseline_scope_mismatch",
    "baseline_source_mismatch",
    "baseline_not_successful",
    "request_method_mismatch",
    "request_session_mismatch",
    "target_mismatch",
    "baseline_observation_missing",
    "baseline_status_mismatch",
    "baseline_session_mismatch",
    "baseline_method_mismatch",
    "baseline_path_mismatch",
    "baseline_target_ref_mismatch",
]


class AuthenticationAssessment(BaseModel):
    outcome: str
    hypothesis_status: str
    evidence_ids: list[str]
    anonymous_status: int | None = None
    session_status: int | None = None
    mismatch_reason: BaselineMismatchReason | None = None


def _same_executed_target(baseline: EvidenceRecord, evidence: EvidenceRecord) -> bool:
    previous = baseline.metadata.get("target_node_id")
    if previous and previous == evidence.metadata.get("target_node_id"):
        return True
    # A host observation can later acquire an endpoint graph identity.
    # Compare the preserved execution addresses, never just a graph label
    # or an untrusted target_ref string, when the identities differ.
    from redteam.orchestration.target_binding import network_identity

    try:
        before = ActionProposal.model_validate(baseline.metadata.get("proposal"))
        after = ActionProposal.model_validate(evidence.metadata.get("proposal"))
        if before.target is None or after.target is None:
            return False
        if isinstance(before.target, UrlTarget) and isinstance(after.target, UrlTarget):
            if urlsplit(before.target.url).scheme != urlsplit(after.target.url).scheme:
                return False
        identity = network_identity(before.target)
        return identity is not None and identity == network_identity(after.target)
    except (ValueError, TypeError):
        return False


def _baseline_mismatch_reason(
    check: AuthenticationCheck,
    proposal: ActionProposal,
    evidence: EvidenceRecord,
    baseline: EvidenceRecord | None,
    previous: Any,
) -> BaselineMismatchReason | None:
    """Return the first failed baseline or request condition, or None when valid."""
    if baseline is None:
        return "baseline_missing"
    if baseline.run_id != evidence.run_id or baseline.engagement_id != evidence.engagement_id:
        return "baseline_scope_mismatch"
    if baseline.source_id != "web.http_request":
        return "baseline_source_mismatch"
    if baseline.metadata.get("success") is not True:
        return "baseline_not_successful"
    if proposal.arguments.get("method", "GET") != "GET":
        return "request_method_mismatch"
    if proposal.arguments.get("session_ref") != check.session_ref:
        return "request_session_mismatch"
    if not _same_executed_target(baseline, evidence):
        return "target_mismatch"
    if not isinstance(previous, dict):
        return "baseline_observation_missing"
    if previous.get("status_code") != 401:
        return "baseline_status_mismatch"
    if previous.get("session_ref") is not None:
        return "baseline_session_mismatch"
    if previous.get("method") != "GET":
        return "baseline_method_mismatch"
    if previous.get("path") != proposal.arguments.get("path"):
        return "baseline_path_mismatch"
    if previous.get("target_ref") != proposal.arguments.get("target_ref"):
        return "baseline_target_ref_mismatch"
    return None


def _assess_observations(
    check: AuthenticationCheck,
    proposal: ActionProposal,
    evidence: EvidenceRecord,
    baseline: EvidenceRecord | None,
    current: dict[str, Any] | None,
) -> AuthenticationAssessment:
    previous = baseline.metadata.get("http_observation") if baseline else None
    reason = _baseline_mismatch_reason(check, proposal, evidence, baseline, previous)
    refs = [str(evidence.evidence_id)]
    if reason is not None:
        return AuthenticationAssessment(
            outcome="baseline_evidence_unavailable_or_mismatched",
            hypothesis_status="inconclusive",
            evidence_ids=refs,
            mismatch_reason=reason,
        )
    refs.insert(0, str(check.baseline_evidence_id))
    status = current["status_code"] if current else None
    outcome = comparison_outcome(status, check.expected_access)
    hypothesis_status = (
        "confirmed"
        if outcome in {"session_access_observed", ACCESS_CONTROL_VIOLATION}
        else "rejected"
        if outcome in {"session_not_accepted", "access_denied"}
        else "inconclusive"
    )
    return AuthenticationAssessment(
        outcome=outcome,
        hypothesis_status=hypothesis_status,
        evidence_ids=refs,
        anonymous_status=401,
        session_status=status,
    )


async def assess_authentication(
    check: AuthenticationCheck,
    proposal: ActionProposal,
    result: ToolResult,
    evidence: EvidenceRecord,
    store: EvidenceStore,
) -> AuthenticationAssessment:
    """Compare observations; HTTP status alone cannot verify a vulnerability."""
    return _assess_observations(
        check, proposal, evidence, await store.get(check.baseline_evidence_id),
        http_observation(proposal.arguments, result),
    )


async def assess_preserved_authentication(
    check: AuthenticationCheck, evidence: EvidenceRecord, store: EvidenceStore,
) -> AuthenticationAssessment:
    """Reuse comparison meaning for an already preserved request and result."""
    from redteam.orchestration.evidence_access import raw_from_metadata

    proposal = ActionProposal.model_validate(evidence.metadata.get("proposal"))
    result = ToolResult.model_validate(raw_from_metadata(evidence.metadata))
    baseline = await store.get(check.baseline_evidence_id)
    if baseline is not None:
        before = ActionProposal.model_validate(baseline.metadata.get("proposal"))
        before_result = ToolResult.model_validate(raw_from_metadata(baseline.metadata))
        # Stored comparisons consume actual preserved inputs/results on both
        # sides. A derived HTTP summary is not itself the authority for status.
        baseline = baseline.model_copy(update={"metadata": {
            **baseline.metadata,
            "http_observation": http_observation(before.arguments, before_result),
        }})
    return _assess_observations(
        check, proposal, evidence, baseline, http_observation(proposal.arguments, result),
    )
