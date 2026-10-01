from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from redteam.core.contracts.target import DeviceTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import Verdict
from redteam.core.ids import ActionId, AssetId, InvocationId, ToolId
from redteam.orchestration.observations.mapper import (
    GraphMutation,
    default_result_mapper_registry,
)
from redteam.orchestration.observations.mobile_runtime import (
    map_mobile_crash_detail,
    map_mobile_crash_list,
    map_mobile_device_logs,
)

NOW = datetime.now(timezone.utc)
TARGET = DeviceTarget(
    device_serial="emulator-5554",
    device_ref="redteam_x86",
    package_name="com.android.insecurebankv2",
    asset_id=AssetId("insecurebankv2"),
)


def result(tool_id: str, output: dict | str | None) -> ToolResult:
    return ToolResult(
        invocation_id=InvocationId("inv_mobile"),
        action_id=ActionId("action_mobile"),
        tool_id=ToolId(tool_id),
        success=True,
        output=output,
        started_at=NOW,
        finished_at=NOW,
    )


def test_fatal_package_log_maps_through_expectation_verdict_and_finding() -> None:
    output = "\n".join(
        [
            json.dumps(
                {
                    "process": "com.android.insecurebankv2",
                    "tag": "AndroidRuntime",
                    "level": "E",
                    "message": "FATAL EXCEPTION: main",
                }
            ),
            json.dumps(
                {
                    "process": "com.android.insecurebankv2",
                    "tag": "ActivityTaskManager",
                    "message": "Force finishing activity",
                }
            ),
        ]
    )

    mutation = map_mobile_device_logs(
        target=TARGET,
        arguments={"device": "emulator-5554"},
        result=result("mobile.mobile_get_device_logs", output),
    )

    assert mutation.nodes[0].attrs == {"mobile_runtime_crash_observed": True}
    assert mutation.nodes[0].facts["mobile_log_entry_count"] == 2
    assessment = mutation.nodes[0].facts["mobile_log_assessment"]
    assert assessment["verdict"] == Verdict.VIOLATION.value
    assert assessment["expectation"]["violation_if_met"] is True
    assert len(mutation.findings) == 1
    assert mutation.findings[0].check_id == "android_runtime_crash"


def test_benign_or_empty_logs_record_pass_without_inventing_a_finding() -> None:
    benign = json.dumps(
        {
            "process": "com.android.insecurebankv2",
            "tag": "ActivityTaskManager",
            "message": "Displayed com.android.insecurebankv2/.LoginActivity",
        }
    )

    for output, expected_count in ((benign, 1), ("No log entries received", 0)):
        mutation = map_mobile_device_logs(
            target=TARGET,
            arguments={},
            result=result("mobile.mobile_get_device_logs", output),
        )
        assert mutation.findings == []
        assert mutation.nodes[0].attrs == {}
        assert mutation.nodes[0].facts["mobile_log_entry_count"] == expected_count
        assert (
            mutation.nodes[0].facts["mobile_log_assessment"]["verdict"]
            == Verdict.PASS.value
        )


def test_crash_inventory_opens_only_a_matching_package_followup() -> None:
    output = json.dumps(
        [
            {
                "id": "other-1",
                "packageName": "com.example.other",
                "timestamp": NOW.isoformat(),
            },
            {
                "id": "bank-2",
                "packageName": "com.android.insecurebankv2",
                "timestamp": (NOW - timedelta(seconds=1)).isoformat(),
            },
            {
                "id": "bank-1",
                "packageName": "com.android.insecurebankv2",
                "timestamp": NOW.isoformat(),
            },
            {
                "id": "bank-stale",
                "packageName": "com.android.insecurebankv2",
                "timestamp": (NOW - timedelta(hours=1)).isoformat(),
            },
        ]
    )

    mutation = map_mobile_crash_list(
        target=TARGET,
        arguments={"device": "emulator-5554"},
        result=result("mobile.mobile_list_crashes", output),
    )

    assert mutation.findings == []
    assert mutation.nodes[0].attrs == {"mobile_crash_observed": True}
    assert mutation.nodes[0].facts == {
        "mobile_crash_report_count": 2,
        "mobile_crash_ids": ["bank-1", "bank-2"],
        "crash_id": "bank-1",
    }


def test_crash_detail_confirms_the_same_runtime_finding_as_fatal_logs() -> None:
    logs = map_mobile_device_logs(
        target=TARGET,
        arguments={},
        result=result(
            "mobile.mobile_get_device_logs",
            json.dumps(
                {
                    "process": "com.android.insecurebankv2",
                    "message": "FATAL EXCEPTION: main",
                }
            ),
        ),
    )
    detail = map_mobile_crash_detail(
        target=TARGET,
        arguments={"device": "emulator-5554", "id": "bank-1"},
        result=result(
            "mobile.mobile_get_crash",
            "Process: com.android.insecurebankv2\nFATAL EXCEPTION: main\njava.lang.Error",
        ),
    )

    assert detail.nodes[0].facts["mobile_crash_detail_id"] == "bank-1"
    assert detail.nodes[0].facts["mobile_crash_detail_assessment"]["verdict"] == (
        Verdict.VIOLATION.value
    )
    assert detail.findings[0].finding_id == logs.findings[0].finding_id


def test_mobile_runtime_mappers_fail_closed_for_malformed_or_unrelated_output() -> None:
    assert map_mobile_device_logs(
        target=TARGET,
        arguments={},
        result=result("mobile.mobile_get_device_logs", "not-jsonl"),
    ) == GraphMutation()
    assert map_mobile_crash_list(
        target=TARGET,
        arguments={},
        result=result("mobile.mobile_list_crashes", '{"unknown": []}'),
    ) == GraphMutation()

    unrelated = map_mobile_crash_detail(
        target=TARGET,
        arguments={"id": "other-1"},
        result=result(
            "mobile.mobile_get_crash",
            "Process: com.example.other\nFATAL EXCEPTION: main",
        ),
    )
    assert unrelated.findings == []
    assert unrelated.nodes[0].facts["mobile_crash_detail_assessment"]["verdict"] == (
        Verdict.PASS.value
    )


def test_default_registry_contains_all_mobile_runtime_mappers() -> None:
    registry = default_result_mapper_registry()

    inventory = registry.map_result(
        tool_id=ToolId("mobile.mobile_list_crashes"),
        target=TARGET,
        arguments={},
        result=result("mobile.mobile_list_crashes", "[]"),
    )
    logs = registry.map_result(
        tool_id=ToolId("mobile.mobile_get_device_logs"),
        target=TARGET,
        arguments={},
        result=result("mobile.mobile_get_device_logs", "No log entries received"),
    )

    assert inventory.nodes
    assert logs.nodes
