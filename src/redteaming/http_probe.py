from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field, replace
import hashlib
import http.client
import json
import re
import ssl
import threading
import time
from collections import deque
from urllib.parse import urlencode
from html.parser import HTMLParser
from html import unescape
from urllib.parse import urljoin, urlparse

from .auth import ANONYMOUS, AuthorizationCheck, HttpIdentityProfile
from .capability.adapters import contains_prompt_injection, quarantine_tool_output
from .detection import HttpObservation, SessionCookieDetector, SensitiveDataExposureDetector
from .events import Event, EventBus
from .models import Asset, Evidence, Scope, VulnerabilityCandidate, VerificationResult, CandidateStatus, stable_id
from .policies import DiscoveryPolicy


class RequestRateLimiter:
    """Small process-local sliding-window limiter shared by probe calls."""

    def __init__(self) -> None:
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, limit: int) -> bool:
        if limit <= 0:
            return False
        now = time.monotonic()
        with self._lock:
            while self._timestamps and now - self._timestamps[0] >= 60.0:
                self._timestamps.popleft()
            if len(self._timestamps) >= limit:
                return False
            self._timestamps.append(now)
            return True


_SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
_BODY_SECRET_PATTERN = re.compile(
    r"(?i)([\"']?\b(?:password|passwd|token|secret|api[_-]?key)\b[\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)"
)


def redact_http_headers(headers: dict[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered == "set-cookie":
            safe[name] = re.sub(r"^([^=]+=)[^;]+", r"\1<redacted>", value)
        elif lowered in _SENSITIVE_HEADERS:
            safe[name] = "<redacted>"
        else:
            safe[name] = value
    return safe


def redact_http_body(body: str, limit: int = 4096) -> str:
    return _BODY_SECRET_PATTERN.sub(r"\1<redacted>", body[:limit])


@dataclass(frozen=True)
class HttpProbe:
    timeout: float = 3.0
    max_body_bytes: int = 65536
    events: EventBus | None = None
    rate_limiter: RequestRateLimiter = dataclass_field(default_factory=RequestRateLimiter)
    discovery_policy: DiscoveryPolicy = dataclass_field(default_factory=DiscoveryPolicy)

    def _target(self, asset: Asset, scope: Scope) -> tuple[str, int, str] | None:
        if asset.kind == "endpoint":
            parsed = urlparse(asset.canonical_value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or not scope.allows(asset.canonical_value):
                return None
            return parsed.scheme, parsed.port or (443 if parsed.scheme == "https" else 80), parsed.hostname
        if asset.kind != "service":
            return None
        port = int(asset.properties.get("port", "0"))
        raw = asset.canonical_value
        match = re.match(r"^(.+):(\d+)/(?:tcp|udp)$", raw)
        if not match or port <= 0:
            return None
        host = match.group(1)
        declared_scheme = str(asset.properties.get("scheme", "")).lower()
        if declared_scheme in {"http", "https"}:
            scheme = declared_scheme
        else:
            scheme = "https" if port in {443, 8443} or str(asset.properties.get("tls", "")).lower() in {
                "1", "true", "yes", "on"
            } else "http"
        url = f"{scheme}://{host}:{port}/"
        if not scope.allows(url):
            return None
        return scheme, port, host

    def probe(self, asset: Asset, scope: Scope, identity: HttpIdentityProfile = ANONYMOUS) -> HttpObservation | None:
        return self.probe_path(asset, scope, "/", identity)

    def probe_path(self, asset: Asset, scope: Scope, path: str,
                   identity: HttpIdentityProfile = ANONYMOUS) -> HttpObservation | None:
        target = self._target(asset, scope)
        if target is None:
            return None
        if not self.rate_limiter.acquire(scope.max_requests_per_minute):
            if self.events:
                self.events.publish(Event("http.rate_limited", {
                    "target": asset.canonical_value, "path": path,
                    "limit_per_minute": scope.max_requests_per_minute,
                }))
            return None
        scheme, port, host = target
        path = path if path.startswith("/") else f"/{path}"
        display_url = f"{scheme}://{host}:{port}{path}"
        if self.events:
            self.events.publish(Event("http.started", {"url": display_url, "identity": identity.name}))
        try:
            if scheme == "https":
                connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                    host, port, timeout=self.timeout, context=ssl.create_default_context()
                )
            else:
                connection = http.client.HTTPConnection(host, port, timeout=self.timeout)
            headers = {"Host": host, "User-Agent": "redteaming-agent/0.1", **identity.as_headers()}
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            body = response.read(self.max_body_bytes)
            headers = {key.lower(): value for key, value in response.getheaders()}
            status = response.status
            connection.close()
        except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError):
            if self.events:
                self.events.publish(Event("http.failed", {"url": display_url}))
            return None
        fingerprint = hashlib.sha256(body).hexdigest()
        body_text = body.decode("utf-8", errors="replace")
        safe_headers = redact_http_headers(headers)
        safe_body = redact_http_body(body_text)
        quarantined_body, injection_suspected = quarantine_tool_output(safe_body, display_url)
        evidence = Evidence(
            kind="http_exchange",
            summary=f"GET {path} {status} from {scheme}://{host}:{port}",
            metadata={"method": "GET", "url": display_url, "status": status,
                      "headers": safe_headers, "body_prefix": safe_body,
                      "body_text": safe_body,
                      "untrusted": True, "origin": display_url,
                      "untrusted_body": quarantined_body,
                      "prompt_injection_suspected": injection_suspected or contains_prompt_injection(body_text)},
        )
        if self.events:
            self.events.publish(Event("http.completed", {"url": display_url, "status": status,
                                                          "identity": identity.name}))
        return HttpObservation(asset, identity.name, path, status, fingerprint,
                               safe_body[:240], evidence)

    def discover_surface(self, service: Asset, scope: Scope, max_paths: int | None = None,
                         identity: HttpIdentityProfile = ANONYMOUS) -> list[HttpObservation]:
        root = self.probe(service, scope, identity)
        if root is None and service.kind == "service" and not service.properties.get("scheme"):
            # TLS is often published on an operator-selected high port. A
            # single bounded HTTPS retry improves coverage without scanning a
            # second port or following redirects.
            https_service = replace(service, properties={**service.properties, "scheme": "https"})
            root = self.probe(https_service, scope, identity)
            if root is not None:
                service = https_service
        if root is None:
            return []
        observations = [root]
        root_url = str(root.evidence.metadata.get("url", ""))
        parsed_root = urlparse(root_url)
        policy = self.discovery_policy
        path_limit = max_paths if max_paths is not None else policy.max_paths
        paths: list[str] = list(policy.seed_paths)
        api_metadata: dict[str, dict[str, object]] = {}
        body = str(root.evidence.metadata.get("body_text", ""))
        parser = _LinkParser()
        parser.feed(body)
        discovered_links = list(parser.hrefs) if policy.follow_same_origin_links else []
        root_headers = root.evidence.metadata.get("headers", {})
        if policy.follow_same_origin_links and isinstance(root_headers, dict):
            discovered_links.extend(re.findall(r"<([^>]+)>", str(root_headers.get("link", ""))))
        for href in discovered_links:
            absolute = urljoin(root_url, href)
            parsed = urlparse(absolute)
            if parsed.scheme != parsed_root.scheme or parsed.netloc != parsed_root.netloc:
                continue
            paths.append(parsed.path or "/")
        seen: set[str] = set()
        index = 0
        while index < len(paths):
            path = paths[index]
            index += 1
            if path in seen or len(observations) >= path_limit:
                continue
            seen.add(path)
            endpoint_url = f"{parsed_root.scheme}://{parsed_root.netloc}{path}"
            endpoint = Asset(kind="endpoint", canonical_value=endpoint_url, parent_id=service.id,
                             properties={"discovered_by": "http-surface", **api_metadata.get(path.split("?", 1)[0], {})})
            observation = self.probe_path(endpoint, scope, path, identity)
            if observation is not None:
                observations.append(observation)
                # OpenAPI/Swagger is treated as an inventory source, not as
                # permission to execute operations. Only same-origin GETs
                # are scheduled by this discovery pass.
                if policy.inspect_api_specifications and path.lower().endswith(("openapi.json", "swagger.json")):
                    try:
                        document = json.loads(str(observation.evidence.metadata.get("body_text", "")))
                    except (TypeError, ValueError):
                        document = {}
                    if isinstance(document, dict) and isinstance(document.get("paths"), dict):
                        for api_path, path_item in document["paths"].items():
                            if isinstance(api_path, str) and api_path.startswith("/"):
                                base_path = api_path.split("{", 1)[0].rstrip("/") or "/"
                                paths.append(base_path)
                                if not isinstance(path_item, dict):
                                    continue
                                operation = path_item.get("get")
                                security = operation.get("security") if isinstance(operation, dict) else None
                                if security is None:
                                    security = document.get("security", [])
                                schemes = tuple(sorted({str(key) for item in security if isinstance(item, dict)
                                                        for key in item})) if isinstance(security, list) else ()
                                if schemes:
                                    api_metadata[base_path] = {
                                        "api_auth_required": True,
                                        "api_security_schemes": schemes,
                                    }
                                parameters = operation.get("parameters", []) if isinstance(operation, dict) else []
                                if not isinstance(parameters, list):
                                    continue
                                for parameter in parameters:
                                    if not isinstance(parameter, dict) or parameter.get("in") != "query":
                                        continue
                                    name = parameter.get("name")
                                    if not isinstance(name, str) or not name or len(name) > 64:
                                        continue
                                    marker = policy.canary_prefix + "-" + hashlib.sha256(
                                        f"{base_path}:{name}".encode("utf-8")
                                    ).hexdigest()[:10]
                                    paths.append(base_path + "?" + urlencode({name: marker}))
                                    if self.events:
                                        self.events.publish(Event("http.api.parameter.discovered", {
                                            "path": base_path, "parameter": name,
                                        }))
                elif path.lower().endswith("robots.txt"):
                    raw = str(observation.evidence.metadata.get("body_text", ""))
                    for line in raw.splitlines():
                        directive, separator, value = line.partition(":")
                        candidate_path = value.strip() if separator and directive.strip().lower() in {"allow", "disallow"} else ""
                        if (candidate_path.startswith("/") and len(candidate_path) <= 512 and
                                not any(marker in candidate_path for marker in ("*", "$"))):
                            paths.append(candidate_path)
                elif path.lower().endswith("sitemap.xml"):
                    raw = str(observation.evidence.metadata.get("body_text", ""))
                    for location in re.findall(r"<loc[^>]*>(.*?)</loc>", raw, flags=re.IGNORECASE | re.DOTALL):
                        absolute = urljoin(root_url, unescape(location.strip()))
                        parsed = urlparse(absolute)
                        if parsed.scheme == parsed_root.scheme and parsed.netloc == parsed_root.netloc:
                            paths.append(parsed.path or "/")
        if self.events:
            self.events.publish(Event("http.surface.discovered", {"service": service.canonical_value,
                                                                    "paths": len(observations)}))
        return observations

    def verify_access_control(self, candidate: VulnerabilityCandidate,
                              observations: list[HttpObservation], scope: Scope,
                              identities: tuple[HttpIdentityProfile, ...]) -> VerificationResult:
        selected = [item for item in observations if item.evidence.id in candidate.evidence_ids]
        if len(selected) < 2:
            return VerificationResult(candidate.id, CandidateStatus.INCONCLUSIVE, (), (),
                                      "comparison observations are incomplete", 0.0)
        profiles = {profile.name: profile for profile in identities}
        replays: list[HttpObservation] = []
        for item in selected:
            profile = profiles.get(item.identity)
            if profile is None:
                return VerificationResult(candidate.id, CandidateStatus.INCONCLUSIVE, (), (),
                                          f"identity profile is unavailable: {item.identity}", 0.0)
            replay = self.probe_path(item.asset, scope, item.object_key, profile)
            if replay is not None:
                replays.append(replay)
        accessible = [item for item in replays if 200 <= item.status_code < 300]
        if len(accessible) < 2 or len({item.body_fingerprint for item in accessible}) < 2:
            return VerificationResult(candidate.id, CandidateStatus.REJECTED,
                                      tuple(item.evidence for item in replays),
                                      ("Replay the same endpoint with each authorized identity.",),
                                      "Replay did not reproduce differential access", 0.2)
        return VerificationResult(
            candidate.id, CandidateStatus.CONFIRMED,
            tuple(item.evidence for item in replays),
            tuple(f"GET {item.evidence.metadata.get('url', item.object_key)} as {item.identity}" for item in replays),
            "The same authorized object request returned different successful representations across identities.",
            0.93,
        )

    def verify_authorization_check(
        self, asset: Asset, check: AuthorizationCheck, scope: Scope,
        identities: tuple[HttpIdentityProfile, ...],
    ) -> tuple[VulnerabilityCandidate, VerificationResult]:
        """Replay an operator-declared owner/object comparison using GET only."""
        profiles = {profile.name: profile for profile in identities}
        names = (check.owner_identity,) + tuple(check.comparison_identities)
        candidate_id = stable_id("candidate", "access_control", asset.id,
                                check.object_key, *names)
        candidate = VulnerabilityCandidate(
            category="access_control",
            title="Comparison identity can access an owner-declared object",
            asset_ids=(asset.id,),
            hypothesis=(f"The operator-declared owner of {check.label} is {check.owner_identity}; "
                        "a comparison identity may receive the same object successfully."),
            test_plan=(
                f"Replay GET {check.object_key} as {check.owner_identity}.",
                "Replay the same GET with every comparison identity.",
                "Compare status and ownership according to the operator's authorization contract.",
            ),
            id=candidate_id,
            confidence=0.0,
        )
        if any(name not in profiles for name in names):
            return candidate, VerificationResult(
                candidate.id, CandidateStatus.INCONCLUSIVE, (), (),
                "an identity named by the authorization check is unavailable", 0.0,
            )
        owner = self.probe_path(asset, scope, check.object_key, profiles[check.owner_identity])
        comparisons = [
            self.probe_path(asset, scope, check.object_key, profiles[name])
            for name in check.comparison_identities
        ]
        observed = tuple(item for item in (owner, *comparisons) if item is not None)
        evidence = tuple(item.evidence for item in observed)
        candidate = replace(candidate, evidence_ids=tuple(item.id for item in evidence))
        if owner is None or not (200 <= owner.status_code < 300) or len(observed) != len(names):
            return candidate, VerificationResult(
                candidate.id, CandidateStatus.INCONCLUSIVE, evidence,
                (f"GET {check.object_key} as {check.owner_identity}",),
                "the owner replay did not establish a successful baseline", 0.0,
            )
        unauthorized = [
            item for item in comparisons if 200 <= item.status_code < 300
        ]
        if unauthorized:
            return candidate, VerificationResult(
                candidate.id, CandidateStatus.CONFIRMED, evidence,
                tuple(f"GET {check.object_key} as {item.identity}" for item in observed),
                "an operator-declared comparison identity received a successful response for the owner object",
                0.96,
            )
        if all(item.status_code in check.denied_statuses for item in comparisons):
            return candidate, VerificationResult(
                candidate.id, CandidateStatus.REJECTED, evidence,
                tuple(f"GET {check.object_key} as {item.identity}" for item in observed),
                "all comparison identities were denied as expected", 0.1,
            )
        return candidate, VerificationResult(
            candidate.id, CandidateStatus.INCONCLUSIVE, evidence,
            tuple(f"GET {check.object_key} as {item.identity}" for item in observed),
            "comparison responses did not match the declared denial contract", 0.4,
        )

    def verify_missing_header(self, candidate: VulnerabilityCandidate, observation: HttpObservation,
                              scope: Scope) -> VerificationResult:
        replay = self.probe_path(observation.asset, scope, observation.object_key)
        if replay is None:
            return VerificationResult(candidate.id, CandidateStatus.INCONCLUSIVE, (), (), "HTTP replay failed", 0.0)
        headers = replay.evidence.metadata.get("headers", {})
        missing = "x-content-type-options" not in headers
        if replay.status_code != observation.status_code or not missing:
            return VerificationResult(candidate.id, CandidateStatus.REJECTED, (replay.evidence,),
                                      ("GET /을 동일 대상에 재요청한다.",), "보안 헤더 부재가 재현되지 않음", 0.2)
        return VerificationResult(
            candidate.id, CandidateStatus.CONFIRMED, (observation.evidence, replay.evidence),
            (f"GET {replay.evidence.metadata['url']}", "응답이 2xx이고 X-Content-Type-Options가 없는지 확인한다."),
            "HTTP 응답에서 X-Content-Type-Options가 반복적으로 누락되어 MIME sniffing 방어 설정이 확인되지 않음", 0.91,
        )

    def verify_http_candidate(self, candidate: VulnerabilityCandidate, observation: HttpObservation,
                              scope: Scope) -> VerificationResult:
        replay = self.probe_path(observation.asset, scope, observation.object_key)
        if replay is None:
            return VerificationResult(candidate.id, CandidateStatus.INCONCLUSIVE, (), (), "HTTP replay failed", 0.0)
        original = observation.evidence.metadata
        current = replay.evidence.metadata
        if candidate.category == "cors_misconfiguration":
            original_signal = (str(original.get("headers", {}).get("access-control-allow-origin", "")) == "*" and
                               str(original.get("headers", {}).get("access-control-allow-credentials", "")).lower() == "true")
            current_signal = (str(current.get("headers", {}).get("access-control-allow-origin", "")) == "*" and
                              str(current.get("headers", {}).get("access-control-allow-credentials", "")).lower() == "true")
        elif candidate.category == "session_cookie_security":
            current_signal = bool(SessionCookieDetector._missing(replay))
            original_signal = bool(SessionCookieDetector._missing(observation))
        elif candidate.category == "server_information_disclosure":
            original_headers = original.get("headers", {})
            current_headers = current.get("headers", {})
            original_value = str(original_headers.get("server", "")) if isinstance(original_headers, dict) else ""
            current_value = str(current_headers.get("server", "")) if isinstance(current_headers, dict) else ""
            original_signal = bool(original_value) and any(char.isdigit() for char in original_value)
            current_signal = bool(current_value) and any(char.isdigit() for char in current_value)
        elif candidate.category == "open_redirect":
            def external(metadata: dict[str, object]) -> bool:
                headers = metadata.get("headers", {})
                location = str(headers.get("location", "")) if isinstance(headers, dict) else ""
                source = urlparse(str(metadata.get("url", "")))
                destination = urlparse(location)
                return (300 <= int(metadata.get("status", 0)) < 400 and
                        destination.scheme in {"http", "https"} and bool(destination.hostname) and
                        bool(source.hostname) and destination.hostname.lower() != source.hostname.lower())
            original_signal = external(original)
            current_signal = external(current)
        elif candidate.category == "directory_listing":
            markers = ("<title>index of /", "directory listing for", "directory listing")
            original_signal = any(marker in str(original.get("body_text", "")).lower() for marker in markers)
            current_signal = any(marker in str(current.get("body_text", "")).lower() for marker in markers)
        elif candidate.category == "api_authentication_gap":
            original_signal = 200 <= observation.status_code < 300
            current_signal = 200 <= replay.status_code < 300
        elif candidate.category == "http_security_policy":
            expected = re.search(r"Missing headers:\s*(.+)$", candidate.hypothesis)
            required = tuple(item.strip() for item in expected.group(1).split(",")) if expected else ()
            current_headers = current.get("headers", {})
            original_headers = original.get("headers", {})
            original_signal = bool(required) and isinstance(original_headers, dict) and all(
                item not in original_headers for item in required
            )
            current_signal = bool(required) and isinstance(current_headers, dict) and all(
                item not in current_headers for item in required
            )
        elif candidate.category == "reflected_input":
            canary_match = re.search(r"redteam-canary-[A-Za-z0-9]+", candidate.hypothesis)
            canary = canary_match.group(0) if canary_match else ""
            original_signal = bool(canary) and canary in str(original.get("body_text", ""))
            current_signal = bool(canary) and canary in str(current.get("body_text", ""))
        elif candidate.category == "sensitive_data_exposure":
            original_signal = bool(SensitiveDataExposureDetector._sensitive_fields(
                str(original.get("body_text", ""))))
            current_signal = bool(SensitiveDataExposureDetector._sensitive_fields(
                str(current.get("body_text", ""))))
        else:
            markers = ("traceback", "stack trace", "at java.", "exception:", "sqlstate")
            original_signal = observation.status_code >= 500 and any(marker in str(original.get("body_text", "")).lower() for marker in markers)
            current_signal = replay.status_code >= 500 and any(marker in str(current.get("body_text", "")).lower() for marker in markers)
        if not original_signal or not current_signal:
            return VerificationResult(candidate.id, CandidateStatus.REJECTED, (replay.evidence,),
                                      (f"GET {replay.evidence.metadata.get('url', observation.object_key)}",),
                                      "The observed signal was not reproducible", 0.2)
        return VerificationResult(candidate.id, CandidateStatus.CONFIRMED,
                                  (observation.evidence, replay.evidence),
                                  (f"GET {replay.evidence.metadata.get('url', observation.object_key)}",),
                                  f"{candidate.title} was reproduced on a second safe GET request.", 0.9)


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"a", "link", "form", "script"}:
            for key, value in attrs:
                if key.lower() in {"href", "action", "src"} and value:
                    self.hrefs.append(value)
