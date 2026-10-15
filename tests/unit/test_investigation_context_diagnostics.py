"""Owned ContextQuery rule codes: typed codes, fixed guidance, caller locations, no input copy."""

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from redteam.orchestration.investigation_context import (
    CONTEXT_RULE_GUIDANCE,
    ContextQuery,
    ContextReadError,
    ContextRef,
    context_rule_diagnostic,
)

MARK = "SECRET-CONTEXT-MARK"
RULE_CASES = [
    pytest.param({"mode": "related"}, "context_relation_root_required", id="related-no-root"),
    pytest.param({"mode": "list", "refs": [{"kind": "task", "identity": "t-1"}]},
                 "context_refs_require_related_or_read", id="list-with-root"),
    pytest.param({"mode": "read", "kind": "task"},
                 "context_read_needs_one_matching_reference", id="read-no-root"),
    pytest.param({"mode": "read", "kind": "task", "refs": [{"kind": "node", "identity": "n"}]},
                 "context_read_needs_one_matching_reference", id="read-kind-mismatch"),
    pytest.param({"mode": "list", "section": "result"},
                 "context_section_requires_read", id="section-without-read"),
    pytest.param({"mode": "read", "kind": "candidate", "section": "result",
                  "refs": [{"kind": "candidate", "identity": "c-1"}]},
                 "context_task_section_required", id="task-section-on-candidate"),
    pytest.param({"mode": "read", "kind": "task", "section": "properties",
                  "refs": [{"kind": "task", "identity": "t-1"}]},
                 "context_graph_section_required", id="graph-section-on-task"),
    pytest.param({"statuses": [MARK]}, "context_status_invalid", id="status-unknown"),
    pytest.param({"target_node_ids": [f"{MARK}\x00"]}, "context_selector_invalid",
                 id="selector-control"),
    pytest.param({"hypothesis_revisions": {MARK: -1}}, "context_revision_invalid",
                 id="revision-negative"),
    pytest.param({"mode": "related", "refs": [{"kind": "task", "identity": f"{MARK}\x00"}]},
                 "context_identity_invalid", id="identity-control"),
]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context: ContextQuery | None = None


class Caller(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_request: Request | None = None


def test_owned_codes_are_exactly_the_ten_rules_with_static_templates():
    assert set(CONTEXT_RULE_GUIDANCE) == {case.values[1] for case in RULE_CASES}
    assert len(CONTEXT_RULE_GUIDANCE) == 10
    for summary in CONTEXT_RULE_GUIDANCE.values():
        assert "{" not in summary and "}" not in summary and MARK not in summary


@pytest.mark.parametrize(("fields", "code"), RULE_CASES)
def test_query_rule_is_typed_and_copies_no_input(fields, code):
    with pytest.raises(ValidationError) as failed:
        ContextQuery.model_validate(fields)
    errors = failed.value.errors(include_input=False, include_url=False, include_context=False)
    assert [error["type"] for error in errors] == [code]
    assert errors[0]["msg"] == CONTEXT_RULE_GUIDANCE[code]
    assert MARK not in repr(errors) and MARK not in str(failed.value)


def test_standalone_ref_identity_is_typed_and_rejects_blank_or_control_values():
    for identity in (f"{MARK}\x1f", "   ", f"{MARK}\x00"):
        with pytest.raises(ValidationError) as failed:
            ContextRef(kind="task", identity=identity)
        assert failed.value.errors()[0]["type"] == "context_identity_invalid"
        assert MARK not in repr(failed.value.errors(include_input=False))


@pytest.mark.parametrize(("fields", "code"), RULE_CASES)
def test_helper_publishes_code_fixed_summary_and_caller_location_only(fields, code):
    with pytest.raises(ValidationError) as failed:
        Caller.model_validate({"context_request": {"context": fields}})
    diagnostic = context_rule_diagnostic(failed.value, stage="hypothesis_planning",
                                         schema=Caller.model_json_schema())
    assert diagnostic is not None
    assert diagnostic.code == code and diagnostic.kind == "format"
    assert diagnostic.summary == CONTEXT_RULE_GUIDANCE[code]
    assert diagnostic.cause_scope == "model_input" and diagnostic.retryable is True
    assert diagnostic.error_type == "ValidationError"
    assert diagnostic.validation_issues and all(
        issue["rule"] == code for issue in diagnostic.validation_issues)
    location = diagnostic.location
    assert location.startswith("$.context_request.context")
    if code == "context_identity_invalid":
        assert location == "$.context_request.context.refs[0].identity"
    else:
        assert location == "$.context_request.context"
    published = diagnostic.model_dump_json()
    assert MARK not in published


def test_helper_leaves_unrelated_failures_to_their_existing_owners():
    with pytest.raises(ValidationError) as count:
        ContextQuery.model_validate({"count": 0})
    with pytest.raises(ValidationError) as extra:
        ContextQuery.model_validate({MARK: "x"})
    for failure in (count.value, extra.value):
        assert context_rule_diagnostic(failure, stage="hypothesis_planning") is None
    assert context_rule_diagnostic(ValueError(MARK), stage="hypothesis_planning") is None
    assert context_rule_diagnostic(
        ContextReadError("context_reference_not_found"), stage="hypothesis_planning") is None
