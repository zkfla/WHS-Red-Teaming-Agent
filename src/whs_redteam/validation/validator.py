"""Promote observations only after reproduction and negative controls."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from uuid import uuid4

import httpx

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    Finding,
    FindingStatus,
    Observation,
    ObservationVerdict,
    Severity,
    TestCategory,
    TestPlan,
    ValidationSummary,
)
from whs_redteam.planning import PlanPolicy
from whs_redteam.policy.budget import PersistentRequestBudget, RequestBudget
from whs_redteam.policy.dns import Resolver, system_resolver
from whs_redteam.policy.scope import ScopeGuard
from whs_redteam.tests_catalog.checks import (
    actor_response_comparison,
    cookie_attributes,
    cors_configuration,
    information_exposure,
    security_headers,
    transport_security,
)
from whs_redteam.tests_catalog.runner import AssessmentRunner
from whs_redteam.tools.http_client import SafeHttpResponse
from whs_redteam.tools.network_gate import NetworkGate

SEVERITY_ORDER = {
    Severity.INFORMATIONAL: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
}


def _response(
    *,
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"ok",
) -> SafeHttpResponse:
    return SafeHttpResponse(
        url="https://negative-control.example.test/",
        status_code=200,
        headers=httpx.Headers(headers or []),
        body=body,
        elapsed_ms=0,
        request_fingerprint="negative-control-request",
        response_fingerprint="negative-control-response",
    )


def negative_control_passes(category: str) -> bool:
    """Verify each analyzer can recognize a known-safe synthetic response."""

    try:
        test_category = TestCategory(category)
        if test_category is TestCategory.SECURITY_HEADERS:
            result = security_headers(
                _response(
                    headers=[
                        ("content-type", "text/html"),
                        ("content-security-policy", "default-src 'self'; frame-ancestors 'none'"),
                        ("x-content-type-options", "nosniff"),
                        ("referrer-policy", "no-referrer"),
                        ("strict-transport-security", "max-age=31536000"),
                    ]
                )
            )
        elif test_category is TestCategory.COOKIE_ATTRIBUTES:
            result = cookie_attributes(
                _response(
                    headers=[
                        ("set-cookie", "session=control; Secure; HttpOnly; SameSite=Lax")
                    ]
                )
            )
        elif test_category is TestCategory.CORS_CONFIGURATION:
            result = cors_configuration(_response(), "https://cors-probe.invalid")
        elif test_category is TestCategory.INFORMATION_EXPOSURE:
            result = information_exposure(_response(headers=[("content-type", "text/plain")]))
        elif test_category is TestCategory.TRANSPORT_SECURITY:
            result = transport_security("https://negative-control.example.test")
        elif test_category is TestCategory.ACTOR_RESPONSE_COMPARISON:
            baseline = _response()
            result = actor_response_comparison(baseline, baseline, "control")
        else:
            return False
        return result.verdict is not ObservationVerdict.FAIL
    except Exception:
        return False


class FindingValidator:
    def __init__(
        self,
        config: EngagementConfig,
        inventory: InventoryStore,
        scope_guard: ScopeGuard,
        audit_logger: AuditLogger,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = system_resolver,
        budget: RequestBudget | PersistentRequestBudget | None = None,
    ) -> None:
        self.config = config
        self.inventory = inventory
        self.scope_guard = scope_guard
        self.audit_logger = audit_logger
        self.transport = transport
        self.resolver = resolver
        self.budget = budget

    async def validate(self, plan: TestPlan) -> tuple[ValidationSummary, list[Finding]]:
        approved = PlanPolicy(self.scope_guard).approve(plan)
        categories = PlanPolicy.categories_by_target(approved)
        targets = list(categories)
        validation_id = uuid4().hex
        budget = self.budget or RequestBudget.from_config(
            self.config, self.audit_logger
        )
        gate = NetworkGate(self.scope_guard, budget, self.audit_logger)
        observations_by_key: dict[tuple[str, str], list[Observation]] = defaultdict(list)

        for repetition in range(self.config.validation_repetitions):
            run_id = f"{validation_id}-r{repetition + 1}"
            runner = AssessmentRunner(
                self.config,
                self.inventory,
                gate,
                self.audit_logger,
                transport=self.transport,
                resolver=self.resolver,
                run_id=run_id,
                selected_categories=categories,
            )
            await runner.run(targets)
            for raw in self.inventory.list_observations(run_id):
                observation = Observation.model_validate(raw)
                observations_by_key[(observation.test_id, observation.target_url)].append(
                    observation
                )

        findings = self._promote(observations_by_key)
        for finding in findings:
            self.inventory.save_finding(validation_id, finding)
        summary = ValidationSummary(
            validation_id=validation_id,
            plan_id=approved.plan_id,
            targets=len(targets),
            planned_tests=len(approved.items),
            confirmed=sum(item.status is FindingStatus.CONFIRMED for item in findings),
            inconclusive=sum(item.status is FindingStatus.INCONCLUSIVE for item in findings),
        )
        self.audit_logger.emit("validation_completed", **summary.model_dump())
        return summary, findings

    def _promote(
        self,
        grouped: dict[tuple[str, str], list[Observation]],
    ) -> list[Finding]:
        findings: list[Finding] = []
        for (test_id, target_url), observations in grouped.items():
            failures = [
                item for item in observations if item.verdict is ObservationVerdict.FAIL
            ]
            if not failures:
                continue
            candidate = failures[0]
            negative_passed = negative_control_passes(candidate.category)
            reproduced = len(failures) == self.config.validation_repetitions
            status = (
                FindingStatus.CONFIRMED
                if reproduced and negative_passed
                else FindingStatus.INCONCLUSIVE
            )
            dedup_material = f"{candidate.category}|{test_id}|{target_url}".encode()
            dedup_key = hashlib.sha256(dedup_material).hexdigest()
            severity = max(
                (item.severity for item in failures),
                key=lambda item: SEVERITY_ORDER[item],
            )
            findings.append(
                Finding(
                    finding_id=f"F-{dedup_key[:16]}",
                    dedup_key=dedup_key,
                    category=candidate.category,
                    test_id=test_id,
                    target_url=target_url,
                    status=status,
                    severity=severity,
                    title=candidate.title,
                    summary=candidate.summary,
                    remediation=candidate.remediation,
                    reproduction_count=len(failures),
                    required_reproductions=self.config.validation_repetitions,
                    negative_control_passed=negative_passed,
                    experiment_ids=tuple(
                        dict.fromkeys(
                            item.experiment_id for item in failures if item.experiment_id
                        )
                    ),
                    evidence_hashes=tuple(
                        dict.fromkeys(
                            item.evidence_hash for item in failures if item.evidence_hash
                        )
                    ),
                    first_observed_at=min(item.observed_at for item in failures),
                )
            )
        return sorted(findings, key=lambda item: (item.target_url, item.test_id))
