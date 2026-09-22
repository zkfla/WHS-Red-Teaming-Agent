"""H3: comparison and posture follow-ups, only where something was observed.

Both rules exist to use Mappers that already shipped (web.http_compare_sessions,
web.http_security_posture). The point of the tests is the *floor* on evidence:
two identities must actually have answered the same path, and a posture
observation must actually be missing. Configuring sessions is not evidence.
"""

from __future__ import annotations

from redteam.orchestration.agents.web import (
    WEB_COMPARE_SESSIONS,
    WEB_SECURITY_POSTURE,
)
from tests.unit.test_orchestration_web_followups import (
    PATH,
    endpoint_graph,
    followups,
    http_observation,
    propose,
    session,
)

_ANALYSIS = (("web", "response_analysis"),)


def _analysis_propose(service):
    return propose(service, operations=_ANALYSIS, verified=True)


# -- compare_sessions ------------------------------------------------------


async def test_two_observed_identities_on_one_path_are_compared() -> None:
    service = await endpoint_graph(
        observations=[
            http_observation(401, session_ref="alice", evidence_id="ev_a"),
            http_observation(200, session_ref="bob", evidence_id="ev_b"),
        ],
        sessions=[session("alice"), session("bob")],
    )

    batch = await _analysis_propose(service)

    drafts = followups(batch, step_prefix="compare-sessions:")
    assert len(drafts) == 1
    step = drafts[0].plan_step
    assert step.tool_id == WEB_COMPARE_SESSIONS
    assert step.operation == "response_analysis"
    assert step.assessment_kind == "observation"
    assert step.arguments["path"] == PATH
    assert {step.arguments["first_session_ref"], step.arguments["second_session_ref"]} == {
        "alice",
        "bob",
    }


async def test_one_observed_identity_is_not_enough_to_compare() -> None:
    service = await endpoint_graph(
        observations=[
            http_observation(401),
            http_observation(200, session_ref="alice", evidence_id="ev_b"),
        ],
        sessions=[session("alice"), session("bob")],
    )

    batch = await _analysis_propose(service)

    # bob is configured but never answered, so there is nothing to compare
    # against and the pair is not invented from the registry.
    assert not followups(batch, step_prefix="compare-sessions:")


async def test_a_configured_but_unobserved_pair_is_never_compared() -> None:
    service = await endpoint_graph(
        observations=[http_observation(401)],
        sessions=[session("alice"), session("bob")],
    )

    batch = await _analysis_propose(service)

    assert not followups(batch, step_prefix="compare-sessions:")


async def test_unregistered_session_refs_in_observations_are_not_compared() -> None:
    # Two refs really did answer this path, but neither is an operator-managed
    # test identity. Treating them as a comparison pair would be inventing a
    # test principal out of whatever the target happened to echo back.
    service = await endpoint_graph(
        observations=[
            http_observation(401, session_ref="rogue-one", evidence_id="ev_a"),
            http_observation(200, session_ref="rogue-two", evidence_id="ev_b"),
        ],
        sessions=[session("alice"), session("bob")],
    )

    batch = await _analysis_propose(service)

    assert not followups(batch, step_prefix="compare-sessions:")


async def test_a_registered_session_scoped_to_another_path_is_not_compared() -> None:
    service = await endpoint_graph(
        observations=[
            http_observation(200, session_ref="alice", evidence_id="ev_a"),
            http_observation(200, session_ref="bob", evidence_id="ev_b"),
        ],
        sessions=[session("alice"), session("bob", paths=("/other",))],
    )

    batch = await _analysis_propose(service)

    assert not followups(batch, step_prefix="compare-sessions:")


async def test_an_already_compared_pair_is_not_compared_again() -> None:
    service = await endpoint_graph(
        observations=[
            http_observation(401, session_ref="alice", evidence_id="ev_a"),
            http_observation(200, session_ref="bob", evidence_id="ev_b"),
            {
                "kind": "http_session_comparison",
                "context_key": "recorded-by-the-mapper",
                "path": PATH,
                "first_session_ref": "alice",
                "second_session_ref": "bob",
                "verdict": "observation_only",
            },
        ],
        sessions=[session("alice"), session("bob")],
    )

    batch = await _analysis_propose(service)

    assert not followups(batch, step_prefix="compare-sessions:")


async def test_comparison_is_not_offered_when_the_catalog_cannot_run_it() -> None:
    service = await endpoint_graph(
        observations=[
            http_observation(401, session_ref="alice", evidence_id="ev_a"),
            http_observation(200, session_ref="bob", evidence_id="ev_b"),
        ],
        sessions=[session("alice"), session("bob")],
    )

    batch = await propose(service, operations=(), verified=True)

    assert not followups(batch, step_prefix="compare-sessions:")


# -- security_posture ------------------------------------------------------


async def test_a_responding_endpoint_gets_a_posture_observation() -> None:
    service = await endpoint_graph(
        observations=[http_observation(200)],
        sessions=[session("alice")],
    )

    batch = await _analysis_propose(service)

    drafts = followups(batch, step_prefix="security-posture:")
    assert len(drafts) == 1
    step = drafts[0].plan_step
    assert step.tool_id == WEB_SECURITY_POSTURE
    assert step.operation == "response_analysis"
    assert step.assessment_kind == "observation"
    # Anonymous posture: no session is named that was not observed.
    assert "session_ref" not in step.arguments
    assert step.arguments["path"] == PATH


async def test_an_endpoint_that_never_answered_gets_no_posture_observation() -> None:
    service = await endpoint_graph(sessions=[session("alice")])

    batch = await _analysis_propose(service)

    assert not followups(batch, step_prefix="security-posture:")


async def test_an_existing_posture_observation_is_not_repeated() -> None:
    service = await endpoint_graph(
        observations=[
            http_observation(200),
            {
                "kind": "http_security_posture",
                "context_key": "recorded-by-the-mapper",
                "path": PATH,
                "session_ref": None,
                "verdict": "observation_only",
            },
        ],
        sessions=[session("alice")],
    )

    batch = await _analysis_propose(service)

    assert not followups(batch, step_prefix="security-posture:")


async def test_a_session_scoped_posture_does_not_satisfy_the_anonymous_one() -> None:
    service = await endpoint_graph(
        observations=[
            http_observation(200),
            {
                "kind": "http_security_posture",
                "context_key": "recorded-by-the-mapper",
                "path": PATH,
                "session_ref": "alice",
                "verdict": "observation_only",
            },
        ],
        sessions=[session("alice")],
    )

    batch = await _analysis_propose(service)

    assert followups(batch, step_prefix="security-posture:")


async def test_posture_is_not_offered_when_the_catalog_cannot_run_it() -> None:
    service = await endpoint_graph(observations=[http_observation(200)])

    batch = await propose(service, operations=(), verified=True)

    assert not followups(batch, step_prefix="security-posture:")


# -- and the new rules stay observation-only ------------------------------


async def test_the_new_rules_never_claim_a_violation() -> None:
    service = await endpoint_graph(
        observations=[
            http_observation(401, session_ref="alice", evidence_id="ev_a"),
            http_observation(200, session_ref="bob", evidence_id="ev_b"),
        ],
        sessions=[session("alice"), session("bob")],
    )

    batch = await _analysis_propose(service)

    drafts = followups(batch, step_prefix="compare-sessions:") + followups(
        batch, step_prefix="security-posture:"
    )
    assert drafts
    for draft in drafts:
        assert draft.plan_step.assessment_kind == "observation"
        hypothesis = draft.plan_step.hypothesis
        assert hypothesis is not None
        assert hypothesis.violation_if_confirmed is False
        assert draft.plan_step.expectation is None
