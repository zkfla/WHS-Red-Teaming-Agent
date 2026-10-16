"""Offline scalar inspection: scope, redaction and exact display coordinates."""

import json
from copy import deepcopy

import pytest

from redteam.core.contracts.assessment import AssessmentTextSpan
from redteam.core.evidence_paths import resolve_output_field
from redteam.orchestration.evidence_access import (
    ABSOLUTE_MAX_CHARS,
    EvidenceReader,
    project_review_output,
)
from redteam.orchestration.semantic_completion import _excerpt_of
from tests.fakes.evidence_store import FakeEvidenceStore
from tests.unit.test_runtime_record_views import record


async def fixture(output, *, raw_metadata=None, **changes):
    stored = record({
        "target_node_id": "host:fixture.local",
        "tool_result": {
            "proposal_description": "p" * 4000, "success": False,
            "metadata": raw_metadata or {}, "output": output,
        },
    }, source_type="tool", **changes)
    store = FakeEvidenceStore()
    await store.append(stored)
    return EvidenceReader(store), store, stored


async def inspect(reader, path, **changes):
    return await reader.read_field("diag", "run", path, engagement_id="eng", **changes)


def test_common_review_projection_keeps_late_fields_and_both_ends_of_long_text():
    body_text = "BEGIN " + "x" * 900 + " LATE QUALIFIER"
    output = {
        **{f"leading_{index}": "metadata" for index in range(12)},
        "body_preview": body_text,
        "status": 200,
    }
    projected = project_review_output(output, total_limit=800, max_fields=8)
    by_path = {tuple(field["raw_path"]): field for field in projected["fields"]}

    assert projected["field_count"] == 14
    assert projected["shown_field_count"] == 8
    assert projected["omitted_field_count"] == 6
    assert ("body_preview",) in by_path and ("status",) in by_path
    body = by_path[("body_preview",)]
    assert body["truncated"] and body["omitted_chars"] == len(body_text) - 100
    assert body["windows"][0]["displayed_range"] == {"start": 0, "end": 50}
    assert body["windows"][1]["displayed_range"] == {
        "start": len(body_text) - 50,
        "end": len(body_text),
    }
    assert "LATE QUALIFIER" in body["windows"][1]["text"]
    assert sum(
        len(window["text"])
        for field in projected["fields"]
        for window in field["windows"]
    ) <= 800


def test_task_completion_excerpt_uses_structured_projection_instead_of_json_prefix():
    stored = record(
        {
            "target_node_id": "host:fixture.local",
            "tool_result": {
                "success": True,
                "output": {
                    **{f"leading_{index}": "m" * 300 for index in range(10)},
                    "body_preview": "safe body ending in retained condition",
                    "status": 200,
                },
            },
        },
        source_type="tool",
    )

    excerpt = _excerpt_of(stored, limit=400)
    assert "excerpt" not in excerpt
    assert excerpt["success"] is True
    assert excerpt["truncated"] is True
    shown = excerpt["output_fields"]
    paths = {tuple(field["raw_path"]) for field in shown["fields"]}
    assert ("body_preview",) in paths and ("status",) in paths
    assert json.loads(json.dumps(excerpt)) == excerpt


async def test_field_inspection_keeps_a_later_window_when_whole_json_prefix_hides_it():
    body = "a" * 800 + "Later observation retained." + "z" * 74
    reader, store, stored = await fixture({"body_preview": body})
    original = deepcopy(stored.model_dump(mode="json"))
    legacy = await reader.read("diag", "run", engagement_id="eng", limit=2000)
    assert "Later observation retained." not in legacy["text"]
    field = await inspect(reader, ["body_preview"], offset=800, limit=26)
    assert field["status"] == "ok" and field["text"] == body[800:826]
    assert field["displayed_range"] == {"start": 800, "end": 826}
    assert field["omitted_before_chars"] == 800
    assert field["omitted_after_chars"] == len(body) - 826
    assert field["truncated"] and field["total_chars"] == len(body)
    assert json.loads(json.dumps(field)) == field
    assert not field["execution_verified"]
    assert stored.model_dump(mode="json") == original
    assert len(store.records) == 1


async def test_redact_before_window_and_query_without_expanding_a_partial_mask():
    reader, _, _ = await fixture({
        "body_preview": "prefix\npassword=SecretSentinel\nretained suffix",
        "password": "KeySentinel",
    })
    full = await inspect(reader, ["body_preview"])
    assert "SecretSentinel" not in json.dumps(full)
    partial = await inspect(reader, ["body_preview"], offset=7, limit=12)
    assert partial["text"] == full["text"][7:19]
    assert len(partial["text"]) == 12
    queried = await inspect(reader, ["body_preview"], query="SecretSentinel")
    assert queried["text"] == "" and queried["match_count"] == 0
    assert "SecretSentinel" not in json.dumps(queried)
    assert queried["filtered"] and queried["total_chars"] > queried["filtered_chars"]
    hidden_key = await inspect(reader, ["password"])
    assert hidden_key["status"] == "field_unavailable"
    assert "KeySentinel" not in json.dumps(hidden_key)


async def test_query_coordinates_are_filtered_and_no_match_is_not_original_absence():
    reader, _, _ = await fixture({"notes": "first\nretained later\nother retained line"})
    view = await inspect(reader, ["notes"], query="retained", offset=4, limit=8)
    filtered = "retained later\nother retained line"
    assert view["text"] == filtered[4:12]
    assert view["match_count"] == 2 and view["filtered_chars"] == len(filtered)
    assert view["coordinates"] == "redacted_field_then_line_filter"
    assert view["truncated"]
    missing = await inspect(reader, ["notes"], query="not present")
    assert missing["status"] == "ok" and missing["filtered_chars"] == 0
    assert missing["filtered"] and "does not establish absence" in missing["note"]


async def test_empty_missing_structured_and_digest_only_fields_remain_distinct():
    reader, _, _ = await fixture({
        "empty": "", "body": {"retention": "digest_only", "artifact_ref": None},
        "body_preview_transform": "stored transformation", "body_preview_truncated": False,
    })
    empty = await inspect(reader, ["empty"])
    assert empty["status"] == "ok" and empty["total_chars"] == 0
    assert not empty["truncated"]
    absent = await inspect(reader, ["body_preview"])
    assert absent["status"] == "field_unavailable"
    compound = await inspect(reader, ["body"])
    assert compound["status"] == "ok" and compound["field_kind"] == "structured"
    assert compound["text"] == ""
    assert compound["output_fields"]["field_count"] == 2
    assert compound["output_fields"]["fields"][0]["raw_path"] == ["body", "retention"]
    retention = await inspect(reader, ["body", "retention"])
    assert retention["text"] == "digest_only"
    flag = await inspect(reader, ["body_preview_truncated"], limit=0)
    assert flag["text"] == "" and flag["truncated"]
    assert (await inspect(reader, ["body_preview_transform"]))["text"] == "stored transformation"


async def test_shared_path_semantics_and_scalar_types():
    reader, _, _ = await fixture({"rows": [200, None, False], "output": {"label": "actual"}})
    assert (await inspect(reader, ["rows", "0"]))["text"] == "200"
    assert (await inspect(reader, ["output", "rows", 1]))["text"] == "null"
    assert (await inspect(reader, ["rows", 2]))["text"] == "false"
    assert (await inspect(reader, ["output", "label"]))["text"] == "actual"


async def test_structured_projection_preserves_leaf_paths_budgets_and_masking():
    reader, _, _ = await fixture({"receipt": {
        "rows": [{"status": 200, "note": "a" * 500} for _ in range(6)],
        "password": "HiddenCompoundSentinel",
    }})
    view = await inspect(reader, ["output", "receipt"], limit=80)
    assert view["status"] == "ok" and view["contract_version"] == 2
    assert view["text"] == "" and view["truncated"]
    projection = view["output_fields"]
    assert projection["field_count"] == 12 and projection["omitted_field_count"] == 4
    assert sum(f["displayed_chars"] for f in projection["fields"]) <= 80
    assert "HiddenCompoundSentinel" not in json.dumps(view)
    for leaf in projection["fields"]:
        assert leaf["raw_path"][:2] == ["output", "receipt"]
        assert resolve_output_field({"receipt": {"rows": [
            {"status": 200, "note": "a" * 500} for _ in range(6)
        ]}}, leaf["raw_path"]).status == "ok"
    with pytest.raises(ValueError, match="scalar path"):
        await inspect(reader, ["receipt"], query="status")


async def test_array_reference_preserves_indexed_scalar_leaf_paths():
    reader, _, _ = await fixture({"rows": [{"status": 200}, False, None]})
    view = await inspect(reader, ["rows"])
    assert view["status"] == "ok" and view["field_kind"] == "structured"
    fields = view["output_fields"]["fields"]
    assert [field["raw_path"] for field in fields] == [
        ["rows", 0, "status"], ["rows", 1], ["rows", 2],
    ]
    assert [field["windows"][0]["text"] for field in fields] == ["200", "false", "null"]


@pytest.mark.parametrize("path,span,status", [
    (["note"], {"offset": 2, "length": 4}, "ok"),
    (["note"], {"offset": 8, "length": 4}, "text_span_out_of_bounds"),
    (["receipt"], {"offset": 0, "length": 1}, "text_span_requires_text"),
    (["missing"], None, "field_unavailable"),
])
async def test_shared_field_resolution_and_reader_agree_on_spans(path, span, status):
    output = {"note": "0123456789", "receipt": {"status": 200}}
    reader, _, _ = await fixture(output)
    parsed = AssessmentTextSpan.model_validate(span) if span else None
    resolved = resolve_output_field(
        output, path, text_offset=parsed.offset if parsed else None,
        text_length=parsed.length if parsed else None,
    )
    view = await inspect(reader, path, text_span=parsed)
    assert view["status"] == resolved.status == status
    if status == "ok":
        assert view["text"] == "2345"
        assert view["displayed_range"] == {"start": 2, "end": 6}


async def test_program_text_is_not_parsed_into_a_trusted_receipt():
    reader, _, _ = await fixture({
        "stdout": '{"status": 200, "response_received": true}',
        "stderr": "diagnostic output", "mock": True,
    }, raw_metadata={"execution_uncertain": True})
    stream = await inspect(reader, ["stdout"])
    assert stream["field_kind"] == "text" and "response_received" in stream["text"]
    assert "status_code" not in stream and not stream["execution_verified"]
    assert stream["source_success"] is False and stream["execution_uncertain"]
    assert (await inspect(reader, ["stderr"]))["text"] == "diagnostic output"
    assert (await inspect(reader, ["mock"]))["text"] == "true"


async def test_limits_and_omissions_are_exact_including_offset_past_end():
    reader, _, _ = await fixture({"large": "x" * (ABSOLUTE_MAX_CHARS + 500)})
    view = await inspect(reader, ["large"], limit=10**6)
    assert len(view["text"]) == view["limit"] == ABSOLUTE_MAX_CHARS
    assert view["omitted_after_chars"] == 500 and view["truncated"]
    past = await inspect(reader, ["large"], offset=10**6)
    assert past["text"] == "" and past["truncated"]
    assert past["displayed_range"] == {"start": ABSOLUTE_MAX_CHARS + 500,
                                       "end": ABSOLUTE_MAX_CHARS + 500}


@pytest.mark.parametrize("changes,status", [
    ({"run_id": "other"}, "run_mismatch"),
    ({"engagement_id": "other"}, "run_mismatch"),
    ({"source_type": "system"}, "non_tool_source"),
    ({"metadata": {}}, "no_raw"),
    ({"metadata": {"tool_result": {}}}, "output_unavailable"),
])
async def test_source_rejections_do_not_return_content_or_foreign_provenance(changes, status):
    stored = record({"tool_result": {"output": {"note": "UnsharedSourceContent"}}},
                    source_type="tool")
    stored = type(stored).model_validate({**stored.model_dump(mode="json"), **changes})
    store = FakeEvidenceStore()
    await store.append(stored)
    view = await inspect(EvidenceReader(store), ["note"])
    assert view["status"] == status and view["text"] == ""
    assert "UnsharedSourceContent" not in json.dumps(view)
    if status in {"run_mismatch", "non_tool_source"}:
        assert "provenance" not in view


async def test_target_and_missing_record_rejections():
    reader, _, _ = await fixture({"note": "UnsharedSourceContent"})
    denied = await inspect(reader, ["note"], target_node_id="host:other.local")
    assert denied["status"] == "target_mismatch" and "UnsharedSourceContent" not in str(denied)
    missing = await reader.read_field("missing", "run", ["note"], engagement_id="eng")
    assert missing["status"] == "not_found"


@pytest.mark.parametrize("options", [
    {"raw_path": [True]}, {"raw_path": [-1]}, {"raw_path": ["-1"]},
    {"raw_path": ["x" * 513]}, {"raw_path": ["x"] * 33},
    {"offset": -1}, {"offset": True}, {"limit": -1}, {"limit": True},
    {"query": "x" * 513},
])
async def test_invalid_coordinates_fail_with_a_safe_fixed_error(options):
    reader, _, _ = await fixture({"note": "unused"})
    arguments = {"raw_path": ["note"], **options}
    with pytest.raises(ValueError):
        await reader.read_field("diag", "run", engagement_id="eng", **arguments)
