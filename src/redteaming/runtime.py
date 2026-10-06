from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from .auth import ANONYMOUS, AuthorizationCheck, HttpIdentityProfile
from .detection import (AccessControlDetector, CorsMisconfigurationDetector, DirectoryListingDetector,
    ErrorDisclosureDetector, HttpObservation, MissingSecurityHeaderDetector, OpenRedirectDetector,
                        OpenApiAuthenticationGapDetector, ServerDisclosureDetector, SessionCookieDetector,
                        ReflectedInputDetector, WebSecurityPolicyDetector, SensitiveDataExposureDetector)
from .detector_registry import DetectorRegistry
from .events import Event, EventBus
from .finding_pipeline import FindingPublisher
from .graphs import AssetGraph, EvidenceStore, ExplorationGraph
from .models import Asset, Evidence, Finding, VerificationResult, CandidateStatus, stable_id
from .models import Scope
from .http_probe import HttpProbe
from .hypotheses import DetectorHypothesisAgent, HypothesisOrchestrator
from .capability.adapters import CapabilityAdapter, ImportedToolResult, ToolOutputParser
from .capability.process import CapabilityExecutor
from .capability.registry import CapabilityRegistry, default_recon_registry
from .skills import SkillRegistry, default_skills
from .recon.runner import PortReconRunner, ReconRun
from .policies import DiscoveryPolicy, ReconPolicy


@dataclass
class LocalRuntime:
    """Dependency-free vertical slice for local development and fixtures."""

    assets: AssetGraph = field(default_factory=AssetGraph)
    exploration: ExplorationGraph = field(default_factory=ExplorationGraph)
    evidence: EvidenceStore = field(default_factory=EvidenceStore)
    publisher: FindingPublisher = field(default_factory=FindingPublisher)
    findings: list[Finding] = field(default_factory=list)
    detector: AccessControlDetector = field(default_factory=AccessControlDetector)
    http_detector: MissingSecurityHeaderDetector = field(default_factory=MissingSecurityHeaderDetector)
    cors_detector: CorsMisconfigurationDetector = field(default_factory=CorsMisconfigurationDetector)
    error_detector: ErrorDisclosureDetector = field(default_factory=ErrorDisclosureDetector)
    cookie_detector: SessionCookieDetector = field(default_factory=SessionCookieDetector)
    server_detector: ServerDisclosureDetector = field(default_factory=ServerDisclosureDetector)
    redirect_detector: OpenRedirectDetector = field(default_factory=OpenRedirectDetector)
    directory_detector: DirectoryListingDetector = field(default_factory=DirectoryListingDetector)
    api_auth_detector: OpenApiAuthenticationGapDetector = field(default_factory=OpenApiAuthenticationGapDetector)
    web_policy_detector: WebSecurityPolicyDetector = field(default_factory=WebSecurityPolicyDetector)
    reflected_detector: ReflectedInputDetector = field(default_factory=ReflectedInputDetector)
    sensitive_data_detector: SensitiveDataExposureDetector = field(default_factory=SensitiveDataExposureDetector)
    hypothesis_orchestrator: HypothesisOrchestrator | None = None
    http_probe: HttpProbe | None = None
    detectors: DetectorRegistry | None = None
    events: EventBus = field(default_factory=EventBus)
    recon_checkpoint: Path | None = None
    recon: PortReconRunner | None = None
    discovery_policy: DiscoveryPolicy = field(default_factory=DiscoveryPolicy)
    recon_policy: ReconPolicy = field(default_factory=ReconPolicy)
    capability_registry: CapabilityRegistry = field(default_factory=default_recon_registry)
    skill_registry: SkillRegistry | None = None
    capability_executor: CapabilityExecutor | None = None

    def __post_init__(self) -> None:
        if self.detectors is None:
            self.detectors = DetectorRegistry((self.detector, self.http_detector,
                                               self.cors_detector, self.error_detector, self.cookie_detector,
                                               self.server_detector, self.directory_detector, self.api_auth_detector,
                                               self.web_policy_detector, self.reflected_detector,
                                               self.sensitive_data_detector, self.redirect_detector))
        if self.hypothesis_orchestrator is None:
            registered_agents = tuple(
                DetectorHypothesisAgent(detector.name, getattr(detector, "category", detector.name), detector)
                for detector in self.detectors.resolve()
            )
            self.hypothesis_orchestrator = HypothesisOrchestrator(registered_agents, events=self.events)
        if self.recon is None:
            self.recon = PortReconRunner(self.assets, self.exploration, self.evidence,
                                         events=self.events, checkpoint_path=self.recon_checkpoint,
                                         policy=self.recon_policy)
        if self.capability_executor is None:
            self.capability_executor = CapabilityExecutor(self.capability_registry, events=self.events)
        if self.skill_registry is None:
            self.skill_registry = SkillRegistry(default_skills())
        if self.http_probe is None:
            self.http_probe = HttpProbe(events=self.events, discovery_policy=self.discovery_policy)

    def run_capability(self, tool_name: str, argv: list[str], scope: Scope,
                       targets: tuple[str, ...], parser: ToolOutputParser,
                       approved: bool = False, timeout: float = 120.0,
                       skill_name: str | None = None) -> ImportedToolResult:
        """Execute one registered adapter and import only normalized observations."""
        assert self.capability_executor is not None
        skill = None
        if skill_name is not None:
            assert self.skill_registry is not None
            skill = self.skill_registry.resolve((skill_name,))[0]
            if tool_name not in skill.tools:
                raise ValueError(f"skill {skill_name!r} does not permit tool {tool_name!r}")
            self.events.publish(Event("skill.execution.started", {
                "skill": skill.name, "tool": tool_name,
            }))
        adapter = CapabilityAdapter(self.capability_executor, parser, self.assets,
                                    self.evidence, self.exploration, self.events)
        try:
            result = adapter.run(tool_name, argv, scope, targets,
                                 approved=approved, timeout=timeout)
        except Exception:
            if skill is not None:
                self.events.publish(Event("skill.execution.failed", {
                    "skill": skill.name, "tool": tool_name,
                }))
            raise
        if skill is not None:
            self.events.publish(Event("skill.execution.completed", {
                "skill": skill.name, "tool": tool_name,
                "asset_count": len(result.assets), "evidence_count": len(result.evidence),
            }))
        return result

    def register_detector(self, detector: object) -> None:
        """Add a specialist without modifying the orchestrator or target code."""
        assert self.detectors is not None
        self.detectors.register(detector)  # type: ignore[arg-type]
        assert self.hypothesis_orchestrator is not None
        self.hypothesis_orchestrator.agents = tuple(
            DetectorHypothesisAgent(item.name, getattr(item, "category", item.name), item)
            for item in self.detectors.resolve()
        )
        self.events.publish(Event("detector.registered", {
            "detector": getattr(detector, "name", "unknown"),
        }))

    def run_network_recon(self, scope: Scope, targets: tuple[str, ...] | None = None,
                          ports: object | None = None) -> list[ReconRun]:
        selected = targets or scope.allowed_hosts
        if not selected:
            raise ValueError("an authorized scope is required")
        assert self.recon is not None
        return [self.recon.run(target, scope) if ports is None else self.recon.run(target, scope, ports) for target in selected]

    def run_http_assessment(self, scope: Scope, runs: list[ReconRun],
                            identities: tuple[HttpIdentityProfile, ...] = (),
                            detector_names: tuple[str, ...] = ()) -> list[Finding]:
        observations: list[HttpObservation] = []
        self.events.publish(Event("http.identification.started", {"service_count": sum(len(run.assets) for run in runs)}))
        assert self.http_probe is not None
        active_identities = identities or (ANONYMOUS,)
        for run in runs:
            for asset in run.assets:
                if asset.kind != "service":
                    continue
                for identity in active_identities:
                    for observation in self.http_probe.discover_surface(asset, scope, identity=identity):
                        canonical_asset = self.assets.upsert(observation.asset)
                        observation = replace(observation, asset=canonical_asset)
                        self.evidence.add(observation.evidence)
                        service_node = self.exploration.add("asset", asset.id)
                        endpoint_node = self.exploration.add("asset", canonical_asset.id)
                        self.exploration.link(service_node, "exposes", endpoint_node)
                        evidence_node = self.exploration.add("evidence", observation.evidence.id)
                        self.exploration.link(endpoint_node, "observed_by", evidence_node)
                        self.events.publish(Event("http.asset.anchored", {
                            "service": asset.canonical_value,
                            "endpoint": canonical_asset.canonical_value,
                            "evidence": observation.evidence.id,
                        }))
                        observations.append(observation)
        self.events.publish(Event("http.identification.completed", {"observation_count": len(observations)}))
        verified: dict[str, VerificationResult] = {}
        assert self.detectors is not None
        selected_detectors = self.detectors.resolve(detector_names) if detector_names else self.detectors.resolve()
        selected_names = [item.name for item in selected_detectors
                          if item.name != self.detector.name or len(active_identities) >= 2]
        self.events.publish(Event("agent.role.started", {
            "role": "verifier", "action": "detector_analysis",
        }))
        assert self.hypothesis_orchestrator is not None
        decisions = self.hypothesis_orchestrator.generate(observations, scope, tuple(selected_names))
        for decision in decisions:
            candidate = decision.candidate
            hypothesis_node = self.exploration.add("hypothesis", decision.hypothesis.id)
            candidate_node = self.exploration.add("candidate", candidate.id)
            self.exploration.link(hypothesis_node, "tests", candidate_node)
            self.events.publish(Event("detector.candidate", {"candidate": candidate.title, "id": candidate.id}))
            self.events.publish(Event("verification.started", {"candidate": candidate.id}))

        def verify_decision(decision):
            candidate = decision.candidate
            detector_name = decision.hypothesis.agent
            detector = self.detectors.get(detector_name)
            custom_verifier = getattr(detector, "verify", None)
            if callable(custom_verifier):
                result = custom_verifier(candidate, observations, self.http_probe, scope, active_identities)
            elif getattr(detector, "transport", None) == "http":
                observation = next(item for item in observations if item.asset.id in candidate.asset_ids)
                if detector_name == self.http_detector.name:
                    result = self.http_probe.verify_missing_header(candidate, observation, scope)
                else:
                    result = self.http_probe.verify_http_candidate(candidate, observation, scope)
            else:
                result = self.http_probe.verify_access_control(candidate, observations, scope, active_identities)
            return candidate.id, result

        # Network verification is I/O-bound. Keep the worker count inside the
        # operator-provided Scope budget, while pool.map preserves hypothesis
        # order for deterministic evidence and event timelines.
        with ThreadPoolExecutor(max_workers=max(1, min(scope.max_concurrency, 32))) as pool:
            for candidate_id, result in pool.map(verify_decision, decisions):
                verified[candidate_id] = result
                self.events.publish(Event("verification.completed", {"candidate": candidate_id,
                                                                         "status": result.status.value}))
        self.events.publish(Event("agent.role.completed", {
            "role": "verifier", "action": "verification",
        }))
        self.events.publish(Event("agent.role.started", {
            "role": "publisher", "action": "finding_publication",
        }))
        findings = self.analyze(observations, verified, {
                                    self.http_detector.name: "Set X-Content-Type-Options: nosniff and review the response security policy.",
                                    self.cors_detector.name: "Restrict allowed origins and never combine wildcard origins with credentials.",
                                    self.error_detector.name: "Return generic error responses and keep stack traces out of client-visible bodies.",
                                    self.cookie_detector.name: "Set Secure, HttpOnly, and SameSite attributes appropriate to the deployment scheme.",
                                    self.server_detector.name: "Minimize version-bearing response headers and disclose only necessary product metadata.",
                                    self.redirect_detector.name: "Validate redirect destinations against an explicit allowlist and reject untrusted external destinations.",
                                    self.directory_detector.name: "Disable directory indexing or restrict listings to explicitly authorized content.",
                                    self.api_auth_detector.name: "Enforce the authentication requirement declared by the API contract before returning protected data.",
                                    self.web_policy_detector.name: "Set browser security policy headers appropriate to the content and transport.",
                                    self.reflected_detector.name: "Contextualize or encode user-controlled output before rendering it in responses.",
                                    self.sensitive_data_detector.name: "Remove sensitive fields from anonymous responses and enforce response-level authorization.",
                                    self.detector.name: "Apply server-side authorization checks and deny access by object ownership.",
                                },
                                tuple(selected_names))
        self.events.publish(Event("agent.role.completed", {
            "role": "publisher", "action": "finding_publication",
            "finding_count": len(findings),
        }))
        for finding in findings:
            self.events.publish(Event("finding.created", {"title": finding.title, "id": finding.id}))
        return findings

    def run_authorization_check(self, asset: Asset, check: AuthorizationCheck,
                                scope: Scope, identities: tuple[HttpIdentityProfile, ...]) -> Finding | None:
        """Run one operator-declared owner/comparison check through the Finding gate."""
        if len(identities) < 2:
            raise ValueError("an authorization check requires at least two identities")
        assert self.http_probe is not None
        candidate, verification = self.http_probe.verify_authorization_check(
            asset, check, scope, identities
        )
        for item in verification.evidence:
            self.evidence.add(item)
        if verification.status is not CandidateStatus.CONFIRMED:
            return None
        finding = self.publisher.publish(
            candidate, verification,
            "Enforce object-level authorization for the declared owner and comparison identities.",
        )
        self.findings.append(finding)
        self.events.publish(Event("finding.created", {"title": finding.title, "id": finding.id}))
        return finding

    def analyze_access_control(
        self,
        observations: list[HttpObservation],
        verified_replays: dict[str, VerificationResult],
        remediation: str | dict[str, str],
    ) -> list[Finding]:
        for observation in observations:
            self.assets.upsert(observation.asset)
            self.evidence.add(observation.evidence)

        findings: list[Finding] = []
        return self.analyze(observations, verified_replays, remediation, (self.detector.name,))

    def _service_key(self, asset_id: str) -> str:
        """Return a host/service identity without including an endpoint path."""
        asset = self.assets.assets.get(asset_id)
        if asset is None:
            return f"asset:{asset_id}"
        seen: set[str] = set()
        while asset.parent_id and asset.parent_id not in seen:
            seen.add(asset.id)
            parent = self.assets.assets.get(asset.parent_id)
            if parent is None:
                break
            asset = parent
        raw = asset.canonical_value
        parsed = urlparse(raw if "://" in raw else f"//{raw}")
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            return f"asset:{asset.id}"
        port = parsed.port
        if port is None and parsed.scheme in {"http", "https"}:
            port = 443 if parsed.scheme == "https" else 80
        return f"{parsed.scheme.lower() or 'tcp'}://{host}:{port or 0}"

    def _candidate_group_key(self, candidate) -> tuple[tuple[str, ...], str, str]:
        root_cause = candidate.root_cause.strip() or f"{candidate.category}:{candidate.title}"
        services = tuple(sorted({self._service_key(item) for item in candidate.asset_ids}))
        return services, candidate.category, root_cause

    def _finding_group_key(self, finding: Finding) -> tuple[tuple[str, ...], str, str]:
        root_cause = finding.root_cause.strip() or f"{finding.category}:{finding.title}"
        services = tuple(sorted({self._service_key(item) for item in finding.asset_ids}))
        return services, finding.category, root_cause

    @staticmethod
    def _merge_unique(*groups: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item for group in groups for item in group))

    def analyze(
        self,
        observations: list[HttpObservation],
        verified_replays: dict[str, VerificationResult],
        remediation: str | dict[str, str],
        detector_names: tuple[str, ...] = (),
    ) -> list[Finding]:
        """Run the detectors selected by the task, then publish verified findings."""
        for observation in observations:
            self.assets.upsert(observation.asset)
            self.evidence.add(observation.evidence)

        findings: list[Finding] = []
        grouped: dict[tuple[tuple[str, ...], str, str], list[tuple[object, VerificationResult, str, object]]] = {}
        for detector in self.detectors.resolve(detector_names):
            for candidate in detector.detect(observations):
                candidate_node = self.exploration.add("candidate", candidate.id)
                verification = verified_replays.get(candidate.id)
                if verification is None:
                    continue
                for item in verification.evidence:
                    self.evidence.add(item)
                selected_remediation = remediation.get(candidate.category, "Review and remediate the verified condition.") if isinstance(remediation, dict) else remediation
                grouped.setdefault(self._candidate_group_key(candidate), []).append(
                    (candidate, verification, selected_remediation, candidate_node)
                )

        # Publish one Finding for one host/service + category + root cause.
        # Endpoint observations remain attached to the Finding as affected
        # assets, rather than becoming duplicate reports.
        for group_key, entries in grouped.items():
            first, _, remediation_text, _ = entries[0]
            asset_ids = self._merge_unique(*(item[0].asset_ids for item in entries))
            evidence_by_id: dict[str, Evidence] = {}
            for _, verification, _, _ in entries:
                for evidence_item in verification.evidence:
                    evidence_by_id[evidence_item.id] = evidence_item
            evidence = tuple(evidence_by_id.values())
            reproduction = self._merge_unique(*(verification.reproduction_steps for _, verification, _, _ in entries))
            impacts = tuple(dict.fromkeys(
                verification.observed_impact.strip()
                for _, verification, _, _ in entries
                if verification.observed_impact.strip()
            ))
            root_cause = group_key[2]
            grouped_candidate = replace(
                first,
                id=stable_id("candidate-group", group_key[1], root_cause, *group_key[0], *asset_ids),
                asset_ids=asset_ids,
                evidence_ids=tuple(item.id for item in evidence),
                root_cause=root_cause,
            )
            grouped_verification = VerificationResult(
                candidate_id=grouped_candidate.id,
                status=CandidateStatus.CONFIRMED,
                evidence=evidence,
                reproduction_steps=reproduction,
                observed_impact="\n".join(impacts),
                confidence=min(item[1].confidence for item in entries),
            )

            existing_index = next(
                (index for index, item in enumerate(self.findings)
                 if self._finding_group_key(item) == group_key),
                None,
            )
            if existing_index is not None:
                existing = self.findings[existing_index]
                merged = replace(
                    existing,
                    asset_ids=self._merge_unique(existing.asset_ids, asset_ids),
                    evidence_ids=self._merge_unique(existing.evidence_ids,
                                                    tuple(item.id for item in evidence)),
                    reproduction_steps=self._merge_unique(existing.reproduction_steps, reproduction),
                    root_cause=root_cause,
                )
                self.findings[existing_index] = merged
                finding = merged
                self.events.publish(Event("finding.grouped", {
                    "category": group_key[1], "finding": finding.id,
                    "endpoint_count": len(finding.asset_ids),
                }))
                is_new = False
            else:
                finding = self.publisher.publish(grouped_candidate, grouped_verification, remediation_text)
                finding = replace(finding, root_cause=root_cause)
                self.findings.append(finding)
                findings.append(finding)
                is_new = True

            finding_node = self.exploration.add("finding", finding.id)
            for candidate, _, _, candidate_node in entries:
                self.exploration.link(candidate_node, "proves", finding_node)
            for evidence_item in evidence:
                evidence_node = self.exploration.add("evidence", evidence_item.id)
                self.exploration.link(finding_node, "supported_by", evidence_node)
            for asset_id in asset_ids:
                asset = self.assets.assets.get(asset_id)
                if asset is not None:
                    asset_node = self.exploration.add("asset", asset.id)
                    self.exploration.link(finding_node, "affects", asset_node)
            if not is_new:
                self.events.publish(Event("finding.duplicate_skipped", {
                    "category": group_key[1], "candidate": grouped_candidate.id,
                }))
        return findings
