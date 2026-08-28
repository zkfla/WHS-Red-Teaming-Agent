"""Orchestrate deterministic Day 3 checks over approved targets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from functools import partial
from uuid import uuid4

import httpx

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    AssessmentSummary,
    Observation,
    ObservationVerdict,
    Severity,
    TestCategory,
)
from whs_redteam.policy.dns import Resolver, resolve_and_pin_targets, system_resolver
from whs_redteam.sanitizer import sanitize_data, sanitize_url
from whs_redteam.tests_catalog.checks import (
    actor_response_comparison,
    cookie_attributes,
    cors_configuration,
    information_exposure,
    security_headers,
    transport_security,
)
from whs_redteam.tools.actor_sessions import ActorSessionPool
from whs_redteam.tools.http_client import SafeHttpClient, SafeHttpResponse
from whs_redteam.tools.network_gate import NetworkGate


class AssessmentRunner:
    CATALOG_VERSION = "day3-v2"

    def __init__(
        self,
        config: EngagementConfig,
        inventory: InventoryStore,
        network_gate: NetworkGate,
        audit_logger: AuditLogger,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = system_resolver,
        run_id: str | None = None,
        probe_origin: str = "https://cors-probe.invalid",
        selected_categories: dict[str, set[TestCategory]] | None = None,
    ) -> None:
        self.config = config
        self.inventory = inventory
        self.network_gate = network_gate
        self.audit_logger = audit_logger
        self.transport = transport
        self.resolver = resolver
        self.run_id = run_id or uuid4().hex
        self.probe_origin = probe_origin
        self.selected_categories = selected_categories
        config_material = json.dumps(
            config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        self.config_hash = hashlib.sha256(config_material).hexdigest()

    async def run(self, targets: Iterable[str]) -> AssessmentSummary:
        target_list = list(dict.fromkeys(targets))[: self.config.max_pages]
        initial_dns = resolve_and_pin_targets(self.config, self.resolver)
        observations: list[Observation] = []
        client = SafeHttpClient(
            self.network_gate,
            self.audit_logger,
            transport=self.transport,
        )
        self.actor_sessions = ActorSessionPool(
            self.config,
            self.network_gate,
            self.audit_logger,
            transport=self.transport,
        )
        for target in target_list:
            observations.extend(await self._assess_target(client, target))
            self.network_gate.raise_if_halted()

        final_dns = resolve_and_pin_targets(self.config, self.resolver)
        if initial_dns.addresses != final_dns.addresses:
            raise RuntimeError("approved target DNS changed during the assessment")

        for observation in observations:
            self.inventory.save_observation(self.run_id, observation)
        summary = self._summarize(target_list, observations)
        self.audit_logger.emit("assessment_completed", **summary.model_dump())
        return summary

    async def _assess_target(
        self,
        client: SafeHttpClient,
        target: str,
    ) -> list[Observation]:
        results: list[Observation] = []
        if self._enabled(target, TestCategory.TRANSPORT_SECURITY):
            results.append(self._finalize(transport_security(target)))
        http_categories = {
            TestCategory.SECURITY_HEADERS,
            TestCategory.COOKIE_ATTRIBUTES,
            TestCategory.CORS_CONFIGURATION,
            TestCategory.INFORMATION_EXPOSURE,
            TestCategory.ACTOR_RESPONSE_COMPARISON,
        }
        if not any(self._enabled(target, category) for category in http_categories):
            return results
        try:
            baseline = await client.get(target)
            cors_probe = (
                await client.get(target, headers={"Origin": self.probe_origin})
                if self._enabled(target, TestCategory.CORS_CONFIGURATION)
                else baseline
            )
        except (httpx.HTTPError, UnicodeError, ValueError) as exc:
            results.append(
                self._finalize(Observation(
                    test_id="WEB-FETCH-001",
                    category="http_fetch",
                    target_url=sanitize_url(target),
                    verdict=ObservationVerdict.INCONCLUSIVE,
                    severity=Severity.INFORMATIONAL,
                    title="HTTP assessment could not be completed",
                    summary="The target response could not be collected safely.",
                    evidence={"error": sanitize_data(f"{type(exc).__name__}: {exc}")},
                ))
            )
            return results
        preflight = None
        if (
            self._enabled(target, TestCategory.CORS_CONFIGURATION)
            and "OPTIONS" in self.config.allowed_methods
        ):
            try:
                preflight = await client.request(
                    "OPTIONS",
                    target,
                    headers={
                        "Origin": self.probe_origin,
                        "Access-Control-Request-Method": "GET",
                    },
                )
            except httpx.HTTPError as exc:
                self.audit_logger.emit(
                    "cors_preflight_inconclusive",
                    url=sanitize_url(target),
                    error=sanitize_data(f"{type(exc).__name__}: {exc}"),
                )
        checks: tuple[
            tuple[
                TestCategory,
                str,
                str,
                Callable[[], Observation],
                tuple[SafeHttpResponse, ...],
            ],
            ...,
        ] = (
            (
                TestCategory.SECURITY_HEADERS,
                "WEB-HEADERS-001",
                "security_headers",
                lambda: security_headers(baseline),
                (baseline,),
            ),
            (
                TestCategory.COOKIE_ATTRIBUTES,
                "WEB-COOKIE-001",
                "cookie_attributes",
                lambda: cookie_attributes(baseline),
                (baseline,),
            ),
            (
                TestCategory.CORS_CONFIGURATION,
                "WEB-CORS-001",
                "cors_configuration",
                lambda: cors_configuration(cors_probe, self.probe_origin, preflight),
                tuple(item for item in (cors_probe, preflight) if item is not None),
            ),
            (
                TestCategory.INFORMATION_EXPOSURE,
                "WEB-INFO-001",
                "information_exposure",
                lambda: information_exposure(baseline),
                (baseline,),
            ),
        )
        for test_category, test_id, category, check, responses in checks:
            if not self._enabled(target, test_category):
                continue
            results.append(
                self._safe_analyze(test_id, category, target, check, responses)
            )
        if not self._enabled(target, TestCategory.ACTOR_RESPONSE_COMPARISON):
            return results
        for actor in self.config.actors:
            state = next(
                item
                for item in self.actor_sessions.summary().actors
                if item.actor_id == actor.actor_id
            )
            if not state.credentials_available or state.credential_collision:
                results.append(
                    self._finalize(Observation(
                        test_id=f"WEB-ACTOR-DIFF-001:{actor.actor_id}",
                        category="actor_response_comparison",
                        target_url=sanitize_url(target),
                        verdict=ObservationVerdict.INCONCLUSIVE,
                        severity=Severity.INFORMATIONAL,
                        title=f"Actor {actor.actor_id} could not be compared",
                        summary=(
                            "The actor credential is unavailable or duplicates another "
                            "configured actor."
                        ),
                        evidence={
                            "actor_id": actor.actor_id,
                            "missing_environment": state.missing_environment,
                            "credential_collision": state.credential_collision,
                        },
                    ))
                )
                continue
            try:
                actor_response = await self.actor_sessions.client(actor.actor_id).get(
                    target
                )
            except httpx.HTTPError as exc:
                results.append(
                    self._finalize(Observation(
                        test_id=f"WEB-ACTOR-DIFF-001:{actor.actor_id}",
                        category="actor_response_comparison",
                        target_url=sanitize_url(target),
                        verdict=ObservationVerdict.INCONCLUSIVE,
                        severity=Severity.INFORMATIONAL,
                        title=f"Actor {actor.actor_id} could not be compared",
                        summary="The actor-specific response could not be collected.",
                        evidence={"error": sanitize_data(f"{type(exc).__name__}: {exc}")},
                    ))
                )
                continue
            results.append(
                self._safe_analyze(
                    f"WEB-ACTOR-DIFF-001:{actor.actor_id}",
                    "actor_response_comparison",
                    target,
                    partial(
                        actor_response_comparison,
                        baseline,
                        actor_response,
                        actor.actor_id,
                    ),
                    (baseline, actor_response),
                )
            )
        return results

    def _enabled(self, target: str, category: TestCategory) -> bool:
        if self.selected_categories is None:
            return True
        return category in self.selected_categories.get(target, set())

    def _safe_analyze(
        self,
        test_id: str,
        category: str,
        target: str,
        check: Callable[[], Observation],
        responses: tuple[SafeHttpResponse, ...],
    ) -> Observation:
        try:
            observation = check()
        except Exception as exc:
            observation = Observation(
                test_id=test_id,
                category=category,
                target_url=sanitize_url(target),
                verdict=ObservationVerdict.INCONCLUSIVE,
                severity=Severity.INFORMATIONAL,
                title=f"Analyzer {test_id} could not complete",
                summary="The individual analyzer failed; other checks continued.",
                evidence={"error": sanitize_data(f"{type(exc).__name__}: {exc}")},
            )
            self.audit_logger.emit(
                "analyzer_inconclusive",
                test_id=test_id,
                target_url=sanitize_url(target),
                error=sanitize_data(f"{type(exc).__name__}: {exc}"),
            )
        return self._finalize(observation, *responses)

    def _finalize(
        self,
        observation: Observation,
        *responses: SafeHttpResponse,
    ) -> Observation:
        response_evidence = [
            {
                "url": sanitize_url(response.url),
                "status_code": response.status_code,
                "request_fingerprint": response.request_fingerprint,
                "response_fingerprint": response.response_fingerprint,
            }
            for response in responses
        ]
        evidence = {
            **observation.evidence,
            "config_hash": self.config_hash,
            "responses": response_evidence,
        }
        experiment_material = (
            f"{self.run_id}|{observation.test_id}|{observation.target_url}"
        ).encode()
        experiment_id = hashlib.sha256(experiment_material).hexdigest()[:24]
        evidence_material = json.dumps(
            evidence, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        return observation.model_copy(
            update={
                "evidence": evidence,
                "experiment_id": experiment_id,
                "evidence_hash": hashlib.sha256(evidence_material).hexdigest(),
                "catalog_version": self.CATALOG_VERSION,
            }
        )

    def _summarize(
        self,
        targets: list[str],
        observations: list[Observation],
    ) -> AssessmentSummary:
        counts = {verdict: 0 for verdict in ObservationVerdict}
        for observation in observations:
            counts[observation.verdict] += 1
        return AssessmentSummary(
            run_id=self.run_id,
            targets_assessed=len(targets),
            observations=len(observations),
            passed=counts[ObservationVerdict.PASS],
            failed=counts[ObservationVerdict.FAIL],
            informational=counts[ObservationVerdict.INFO],
            inconclusive=counts[ObservationVerdict.INCONCLUSIVE],
        )
