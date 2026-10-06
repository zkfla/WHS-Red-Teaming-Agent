from __future__ import annotations

from dataclasses import dataclass
import json
from urllib.parse import parse_qs, urlparse

from .models import Asset, Evidence, VulnerabilityCandidate, stable_id


@dataclass(frozen=True)
class HttpObservation:
    """Normalized result of an authorized comparative request."""

    asset: Asset
    identity: str
    object_key: str
    status_code: int
    body_fingerprint: str
    body_summary: str
    evidence: Evidence


class AccessControlDetector:
    """Finds high-signal authorization candidates from identity comparisons."""

    name = "access-control"
    category = "access_control"
    transport = "http"

    def detect(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        grouped: dict[tuple[str, str], list[HttpObservation]] = {}
        for item in observations:
            grouped.setdefault((item.asset.id, item.object_key), []).append(item)

        candidates: list[VulnerabilityCandidate] = []
        for (asset_id, object_key), items in grouped.items():
            if len({item.identity for item in items}) < 2:
                continue
            accessible = [item for item in items if 200 <= item.status_code < 300]
            if len(accessible) < 2:
                continue
            if len({item.body_fingerprint for item in accessible}) < 2:
                continue

            candidates.append(
                VulnerabilityCandidate(
                    category=self.category,
                    title="Potential broken object-level authorization",
                    asset_ids=(asset_id,),
                    hypothesis=(
                        f"Different identities can retrieve different representations of object {object_key}; "
                        "authorization enforcement requires verification."
                    ),
                    test_plan=(
                        "Replay the same object request with the lower-privileged identity.",
                        "Compare status, response ownership, and sensitive fields.",
                    ),
                    evidence_ids=tuple(item.evidence.id for item in accessible),
                    confidence=0.78,
                    id=stable_id("candidate", self.category, asset_id, object_key),
                )
            )
        return candidates


class MissingSecurityHeaderDetector:
    """Create a candidate only for a reproducible HTTP hardening gap."""

    name = "http-security-headers"
    category = "http_security_headers"
    transport = "http"

    def detect(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        candidates: list[VulnerabilityCandidate] = []
        for item in observations:
            headers = item.evidence.metadata.get("headers", {})
            if not isinstance(headers, dict) or not (200 <= item.status_code < 400):
                continue
            if "x-content-type-options" in headers:
                continue
            candidates.append(VulnerabilityCandidate(
                category=self.category,
                title="HTTP response is missing X-Content-Type-Options",
                asset_ids=(item.asset.id,),
                hypothesis="The service does not explicitly disable browser MIME sniffing.",
                test_plan=("Replay GET /.", "Confirm the response remains successful and the header remains absent."),
                evidence_ids=(item.evidence.id,),
                confidence=0.82,
                id=stable_id("candidate", self.category, item.asset.id),
            ))
        return candidates


class WebSecurityPolicyDetector:
    """Detect a bounded set of missing browser policy headers from responses."""

    name = "web-security-policy"
    category = "http_security_policy"
    transport = "http"

    @staticmethod
    def _required(item: HttpObservation) -> tuple[str, ...]:
        headers = item.evidence.metadata.get("headers", {})
        if not isinstance(headers, dict):
            return ()
        # X-Content-Type-Options has a dedicated detector; keep this specialist
        # focused on the additional policy headers to avoid duplicate Findings.
        required = ["referrer-policy", "permissions-policy"]
        content_type = str(headers.get("content-type", "")).lower()
        body = str(item.evidence.metadata.get("body_text", "")).lower()
        if "text/html" in content_type or "<html" in body:
            required.append("content-security-policy")
        url = str(item.evidence.metadata.get("url", ""))
        if url.startswith("https://"):
            required.append("strict-transport-security")
        return tuple(required)

    def detect(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        candidates: list[VulnerabilityCandidate] = []
        for item in observations:
            if not (200 <= item.status_code < 400):
                continue
            headers = item.evidence.metadata.get("headers", {})
            if not isinstance(headers, dict):
                continue
            missing = tuple(name for name in self._required(item) if name not in headers)
            if not missing:
                continue
            candidates.append(VulnerabilityCandidate(
                category=self.category,
                title="HTTP response is missing browser security policy headers",
                asset_ids=(item.asset.id,),
                hypothesis="Missing headers: " + ", ".join(missing),
                test_plan=("Replay the same safe request.",
                           "Confirm the same browser policy headers remain absent."),
                evidence_ids=(item.evidence.id,),
                confidence=0.76,
                id=stable_id("candidate", self.category, item.asset.id, *missing),
            ))
        return candidates


class CorsMisconfigurationDetector:
    """Find high-signal CORS combinations from observed response headers."""

    name = "cors-misconfiguration"
    category = "cors_misconfiguration"
    transport = "http"

    def detect(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        candidates: list[VulnerabilityCandidate] = []
        for item in observations:
            headers = item.evidence.metadata.get("headers", {})
            if not isinstance(headers, dict) or not (200 <= item.status_code < 400):
                continue
            origin = str(headers.get("access-control-allow-origin", "")).strip()
            credentials = str(headers.get("access-control-allow-credentials", "")).lower().strip()
            if origin != "*" or credentials != "true":
                continue
            candidates.append(VulnerabilityCandidate(
                category=self.category,
                title="CORS permits wildcard origin with credentials",
                asset_ids=(item.asset.id,),
                hypothesis="A browser may accept credentialed cross-origin responses from any origin.",
                test_plan=("Replay the same request.", "Confirm wildcard origin and credentialed CORS headers persist."),
                evidence_ids=(item.evidence.id,),
                confidence=0.84,
                id=stable_id("candidate", self.category, item.asset.id, item.object_key),
            ))
        return candidates


class ErrorDisclosureDetector:
    """Find likely server-side error detail leakage without sending mutations."""

    name = "error-disclosure"
    category = "error_disclosure"
    transport = "http"
    _markers = ("traceback", "stack trace", "at java.", "exception:", "sqlstate")

    def detect(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        candidates: list[VulnerabilityCandidate] = []
        for item in observations:
            if item.status_code < 500:
                continue
            body = str(item.evidence.metadata.get("body_text", "")).lower()
            marker = next((value for value in self._markers if value in body), None)
            if marker is None:
                continue
            candidates.append(VulnerabilityCandidate(
                category=self.category,
                title="Server error response exposes implementation details",
                asset_ids=(item.asset.id,),
                hypothesis=f"A 5xx response contains a server error marker ({marker}).",
                test_plan=("Replay the same safe GET request.", "Confirm the status and diagnostic marker persist."),
                evidence_ids=(item.evidence.id,),
                confidence=0.87,
                id=stable_id("candidate", self.category, item.asset.id, item.object_key),
            ))
        return candidates


class SessionCookieDetector:
    """Find sensitive session cookies missing browser-enforced protections."""

    name = "session-cookie-security"
    category = "session_cookie_security"
    transport = "http"
    _sensitive_names = ("session", "sid", "auth", "token", "jwt", "access")

    @classmethod
    def _missing(cls, observation: HttpObservation) -> tuple[str, ...]:
        headers = observation.evidence.metadata.get("headers", {})
        if not isinstance(headers, dict):
            return ()
        raw = str(headers.get("set-cookie", ""))
        if not raw:
            return ()
        cookie_name = raw.split("=", 1)[0].strip().lower()
        if not any(marker in cookie_name for marker in cls._sensitive_names):
            return ()
        lowered = raw.lower()
        required = ["httponly", "samesite"]
        url = str(observation.evidence.metadata.get("url", ""))
        if url.startswith("https://"):
            required.append("secure")
        return tuple(attribute for attribute in required if attribute not in lowered)

    def detect(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        candidates: list[VulnerabilityCandidate] = []
        for item in observations:
            missing = self._missing(item)
            if not missing:
                continue
            candidates.append(VulnerabilityCandidate(
                category=self.category,
                title="Sensitive session cookie is missing security attributes",
                asset_ids=(item.asset.id,),
                hypothesis=f"Session-like cookie is missing: {', '.join(missing)}.",
                test_plan=("Replay the same safe request.", "Confirm the cookie attributes remain missing."),
                evidence_ids=(item.evidence.id,),
                confidence=0.88,
                id=stable_id("candidate", self.category, item.asset.id, item.object_key),
            ))
        return candidates


class ServerDisclosureDetector:
    """Find version-bearing server headers that are reproducible passively."""

    name = "server-information-disclosure"
    category = "server_information_disclosure"
    transport = "http"

    def detect(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        candidates: list[VulnerabilityCandidate] = []
        for item in observations:
            headers = item.evidence.metadata.get("headers", {})
            if not isinstance(headers, dict) or not (200 <= item.status_code < 400):
                continue
            value = str(headers.get("server", "")).strip()
            # A product/version token is more actionable than a generic header.
            if not value or not any(char.isdigit() for char in value):
                continue
            candidates.append(VulnerabilityCandidate(
                category=self.category,
                title="HTTP response discloses a version-bearing server identifier",
                asset_ids=(item.asset.id,),
                hypothesis="The response Server header exposes product or version details useful for fingerprinting.",
                test_plan=("Replay the same safe request.", "Confirm the version-bearing Server header persists."),
                evidence_ids=(item.evidence.id,),
                confidence=0.74,
                id=stable_id("candidate", self.category, item.asset.id, item.object_key),
            ))
        return candidates


class OpenRedirectDetector:
    """Find redirects whose Location leaves the authorized service origin."""

    name = "open-redirect"
    category = "open_redirect"
    transport = "http"

    @staticmethod
    def _external(observation: HttpObservation) -> bool:
        if not (300 <= observation.status_code < 400):
            return False
        headers = observation.evidence.metadata.get("headers", {})
        if not isinstance(headers, dict):
            return False
        location = str(headers.get("location", "")).strip()
        target = urlparse(str(observation.evidence.metadata.get("url", "")))
        redirect = urlparse(location)
        return bool(redirect.scheme in {"http", "https"} and redirect.hostname and
                    target.hostname and redirect.hostname.lower() != target.hostname.lower())

    def detect(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        candidates: list[VulnerabilityCandidate] = []
        for item in observations:
            if not self._external(item):
                continue
            candidates.append(VulnerabilityCandidate(
                category=self.category,
                title="HTTP response redirects to an external origin",
                asset_ids=(item.asset.id,),
                hypothesis="A reachable endpoint returns an absolute redirect outside its service origin.",
                test_plan=("Replay the same safe GET request without following redirects.",
                           "Confirm the external Location header persists."),
                evidence_ids=(item.evidence.id,),
                confidence=0.86,
                id=stable_id("candidate", self.category, item.asset.id, item.object_key),
            ))
        return candidates


class DirectoryListingDetector:
    """Find high-signal directory indexes in safe response bodies."""

    name = "directory-listing"
    category = "directory_listing"
    transport = "http"
    _markers = ("<title>index of /", "directory listing for", "directory listing")

    def detect(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        candidates: list[VulnerabilityCandidate] = []
        for item in observations:
            if not (200 <= item.status_code < 400):
                continue
            body = str(item.evidence.metadata.get("body_text", "")).lower()
            marker = next((value for value in self._markers if value in body), None)
            if marker is None:
                continue
            candidates.append(VulnerabilityCandidate(
                category=self.category,
                title="HTTP response appears to expose a directory listing",
                asset_ids=(item.asset.id,),
                hypothesis=f"The safe response contains a directory-index marker ({marker}).",
                test_plan=("Replay the same safe GET request.", "Confirm the directory-index marker persists."),
                evidence_ids=(item.evidence.id,),
                confidence=0.86,
                id=stable_id("candidate", self.category, item.asset.id, item.object_key),
            ))
        return candidates


class OpenApiAuthenticationGapDetector:
    """Identify anonymous success on an endpoint declared protected by OpenAPI."""

    name = "openapi-authentication-gap"
    category = "api_authentication_gap"
    transport = "http"

    def detect(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        candidates: list[VulnerabilityCandidate] = []
        for item in observations:
            if item.identity != "anonymous" or not (200 <= item.status_code < 300):
                continue
            if not bool(item.asset.properties.get("api_auth_required")):
                continue
            schemes = ", ".join(str(value) for value in item.asset.properties.get("api_security_schemes", ()))
            candidates.append(VulnerabilityCandidate(
                category=self.category,
                title="OpenAPI-declared protected endpoint accepts an anonymous request",
                asset_ids=(item.asset.id,),
                hypothesis=(f"The endpoint is declared protected by OpenAPI ({schemes}) but returned success anonymously."),
                test_plan=("Replay the same anonymous GET request.", "Confirm the endpoint remains successful without an identity header."),
                evidence_ids=(item.evidence.id,),
                confidence=0.83,
                id=stable_id("candidate", self.category, item.asset.id, item.object_key),
            ))
        return candidates


class SensitiveDataExposureDetector:
    """Find sensitive-looking fields in anonymous successful JSON responses."""

    name = "sensitive-data-exposure"
    category = "sensitive_data_exposure"
    transport = "http"
    _field_markers = (
        "password", "passwd", "secret", "token", "api_key", "apikey",
        "access_token", "refresh_token", "private_key", "ssn",
        "social_security", "credit_card", "card_number",
    )

    @classmethod
    def _sensitive_fields(cls, body: str) -> tuple[str, ...]:
        try:
            payload = json.loads(body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        found: set[str] = set()

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalized = str(key).lower().replace("-", "_")
                    if any(marker in normalized for marker in cls._field_markers):
                        found.add(str(key))
                    visit(nested)
            elif isinstance(value, list):
                for nested in value[:32]:
                    visit(nested)

        visit(payload)
        return tuple(sorted(found))

    def detect(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        candidates: list[VulnerabilityCandidate] = []
        for item in observations:
            if item.identity != "anonymous" or not (200 <= item.status_code < 300):
                continue
            headers = item.evidence.metadata.get("headers", {})
            content_type = str(headers.get("content-type", "")) if isinstance(headers, dict) else ""
            body = str(item.evidence.metadata.get("body_text", ""))
            if "json" not in content_type.lower() and not body.lstrip().startswith(("{", "[")):
                continue
            fields = self._sensitive_fields(body)
            if not fields:
                continue
            candidates.append(VulnerabilityCandidate(
                category=self.category,
                title="Anonymous JSON response exposes sensitive-looking fields",
                asset_ids=(item.asset.id,),
                hypothesis=("An anonymous successful response contains sensitive field names: "
                            + ", ".join(fields[:8]) + "."),
                test_plan=("Replay the same safe GET request anonymously.",
                           "Confirm the same sensitive field names remain present without exposing values."),
                evidence_ids=(item.evidence.id,),
                confidence=0.84,
                id=stable_id("candidate", self.category, item.asset.id, item.object_key, *fields),
            ))
        return candidates


class ReflectedInputDetector:
    """Find inert query canaries reflected into a response body."""

    name = "reflected-input"
    category = "reflected_input"
    transport = "http"
    _prefix = "redteam-canary-"

    def detect(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        candidates: list[VulnerabilityCandidate] = []
        for item in observations:
            if not (200 <= item.status_code < 400):
                continue
            query = parse_qs(urlparse(item.object_key).query, keep_blank_values=True)
            body = str(item.evidence.metadata.get("body_text", ""))
            reflected = tuple(value for values in query.values() for value in values
                              if value.startswith(self._prefix) and value in body)
            if not reflected:
                continue
            candidates.append(VulnerabilityCandidate(
                category=self.category,
                title="Inert query input is reflected in the HTTP response",
                asset_ids=(item.asset.id,),
                hypothesis=f"The response reflects a test canary ({reflected[0]}) supplied in a query parameter.",
                test_plan=("Replay the same inert GET request.", "Confirm the canary remains reflected without active payloads."),
                evidence_ids=(item.evidence.id,),
                confidence=0.72,
                id=stable_id("candidate", self.category, item.asset.id, item.object_key),
            ))
        return candidates
