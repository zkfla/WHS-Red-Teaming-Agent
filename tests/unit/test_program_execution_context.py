"""Owner metadata and bounded original views; no Docker/model/target calls."""

import json
from types import SimpleNamespace

import pytest

from redteam.capability.gated_window import GatedWindowExtension
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.network import WINDOW_EGRESS_RECORD_KIND, WINDOW_EGRESS_SOURCE_ID
from redteam.core.convert import digest_payload
from redteam.core.enums import EvidenceSourceType
from redteam.core.ports.program_discovery import ProgramStaticMetadata
from redteam.orchestration.analysis import claim_target_is_grounded
from redteam.orchestration.evidence_access import EvidenceReader, project_original_records
from redteam.orchestration.tool_discovery import (
    MAX_PASSIVE_PROGRAM_CARDS,
    discovery_cards_for_context,
    program_describe_handler,
    program_execution_environment,
)
from tests.fakes.evidence_store import FakeEvidenceStore
from tests.unit.test_gated_window import _policy
from tests.unit.test_shell_adapter import _FakeDocker, _manager
from tests.unit.test_shell_evidence_004a import ENGAGEMENT, RUN, _proposal, _record, _result
from tests.unit.test_tool_discovery import StubPrograms, _ctx, _deps, _row


def test_owner_routes_do_not_probe_or_authorize_and_selection_still_wins():
    manager = _manager(_FakeDocker())
    closed = manager.describe_execution_environment()
    assert [route.status for route in closed.routes] == ["local_only", "unavailable", "unavailable"]
    policy = _policy().model_copy(update={"target_ports": {"t": (8080,)}})
    extension = GatedWindowExtension(
        manager=manager, ledger=None, run_docker=None, policy=policy,
        target_route_identity={"connection": "daemon_routed"},
        docker_endpoint={"context": "fixture", "host": "unix:///fixture"},
    )
    manager.set_execution_admission(extension)
    environment = program_execution_environment(manager)
    target = environment["routes"][1]
    assert target["status"] == "target_ready"
    assert target["selectors"] == {"t": {"ips": ["10.0.0.9"], "ports": [8080]}}
    assert "raw_socket" in environment["unsupported_modes"]
    assert not environment["target_execution_verified"]
    assert not environment["execution_permission_granted"]
    selected = program_execution_environment(manager, {"analysis.shell_run"})
    assert selected["routes"][0]["tool_ids"] == ["analysis.shell_run"]
    assert selected["routes"][1]["status"] == "unavailable"
    assert not selected["routes"][1]["tool_ids"]
    extension.target_route_identity = None
    assert manager.describe_execution_environment().routes[1].status == "configured_unverified"
    assert program_execution_environment(SimpleNamespace())["status"] == "unknown"

    capacity = policy.budget.global_capacity.model_copy(update={"max_output_bytes": 0})
    extension.policy = policy.model_copy(update={
        "budget": policy.budget.model_copy(update={"global_capacity": capacity}),
    })
    assert manager.describe_execution_environment().routes[1].reason_code == (
        "network_purpose_budget_disabled"
    )


async def test_large_inventory_is_searchable_without_filling_passive_context():
    provider = StubPrograms(image=[_row(f"p{i:04d}") for i in range(500)])
    deps = _deps(shell_programs=provider)
    cards, status = await discovery_cards_for_context(view=None, eligible_ids=[], provider=provider)
    assert len(cards) == MAX_PASSIVE_PROGRAM_CARDS
    assert status["cards_total"] == status["cards_counts"]["program"] == 500
    assert status["cards_truncated"] and "not an allowlist" in status["cards_guidance"]
    # An executable outside the sample is still discoverable; legacy mode metadata is unknown.
    described = await program_describe_handler({"name": "p0499"}, _ctx(deps))
    assert described.ok and described.payload["executable"] == "/usr/bin/p0499"
    assert described.payload["execution_environment"]["status"] == "unknown"
    assert not (await program_describe_handler({"name": "not-installed"}, _ctx(deps))).ok


async def test_installed_privileged_mode_is_not_reported_as_generally_supported():
    row = _row("tcpdump")
    row.metadata = ProgramStaticMetadata(
        purposes=["local", "privileged"],
        network_families=["ipv4", "ipv6"],
        network_protocols=["packet_capture"],
        required_capabilities=["CAP_NET_RAW"],
        unsupported_modes=["live_capture"],
        unprivileged_alternatives=["read an existing capture (-r)"],
        path_status="effective",
        resolved_path="/usr/bin/tcpdump",
    )
    described = await program_describe_handler(
        {"name": "tcpdump"}, _ctx(_deps(shell_programs=StubPrograms(image=[row])))
    )
    metadata = described.payload["program_metadata"]
    assert metadata["required_capabilities"] == ["CAP_NET_RAW"]
    assert metadata["unsupported_modes"] == ["live_capture"]
    assert "read an existing capture" in metadata["unprivileged_alternatives"][0]
    assert not described.payload["execution_environment"]["execution_permission_granted"]


def companion_for(record):
    payload = {"record_kind": WINDOW_EGRESS_RECORD_KIND, "companion": {
        "source_id": WINDOW_EGRESS_SOURCE_ID, "window_id": "w1", "run_id": str(RUN),
        "instance_id": "instance-1", "epoch": 1,
        "holder": {"action_id": str(record.action_id), "invocation_id": str(record.invocation_id)},
        "scope": [{"ip": "10.0.0.9", "port": 8080, "proto": "tcp"}],
        "admissions": [], "outcome": "unknown",
    }, "logical_target": record.metadata["proposal"]["target"],
        "target_route_identity": {"connection": "daemon_routed"}}
    return EvidenceRecord.model_validate({
        "evidence_id": "ev-gate", "run_id": RUN, "engagement_id": ENGAGEMENT,
        "source_type": EvidenceSourceType.SYSTEM, "source_id": WINDOW_EGRESS_SOURCE_ID,
        "action_id": record.action_id, "invocation_id": record.invocation_id,
        "content_type": "application/json", "digest": digest_payload(payload),
        "summary": "Protected admission fixture", "created_at": record.created_at,
        "metadata": payload,
    })


@pytest.mark.parametrize("case", ["linked", "missing", "wrong_holder", "wrong_run", "digest",
                                  "recovered", "wrong_target"])
async def test_failed_unknown_truncated_original_preserves_both_channels_and_exact_gate(case):
    original = _result(success=False).model_copy(update={
        "output": {**_result(success=False).output, "stdout": "X" * 20000,
                   "stderr": "bounded error", "exit_code": None,
                   "timed_out": True, "truncated": True},
        "metadata": {"execution_uncertain": True, "gated_evidence_id": "ev-gate"},
    })
    record = _record(_proposal(targeted=True), original, True)
    companion = companion_for(record)
    if case == "wrong_holder":
        companion = companion.model_copy(update={"invocation_id": "other"})
    elif case == "wrong_run":
        companion = companion.model_copy(update={"run_id": "other"})
    elif case == "digest":
        companion = companion.model_copy(update={"digest": "wrong"})
    elif case in {"recovered", "wrong_target"}:
        payload = dict(companion.metadata)
        payload.pop("logical_target")
        payload["logical_endpoint"] = {
            "host": "target.local" if case == "recovered" else "another.local", "port": 8080,
        }
        companion = companion.model_copy(update={"metadata": payload,
                                                  "digest": digest_payload(payload)})
    records = [record] + ([] if case == "missing" else [companion])
    store = FakeEvidenceStore()
    for item in records:
        await store.append(item)
    view = await EvidenceReader(store).read(
        str(record.evidence_id), str(RUN), engagement_id=str(ENGAGEMENT), limit=500,
    )
    assert view["execution"]["success"] is False
    assert view["execution"]["execution_uncertain"]
    assert view["execution"]["accounting"]["timed_out"]
    assert view["execution"]["accounting"]["truncated"]
    assert view["program_output"]["stderr"] == "bounded error"
    assert view["program_output"]["stdout_truncated"]
    assert len(view["text"]) + sum(len(view["program_output"][key])
                                  for key in ("stdout", "stderr")) <= 500
    linked = case in {"linked", "recovered", "wrong_target"}
    expected = "linked" if linked else "missing" if case == "missing" else "unbound"
    assert view["execution"]["gate_companion"]["status"] == expected
    assert view["execution"]["assertion_level"] == "program_output"
    assert not view["execution"]["target_execution_verified"]
    assert await claim_target_is_grounded(
        None, record, view["execution"]["target_node_id"], evidence_store=store,
    ) is (case in {"linked", "recovered"})
    planner = project_original_records(records, run_id=RUN, engagement_id=ENGAGEMENT,
                                       total_limit=500)
    assert planner["records"][0] == {**view, "task_id": None,
                                     "target_node_id": view["execution"]["target_node_id"]}
    if linked:
        gate = await EvidenceReader(store).read("ev-gate", str(RUN), limit=500)
        assert gate["view_kind"] == "network_admission" and not gate["execution_original"]
        assert view["execution"]["gate_companion"]["outcome"] == "unknown"
    assert record.metadata["tool_result"]["output"]["stdout"] == "X" * 20000
    assert "program assertion" not in json.dumps(record.metadata)  # Original is unchanged.
