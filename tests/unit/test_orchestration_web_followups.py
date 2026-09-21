"""Graph-driven Web follow-ups: bounded, and never beyond what was observed.

The provider is called directly so each rule can be exercised against an
exact graph instead of through a whole run. The negative cases matter as much
as the positive ones: a rule that quietly invents a redirect target, a cookie
name or an auth-challenge scheme would put unobserved claims into the Attack
Graph, and the current web.http_request contract simply does not return them.
"""

from __future__ import annotations

from redteam.attack_graph.identity import endpoint_node_id
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.enums import GraphNodeType
from redteam.orchestration.agents.builtin import WEB_API_AGENT
from redteam.orchestration.agents.providers import GraphCandidateContext
from redteam.orchestration.agents.web import WEB_COMPARE_ANONYMOUS, WebCandidateProvider
from redteam.orchestration.authentication import request_key
from redteam.orchestration.candidates.compiler import candidate_key

HOST = "a.local"
PATH = "/orders/1"

#: Keys the tool never returns. If any of these ever appears in candidate
#: arguments, the provider has started inventing facts.
NEVER_OBSERVED_KEYS = {
    "location",
    "www_authenticate",
    "www-authenticate",
    "set_cookie",
    "set-cookie",
    "cookie",
    "headers",
    "body",
}


def http_observation(
    status: int, *, session_ref: str | None = None, evidence_id: str = "ev_1", path: str = PATH
) -> dict:
    """The shape redteam.orchestration.authentication.http_observation produces."""

    return {
        "kind": "http_response",
        "context_key": request_key("GET", path, session_ref),
        "method": "GET",
        "path": path,
        "target_ref": HOST,
        "session_ref": session_ref,
        "status_code": status,
        "redirect": 300 <= status < 400,
        "body_sha256": None,
        "evidence_id": evidence_id,
    }


def session(session_ref: str, *, paths=(PATH,), expected_access: str = "unspecified") -> dict:
    return {
        "session_ref": session_ref,
        "paths": list(paths),
        "expected_access": expected_access,
    }


async def endpoint_graph(*, observations=(), sessions=()):
    service = AttackGraphService(InMemoryAttackGraphStore())
    host_facts = {"target_ref": HOST}
    if sessions:
        host_facts["auth_session_config"] = {"sessions": list(sessions)}
    await service.upsert_node(
        GraphNodeType.HOST, key=HOST, attrs={"in_scope": True}, facts=host_facts
    )
    await service.upsert_node(
        GraphNodeType.ENDPOINT,
        node_id=endpoint_node_id("GET", PATH, host=HOST),
        attrs={"web_inspected": True},
        facts={
            "host": HOST,
            "path": PATH,
            "method": "GET",
            "observed_target": PATH,
            "observations": list(observations),
        },
    )
    return service


async def propose(service, *, operations=(), verified=False):
    view = service.view()
    observation = await service.observe(step=1)
    context = GraphCandidateContext(
        view=view,
        observation=observation,
        step=1,
        available_operations=tuple(operations),
        operations_verified=verified,
    )
    return await WebCandidateProvider().propose_graph(WEB_API_AGENT, context)


def followups(batch, *, step_prefix: str):
    return [item for item in batch.candidates if item.plan_step.step_id.startswith(step_prefix)]


def test_the_provider_declares_the_operations_it_may_use() -> None:
    # The runtime probes exactly these against the live Tool Catalog, so a
    # tool-specific follow-up can never be offered for an action no tool
    # implements.
    assert WebCandidateProvider.declared_operations == (
        ("web", "http_request"),
        ("web", "auth_attempt"),
    )


async def test_anonymous_401_with_a_registered_session_probes_with_that_session() -> None:
    service = await endpoint_graph(
        observations=[http_observation(401)],
        sessions=[session("alice")],
    )

    batch = await propose(service)

    probes = followups(batch, step_prefix="auth-check:")
    assert len(probes) == 1
    assert probes[0].plan_step.arguments["session_ref"] == "alice"
    assert probes[0].plan_step.authentication_check is not None
    assert str(probes[0].target_node_ids[0]) == str(endpoint_node_id("GET", PATH, host=HOST))


async def test_anonymous_and_session_candidates_are_distinct_identities() -> None:
    # The anonymous baseline and the session probe are the same request to the
    # same path; only the identity differs, and that must be visible in the
    # candidate key or the Work Ledger would dedup one against the other.
    service = await endpoint_graph(
        observations=[http_observation(401)],
        sessions=[session("alice")],
    )

    batch = await propose(service)

    anonymous = followups(batch, step_prefix="anonymous-baseline:")
    session_probe = followups(batch, step_prefix="auth-check:")
    assert not anonymous, "the anonymous 401 already is the baseline"
    assert candidate_key(session_probe[0]) != candidate_key(
        session_probe[0].model_copy(
            update={
                "plan_step": session_probe[0].plan_step.model_copy(
                    update={"arguments": {"target_ref": HOST, "path": PATH, "method": "GET"}}
                )
            }
        )
    )


async def test_a_refused_session_probe_asks_for_the_missing_anonymous_baseline() -> None:
    # An operator said alice should be allowed, yet the session was refused.
    # Without an anonymous baseline that is uninterpretable, so the provider
    # asks for the missing half of an existing comparison, not for a new
    # target.
    service = await endpoint_graph(
        observations=[http_observation(401, session_ref="alice")],
        sessions=[session("alice", expected_access="allow")],
    )

    # The catalog proves the single-call comparison is not executable here,
    # so the two-request baseline is the only route left.
    batch = await propose(service, operations=(), verified=True)

    baselines = followups(batch, step_prefix="anonymous-baseline:")
    assert len(baselines) == 1
    assert "session_ref" not in baselines[0].plan_step.arguments
    assert baselines[0].plan_step.arguments["method"] == "GET"


async def test_a_session_403_compares_against_a_different_identity() -> None:
    service = await endpoint_graph(
        observations=[http_observation(403, session_ref="alice")],
        sessions=[session("alice", expected_access="allow"), session("bob")],
    )

    batch = await propose(service)

    alternatives = followups(batch, step_prefix="authorization-check:")
    assert len(alternatives) == 1
    assert alternatives[0].plan_step.arguments["session_ref"] == "bob"
    # It is an observation until a comparison says otherwise: no violation is
    # claimed up front.
    assert alternatives[0].plan_step.hypothesis is not None
    assert alternatives[0].plan_step.hypothesis.violation_if_confirmed is False


async def test_an_anonymous_403_starts_no_follow_up() -> None:
    # An anonymous 403 is fully explained by "no credentials were presented".
    # Deriving a permission claim from it would read a meaning into the status
    # that the observation does not carry.
    service = await endpoint_graph(
        observations=[http_observation(403)],
        sessions=[session("alice")],
    )

    batch = await propose(service)

    assert not followups(batch, step_prefix="authorization-check:")
    assert not followups(batch, step_prefix="auth-check:")
    assert not followups(batch, step_prefix="anonymous-baseline:")


async def test_a_redirect_status_produces_no_location_derived_candidate() -> None:
    # 302 is observed; the Location value is not returned by the tool at all,
    # so there is nothing to propose "the login endpoint" from.
    service = await endpoint_graph(
        observations=[http_observation(302)],
        sessions=[session("alice")],
    )

    batch = await propose(service)

    assert len(batch.candidates) == 1, "only the playbook inspection step remains"
    for draft in batch.candidates:
        assert NEVER_OBSERVED_KEYS.isdisjoint(draft.plan_step.arguments)


async def test_an_observed_redirect_path_produces_a_bounded_followup() -> None:
    observation = http_observation(302)
    observation["redirect_metadata"] = {
        "detected": True,
        "location_path": "/login",
        "query_omitted": False,
        "fragment_omitted": False,
    }
    service = await endpoint_graph(observations=[observation], sessions=[session("alice")])

    batch = await propose(service)

    redirects = followups(batch, step_prefix="redirect-followup:")
    assert len(redirects) == 1
    assert redirects[0].plan_step.arguments["path"] == "/login"
    assert redirects[0].plan_step.arguments["method"] == "GET"
    assert redirects[0].plan_step.hypothesis is not None


async def test_redirect_followup_requires_a_catalog_bound_http_operation() -> None:
    observation = http_observation(302)
    observation["redirect_metadata"] = {"detected": True, "location_path": "/login"}
    service = await endpoint_graph(observations=[observation])

    batch = await propose(service, operations=(("web", "other_operation"),), verified=True)

    assert not followups(batch, step_prefix="redirect-followup:")


async def test_header_and_cookie_values_are_never_materialized() -> None:
    service = await endpoint_graph(
        observations=[http_observation(401)],
        sessions=[session("alice")],
    )

    batch = await propose(service)

    assert batch.candidates
    for draft in batch.candidates:
        assert NEVER_OBSERVED_KEYS.isdisjoint(draft.plan_step.arguments)
        assert "set-cookie" not in draft.plan_step.arguments


async def test_the_single_call_comparison_is_offered_only_when_the_catalog_binds_it() -> None:
    service = await endpoint_graph(
        observations=[http_observation(401, session_ref="alice")],
        sessions=[session("alice", expected_access="allow")],
    )

    unbound = await propose(service, operations=(), verified=True)
    bound = await propose(service, operations=(("web", "auth_attempt"),), verified=True)

    assert not followups(unbound, step_prefix="compare-anonymous:")
    comparisons = followups(bound, step_prefix="compare-anonymous:")
    assert len(comparisons) == 1
    assert comparisons[0].plan_step.tool_id == WEB_COMPARE_ANONYMOUS
    assert comparisons[0].plan_step.operation == "auth_attempt"
    assert comparisons[0].plan_step.arguments["session_ref"] == "alice"


async def test_without_a_catalog_the_declared_binding_is_still_trusted() -> None:
    # operations_verified=False means "unknown", not "none": with no Tool
    # Catalog wired in, the declared ToolId binding is the only evidence of
    # executability that exists (the same rule the compiler documents), so the
    # provider keeps the permissive behaviour instead of silently disabling
    # every tool-specific follow-up.
    service = await endpoint_graph(
        observations=[http_observation(401, session_ref="alice")],
        sessions=[session("alice", expected_access="allow")],
    )

    batch = await propose(service, operations=(), verified=False)

    assert followups(batch, step_prefix="compare-anonymous:")
