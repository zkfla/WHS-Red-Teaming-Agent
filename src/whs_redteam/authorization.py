"""Bounded owner/non-owner authorization comparison for explicit fixtures."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from urllib.parse import urlsplit

from whs_redteam.config import AuthorizationFixture
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ActionProposal,
    Capability,
    Finding,
    FindingStatus,
    Severity,
    utc_now,
)
from whs_redteam.orchestration.adapters import ProbeContext
from whs_redteam.resource_model import template_endpoint_url
from whs_redteam.tools.actor_sessions import ActorSessionPool
from whs_redteam.tools.response_diff import compare_responses

DENIED_STATUSES = frozenset({401, 403, 404})


class AuthorizationMatrixProbe:
    """Compare only explicitly declared owner resources; never mutate identifiers."""

    capability = Capability.IDOR_SAFE

    def __init__(
        self,
        fixtures: tuple[AuthorizationFixture, ...],
        sessions: ActorSessionPool,
        validation_id: Callable[[], str | None],
        repetitions: int,
    ) -> None:
        self.fixtures = fixtures
        self.sessions = sessions
        self.validation_id = validation_id
        self.repetitions = repetitions

    async def execute(
        self, proposal: ActionProposal, context: ProbeContext
    ) -> dict[str, object]:
        fixture = self._fixture(proposal)
        for request_url in (
            fixture.target_url,
            fixture.negative_control_url,
        ):
            if not self._within_scope(request_url, proposal.target_url):
                raise RuntimeError(
                    "authorization fixture escaped its approved action scope"
                )
        validation_id = self.validation_id()
        if validation_id is None:
            raise RuntimeError("IDOR comparison requires completed baseline validation")
        results: dict[str, list[dict[str, object]]] = {
            actor_id: [] for actor_id in fixture.non_owner_actors
        }
        for repetition in range(self.repetitions):
            owner_response = await self.sessions.client(
                fixture.owner_actor
            ).get(fixture.target_url)
            owner_success = 200 <= owner_response.status_code < 300
            for actor_id in fixture.non_owner_actors:
                actor_client = self.sessions.client(actor_id)
                control = await actor_client.get(fixture.negative_control_url)
                candidate = await actor_client.get(fixture.target_url)
                comparison = compare_responses(owner_response, candidate)
                control_denied = control.status_code in DENIED_STATUSES
                candidate_success = 200 <= candidate.status_code < 300
                owner_equivalent = bool(
                    comparison["status_equal"]
                    and comparison["content_type_equal"]
                    and comparison["body_equal"]
                )
                results[actor_id].append(
                    {
                        "repetition": repetition + 1,
                        "owner_status": owner_response.status_code,
                        "non_owner_status": candidate.status_code,
                        "negative_control_status": control.status_code,
                        "owner_success": owner_success,
                        "negative_control_denied": control_denied,
                        "owner_equivalent_response": owner_equivalent,
                        "unauthorized_read_candidate": (
                            owner_success
                            and control_denied
                            and candidate_success
                            and owner_equivalent
                        ),
                    }
                )
        actors_confirmed: list[str] = []
        actors_inconclusive: list[str] = []
        for actor_id, observations in results.items():
            if all(
                bool(item["unauthorized_read_candidate"])
                for item in observations
            ):
                actors_confirmed.append(actor_id)
                self._save_finding(
                    context.inventory,
                    validation_id,
                    fixture,
                    actor_id,
                    observations,
                )
            elif any(
                200 <= int(item["non_owner_status"]) < 300
                for item in observations
            ):
                actors_inconclusive.append(actor_id)
        return {
            "fixture_id": fixture.fixture_id,
            "endpoint_template": template_endpoint_url(fixture.target_url),
            "owner_actor": fixture.owner_actor,
            "actors_tested": list(fixture.non_owner_actors),
            "actors_confirmed": actors_confirmed,
            "actors_inconclusive": actors_inconclusive,
            "repetitions": self.repetitions,
            "matrix": results,
        }

    def _fixture(self, proposal: ActionProposal) -> AuthorizationFixture:
        for fixture in self.fixtures:
            if (
                fixture.fixture_id == proposal.fixture_id
                and fixture.approval_scope_url == proposal.target_url
                and fixture.owner_actor == proposal.actor_id
            ):
                return fixture
        raise RuntimeError("IDOR proposal does not match an explicit fixture")

    @staticmethod
    def _within_scope(candidate_url: str, scope_url: str) -> bool:
        candidate = urlsplit(candidate_url)
        scope = urlsplit(scope_url)
        candidate_port = candidate.port or (
            443 if candidate.scheme == "https" else 80
        )
        scope_port = scope.port or (443 if scope.scheme == "https" else 80)
        scope_path = (scope.path or "/").rstrip("/") + "/"
        candidate_path = (candidate.path or "/").rstrip("/") + "/"
        return (
            candidate.scheme.lower(),
            (candidate.hostname or "").lower(),
            candidate_port,
        ) == (
            scope.scheme.lower(),
            (scope.hostname or "").lower(),
            scope_port,
        ) and candidate_path.startswith(scope_path)

    def _save_finding(
        self,
        inventory: InventoryStore,
        validation_id: str,
        fixture: AuthorizationFixture,
        non_owner_actor: str,
        observations: list[dict[str, object]],
    ) -> None:
        endpoint_template = template_endpoint_url(fixture.target_url)
        dedup_key = hashlib.sha256(
            (
                f"idor|{fixture.fixture_id}|{endpoint_template}|"
                f"{fixture.owner_actor}|{non_owner_actor}"
            ).encode()
        ).hexdigest()
        evidence_hashes = tuple(
            hashlib.sha256(
                json.dumps(
                    observation,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            for observation in observations
        )
        experiment_ids = tuple(
            hashlib.sha256(
                f"{fixture.fixture_id}|{non_owner_actor}|{index}".encode()
            ).hexdigest()[:24]
            for index in range(1, len(observations) + 1)
        )
        inventory.save_finding(
            validation_id,
            Finding(
                finding_id=f"F-{dedup_key[:16]}",
                dedup_key=dedup_key,
                category="idor",
                test_id=f"WEB-IDOR-001:{fixture.fixture_id}:{non_owner_actor}",
                target_url=endpoint_template,
                status=FindingStatus.CONFIRMED,
                severity=Severity.HIGH,
                title="Non-owner received the owner-equivalent resource response",
                summary=(
                    f"Actor {non_owner_actor} repeatedly received an owner-equivalent "
                    "response while its negative-control request was denied."
                ),
                remediation=(
                    "Enforce object-level authorization on every resource lookup and "
                    "derive ownership from the authenticated server-side identity."
                ),
                reproduction_count=len(observations),
                required_reproductions=self.repetitions,
                negative_control_passed=True,
                experiment_ids=experiment_ids,
                evidence_hashes=evidence_hashes,
                first_observed_at=utc_now(),
            ),
        )
