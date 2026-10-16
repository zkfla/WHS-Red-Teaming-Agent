"""Card-first discovery over the single source per kind (TASK-004).

Per-kind card projection, shared filter semantics, the six discovery
handlers over stub sources, the trusted workroom subject (caller input
never becomes the workroom key), registry-plumbed contributions,
advertised-vs-bindings planning, and a two-turn model proof: cards
first, full schema only after a detail lookup.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import ToolId
from redteam.orchestration.local_tools import LocalToolContext, LocalToolRegistry
from redteam.orchestration.methodology import ReferenceEntry, ReferenceLibrary
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.tool_discovery import (
    PROGRAM_DESCRIBE_ID,
    PROGRAM_LIST_ID,
    REFERENCE_DESCRIBE_ID,
    REFERENCE_LIST_ID,
    TOOL_GET_DETAIL_ID,
    TOOL_LIST_CARDS_ID,
    build_native_bindings,
    detail_fetched_tool_ids,
    discovery_cards_for_context,
    discovery_tool_contributions,
    local_helper_card,
    program_card,
    program_describe_handler,
    program_error_code,
    program_list_handler,
    reference_card,
    reference_describe_handler,
    reference_list_handler,
    select_advertised_tool_ids,
    snapshot_cards,
    tool_get_detail_handler,
    tool_list_cards_handler,
)
from tests.fakes import DummyToolGateway

NEEDLE = "needle-xyz-discovery-probe"
INVENTORY_TOOL = "recon.inventory"


def _mcp_tool(tool_id, tool_name, *, capability, description="", operations=()):
    return ToolDescriptor(
        tool_id=ToolId(tool_id),
        server_id="server-" + tool_name,
        tool_name=tool_name,
        description=description,
        capability_tags=[capability],
        operations=list(operations),
    )


def _inventory_descriptor():
    return _mcp_tool(
        INVENTORY_TOOL,
        "inventory",
        capability="recon",
        description="Inventory probe.",
        operations=["service_discovery"],
    )


def _row(name, *, executable="", version="", provenance="", scope="image"):
    binary = executable or ("/usr/bin/" + name)
    return SimpleNamespace(
        name=name,
        executable=binary,
        version=version,
        provenance=provenance,
        owner_scope=scope,
    )


class StubPrograms:
    """Structural stand-in for the TASK-003 provider (no shared import)."""

    def __init__(self, image=(), instance=(), source_id="shell-workroom"):
        self._image = list(image)
        self._instance = list(instance)
        self.source_id = source_id
        self.seen_subjects: list[tuple[str, str]] = []

    async def list_image_programs(self, manifest_path=None):
        return list(self._image)

    async def list_instance_programs(self, *, run_id, instance_id, workroom_key=None):
        self.seen_subjects.append((run_id, instance_id))
        return list(self._instance)


def _reference_entry():
    return ReferenceEntry(
        reference_id="web-basics",
        path="web/basics.md",
        sha256="ab" * 32,
        title="Web Basics",
        domains=("recon",),
        source_id="src1",
        source_commit="a" * 40,
        review_caveat="Reviewed for reference only.",
    )


def _library():
    return ReferenceLibrary(
        reader_tool_id=ToolId("analysis.read_file"),
        asset_ref="asset1",
        engagement_id="eng1",
        run_id="run1",
        entries=(_reference_entry(),),
    )


def _deps(**overrides):
    base: dict[str, Any] = {
        "tool_catalog": DummyToolGateway(tools=[_inventory_descriptor()]),
        "tool_gateway": None,
        "local_tools": LocalToolRegistry(),
        "methodology_library": _library(),
        "shell_programs": StubPrograms(
            image=[_row("nmap", executable="/usr/bin/nmap", version="7.94", provenance="kali")],
            instance=[_row(
                "mytool",
                executable="/workroom/.local/bin/mytool",
                version="0.1",
                provenance="/workroom/.local",
                scope="instance",
            )],
        ),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _ctx(deps, *, run_id="run_cards1", instance_id="i1"):
    runtime = SimpleNamespace(run_id=run_id, instance_id=instance_id)
    return LocalToolContext(deps=deps, runtime=runtime)


def test_local_helper_card_is_callable():
    from redteam.orchestration.tool_discovery import tool_list_cards_descriptor

    descriptor = tool_list_cards_descriptor()
    card = local_helper_card(descriptor)
    assert card.kind == "local_helper"
    assert card.owner_scope == "local"
    assert card.server_id == "local"
    assert card.callable is True
    assert card.tool_name == TOOL_LIST_CARDS_ID
    assert card.health_status == ToolHealthStatus.HEALTHY


def test_program_card_keeps_provider_scope_verbatim():
    image = program_card(
        name="nmap",
        executable="/usr/bin/nmap",
        version="7.94",
        provenance="kali",
        owner_scope="image",
        source_id="shell-workroom",
    )
    assert image.kind == "program"
    assert image.callable is False
    assert image.server_id == "shell-workroom"
    assert image.tool_id.startswith("program.image.nmap.")
    twin = program_card(
        name="nmap",
        executable="/usr/local/bin/nmap",
        version="7.94",
        provenance="kali",
        owner_scope="image",
        source_id="shell-workroom",
    )
    assert twin.tool_id.startswith("program.image.nmap.")
    assert twin.tool_id != image.tool_id
    assert image.owner_scope == "image"
    assert "7.94" in image.title
    assert "/usr/bin/nmap" in image.summary
    assert image.health_status == ToolHealthStatus.UNKNOWN
    inst = program_card(
        name="mytool",
        executable="",
        version=None,
        provenance=None,
        owner_scope="instance",
        source_id="shell-workroom",
    )
    assert inst.owner_scope == "instance"
    assert inst.tool_id.startswith("program.instance.mytool.")
    assert inst.callable is False


def test_reference_card_is_metadata_only():
    card = reference_card(_reference_entry())
    assert card.kind == "reference"
    assert card.owner_scope == "reference"
    assert card.callable is False
    assert card.tool_id == "reference.web-basics"
    assert card.server_id == "src1"


def test_query_reads_full_fields_beyond_truncation():
    from redteam.orchestration.local_tools import LocalToolDescriptor

    long_description = "x" * 300 + " " + NEEDLE
    helper = LocalToolDescriptor(tool_id="local.probe", description=long_description)
    cards = snapshot_cards(
        [local_helper_card(helper)],
        [_row("probe").name + " " + long_description],
        query=NEEDLE,
    )
    assert [str(card.tool_id) for card in cards] == ["local.probe"]
    assert NEEDLE not in cards[0].summary
    program_hits = snapshot_cards(
        [
            program_card(
                name="dig",
                executable="/usr/bin/dig",
                version="9.18",
                provenance="kali",
                owner_scope="image",
                source_id="shell-workroom",
            )
        ],
        ["dig /usr/bin/dig 9.18 kali image " + NEEDLE],
        query=NEEDLE,
    )
    assert [card.tool_name for card in program_hits] == ["dig"]


def test_unknown_kind_raises():
    import pytest

    with pytest.raises(ValueError):
        snapshot_cards([], [], kind="mcp-tools")


def test_capability_filter_keeps_mcp_vocabulary_exact():
    from redteam.orchestration.local_tools import LocalToolDescriptor

    helper = LocalToolDescriptor(tool_id="local.probe", description="probe")
    cards = snapshot_cards(
        [local_helper_card(helper)],
        ["local.probe probe"],
        capability="recon",
    )
    assert cards == []
    view_cards = DummyToolGateway(tools=[_inventory_descriptor()]).list_cards(capability="recon")
    assert [str(card.tool_id) for card in view_cards] == [INVENTORY_TOOL]


def test_manifest_path_comes_from_owner_single_source():
    from redteam.capability.shell_workroom import (
        PROGRAM_MANIFEST_FILENAME,
        default_program_manifest_path,
    )

    assert PROGRAM_MANIFEST_FILENAME == "program-manifest.json"
    assert default_program_manifest_path().replace(chr(92), "/").endswith(
        "deploy/docker/shell-workroom/program-manifest.json"
    )


async def test_tool_list_merges_all_kinds():
    deps = _deps()
    for contribution in discovery_tool_contributions():
        descriptor, handler = contribution.factory(object())
        deps.local_tools.register(descriptor, handler)
    result = await tool_list_cards_handler({}, _ctx(deps))
    assert result.ok is True
    kinds = {card["kind"] for card in result.payload["cards"]}
    assert kinds == {"mcp_tool", "local_helper", "program", "reference"}
    by_key = {(card["tool_name"], card["kind"], card["owner_scope"]): card
              for card in result.payload["cards"]}
    assert by_key[("inventory", "mcp_tool", "mcp")]["callable"] is True
    assert by_key[("nmap", "program", "image")]["callable"] is False
    assert by_key[("nmap", "program", "image")]["tool_id"].startswith("program.image.nmap.")
    assert by_key[("mytool", "program", "instance")]["owner_scope"] == "instance"
    assert by_key[("web-basics", "reference", "reference")]["callable"] is False
    assert result.payload["truncated"] is False


async def test_tool_list_kind_filter_and_unknown_kind():
    deps = _deps()
    result = await tool_list_cards_handler({"kind": "program"}, _ctx(deps))
    assert result.ok is True
    assert {card["kind"] for card in result.payload["cards"]} == {"program"}
    bad = await tool_list_cards_handler({"kind": "mcp-tools"}, _ctx(deps))
    assert bad.ok is False


async def test_tool_get_detail_serves_local_and_catalog():
    deps = _deps()
    local_id = "local.probe"
    from redteam.orchestration.local_tools import LocalToolDescriptor

    async def probe_handler(tool_input, context):
        from redteam.orchestration.local_tools import LocalToolResult

        return LocalToolResult(ok=True, payload={})

    deps.local_tools.register(
        LocalToolDescriptor(tool_id=local_id, description="Probe helper."), probe_handler
    )
    found = await tool_get_detail_handler({"tool_id": local_id}, _ctx(deps))
    assert found.ok is True
    assert found.payload["card"]["kind"] == "local_helper"
    mcp = await tool_get_detail_handler({"tool_id": INVENTORY_TOOL}, _ctx(deps))
    assert mcp.ok is True
    assert mcp.payload["operations"] == ["service_discovery"]
    missing = await tool_get_detail_handler({"tool_id": "nope.missing"}, _ctx(deps))
    assert missing.ok is False
    blank = await tool_get_detail_handler({}, _ctx(deps))
    assert blank.ok is False


async def test_program_list_reports_status_when_unwired():
    deps = _deps(shell_programs=None)
    result = await program_list_handler({}, _ctx(deps))
    assert result.ok is True
    assert result.payload["cards"] == []
    assert "program_manifest_status" in result.payload
    assert "workroom_status" in result.payload


async def test_program_list_trusts_run_subject_not_caller():
    deps = _deps()
    result = await program_list_handler({"query": "nmap"}, _ctx(deps))
    assert result.ok is True
    names = {card["tool_name"] for card in result.payload["cards"]}
    assert names == {"nmap"}
    assert result.payload["program_manifest_status"] == "published"
    assert result.payload["workroom_status"] == "listed"
    provider = deps.shell_programs
    assert provider.seen_subjects == [("run_cards1", "i1")]
    from redteam.orchestration.tool_discovery import program_list_descriptor

    schema = program_list_descriptor().input_schema
    assert "workroom_key" not in schema["properties"]


async def test_program_list_without_subject_omits_instance_rows():
    deps = _deps()
    result = await program_list_handler({}, _ctx(deps, instance_id=None))
    assert result.ok is True
    scopes = {card["owner_scope"] for card in result.payload["cards"]}
    assert scopes == {"image"}
    assert deps.shell_programs.seen_subjects == []
    assert result.payload["workroom_status"] != "listed"


async def test_program_describe_roundtrip_and_unknown():
    deps = _deps()
    found = await program_describe_handler({"name": "mytool"}, _ctx(deps))
    assert found.ok is True
    assert found.payload["owner_scope"] == "instance"
    assert found.payload["version"] == "0.1"
    assert found.payload["executable"] == "/workroom/.local/bin/mytool"
    assert found.payload["provenance"] == "/workroom/.local"
    assert "run_with" in found.payload
    missing = await program_describe_handler({"name": "nope"}, _ctx(deps))
    assert missing.ok is False


async def test_reference_describe_is_metadata_only():
    deps = _deps()
    found = await reference_describe_handler({"reference_id": "web-basics"}, _ctx(deps))
    assert found.ok is True
    assert "content" not in found.payload
    assert found.payload["trust"] == "untrusted_reference"
    assert found.payload["execution_permission"] is False
    assert found.payload["read_with"]["reader_tool_id"] == "analysis.read_file"
    listed = await reference_list_handler({"domain": "recon"}, _ctx(deps))
    assert listed.ok is True
    assert [card["tool_id"] for card in listed.payload["cards"]] == ["reference.web-basics"]
    empty = await reference_list_handler({"domain": "web"}, _ctx(deps))
    assert empty.payload["cards"] == []
    missing = await reference_describe_handler({"reference_id": "nope"}, _ctx(deps))
    assert missing.ok is False


async def test_contributions_plumb_through_local_registry():
    registry = LocalToolRegistry()
    for contribution in discovery_tool_contributions():
        descriptor, handler = contribution.factory(object())
        registry.register(descriptor, handler)
    assert len(registry.descriptors()) == 6
    context = _ctx(_deps())
    listed = await registry.call(TOOL_LIST_CARDS_ID, {}, context)
    assert listed.ok is True
    assert listed.payload["total"] >= 4
    detail = await registry.call(TOOL_GET_DETAIL_ID, {"tool_id": INVENTORY_TOOL}, context)
    assert detail.ok is True
    programs = await registry.call(PROGRAM_LIST_ID, {}, context)
    assert programs.ok is True
    described = await registry.call(PROGRAM_DESCRIBE_ID, {"name": "nmap"}, context)
    assert described.ok is True
    refs = await registry.call(REFERENCE_LIST_ID, {}, context)
    assert refs.ok is True
    ref_detail = await registry.call(
        REFERENCE_DESCRIBE_ID, {"reference_id": "web-basics"}, context
    )
    assert ref_detail.ok is True


def test_bindings_cover_every_eligible_name():
    bindings = build_native_bindings(
        catalog_tool_ids=[INVENTORY_TOOL, "web.other"],
        local_tool_ids=[TOOL_LIST_CARDS_ID],
    )
    assert bindings[exposed_tool_name(INVENTORY_TOOL)] == INVENTORY_TOOL
    assert bindings[INVENTORY_TOOL] == INVENTORY_TOOL
    assert bindings[exposed_tool_name("web.other")] == "web.other"
    assert bindings[exposed_tool_name(TOOL_LIST_CARDS_ID)] == TOOL_LIST_CARDS_ID
    assert bindings[TOOL_LIST_CARDS_ID] == TOOL_LIST_CARDS_ID
    assert "unselected.tool" not in bindings


def test_native_binding_alias_collisions_fail_closed(monkeypatch):
    monkeypatch.setattr(
        "redteam.orchestration.tool_discovery.exposed_tool_name",
        lambda _identity: "shared_alias",
    )
    with pytest.raises(ValueError, match="^native_tool_binding_collision$"):
        build_native_bindings(
            catalog_tool_ids=["catalog.one", "catalog.two"], local_tool_ids=[],
        )


def test_native_binding_alias_cannot_shadow_another_canonical_id(monkeypatch):
    monkeypatch.setattr(
        "redteam.orchestration.tool_discovery.exposed_tool_name",
        lambda identity: "catalog.two" if identity == "catalog.one" else "alias_two",
    )
    with pytest.raises(ValueError, match="^native_tool_binding_collision$"):
        build_native_bindings(
            catalog_tool_ids=["catalog.one", "catalog.two"], local_tool_ids=[],
        )


def test_raw_detail_call_is_resolved_and_only_eligible_tool_is_advertised():
    bindings = {
        exposed_tool_name(INVENTORY_TOOL): INVENTORY_TOOL,
        exposed_tool_name("catalog.unselected"): "catalog.unselected",
        exposed_tool_name(TOOL_GET_DETAIL_ID): TOOL_GET_DETAIL_ID,
    }
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "raw-detail",
                "type": "function",
                "function": {
                    "name": TOOL_GET_DETAIL_ID,
                    "arguments": json.dumps({"tool_id": INVENTORY_TOOL}),
                },
            }],
        },
        {"role": "tool", "tool_call_id": "raw-detail", "content": json.dumps({"ok": True})},
    ]

    fetched = detail_fetched_tool_ids(messages, bindings=bindings)
    assert fetched == [INVENTORY_TOOL]
    assert select_advertised_tool_ids(
        eligible_ids=[INVENTORY_TOOL], fetched_ids=fetched,
    ) == [INVENTORY_TOOL]
    assert select_advertised_tool_ids(
        eligible_ids=["catalog.unselected"], fetched_ids=fetched,
    ) == []


def test_detail_fetched_tracks_successful_lookups_only():
    detail_name = exposed_tool_name(TOOL_GET_DETAIL_ID)
    bindings = {detail_name: TOOL_GET_DETAIL_ID, exposed_tool_name("other"): "other"}
    good_args = json.dumps({"tool_id": INVENTORY_TOOL})
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": detail_name, "arguments": good_args},
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": detail_name, "arguments": good_args},
                },
                {
                    "id": "c3",
                    "type": "function",
                    "function": {"name": "unrelated", "arguments": good_args},
                },
                {
                    "id": "c4",
                    "type": "function",
                    "function": {"name": detail_name, "arguments": "not-json{"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"ok": True})},
        {"role": "tool", "tool_call_id": "c2", "content": json.dumps({"ok": False})},
        {"role": "tool", "tool_call_id": "c3", "content": json.dumps({"ok": True})},
        {"role": "tool", "tool_call_id": "c4", "content": json.dumps({"ok": True})},
    ]
    assert detail_fetched_tool_ids(messages, bindings=bindings) == [INVENTORY_TOOL]
    assert detail_fetched_tool_ids([], bindings=bindings) == []
    assert detail_fetched_tool_ids(None, bindings=bindings) == []


def test_advertised_is_eligible_fetched_minus_locals():
    assert select_advertised_tool_ids(eligible_ids=["a", "b"], fetched_ids=["b", "c"]) == ["b"]
    first = select_advertised_tool_ids(
        eligible_ids=["a", TOOL_LIST_CARDS_ID],
        fetched_ids=[TOOL_LIST_CARDS_ID],
        local_tool_ids=[TOOL_LIST_CARDS_ID],
    )
    assert first == []
    assert select_advertised_tool_ids(eligible_ids=[], fetched_ids=["a"]) == []


def test_discovery_grants_nothing_and_hides_nothing_registered():
    eligible = [INVENTORY_TOOL]
    bindings = build_native_bindings(catalog_tool_ids=eligible, local_tool_ids=[])
    assert exposed_tool_name(INVENTORY_TOOL) in bindings
    assert select_advertised_tool_ids(eligible_ids=eligible, fetched_ids=[]) == []














async def test_program_list_paginates_past_100_items():
    rows = [
        _row(f"tool{i:03d}", executable=f"/usr/bin/tool{i:03d}", version="1.0", provenance="kali")
        for i in range(250)
    ]
    deps = _deps()
    instance = _row(
        "mytool",
        executable="/workroom/.local/bin/mytool",
        version="0.1",
        provenance="/workroom/.local",
        scope="instance",
    )
    deps.shell_programs = StubPrograms(image=rows, instance=[instance])
    seen: list[str] = []
    offset: int | None = 0
    pages = 0
    total = 0
    while offset is not None:
        result = await program_list_handler({"offset": offset, "limit": 100}, _ctx(deps))
        assert result.ok is True
        payload = result.payload
        total = payload["total"]
        seen.extend(card["tool_id"] for card in payload["cards"])
        pages += 1
        offset = payload["next_offset"]
        assert payload["truncated"] is (offset is not None)
    assert total == 251
    assert pages == 3
    assert len(seen) == 251 and len(set(seen)) == 251
    assert sum(1 for item in seen if item.startswith("program.image.tool")) == 250
    assert sum(1 for item in seen if item.startswith("program.instance.mytool.")) == 1


async def test_list_paging_rejects_out_of_range():
    deps = _deps()
    assert (await program_list_handler({"offset": -1}, _ctx(deps))).ok is False
    assert (await program_list_handler({"limit": 0}, _ctx(deps))).ok is False
    assert (await program_list_handler({"limit": 101}, _ctx(deps))).ok is False
    assert (await tool_list_cards_handler({"limit": 101}, _ctx(deps))).ok is False
    second = await program_list_handler({"offset": 1, "limit": 1}, _ctx(deps))
    assert second.ok is True
    assert second.payload["offset"] == 1
    assert len(second.payload["cards"]) == 1


def test_bootstrap_flag_lives_on_provider_declarations():
    flags = {}
    for contribution in discovery_tool_contributions():
        descriptor, _handler = contribution.factory(object())
        flags[descriptor.tool_id] = descriptor.bootstrap
    assert flags[TOOL_LIST_CARDS_ID] is True
    assert flags[TOOL_GET_DETAIL_ID] is True
    assert flags[PROGRAM_LIST_ID] is False
    assert flags[PROGRAM_DESCRIBE_ID] is False
    assert flags[REFERENCE_LIST_ID] is False
    assert flags[REFERENCE_DESCRIBE_ID] is False
    from redteam.orchestration.local_tools import LocalToolDescriptor

    assert LocalToolDescriptor(tool_id="x", description="y").bootstrap is False






async def test_shadowed_command_keeps_distinct_cards_and_effective_describe():
    image_curl = _row("curl", executable="/usr/bin/curl", version="7.81", provenance="kali")
    instance_curl = _row(
        "curl",
        executable="/workroom/.local/bin/curl",
        version="8.5",
        provenance="/workroom/.local",
        scope="instance",
    )
    deps = _deps()
    deps.shell_programs = StubPrograms(image=[image_curl], instance=[instance_curl])
    listed = await program_list_handler({}, _ctx(deps))
    assert listed.ok is True
    ids = [card["tool_id"] for card in listed.payload["cards"]]
    assert len(ids) == len(set(ids)) == 2
    assert all(item.startswith("program.") and "." in item[len("program."):] for item in ids)
    by_scope = {card["owner_scope"]: card for card in listed.payload["cards"]}
    assert set(by_scope) == {"image", "instance"}
    image = await program_describe_handler({"tool_id": by_scope["image"]["tool_id"]}, _ctx(deps))
    assert image.ok is True
    assert image.payload["executable"] == "/usr/bin/curl"
    assert image.payload["version"] == "7.81"
    assert image.payload["owner_scope"] == "image"
    assert image.payload["resolution"] == "exact"
    inst = await program_describe_handler({"name": "curl", "owner_scope": "instance"}, _ctx(deps))
    assert inst.ok is True
    assert inst.payload["executable"] == "/workroom/.local/bin/curl"
    assert inst.payload["version"] == "8.5"
    effective = await program_describe_handler({"name": "curl"}, _ctx(deps))
    assert effective.ok is True
    assert effective.payload["owner_scope"] == "instance"
    assert effective.payload["executable"] == "/workroom/.local/bin/curl"
    assert effective.payload["resolution"] == "effective-path-preference"
    assert effective.payload["available_scopes"] == ["image", "instance"]
    unknown = await program_describe_handler(
        {"tool_id": "program.image.nope.0123456789ab"}, _ctx(deps)
    )
    assert unknown.ok is False
    missing = await program_describe_handler({}, _ctx(deps))
    assert missing.ok is False


async def test_discovery_context_carries_program_status_on_failure():
    from redteam.capability.shell_workroom import ShellWorkroomError

    class FailingProvider(StubPrograms):
        async def list_image_programs(self, manifest_path=None):
            raise ShellWorkroomError("shell_manifest_unreadable")

    deps = _deps()
    deps.shell_programs = FailingProvider(
        instance=[_row("mytool", executable="/workroom/.local/bin/mytool", scope="instance")]
    )
    cards, extra = await discovery_cards_for_context(
        view=deps.tool_catalog,
        eligible_ids=[INVENTORY_TOOL],
        local_descriptors=[],
        provider=deps.shell_programs,
        runtime=SimpleNamespace(run_id="run_cards1", instance_id="i1"),
        library=None,
        include_live_programs=True,
    )
    assert {card.kind for card in cards} == {"mcp_tool", "program"}
    assert "shell_manifest_unreadable" in extra["program_manifest_status"]
    assert "RuntimeError" not in extra["program_manifest_status"]
    assert extra["workroom_status"] == "listed"
    ids = {str(card.tool_id) for card in cards}
    assert INVENTORY_TOOL in ids
    assert sum(1 for item in ids if item.startswith("program.instance.mytool.")) == 1


async def test_same_scope_duplicate_paths_stay_ambiguous():
    first = _row("curl", executable="/usr/bin/curl", version="7.81", provenance="kali")
    second = _row("curl", executable="/usr/local/bin/curl", version="7.81", provenance="kali")
    deps = _deps()
    deps.shell_programs = StubPrograms(image=[first, second])
    listed = await program_list_handler({}, _ctx(deps))
    assert listed.ok is True
    cards = [card for card in listed.payload["cards"] if card["tool_name"] == "curl"]
    assert len(cards) == 2
    assert cards[0]["tool_id"] != cards[1]["tool_id"]
    seen = set()
    for card in cards:
        found = await program_describe_handler({"tool_id": card["tool_id"]}, _ctx(deps))
        assert found.ok is True
        assert found.payload["resolution"] == "exact"
        seen.add(found.payload["executable"])
    assert seen == {"/usr/bin/curl", "/usr/local/bin/curl"}
    scoped = await program_describe_handler({"name": "curl", "owner_scope": "image"}, _ctx(deps))
    assert scoped.ok is False
    bare = await program_describe_handler({"name": "curl"}, _ctx(deps))
    assert bare.ok is False


async def test_provider_error_codes_surface_verbatim():
    from redteam.capability.shell_workroom import ShellWorkroomError

    class AbsentProvider(StubPrograms):
        async def list_instance_programs(self, *, run_id, instance_id, workroom_key=None):
            raise ShellWorkroomError("shell_workroom_not_running")

    deps = _deps()
    deps.shell_programs = AbsentProvider(image=[_row("nmap", executable="/usr/bin/nmap")])
    result = await program_list_handler({}, _ctx(deps))
    assert result.ok is True
    assert result.payload["workroom_status"] == "not_running"
    assert result.payload["program_manifest_status"] == "published"

    class BrokenProvider(StubPrograms):
        async def list_image_programs(self, manifest_path=None):
            raise ShellWorkroomError("shell_manifest_unreadable")

    deps.shell_programs = BrokenProvider()
    broken = await program_list_handler({}, _ctx(deps))
    assert broken.ok is True
    assert "shell_manifest_unreadable" in broken.payload["program_manifest_status"]


async def test_live_query_runs_only_on_explicit_program_tools():
    deps = _deps()
    provider = deps.shell_programs
    calls: list[str] = []

    async def count_image(manifest_path=None):
        calls.append("image")
        return [
            _row("nmap", executable="/usr/bin/nmap", version="7.94", provenance="kali")
        ]

    async def count_instance(*, run_id, instance_id, workroom_key=None):
        calls.append("instance")
        return []

    provider.list_image_programs = count_image
    provider.list_instance_programs = count_instance
    runtime = SimpleNamespace(run_id="run_cards1", instance_id="i1")
    cards, _extra = await discovery_cards_for_context(
        view=None,
        eligible_ids=[],
        local_descriptors=[],
        provider=provider,
        runtime=runtime,
        library=None,
    )
    assert calls == ["image"]
    assert len(cards) == 1
    assert cards[0].tool_name == "nmap"
    await program_list_handler({}, _ctx(deps))
    assert calls == ["image", "image", "instance"]




def test_error_code_ignores_unvalidated_text():
    class Noisy(Exception):
        pass

    assert program_error_code(Noisy("/tmp/secret-token")) == "Noisy"

    class FakeCode(Exception):
        def __init__(self):
            super().__init__("anything")
            self.reason_code = "shell_workroom_not_running"

    assert program_error_code(FakeCode()) == "shell_workroom_not_running"

    class BadCode(Exception):
        def __init__(self):
            super().__init__("x")
            self.reason_code = "../../etc/passwd"

    assert program_error_code(BadCode()) == "BadCode"

    trailing_newline = FakeCode()
    trailing_newline.reason_code += "\n"
    assert program_error_code(trailing_newline) == "FakeCode"


async def test_custom_provider_source_id_reaches_cards():
    deps = _deps()
    deps.shell_programs = StubPrograms(
        image=[_row("nmap", executable="/usr/bin/nmap")],
        source_id="custom-programs",
    )
    result = await program_list_handler({}, _ctx(deps))
    assert result.ok is True
    assert {card["server_id"] for card in result.payload["cards"]} == {"custom-programs"}
    detail = await program_describe_handler({"name": "nmap"}, _ctx(deps))
    assert detail.ok is True
    assert detail.payload["card"]["server_id"] == "custom-programs"
    assert "shell-workroom" not in {
        card["server_id"] for card in result.payload["cards"]
    }
