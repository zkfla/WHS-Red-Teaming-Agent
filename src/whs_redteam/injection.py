"""Bounded, non-state-changing injection probes for explicit GET fixtures."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

from whs_redteam.config import EngagementConfig, InjectionFixture
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ActionProposal,
    ApiRequestSchemaRecord,
    Capability,
    Finding,
    FindingStatus,
    ObservedValueType,
    Severity,
    utc_now,
)
from whs_redteam.orchestration.adapters import ProbeContext
from whs_redteam.resource_model import template_endpoint_url
from whs_redteam.tools.actor_sessions import ActorSessionPool
from whs_redteam.tools.http_client import SafeHttpClient, SafeHttpResponse

SQL_ERROR_PATTERNS = (
    re.compile(r"SQL syntax.*MySQL", re.I),
    re.compile(r"Warning.*mysql_", re.I),
    re.compile(r"PostgreSQL.*ERROR", re.I),
    re.compile(r"unterminated quoted string", re.I),
    re.compile(r"SQLite(?:3)?(?:::| error| exception)", re.I),
    re.compile(r"sqlite3\.(?:OperationalError|DatabaseError)", re.I),
    re.compile(r"ORA-\d{4,5}", re.I),
    re.compile(r"Microsoft.*ODBC.*SQL Server", re.I),
    re.compile(r"Unclosed quotation mark after the character string", re.I),
    re.compile(r"You have an error in your SQL syntax", re.I),
)

SENSITIVE_PARAMETER_WORDS = frozenset(
    {
        "authorization",
        "callback",
        "code",
        "cookie",
        "csrf",
        "file",
        "host",
        "key",
        "next",
        "otp",
        "pass",
        "password",
        "path",
        "port",
        "redirect",
        "return",
        "secret",
        "session",
        "token",
        "uri",
        "url",
        "webhook",
    }
)


class XSSBrowserConfirmer(Protocol):
    async def confirm(
        self, fixture: InjectionFixture, token: str
    ) -> dict[str, object]: ...


def discover_injection_fixtures(
    config: EngagementConfig,
    schemas: Iterable[ApiRequestSchemaRecord],
) -> tuple[InjectionFixture, ...]:
    """Derive deterministic, value-free GET candidates from observed query schemas."""

    if not config.auto_injection_discovery:
        return ()
    explicit_checks: dict[tuple[str, str], frozenset[str]] = {
        (fixture.target_url, fixture.parameter): fixture.checks
        for fixture in config.injection_fixtures
    }
    existing_ids = {
        fixture.fixture_id
        for fixture in (*config.authorization_fixtures, *config.injection_fixtures)
    }
    candidates: list[InjectionFixture] = []
    ordered = sorted(
        schemas,
        key=lambda item: (item.endpoint_url, item.method, item.resource_type),
    )
    for schema in ordered:
        if schema.method != "GET" or not schema.query_schema:
            continue
        path_words = {
            word.lower()
            for segment in urlsplit(schema.endpoint_url).path.split("/")
            for word in re.split(r"[^A-Za-z0-9]+", segment)
            if word
        }
        if path_words & {word.lower() for word in config.risky_path_keywords}:
            continue
        for parameter, node in sorted(schema.query_schema.items()):
            normalized_words = {
                word
                for word in re.split(r"[^a-z0-9]+", parameter.lower())
                if word
            }
            if normalized_words & SENSITIVE_PARAMETER_WORDS:
                continue
            value_type = node.type
            if value_type is ObservedValueType.ARRAY and node.items is not None:
                value_type = node.items.type
            if value_type in {
                ObservedValueType.INTEGER,
                ObservedValueType.NUMBER,
            }:
                parameter_kind = "integer"
                baseline_value = "1"
                compatible_checks = {"sqli"}
            elif value_type in {
                ObservedValueType.STRING,
                ObservedValueType.UNKNOWN,
                ObservedValueType.MIXED,
            }:
                parameter_kind = "string"
                baseline_value = "safe"
                compatible_checks = {"sqli", "xss_reflection"}
            else:
                continue
            checks = (
                set(config.auto_injection_checks)
                & compatible_checks
                - set(explicit_checks.get((schema.endpoint_url, parameter), frozenset()))
            )
            if not checks:
                continue
            digest = hashlib.sha256(
                f"{schema.endpoint_url}|{parameter}".encode()
            ).hexdigest()[:20]
            fixture_id = f"auto-{digest}"
            if fixture_id in existing_ids:
                continue
            try:
                candidate = InjectionFixture(
                    fixture_id=fixture_id,
                    approval_scope_url=schema.endpoint_url,
                    target_url=schema.endpoint_url,
                    parameter=parameter,
                    baseline_value=baseline_value,
                    parameter_kind=parameter_kind,
                    checks=frozenset(checks),
                    actor_id=config.auto_injection_actor_id,
                    source="discovered_query_schema",
                )
            except ValueError:
                continue
            candidates.append(candidate)
            existing_ids.add(fixture_id)
            if len(candidates) >= config.max_auto_injection_fixtures:
                return tuple(candidates)
    return tuple(candidates)


def _request_url(fixture: InjectionFixture, value: str) -> str:
    parsed = urlsplit(fixture.target_url)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path or "/",
            urlencode({fixture.parameter: value}),
            "",
        )
    )


def _body_hash(response: SafeHttpResponse) -> str:
    return hashlib.sha256(response.body).hexdigest()


def _content_type(response: SafeHttpResponse) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _has_sql_error(response: SafeHttpResponse) -> bool:
    return any(pattern.search(response.text) for pattern in SQL_ERROR_PATTERNS)


class _CanaryElementParser(HTMLParser):
    def __init__(self, token: str) -> None:
        super().__init__(convert_charrefs=True)
        self.token = token
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "whs-probe":
            return
        values = {name.lower(): value for name, value in attrs}
        if values.get("data-whs") == self.token:
            self.found = True


def _parsed_canary(response: SafeHttpResponse, token: str) -> bool:
    if _content_type(response) not in {"text/html", "application/xhtml+xml"}:
        return False
    parser = _CanaryElementParser(token)
    parser.feed(response.text)
    return parser.found


class _InjectionProbe:
    check_name: str

    def __init__(
        self,
        fixtures: tuple[InjectionFixture, ...],
        anonymous_client: SafeHttpClient,
        actor_sessions: ActorSessionPool,
        validation_id: Callable[[], str | None],
        repetitions: int,
        browser_confirmer: XSSBrowserConfirmer | None = None,
    ) -> None:
        self.fixtures = fixtures
        self.anonymous_client = anonymous_client
        self.actor_sessions = actor_sessions
        self.validation_id = validation_id
        self.repetitions = repetitions
        self.browser_confirmer = browser_confirmer

    def _fixture(self, proposal: ActionProposal) -> InjectionFixture:
        for fixture in self.fixtures:
            if (
                fixture.fixture_id == proposal.fixture_id
                and fixture.approval_scope_url == proposal.target_url
                and fixture.actor_id == proposal.actor_id
                and self.check_name in fixture.checks
            ):
                return fixture
        raise RuntimeError("injection proposal does not match an explicit fixture")

    def _client(self, fixture: InjectionFixture) -> SafeHttpClient:
        if fixture.actor_id is None:
            return self.anonymous_client
        return self.actor_sessions.client(fixture.actor_id)

    def _validation_id(self) -> str:
        validation_id = self.validation_id()
        if validation_id is None:
            raise RuntimeError("injection probe requires completed baseline validation")
        return validation_id

    def _save_finding(
        self,
        inventory: InventoryStore,
        validation_id: str,
        fixture: InjectionFixture,
        *,
        category: str,
        test_id: str,
        severity: Severity,
        title: str,
        summary: str,
        remediation: str,
        observations: list[dict[str, object]],
    ) -> None:
        endpoint_template = template_endpoint_url(fixture.target_url)
        dedup_key = hashlib.sha256(
            f"{category}|{fixture.fixture_id}|{endpoint_template}|{fixture.parameter}".encode()
        ).hexdigest()
        evidence_hashes = tuple(
            hashlib.sha256(
                json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            for item in observations
        )
        experiment_ids = tuple(
            hashlib.sha256(
                f"{fixture.fixture_id}|{category}|{index}".encode()
            ).hexdigest()[:24]
            for index in range(1, len(observations) + 1)
        )
        inventory.save_finding(
            validation_id,
            Finding(
                finding_id=f"F-{dedup_key[:16]}",
                dedup_key=dedup_key,
                category=category,
                test_id=f"{test_id}:{fixture.fixture_id}",
                target_url=endpoint_template,
                status=FindingStatus.CONFIRMED,
                severity=severity,
                title=title,
                summary=summary,
                remediation=remediation,
                reproduction_count=len(observations),
                required_reproductions=self.repetitions,
                negative_control_passed=True,
                experiment_ids=experiment_ids,
                evidence_hashes=evidence_hashes,
                first_observed_at=utc_now(),
            ),
        )


class SQLInjectionProbe(_InjectionProbe):
    """Confirm error-based or strict true/false response SQL injection signals."""

    capability = Capability.SQLI_SAFE
    check_name = "sqli"

    async def execute(
        self, proposal: ActionProposal, context: ProbeContext
    ) -> dict[str, object]:
        fixture = self._fixture(proposal)
        validation_id = self._validation_id()
        client = self._client(fixture)
        results: list[dict[str, object]] = []

        for repetition in range(1, self.repetitions + 1):
            baseline_value = fixture.baseline_value
            safe_control_value = baseline_value + "WHSCTRL"
            quote_value = baseline_value + "'"
            if fixture.parameter_kind == "integer":
                true_value = baseline_value + " AND 1=1"
                false_value = baseline_value + " AND 1=2"
            else:
                true_value = baseline_value + "' AND '1'='1"
                false_value = baseline_value + "' AND '1'='2"

            baseline = await client.get(_request_url(fixture, baseline_value))
            control = await client.get(_request_url(fixture, safe_control_value))
            quote = await client.get(_request_url(fixture, quote_value))
            truthy = await client.get(_request_url(fixture, true_value))
            falsy = await client.get(_request_url(fixture, false_value))

            error_signal = (
                not _has_sql_error(baseline)
                and not _has_sql_error(control)
                and _has_sql_error(quote)
            )
            boolean_signal = (
                baseline.status_code == truthy.status_code
                and baseline.status_code == falsy.status_code
                and _content_type(baseline) == _content_type(truthy)
                and _content_type(baseline) == _content_type(falsy)
                and _body_hash(baseline) == _body_hash(truthy)
                and _body_hash(baseline) != _body_hash(falsy)
                and not _has_sql_error(baseline)
                and not _has_sql_error(truthy)
                and not _has_sql_error(falsy)
            )
            results.append(
                {
                    "repetition": repetition,
                    "baseline_status": baseline.status_code,
                    "control_status": control.status_code,
                    "quote_status": quote.status_code,
                    "true_status": truthy.status_code,
                    "false_status": falsy.status_code,
                    "error_signal": error_signal,
                    "boolean_signal": boolean_signal,
                }
            )

        confirmed_modes = [
            mode
            for mode in ("error_signal", "boolean_signal")
            if all(bool(item[mode]) for item in results)
        ]
        if confirmed_modes:
            self._save_finding(
                context.inventory,
                validation_id,
                fixture,
                category="sqli",
                test_id="WEB-SQLI-001",
                severity=Severity.HIGH,
                title="SQL injection response oracle was reproduced",
                summary=(
                    "A bounded GET parameter mutation repeatedly produced a database-error "
                    "or strict true/false response oracle while its controls stayed clean."
                ),
                remediation=(
                    "Use parameterized queries, reject unexpected input types, and return "
                    "generic database errors."
                ),
                observations=results,
            )
        return {
            "fixture_id": fixture.fixture_id,
            "fixture_source": fixture.source,
            "endpoint_template": template_endpoint_url(fixture.target_url),
            "parameter": fixture.parameter,
            "actor_id": fixture.actor_id or "anonymous",
            "repetitions": self.repetitions,
            "confirmed": bool(confirmed_modes),
            "confirmed_modes": confirmed_modes,
            "matrix": results,
        }


class ReflectedXSSProbe(_InjectionProbe):
    """Confirm unescaped HTML injection with a non-script custom-element canary."""

    capability = Capability.XSS_SAFE
    check_name = "xss_reflection"

    async def execute(
        self, proposal: ActionProposal, context: ProbeContext
    ) -> dict[str, object]:
        fixture = self._fixture(proposal)
        validation_id = self._validation_id()
        client = self._client(fixture)
        results: list[dict[str, object]] = []

        for repetition in range(1, self.repetitions + 1):
            token = hashlib.sha256(
                f"{fixture.fixture_id}|xss|{repetition}".encode()
            ).hexdigest()[:20]
            payload = f'<whs-probe data-whs="{token}"></whs-probe>'
            baseline = await client.get(_request_url(fixture, fixture.baseline_value))
            control = await client.get(_request_url(fixture, token))
            candidate = await client.get(_request_url(fixture, payload))
            baseline_clean = not _parsed_canary(baseline, token)
            control_clean = not _parsed_canary(control, token)
            parsed_injection = _parsed_canary(candidate, token)
            browser_result = (
                await self.browser_confirmer.confirm(fixture, token)
                if parsed_injection and self.browser_confirmer is not None
                else {"status": "not_requested", "confirmed": False}
            )
            results.append(
                {
                    "repetition": repetition,
                    "baseline_status": baseline.status_code,
                    "control_status": control.status_code,
                    "candidate_status": candidate.status_code,
                    "baseline_clean": baseline_clean,
                    "negative_control_clean": control_clean,
                    "html_canary_parsed": parsed_injection,
                    "confirmed_html_injection": (
                        baseline_clean and control_clean and parsed_injection
                    ),
                    "browser_confirmation_status": browser_result["status"],
                    "javascript_execution_confirmed": bool(
                        browser_result["confirmed"]
                    ),
                }
            )

        confirmed = all(
            bool(item["confirmed_html_injection"]) for item in results
        )
        if confirmed:
            self._save_finding(
                context.inventory,
                validation_id,
                fixture,
                category="xss_reflection",
                test_id="WEB-XSS-REFLECT-001",
                severity=Severity.MEDIUM,
                title="Unescaped reflected HTML injection was reproduced",
                summary=(
                    "A harmless custom-element canary was repeatedly parsed as markup. "
                    "Script execution was intentionally not attempted, so this is an XSS "
                    "candidate rather than proof of JavaScript execution."
                ),
                remediation=(
                    "Apply context-aware output encoding, use safe DOM APIs, and enforce a "
                    "restrictive Content-Security-Policy as defense in depth."
                ),
                observations=results,
            )
        javascript_confirmed = self.browser_confirmer is not None and all(
            bool(item["javascript_execution_confirmed"]) for item in results
        )
        if javascript_confirmed:
            self._save_finding(
                context.inventory,
                validation_id,
                fixture,
                category="xss",
                test_id="WEB-XSS-EXEC-001",
                severity=Severity.HIGH,
                title="Reflected JavaScript execution was reproduced",
                summary=(
                    "A harmless browser canary repeatedly changed only a dedicated DOM "
                    "attribute after the reflected event handler executed."
                ),
                remediation=(
                    "Apply context-aware output encoding, use safe DOM APIs, and enforce "
                    "a restrictive Content-Security-Policy."
                ),
                observations=results,
            )
        return {
            "fixture_id": fixture.fixture_id,
            "fixture_source": fixture.source,
            "endpoint_template": template_endpoint_url(fixture.target_url),
            "parameter": fixture.parameter,
            "actor_id": fixture.actor_id or "anonymous",
            "repetitions": self.repetitions,
            "confirmed_html_injection": confirmed,
            "javascript_execution_attempted": self.browser_confirmer is not None,
            "javascript_execution_confirmed": javascript_confirmed,
            "matrix": results,
        }
