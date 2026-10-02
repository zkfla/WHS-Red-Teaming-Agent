"""Synthetic Scope DENY cases shared by recording regression tests."""

from __future__ import annotations

from dataclasses import dataclass

from redteam.control.audit import AuditEntry
from redteam.control.severity import Severity, to_tag
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.target import AnyTarget, EndpointTarget, HostTarget, UrlTarget
from redteam.core.enums import PolicyVerdict


@dataclass(frozen=True)
class ScopeDenyCase:
    name: str
    scope: EngagementScope
    target: AnyTarget
    cause: str
    severity: Severity = Severity.BLOCK_ACTION


SCOPE_DENY_CASES = [
    ScopeDenyCase("port_mismatch", EngagementScope(allowed_hosts=["127.0.0.1:8080"]),
                  HostTarget(host="127.0.0.1:9090"), "not within"),
    ScopeDenyCase("bare_host_deny", EngagementScope(allowed_hosts=["127.0.0.1"],
                  denied_hosts=["127.0.0.1"]), HostTarget(host="127.0.0.1:8080"),
                  "explicitly denied"),
    ScopeDenyCase("allow_requires_port", EngagementScope(allowed_hosts=["127.0.0.1:8080"]),
                  HostTarget(host="127.0.0.1"), "port cannot be inferred"),
    ScopeDenyCase("malformed_request_port", EngagementScope(allowed_hosts=["127.0.0.1"]),
                  HostTarget(host="127.0.0.1:notaport"), "malformed host target"),
    ScopeDenyCase("endpoint_port_conflict", EngagementScope(allowed_hosts=["127.0.0.1"]),
                  EndpointTarget(host="127.0.0.1:8080", port=9090), "port conflict"),
    ScopeDenyCase("request_port_out_of_range", EngagementScope(allowed_hosts=["127.0.0.1"]),
                  HostTarget(host="127.0.0.1:99999"), "malformed host target"),
    ScopeDenyCase("empty_allow_port", EngagementScope(allowed_hosts=["127.0.0.1:"]),
                  HostTarget(host="127.0.0.1:8080"), "ignored malformed allow entries"),
    ScopeDenyCase("http_default_port_deny", EngagementScope(allowed_hosts=["scope.example"],
                  denied_hosts=["scope.example:80"]), UrlTarget(url="http://scope.example/"),
                  "explicitly denied"),
    ScopeDenyCase("deny_requires_unknown_port", EngagementScope(allowed_hosts=["127.0.0.1"],
                  denied_hosts=["127.0.0.1:8080"]), HostTarget(host="127.0.0.1"),
                  "carries no port"),
    ScopeDenyCase("malformed_denied_hosts", EngagementScope(allowed_hosts=["127.0.0.1"],
                  denied_hosts=["127.0.0.1:notaport"]), HostTarget(host="127.0.0.1"),
                  "malformed deny entries", Severity.ABORT_RUN),
    ScopeDenyCase("malformed_denied_domains", EngagementScope(allowed_hosts=["127.0.0.1"],
                  denied_domains=["scope.example:"]), HostTarget(host="127.0.0.1"),
                  "malformed deny entries", Severity.ABORT_RUN),
]

# One ordinary request failure and one fatal deny configuration failure.
PERSISTENCE_CASES = [SCOPE_DENY_CASES[0], SCOPE_DENY_CASES[9]]


def assert_scope_audit(
    proposal: ActionProposal, decision: PolicyDecision, audit: AuditEntry, case: ScopeDenyCase
) -> None:
    assert decision.verdict == PolicyVerdict.DENY
    assert any(case.cause in reason for reason in decision.reasons)
    assert "scope" in decision.matched_rules
    assert to_tag(case.severity) in decision.matched_rules
    assert proposal.action_id == decision.action_id == audit.action_id
    assert audit.run_id == proposal.run_id
    assert audit.engagement_id == proposal.engagement_id
    assert audit.decision == decision


def assert_scope_evidence(
    proposal: ActionProposal, decision: PolicyDecision, evidence: EvidenceRecord
) -> None:
    assert evidence.action_id == proposal.action_id == decision.action_id
    assert evidence.run_id == proposal.run_id
    assert evidence.engagement_id == proposal.engagement_id
    assert evidence.metadata["reasons"] == decision.reasons
    assert evidence.metadata["matched_rules"] == decision.matched_rules
    assert evidence.metadata["record_kind"] == "blocked_action"
    assert evidence.metadata["verdict"] == "deny"
    assert evidence.invocation_id is None
    assert evidence.artifact_ref is None
