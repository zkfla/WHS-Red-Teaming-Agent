import sys
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import json

sys.path.insert(0, "src")

from redteaming.detection import (CorsMisconfigurationDetector, DirectoryListingDetector,
                                  ErrorDisclosureDetector, HttpObservation, MissingSecurityHeaderDetector,
                                  OpenApiAuthenticationGapDetector, ServerDisclosureDetector, SessionCookieDetector,
                                  ReflectedInputDetector, WebSecurityPolicyDetector, SensitiveDataExposureDetector,
                                  OpenRedirectDetector)
from redteaming.auth import AuthorizationCheck, HttpIdentityProfile
from redteaming.browser_session import BrowserSessionError, validate_browser_url
from redteaming.evaluation import EvaluationCase, EvaluationHarness, load_evaluation_cases, render_benchmark
from redteaming.http_probe import HttpProbe, RequestRateLimiter, redact_http_body, redact_http_headers
from redteaming.models import Asset, CandidateStatus, Evidence, Finding, Scope, VerificationResult, VulnerabilityCandidate
from redteaming.orchestration import EngagementOrchestrator, ScenarioPolicyError
from redteaming.events import Event, EventBus
from redteaming.hypotheses import (DetectorHypothesisAgent, Hypothesis, HypothesisDecision,
                                   HypothesisOrchestrator, HypothesisPrioritizer)
from redteaming.planning import EngagementPlan, EngagementPlanner, PlanPolicy, PlanPolicyError, PlanStep
from redteaming.reporting import render_engagement_json, render_engagement_report, render_markdown
from redteaming.runtime import LocalRuntime
from redteaming.scenarios import ScenarioRegistry
from redteaming.objectives import ObjectiveGraph, ObjectiveGraphError, ObjectiveStatus
from redteaming.persistence import load_snapshot, save_snapshot


class VerticalSliceTests(unittest.TestCase):
    def test_browser_session_handoff_is_scope_gated_before_optional_dependency(self):
        scope = Scope(("https://approved.invalid",))
        validate_browser_url("https://approved.invalid/login", scope)
        with self.assertRaises(BrowserSessionError):
            validate_browser_url("https://outside.invalid/login", scope)
        with self.assertRaises(BrowserSessionError):
            validate_browser_url("javascript:alert(1)", scope)

    def test_owner_supplied_evaluation_cases_load_from_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps({"cases": [{
                "name": "baseline", "expected_categories": ["directory_listing"]
            }]}), encoding="utf-8")
            cases = load_evaluation_cases(path)
            self.assertEqual(cases[0], EvaluationCase("baseline", frozenset({"directory_listing"})))

    def test_evaluation_compares_repeat_runs_by_finding_signature(self):
        first = [Finding("header", "http_security_headers", "low", ("asset-1",), ("GET /",),
                          "impact", ("evidence-1",), "fix")]
        second = [Finding("header", "http_security_headers", "low", ("asset-1",), ("GET /",),
                           "impact", ("evidence-2",), "fix"),
                  Finding("cookie", "session_cookie_security", "medium", ("asset-2",), ("GET /",),
                           "impact", ("evidence-3",), "fix")]
        comparison = EvaluationHarness.compare_runs(first, second)
        self.assertEqual(comparison.stable_count, 1)
        self.assertEqual(comparison.stability, 0.5)

    def test_server_disclosure_detector_requires_version_signal(self):
        asset = Asset(kind="endpoint", canonical_value="https://authorized.invalid/")
        observation = HttpObservation(
            asset=asset, identity="anonymous", object_key="/", status_code=200,
            body_fingerprint="body", body_summary="ok",
            evidence=Evidence(kind="http_exchange", summary="response", metadata={
                "headers": {"server": "ExampleServer/4.2"}, "body_text": "ok"
            }),
        )
        candidates = ServerDisclosureDetector().detect([observation])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].category, "server_information_disclosure")

    def test_open_redirect_detector_only_accepts_external_absolute_location(self):
        asset = Asset(kind="endpoint", canonical_value="https://authorized.invalid/redirect")
        observation = HttpObservation(
            asset=asset, identity="anonymous", object_key="/redirect", status_code=302,
            body_fingerprint="body", body_summary="redirect",
            evidence=Evidence(kind="http_exchange", summary="redirect", metadata={
                "url": "https://authorized.invalid/redirect",
                "headers": {"location": "https://outside.invalid/login"}, "body_text": ""
            }),
        )
        candidates = OpenRedirectDetector().detect([observation])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].category, "open_redirect")

    def test_directory_listing_detector_uses_passive_body_marker(self):
        asset = Asset(kind="endpoint", canonical_value="https://authorized.invalid/files")
        observation = HttpObservation(
            asset=asset, identity="anonymous", object_key="/files", status_code=200,
            body_fingerprint="body", body_summary="listing",
            evidence=Evidence(kind="http_exchange", summary="response", metadata={
                "headers": {}, "body_text": "<title>Index of /files</title>"
            }),
        )
        candidates = DirectoryListingDetector().detect([observation])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].category, "directory_listing")

    def test_openapi_auth_metadata_creates_anonymous_access_candidate(self):
        asset = Asset(kind="endpoint", canonical_value="https://authorized.invalid/api/items",
                      properties={"api_auth_required": True, "api_security_schemes": ("bearerAuth",)})
        observation = HttpObservation(
            asset=asset, identity="anonymous", object_key="/api/items", status_code=200,
            body_fingerprint="body", body_summary="data",
            evidence=Evidence(kind="http_exchange", summary="protected response", metadata={
                "headers": {}, "body_text": "{\"id\":1}"
            }),
        )
        candidates = OpenApiAuthenticationGapDetector().detect([observation])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].category, "api_authentication_gap")

    def test_sensitive_json_fields_are_detected_without_storing_values(self):
        asset = Asset(kind="endpoint", canonical_value="https://authorized.invalid/profile")
        observation = HttpObservation(
            asset=asset, identity="anonymous", object_key="/profile", status_code=200,
            body_fingerprint="body", body_summary="profile",
            evidence=Evidence(kind="http_exchange", summary="anonymous profile", metadata={
                "headers": {"content-type": "application/json"},
                "body_text": '{"user":"demo","access_token":"<redacted>"}',
            }),
        )
        candidates = SensitiveDataExposureDetector().detect([observation])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].category, "sensitive_data_exposure")
        self.assertNotIn("<redacted>", candidates[0].hypothesis)

    def test_web_security_policy_detector_uses_content_and_transport_context(self):
        asset = Asset(kind="endpoint", canonical_value="https://authorized.invalid/")
        observation = HttpObservation(
            asset=asset, identity="anonymous", object_key="/", status_code=200,
            body_fingerprint="body", body_summary="html",
            evidence=Evidence(kind="http_exchange", summary="html response", metadata={
                "url": "https://authorized.invalid/", "headers": {"content-type": "text/html"},
                "body_text": "<html><body>ok</body></html>",
            }),
        )
        candidates = WebSecurityPolicyDetector().detect([observation])
        self.assertEqual(len(candidates), 1)
        self.assertIn("content-security-policy", candidates[0].hypothesis)
        self.assertIn("strict-transport-security", candidates[0].hypothesis)

    def test_reflected_input_detector_requires_inert_canary_in_response(self):
        canary = "redteam-canary-abc123"
        asset = Asset(kind="endpoint", canonical_value="https://authorized.invalid/search")
        observation = HttpObservation(
            asset=asset, identity="anonymous", object_key=f"/search?q={canary}", status_code=200,
            body_fingerprint="body", body_summary="reflected",
            evidence=Evidence(kind="http_exchange", summary="response", metadata={
                "headers": {}, "body_text": f"<p>{canary}</p>"
            }),
        )
        candidates = ReflectedInputDetector().detect([observation])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].category, "reflected_input")

    def test_engagement_snapshot_restores_audit_state_without_identity_data(self):
        runtime = LocalRuntime()
        asset = runtime.assets.upsert(Asset(kind="endpoint", canonical_value="https://authorized.invalid/health"))
        evidence = runtime.evidence.add(Evidence(
            kind="http_exchange", summary="safe response", metadata={"headers": {"content-type": "text/plain"}}
        ))
        runtime.exploration.add("asset", asset.id)
        runtime.events.publish(Event("test.completed", {"asset": asset.id}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engagement.json"
            save_snapshot(path, runtime.assets, runtime.evidence, runtime.findings,
                          runtime.exploration, runtime.events.history)
            restored = LocalRuntime()
            events, plan = load_snapshot(path, restored.assets, restored.evidence,
                                         restored.findings, restored.exploration)
            self.assertIsNone(plan)
            self.assertIn(asset.id, restored.assets.assets)
            self.assertIn(evidence.id, restored.evidence.items)
            self.assertEqual(events[0].name, "test.completed")
            self.assertNotIn("Authorization", path.read_text(encoding="utf-8"))

    def test_observation_to_validated_finding(self):
        asset = Asset(kind="endpoint", canonical_value="https://app.test/api/orders/42")
        observations = [
            HttpObservation(
                asset=asset,
                identity="owner",
                object_key="order:42",
                status_code=200,
                body_fingerprint="owner-order-42",
                body_summary="owner order",
                evidence=Evidence(kind="http_exchange", summary="owner response"),
            ),
            HttpObservation(
                asset=asset,
                identity="viewer",
                object_key="order:42",
                status_code=200,
                body_fingerprint="viewer-order-42",
                body_summary="same order returned to viewer",
                evidence=Evidence(kind="http_exchange", summary="viewer response"),
            ),
        ]

        runtime = LocalRuntime()
        candidates = runtime.detector.detect(observations)
        self.assertEqual(len(candidates), 1)
        verification = VerificationResult(
            candidate_id=candidates[0].id,
            status=CandidateStatus.CONFIRMED,
            evidence=(Evidence(kind="replay", summary="viewer replay reproduced access"),),
            reproduction_steps=("Authenticate as viewer.", "Request order:42.", "Observe owner data."),
            observed_impact="A viewer can access an object belonging to another identity.",
            confidence=0.96,
        )

        findings = runtime.analyze_access_control(observations, {candidates[0].id: verification}, "Enforce object-level authorization.")

        self.assertEqual(len(findings), 1)
        self.assertIn("validated", render_markdown(findings[0]))
        self.assertGreaterEqual(len(runtime.exploration.edges), 3)

    def test_objective_graph_requires_order_and_evidence(self):
        graph = ObjectiveGraph.from_steps((
            type("Step", (), {"action": "recon", "objective": "discover"})(),
            type("Step", (), {"action": "verify", "objective": "verify"})(),
        ))
        first, second = tuple(graph.objectives)
        with self.assertRaises(ObjectiveGraphError):
            graph.transition(second, ObjectiveStatus.IN_PROGRESS)
        graph.transition(first, ObjectiveStatus.IN_PROGRESS)
        with self.assertRaises(ObjectiveGraphError):
            graph.transition(first, ObjectiveStatus.COMPLETED)
        graph.transition(first, ObjectiveStatus.COMPLETED, ("evidence-1",))
        self.assertEqual(graph.ready()[0].id, second)

    def test_missing_header_candidate_is_verified_before_finding(self):
        asset = Asset(kind="service", canonical_value="127.0.0.1:8000/tcp", properties={"port": "8000"})
        observation = HttpObservation(
            asset=asset, identity="anonymous", object_key="/", status_code=200,
            body_fingerprint="body", body_summary="ok",
            evidence=Evidence(kind="http_exchange", summary="GET /", metadata={"headers": {}}),
        )
        candidates = MissingSecurityHeaderDetector().detect([observation])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].category, "http_security_headers")

    def test_access_control_replay_uses_supplied_identity_profiles(self):
        asset = Asset(kind="endpoint", canonical_value="http://app.test/api/items/1")
        original = [
            HttpObservation(asset, "owner", "/api/items/1", 200, "owner", "owner",
                            Evidence(kind="http_exchange", summary="owner", metadata={"url": "http://app.test/api/items/1"})),
            HttpObservation(asset, "viewer", "/api/items/1", 200, "viewer", "viewer",
                            Evidence(kind="http_exchange", summary="viewer", metadata={"url": "http://app.test/api/items/1"})),
        ]
        from redteaming.detection import AccessControlDetector
        candidate = AccessControlDetector().detect(original)[0]

        class ReplayProbe(HttpProbe):
            def probe_path(self, asset, scope, path, identity=HttpIdentityProfile("anonymous")):
                return HttpObservation(asset, identity.name, path, 200, identity.name, identity.name,
                                       Evidence(kind="replay", summary=identity.name,
                                                metadata={"url": f"http://app.test{path}"}))

        result = ReplayProbe().verify_access_control(
            candidate, original, type("Scope", (), {"allows": lambda self, target: True})(),
            (HttpIdentityProfile("owner"), HttpIdentityProfile("viewer")),
        )
        self.assertEqual(result.status, CandidateStatus.CONFIRMED)
        self.assertEqual(len(result.evidence), 2)

    def test_operator_authorization_check_requires_owner_baseline_and_reproduces_access(self):
        asset = Asset(kind="endpoint", canonical_value="http://app.test/api/items/1")

        class AuthorizationProbe(HttpProbe):
            def __init__(self, comparison_status):
                super().__init__()
                self.comparison_status = comparison_status

            def probe_path(self, asset, scope, path, identity=HttpIdentityProfile("anonymous")):
                status = 200 if identity.name == "owner" else self.comparison_status
                return HttpObservation(
                    asset, identity.name, path, status, f"{identity.name}-{status}", "response",
                    Evidence(kind="replay", summary=f"{identity.name} response",
                             metadata={"url": f"http://app.test{path}"}),
                )

        profiles = (HttpIdentityProfile("owner"), HttpIdentityProfile("viewer"))
        check = AuthorizationCheck("/api/items/1", "owner", ("viewer",), "order 1")
        runtime = LocalRuntime()
        runtime.http_probe = AuthorizationProbe(200)
        finding = runtime.run_authorization_check(asset, check, Scope(("http://app.test",)), profiles)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.category, "access_control")

        protected_runtime = LocalRuntime()
        protected_runtime.http_probe = AuthorizationProbe(403)
        self.assertIsNone(
            protected_runtime.run_authorization_check(asset, check,
                                                      Scope(("http://app.test",)), profiles)
        )

    def test_surface_discovery_expands_openapi_paths_without_target_constants(self):
        service = Asset(kind="service", canonical_value="app.test:80/tcp", properties={"port": "80"})

        class SurfaceProbe(HttpProbe):
            def probe_path(self, asset, scope, path, identity=HttpIdentityProfile("anonymous")):
                body = ('{"security": [{"bearerAuth": []}], "paths": {"/api/orders/{id}": '
                        '{"get": {"parameters": [{"in": "query", "name": "search"}]}}}}'
                        if path == "/openapi.json" else "{}")
                return HttpObservation(
                    asset, identity.name, path, 200, path, body,
                    Evidence(kind="http_exchange", summary=path,
                             metadata={"url": "http://app.test:80" + path, "body_text": body, "headers": {}}),
                )

        observations = SurfaceProbe().discover_surface(service, Scope(("http://app.test:80",)))
        self.assertIn("/api/orders", {item.object_key for item in observations})
        self.assertTrue(any(item.object_key.startswith("/api/orders?search=redteam-canary-")
                            for item in observations))
        api_observations = [item for item in observations if item.object_key == "/api/orders"]
        self.assertTrue(api_observations[0].asset.properties["api_auth_required"])
        self.assertEqual(api_observations[0].asset.properties["api_security_schemes"], ("bearerAuth",))

    def test_surface_discovery_expands_robots_sitemap_and_script_paths(self):
        service = Asset(kind="service", canonical_value="app.test:80/tcp", properties={"port": "80"})

        class DiscoveryProbe(HttpProbe):
            def probe_path(self, asset, scope, path, identity=HttpIdentityProfile("anonymous")):
                body = {
                    "/robots.txt": "User-agent: *\nDisallow: /admin\n",
                    "/sitemap.xml": "<urlset><url><loc>http://app.test:80/health</loc></url></urlset>",
                    "/": '<html><script src="/app.js"></script></html>',
                }.get(path, "{}")
                headers = {"link": "</linked>"} if path == "/" else {}
                return HttpObservation(
                    asset, identity.name, path, 200, path, body,
                    Evidence(kind="http_exchange", summary=path,
                             metadata={"url": "http://app.test:80" + path, "body_text": body, "headers": headers}),
                )

        observations = DiscoveryProbe().discover_surface(service, Scope(("http://app.test:80",)))
        discovered = {item.object_key for item in observations}
        self.assertIn("/admin", discovered)
        self.assertIn("/health", discovered)
        self.assertIn("/app.js", discovered)
        self.assertIn("/linked", discovered)

    def test_http_request_rate_limiter_enforces_scope_budget(self):
        limiter = RequestRateLimiter()
        self.assertTrue(limiter.acquire(1))
        self.assertFalse(limiter.acquire(1))
        self.assertFalse(limiter.acquire(0))

    def test_http_probe_honors_declared_tls_on_nonstandard_port(self):
        service = Asset(kind="service", canonical_value="app.test:9443/tcp",
                        properties={"port": "9443", "tls": "true"})
        probe = HttpProbe()
        self.assertEqual(probe._target(service, Scope(("https://app.test:9443",)))[0], "https")

    def test_http_evidence_redacts_credentials_but_keeps_security_signals(self):
        headers = redact_http_headers({"set-cookie": "session=secret; Path=/", "x-content-type-options": "nosniff"})
        self.assertEqual(headers["set-cookie"], "session=<redacted>; Path=/")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertNotIn("secret", redact_http_body('{"token":"secret"}'))

    def test_cors_candidate_requires_reproducible_headers(self):
        asset = Asset(kind="endpoint", canonical_value="http://app.test/api")
        evidence = Evidence(kind="http_exchange", summary="cors",
                            metadata={"headers": {"access-control-allow-origin": "*",
                                                  "access-control-allow-credentials": "true"},
                                      "url": "http://app.test/api"})
        observation = HttpObservation(asset, "anonymous", "/api", 200, "body", "ok", evidence)
        candidate = CorsMisconfigurationDetector().detect([observation])[0]

        class CorsProbe(HttpProbe):
            def probe_path(self, asset, scope, path, identity=HttpIdentityProfile("anonymous")):
                return observation

        result = CorsProbe().verify_http_candidate(
            candidate, observation, Scope(("http://app.test",))
        )
        self.assertEqual(result.status, CandidateStatus.CONFIRMED)

    def test_error_disclosure_detector_is_passive_and_specific(self):
        asset = Asset(kind="endpoint", canonical_value="http://app.test/failure")
        evidence = Evidence(kind="http_exchange", summary="error",
                            metadata={"body_text": "Traceback: internal path", "headers": {}})
        observation = HttpObservation(asset, "anonymous", "/failure", 500, "error", "Traceback", evidence)
        candidates = ErrorDisclosureDetector().detect([observation])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].category, "error_disclosure")

    def test_session_cookie_detector_requires_sensitive_cookie_and_flags(self):
        asset = Asset(kind="endpoint", canonical_value="https://app.test/")
        evidence = Evidence(
            kind="http_exchange", summary="session cookie",
            metadata={"url": "https://app.test/", "headers": {"set-cookie": "session=abc; Path=/"}},
        )
        observation = HttpObservation(asset, "anonymous", "/", 200, "cookie", "ok", evidence)
        candidates = SessionCookieDetector().detect([observation])
        self.assertEqual(len(candidates), 1)
        self.assertIn("httponly", candidates[0].hypothesis)

    def test_planner_fallback_is_structured_and_scope_bound(self):
        scope = type("Scope", (), {"allowed_hosts": ("staging.example.com",)})()
        plan = EngagementPlanner().create("초기 정찰과 검증", scope)
        self.assertEqual(plan.source, "deterministic-fallback")
        self.assertEqual(plan.scope_hosts, ("staging.example.com",))
        self.assertEqual(plan.steps[-1].action, "reporting")
        self.assertEqual(plan.scenario_id, "external-exposure")
        self.assertIn("reconnaissance", plan.scenario_tactics)
        self.assertIn("T1046", plan.scenario_techniques)
        self.assertIn("T1046", {technique for step in plan.steps for technique in step.techniques})
        auth_plan = EngagementPlanner().create("로그인 사용자 권한과 인가를 점검해줘", scope)
        self.assertEqual(auth_plan.scenario_id, "authenticated-web")

    def test_plan_policy_enforces_scenario_allowed_actions(self):
        scope = Scope(("app.test",))
        plan = EngagementPlan(
            objective="external", scope_hosts=scope.allowed_hosts,
            steps=(PlanStep("not-an-allowed-action", "bad", "bad"),),
            stop_conditions=(), source="test", id="plan", scenario_id="external-exposure",
        )
        with self.assertRaises(PlanPolicyError):
            PlanPolicy().validate(plan, scope)

    def test_scenario_registry_is_metadata_only_and_reportable(self):
        scenarios = ScenarioRegistry().all()
        self.assertTrue(any(item.id == "authenticated-web" and item.requires_identity for item in scenarios))
        plan = EngagementPlanner().create("authorized web comparison", Scope(("app.test",)))
        report = render_engagement_report([], [], plan=plan)
        self.assertIn("Engagement Scenario", report)
        self.assertIn("external-exposure", report)
        self.assertIn("ATT&CK tactics", report)
        self.assertIn("T1046", report)

    def test_orchestrator_handoffs_roles_without_implicit_report(self):
        class FakeRuntime:
            def __init__(self):
                self.events = EventBus()
                self.calls = []

            def run_network_recon(self, scope):
                self.calls.append("worker")
                return [object()]

            def run_http_assessment(self, scope, runs, identities=()):
                self.calls.append("verifier")
                return []

        runtime = FakeRuntime()
        report_events = []
        runtime.events.subscribe("report.ready", report_events.append)
        result = EngagementOrchestrator(runtime).execute("authorized assessment", Scope(("app.test",)))
        self.assertEqual(runtime.calls, ["worker", "verifier"])
        self.assertEqual(result.findings, ())
        self.assertEqual(len(report_events), 1)

    def test_orchestrator_resumes_completed_plan_without_repeating_workers(self):
        class ResumeRuntime:
            def __init__(self):
                self.events = EventBus()
                self.calls = []

            def run_network_recon(self, scope):
                self.calls.append("worker")
                return [object()]

            def run_http_assessment(self, scope, runs, identities=()):
                self.calls.append("verifier")
                return []

        runtime = ResumeRuntime()
        orchestrator = EngagementOrchestrator(runtime)
        scope = Scope(("app.test",))
        orchestrator.execute("authorized assessment", scope)
        orchestrator.execute("authorized assessment", scope)
        self.assertEqual(runtime.calls, ["worker", "verifier"])
        self.assertGreaterEqual(sum(1 for event in runtime.events.history
                                    if event.name == "objective.resumed"), 6)

    def test_snapshot_plan_can_be_reused_for_resume_after_new_orchestrator(self):
        class ResumeRuntime:
            def __init__(self):
                self.events = EventBus()
                self.calls = []

            def run_network_recon(self, scope):
                self.calls.append("worker")
                return [object()]

            def run_http_assessment(self, scope, runs, identities=()):
                self.calls.append("verifier")
                return []

        scope = Scope(("app.test",))
        first_runtime = ResumeRuntime()
        first = EngagementOrchestrator(first_runtime).execute("authorized assessment", scope)
        second_runtime = ResumeRuntime()
        second_runtime.events.history.extend(first_runtime.events.history)
        second = EngagementOrchestrator(second_runtime).execute(
            "continue the assessment", scope, resume_plan=first.plan
        )
        self.assertEqual(second.plan.id, first.plan.id)
        self.assertEqual(second_runtime.calls, [])
        self.assertGreaterEqual(sum(1 for event in second_runtime.events.history
                                    if event.name == "objective.resumed"), 6)

    def test_authenticated_scenario_requires_two_identity_profiles(self):
        with self.assertRaises(ScenarioPolicyError):
            EngagementOrchestrator(LocalRuntime()).execute(
                "로그인 사용자 권한과 인가를 점검해줘", Scope(("app.test",))
            )

    def test_evaluation_scores_relevance_and_reproducibility(self):
        asset = Asset(kind="endpoint", canonical_value="http://app.test/api/items/1")
        candidate = VulnerabilityCandidate(
            category="access_control", title="BOLA", asset_ids=(asset.id,),
            hypothesis="cross-user access", test_plan=("replay",), confidence=0.9,
        )
        finding = LocalRuntime().publisher.publish(
            candidate,
            VerificationResult(candidate.id, CandidateStatus.CONFIRMED,
                               (Evidence(kind="replay", summary="reproduced"),),
                               ("GET /api/items/1",), "unauthorized access", 0.95),
            "Enforce ownership",
        )
        result = EvaluationHarness().evaluate(EvaluationCase("authz", frozenset({"access_control"})), [finding])
        self.assertEqual(result.coverage, 1.0)
        self.assertEqual(result.precision, 1.0)
        self.assertEqual(result.reproducibility_rate, 1.0)
        self.assertEqual(result.missing_categories, ())
        self.assertEqual(result.unexpected_categories, ())

    def test_json_report_is_machine_readable_and_contains_evidence(self):
        finding = Finding("validated", "test", "low", (), (), "impact", (), "fix")
        payload = json.loads(render_engagement_json(
            [Asset(kind="host", canonical_value="approved.test")],
            [Evidence(kind="observation", summary="reachable")],
            [finding],
        ))
        self.assertEqual(payload["assets"][0]["canonical_value"], "approved.test")
        self.assertEqual(payload["evidence"][0]["summary"], "reachable")
        self.assertEqual(payload["findings"][0]["status"], "validated")

    def test_evaluation_benchmark_compares_repeated_finding_runs(self):
        asset = Asset(kind="endpoint", canonical_value="http://app.test/api/items/1")
        candidate = VulnerabilityCandidate(
            category="access_control", title="BOLA", asset_ids=(asset.id,),
            hypothesis="cross-user access", test_plan=("replay",), confidence=0.9,
        )
        publisher = LocalRuntime().publisher
        finding = publisher.publish(
            candidate,
            VerificationResult(candidate.id, CandidateStatus.CONFIRMED,
                               (Evidence(kind="replay", summary="reproduced"),),
                               ("GET /api/items/1",), "unauthorized access", 0.95),
            "Enforce ownership",
        )
        result = EvaluationHarness().benchmark(
            EvaluationCase("authz", frozenset({"access_control"})),
            ((finding,), (finding,)),
        )
        self.assertEqual(result.repetitions, 2)
        self.assertEqual(result.stability, 1.0)
        self.assertIn("repetitions=2", render_benchmark(result))

    def test_report_includes_execution_timeline_when_events_are_supplied(self):
        bus = EventBus()
        bus.publish(__import__("redteaming.events", fromlist=["Event"]).Event(
            "recon.started", {"target": "authorized.test"}
        ))
        report = render_engagement_report([], [], exploration=None, events=bus.history)
        self.assertIn("Execution Timeline", report)
        self.assertIn("recon.started", report)

    def test_report_includes_authorized_scope_from_plan(self):
        plan = EngagementPlanner().create("authorized assessment", Scope(("approved.test:443",)))
        report = render_engagement_report([], [], plan=plan)
        self.assertIn("Authorized Scope", report)
        self.assertIn("approved.test:443", report)

    def test_hypothesis_agent_creates_gated_candidate_and_events(self):
        asset = Asset(kind="endpoint", canonical_value="http://app.test/")
        observation = HttpObservation(
            asset, "anonymous", "/", 200, "body", "ok",
            Evidence(kind="http_exchange", summary="safe response", metadata={"headers": {}}),
        )
        detector = MissingSecurityHeaderDetector()
        bus = EventBus()
        decisions = HypothesisOrchestrator(
            (DetectorHypothesisAgent(detector.name, detector.category, detector),), events=bus
        ).generate([observation], Scope(("http://app.test",)), (detector.name,))
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0].accepted)
        self.assertTrue(any(event.name == "hypothesis.created" for event in bus.history))

    def test_findings_group_by_service_category_and_root_cause(self):
        class GroupingDetector:
            name = "grouping-detector"
            category = "authorization"

            def detect(self, observations):
                candidates = []
                for observation in observations:
                    root_cause = "shared authorization flaw" if observation.object_key != "/different" else "different authorization flaw"
                    candidates.append(VulnerabilityCandidate(
                        category=self.category,
                        title="Authorization boundary is not enforced",
                        asset_ids=(observation.asset.id,),
                        hypothesis=root_cause,
                        test_plan=("Replay the authorized request.",),
                        id=f"grouping-{observation.asset.id}",
                        root_cause=root_cause,
                    ))
                return candidates

        runtime = LocalRuntime()
        detector = GroupingDetector()
        runtime.register_detector(detector)
        service_one = runtime.assets.upsert(Asset(
            kind="service", canonical_value="app-one.test:443/tcp",
            properties={"port": 443, "scheme": "https"},
        ))
        service_two = runtime.assets.upsert(Asset(
            kind="service", canonical_value="app-two.test:443/tcp",
            properties={"port": 443, "scheme": "https"},
        ))
        endpoint_one = runtime.assets.upsert(Asset(
            kind="endpoint", canonical_value="https://app-one.test:443/one",
            parent_id=service_one.id,
        ))
        endpoint_two = runtime.assets.upsert(Asset(
            kind="endpoint", canonical_value="https://app-one.test:443/two",
            parent_id=service_one.id,
        ))
        endpoint_three = runtime.assets.upsert(Asset(
            kind="endpoint", canonical_value="https://app-two.test:443/three",
            parent_id=service_two.id,
        ))
        endpoint_four = runtime.assets.upsert(Asset(
            kind="endpoint", canonical_value="https://app-one.test:443/different",
            parent_id=service_one.id,
        ))
        observations = [
            HttpObservation(endpoint_one, "anonymous", "/one", 200, "one", "ok",
                            Evidence(kind="http_exchange", summary="one")),
            HttpObservation(endpoint_two, "anonymous", "/two", 200, "two", "ok",
                            Evidence(kind="http_exchange", summary="two")),
            HttpObservation(endpoint_three, "anonymous", "/three", 200, "three", "ok",
                            Evidence(kind="http_exchange", summary="three")),
            HttpObservation(endpoint_four, "anonymous", "/different", 200, "four", "ok",
                            Evidence(kind="http_exchange", summary="four")),
        ]
        candidates = detector.detect(observations)
        verified = {
            candidate.id: VerificationResult(
                candidate.id, CandidateStatus.CONFIRMED,
                (Evidence(kind="replay", summary=candidate.hypothesis),),
                ("Replay the request.",), "Authorization boundary bypass reproduced.", 0.95,
            )
            for candidate in candidates
        }

        findings = runtime.analyze(
            observations, verified, {detector.category: "Enforce authorization."},
            (detector.name,),
        )

        self.assertEqual(len(findings), 3)
        grouped = next(item for item in findings if endpoint_one.id in item.asset_ids and endpoint_two.id in item.asset_ids)
        self.assertEqual(len(grouped.asset_ids), 2)
        self.assertEqual(sum(1 for item in findings if endpoint_three.id in item.asset_ids), 1)
        self.assertEqual(sum(1 for item in findings if item.root_cause == "different authorization flaw"), 1)

    def test_hypothesis_gate_rejects_mutating_actions(self):
        from redteaming.hypotheses import HypothesisGate
        accepted, reason = HypothesisGate().evaluate(
            Hypothesis("unsafe", "agent", "test", ("asset",), "test", ("evidence",),
                       "POST a mutation", 0.9),
            Scope(("app.test",)),
        )
        self.assertFalse(accepted)
        self.assertIn("mutation", reason)

    def test_llm_prioritizer_can_only_reorder_known_hypotheses(self):
        class FakeClient:
            def chat(self, messages):
                return '{"ids":["h2","unknown","h1"]}'

        candidate = VulnerabilityCandidate(
            category="test", title="test", asset_ids=("asset",),
            hypothesis="test", test_plan=("Replay GET.",),
        )
        first = HypothesisDecision(
            Hypothesis("h1", "agent-a", "test", ("asset",), "first", ("Replay GET.",), "Replay GET.", 0.9),
            candidate, True, "ok",
        )
        second = HypothesisDecision(
            Hypothesis("h2", "agent-b", "test", ("asset",), "second", ("Replay GET.",), "Replay GET.", 0.8),
            candidate, True, "ok",
        )
        ordered = HypothesisPrioritizer(FakeClient()).order((first, second), Scope(("app.test",)))
        self.assertEqual([item.hypothesis.id for item in ordered], ["h2", "h1"])

    def test_llm_observation_context_is_explicit_and_signal_only(self):
        captured = []

        class CapturingClient:
            def chat(self, messages):
                captured.append(messages[0]["content"])
                return '{"ids":["h1"]}'

        candidate = VulnerabilityCandidate(
            category="test", title="test", asset_ids=("asset",),
            hypothesis="test", test_plan=("Replay GET.",),
        )
        decision = HypothesisDecision(
            Hypothesis("h1", "agent-a", "test", ("asset",), "first", ("Replay GET.",), "Replay GET.", 0.9),
            candidate, True, "ok",
        )
        observation = HttpObservation(
            Asset(kind="endpoint", canonical_value="https://app.test/private"),
            "viewer", "/private?token=secret", 500, "fingerprint", "safe summary",
            Evidence(kind="http_exchange", summary="response", metadata={
                "headers": {"authorization": "<redacted>", "content-type": "text/html"},
                "body_text": "Traceback: internal detail",
                "evidence_id": "must-stay-local",
            }),
        )
        with patch.dict(os.environ, {"REDTEAM_LLM_SEND_OBSERVATION_METADATA": "true"}):
            HypothesisPrioritizer(CapturingClient()).order(
                (decision, HypothesisDecision(
                    Hypothesis("h2", "agent-b", "test", ("asset",), "second", ("Replay GET.",), "Replay GET.", 0.8),
                    candidate, True, "ok",
                )), Scope(("https://app.test",)), [observation]
            )
        self.assertIn("ObservationContext", captured[0])
        self.assertIn("/private", captured[0])
        self.assertIn("error_marker", captured[0])
        self.assertNotIn("secret", captured[0])
        self.assertNotIn("must-stay-local", captured[0])


if __name__ == "__main__":
    unittest.main()
