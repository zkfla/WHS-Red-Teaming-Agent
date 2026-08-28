"""In-memory, actor-isolated authentication sessions.

Credential values are read from the process environment only when a client is
requested. Summaries contain actor IDs and missing environment variable names,
never credential values.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from http.cookies import SimpleCookie

import httpx
from pydantic import BaseModel, ConfigDict

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.tools.http_client import SafeHttpClient
from whs_redteam.tools.network_gate import NetworkGate


class ActorSessionState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    actor_id: str
    credentials_available: bool
    authenticated: bool | None = None
    credential_collision: bool = False
    missing_environment: tuple[str, ...] = ()


class ActorSessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    configured: int
    credentials_available: int
    authenticated: int
    unavailable: int
    credential_collisions: tuple[tuple[str, str], ...] = ()
    actors: tuple[ActorSessionState, ...]


class ActorSessionPool:
    """Create one cookie jar and credential header set per configured actor."""

    def __init__(
        self,
        config: EngagementConfig,
        network_gate: NetworkGate,
        audit_logger: AuditLogger,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.network_gate = network_gate
        self.audit_logger = audit_logger
        self.transport = transport
        self._environment = environment if environment is not None else os.environ
        self._clients: dict[str, SafeHttpClient] = {}

    def summary(self) -> ActorSessionSummary:
        return summarize_actor_sessions(self.config, self._environment)

    def client(self, actor_id: str) -> SafeHttpClient:
        try:
            actor = next(
                actor for actor in self.config.actors if actor.actor_id == actor_id
            )
        except StopIteration as exc:
            raise KeyError(f"unknown actor: {actor_id}") from exc
        state = self._state(actor_id)
        if state.credential_collision:
            raise RuntimeError(
                "actor credential duplicates another configured actor"
            )
        if not state.credentials_available:
            names = ", ".join(state.missing_environment)
            raise RuntimeError(f"actor credential environment is incomplete: {names}")
        if actor_id not in self._clients:
            headers = {
                header: self._environment[variable]
                for header, variable in actor.header_env.items()
                if header != "cookie"
            }
            initial_cookies: dict[str, str] = {}
            cookie_variable = actor.header_env.get("cookie")
            if cookie_variable is not None:
                parsed = SimpleCookie()
                parsed.load(self._environment[cookie_variable])
                initial_cookies = {
                    name: morsel.value for name, morsel in parsed.items()
                }
                if not initial_cookies:
                    raise RuntimeError("actor cookie credential is malformed")
            self._clients[actor_id] = SafeHttpClient(
                self.network_gate,
                self.audit_logger,
                transport=self.transport,
                default_headers=headers,
                initial_cookies=initial_cookies,
            )
        return self._clients[actor_id]

    def _state(self, actor_id: str) -> ActorSessionState:
        actor = next(
            actor for actor in self.config.actors if actor.actor_id == actor_id
        )
        missing = tuple(
            variable
            for variable in actor.header_env.values()
            if not self._environment.get(variable)
        )
        return ActorSessionState(
            actor_id=actor_id,
            credentials_available=not missing,
            missing_environment=missing,
        )


def summarize_actor_sessions(
    config: EngagementConfig,
    environment: Mapping[str, str] | None = None,
) -> ActorSessionSummary:
    """Return persistable readiness metadata without reading credential values."""

    source = environment if environment is not None else os.environ
    credential_material = {
        actor.actor_id: tuple(
            (header, source.get(variable, ""))
            for header, variable in sorted(actor.header_env.items())
        )
        for actor in config.actors
        if all(source.get(variable) for variable in actor.header_env.values())
    }
    collisions = tuple(
        (left, right)
        for index, left in enumerate(sorted(credential_material))
        for right in sorted(credential_material)[index + 1 :]
        if credential_material[left] == credential_material[right]
    )
    colliding = {actor_id for pair in collisions for actor_id in pair}
    states = tuple(
        ActorSessionState(
            actor_id=actor.actor_id,
            credentials_available=all(
                source.get(variable) for variable in actor.header_env.values()
            ),
            credential_collision=actor.actor_id in colliding,
            missing_environment=tuple(
                variable
                for variable in actor.header_env.values()
                if not source.get(variable)
            ),
        )
        for actor in config.actors
    )
    available = sum(state.credentials_available for state in states)
    return ActorSessionSummary(
        configured=len(states),
        credentials_available=available,
        authenticated=0,
        unavailable=len(states) - available,
        credential_collisions=collisions,
        actors=states,
    )
