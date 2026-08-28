import asyncio
import json
from pathlib import Path

import httpx

from whs_redteam.audit import AuditLogger
from whs_redteam.config import ActorConfig, EngagementConfig
from whs_redteam.policy.budget import RequestBudget
from whs_redteam.policy.scope import ScopeGuard
from whs_redteam.sanitizer import REDACTED, sanitize_data
from whs_redteam.tools.actor_sessions import (
    ActorSessionPool,
    summarize_actor_sessions,
)
from whs_redteam.tools.network_gate import NetworkGate


def _pool(
    tmp_path: Path,
    transport: httpx.AsyncBaseTransport,
    environment: dict[str, str],
) -> ActorSessionPool:
    config = EngagementConfig(
        name="actor-isolation",
        targets=("https://example.test",),
        actors=(
            ActorConfig(
                actor_id="alice",
                header_env={"Authorization": "ALICE_AUTH"},
            ),
            ActorConfig(
                actor_id="bob",
                header_env={"Authorization": "BOB_AUTH"},
            ),
        ),
    )
    logger = AuditLogger(tmp_path / "events.jsonl")
    gate = NetworkGate(
        ScopeGuard(config, logger),
        RequestBudget(20, 20, 60),
        logger,
    )
    return ActorSessionPool(
        config,
        gate,
        logger,
        transport=transport,
        environment=environment,
    )


def test_actor_cookie_jars_and_headers_are_isolated(tmp_path: Path) -> None:
    observed: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("authorization", "")
        cookie = request.headers.get("cookie", "")
        observed.append((authorization, cookie))
        identity = "alice" if authorization == "Bearer alice-secret" else "bob"
        return httpx.Response(
            200,
            headers={"Set-Cookie": f"session={identity}; Path=/; HttpOnly"},
        )

    pool = _pool(
        tmp_path,
        httpx.MockTransport(handler),
        {
            "ALICE_AUTH": "Bearer alice-secret",
            "BOB_AUTH": "Bearer bob-secret",
        },
    )

    async def exercise() -> None:
        await pool.client("alice").get("https://example.test/first")
        await pool.client("bob").get("https://example.test/first")
        await pool.client("alice").get("https://example.test/second")
        await pool.client("bob").get("https://example.test/second")

    asyncio.run(exercise())

    assert observed == [
        ("Bearer alice-secret", ""),
        ("Bearer bob-secret", ""),
        ("Bearer alice-secret", "session=alice"),
        ("Bearer bob-secret", "session=bob"),
    ]
    audit = (tmp_path / "events.jsonl").read_text()
    assert "alice-secret" not in audit
    assert "bob-secret" not in audit


def test_actor_summary_contains_names_and_readiness_but_no_values() -> None:
    config = EngagementConfig(
        name="actor-summary",
        targets=("https://example.test",),
        actors=(
            ActorConfig(
                actor_id="alice",
                header_env={
                    "Authorization": "ALICE_AUTH",
                    "X-Api-Key": "ALICE_KEY",
                },
            ),
        ),
    )
    summary = summarize_actor_sessions(
        config,
        {"ALICE_AUTH": "Bearer do-not-persist"},
    )
    serialized = json.dumps(summary.model_dump(mode="json"))

    assert summary.configured == 1
    assert summary.credentials_available == 0
    assert summary.authenticated == 0
    assert summary.actors[0].missing_environment == ("ALICE_KEY",)
    assert "do-not-persist" not in serialized


def test_initial_cookie_credential_joins_actor_private_cookie_jar(
    tmp_path: Path,
) -> None:
    observed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.headers.get("cookie", ""))
        return httpx.Response(
            200,
            headers={"Set-Cookie": "csrf=fresh-token; Path=/"},
        )

    config = EngagementConfig(
        name="cookie-actor",
        targets=("https://example.test",),
        actors=(
            ActorConfig(
                actor_id="cookie_user",
                header_env={"Cookie": "COOKIE_USER_AUTH"},
            ),
        ),
    )
    logger = AuditLogger(tmp_path / "cookie-events.jsonl")
    gate = NetworkGate(
        ScopeGuard(config, logger),
        RequestBudget(10, 10, 60),
        logger,
    )
    pool = ActorSessionPool(
        config,
        gate,
        logger,
        transport=httpx.MockTransport(handler),
        environment={"COOKIE_USER_AUTH": "session=seed-token"},
    )

    async def exercise() -> None:
        await pool.client("cookie_user").get("https://example.test/first")
        await pool.client("cookie_user").get("https://example.test/second")

    asyncio.run(exercise())

    assert "session=seed-token" in observed[0]
    assert "session=seed-token" in observed[1]
    assert "csrf=fresh-token" in observed[1]
    assert "seed-token" not in (tmp_path / "cookie-events.jsonl").read_text()


def test_set_cookie_header_is_treated_as_secret() -> None:
    assert sanitize_data({"Set-Cookie": "session=server-secret"}) == {
        "Set-Cookie": REDACTED
    }


def test_equal_runtime_credentials_are_reported_as_a_collision(
    tmp_path: Path,
) -> None:
    pool = _pool(
        tmp_path,
        httpx.MockTransport(lambda _: httpx.Response(200)),
        {
            "ALICE_AUTH": "Bearer shared-value",
            "BOB_AUTH": "Bearer shared-value",
        },
    )
    summary = pool.summary()

    assert summary.credential_collisions == (("alice", "bob"),)
    assert all(state.credential_collision for state in summary.actors)
