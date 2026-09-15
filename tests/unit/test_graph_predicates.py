"""Attack Graph property layout: merge semantics, retraction, secret guard."""

from __future__ import annotations

import pytest

from redteam.attack_graph.predicates import (
    Confidence,
    Provenance,
    SensitiveValueError,
    apply_retraction,
    attrs_of,
    build_properties,
    facts_of,
    is_stale,
    last_seen_step,
    merge_properties,
    predicate_flags,
    validate_attrs,
)


def test_build_properties_lays_out_every_reserved_key() -> None:
    props = build_properties(
        attrs={"reachable": True},
        facts={"banner": "nginx"},
        provenance=Provenance(step=1, source="tool:x", confidence=Confidence.MEASURED),
        step=1,
    )
    assert attrs_of(props) == {"reachable": True}
    assert facts_of(props) == {"banner": "nginx"}
    assert props["first_seen_step"] == props["last_seen_step"] == 1
    assert props["provenance"][0]["confidence"] == "measured"
    assert props["retractions"] == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("password", "hunter2"),
        ("api_key", "abc"),
        ("note", "4242424242424242"),
        ("SESSION_COOKIE", "x"),
    ],
)
def test_secrets_never_enter_the_graph(key: str, value: str) -> None:
    # The graph is long-lived and gets exported to the Evidence Store, so
    # a leak here cannot be walked back.
    with pytest.raises(SensitiveValueError):
        build_properties(attrs={key: value})
    with pytest.raises(SensitiveValueError):
        build_properties(facts={key: value})


def test_attrs_hold_predicates_not_measurements() -> None:
    with pytest.raises(SensitiveValueError):
        validate_attrs({"latency": 1.5})
    assert validate_attrs({"reachable": True, "port": 22, "banner": "x"})


def test_merge_keeps_true_sticky_and_never_erases_observations() -> None:
    first = build_properties(attrs={"reachable": True}, facts={"ports": [22]}, step=1)
    second = build_properties(
        attrs={"reachable": False}, facts={"ports": [80], "tls": None}, step=3
    )

    merged = merge_properties(first, second)

    # Observed-once stays observed until it is explicitly retracted.
    assert attrs_of(merged)["reachable"] is True
    # Lists union rather than replace; an unobserved (None) field is ignored.
    assert facts_of(merged)["ports"] == [22, 80]
    assert "tls" not in facts_of(merged)
    assert merged["first_seen_step"] == 1
    assert last_seen_step(merged) == 3


def test_merge_replaces_non_bool_scalars() -> None:
    merged = merge_properties(
        build_properties(facts={"banner": "nginx/1.24"}, step=1),
        build_properties(facts={"banner": "nginx/1.25"}, step=2),
    )
    assert facts_of(merged)["banner"] == "nginx/1.25"


def test_merge_deduplicates_provenance() -> None:
    provenance = Provenance(step=1, source="tool:x")
    props = build_properties(provenance=provenance, step=1)
    merged = merge_properties(props, props)
    assert len(merged["provenance"]) == 1


def test_retraction_records_why_and_drops_the_predicate() -> None:
    props = build_properties(attrs={"funded": True, "created": True}, step=1)

    props, changed = apply_retraction(props, "funded", step=5, reason="balance spent")

    assert changed is True
    assert "funded" not in attrs_of(props)
    assert props["retractions"] == [{"step": 5, "key": "funded", "reason": "balance spent"}]


def test_retracting_an_absent_predicate_changes_nothing() -> None:
    props = build_properties(attrs={"created": True}, step=1)
    unchanged, changed = apply_retraction(props, "funded", step=2, reason="n/a")
    assert changed is False
    assert unchanged is props


def test_retraction_requires_a_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        apply_retraction(build_properties(attrs={"a": True}), "a", step=1, reason="")


def test_staleness_uses_last_seen_step() -> None:
    props = build_properties(step=2)
    assert is_stale(props, current_step=9, max_age=5) is True
    assert is_stale(props, current_step=6, max_age=5) is False
    assert is_stale(props, current_step=99, max_age=0) is False


def test_predicate_flags_render_only_true_predicates() -> None:
    props = build_properties(attrs={"reachable": True, "tls": False, "probed": True})
    assert predicate_flags(props) == "+probed+reachable"


def test_reserved_keys_cannot_be_smuggled_in_as_extras() -> None:
    with pytest.raises(ValueError, match="reserved"):
        build_properties(extra={"attrs": {"x": True}})
