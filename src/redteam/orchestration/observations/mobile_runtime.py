"""Map mobile-next runtime observations into device facts and findings."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from redteam.attack_graph.identity import node_id_for_target
from redteam.core.contracts.target import AnyTarget, DeviceTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphNodeType, Verdict
from redteam.orchestration.expectations import Expectation, verdict_for
from redteam.orchestration.observations.mapper import (
    FindingMutation,
    GraphMutation,
    NodeMutation,
)

MAX_LOG_OUTPUT_CHARS = 1_000_000
MAX_CRASH_OUTPUT_CHARS = 2_000_000
CRASH_LOOKBACK = timedelta(minutes=5)
_CRASH_MARKERS = (
    "fatal exception",
    "fatal signal",
    "uncaught exception",
)
_CRASH_ID_FIELDS = ("id", "crashId", "crash_id")
_PACKAGE_FIELDS = ("packageName", "package_name", "package", "processName", "process")


def _stable_id(prefix: str, target: DeviceTarget, check_id: str) -> str:
    payload = f"{node_id_for_target(target)}|{check_id}".encode()
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


def _assessment(
    *, field: str, description: str, observed: bool, result: ToolResult
) -> tuple[dict[str, Any], Expectation, Verdict, str]:
    expectation = Expectation(
        kind="output_field_equals",
        field=field,
        value=True,
        description=description,
        violation_if_met=True,
    )
    assessed_result = result.model_copy(update={"output": {field: observed}})
    verdict, reason = verdict_for(expectation, assessed_result)
    return (
        {
            "expectation": expectation.model_dump(mode="json"),
            "verdict": verdict.value,
            "reason": reason,
        },
        expectation,
        verdict,
        reason,
    )


def _finding(
    *,
    target: DeviceTarget,
    expectation: Expectation,
    verdict: Verdict,
    reason: str,
    source: str,
) -> FindingMutation | None:
    if verdict is not Verdict.VIOLATION:
        return None
    check_id = "android_runtime_crash"
    return FindingMutation(
        check_id=check_id,
        hypothesis_id=_stable_id("hyp_runtime", target, check_id),
        finding_id=_stable_id("find_runtime", target, check_id),
        title="Android application crash observed at runtime",
        description=(
            "Runtime diagnostics contain a fatal signal or crash report for the "
            "configured Android package."
        ),
        severity="medium",
        expectation=expectation,
        verdict=verdict,
        reason=reason,
        target_node_id=node_id_for_target(target),
        metadata={
            "claim_key": _stable_id("claim_runtime", target, check_id),
            "check_id": check_id,
            "analyzer": "mobile-next-runtime-v1",
            "observation_source": source,
            "device_serial": target.device_serial,
            "package_name": target.package_name,
        },
    )


def _contains_package_crash(value: Any, package_name: str) -> bool:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    lowered = text.lower()
    return package_name.lower() in lowered and any(marker in lowered for marker in _CRASH_MARKERS)


def _target_node(target: DeviceTarget, *, attrs: dict | None = None, facts: dict) -> NodeMutation:
    return NodeMutation(
        node_type=GraphNodeType.ASSET,
        node_id=node_id_for_target(target),
        attrs=attrs or {},
        facts=facts,
    )


def map_mobile_device_logs(
    *, target: AnyTarget, arguments: dict, result: ToolResult
) -> GraphMutation:
    del arguments
    output = result.output
    if (
        not result.success
        or not isinstance(target, DeviceTarget)
        or not isinstance(output, str)
        or len(output) > MAX_LOG_OUTPUT_CHARS
    ):
        return GraphMutation()

    entries: list[dict[str, Any]] = []
    if output.strip() != "No log entries received":
        try:
            for line in output.splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    return GraphMutation()
                entries.append(entry)
        except (json.JSONDecodeError, UnicodeError):
            return GraphMutation()

    observed = any(_contains_package_crash(entry, target.package_name) for entry in entries)
    assessment, expectation, verdict, reason = _assessment(
        field="package_fatal_signal_observed",
        description="A fatal runtime signal for the configured package is observed after launch.",
        observed=observed,
        result=result,
    )
    finding = _finding(
        target=target,
        expectation=expectation,
        verdict=verdict,
        reason=reason,
        source="device_logs",
    )
    return GraphMutation(
        nodes=[
            _target_node(
                target,
                attrs={"mobile_runtime_crash_observed": True} if observed else {},
                facts={
                    "mobile_log_entry_count": len(entries),
                    "mobile_log_assessment": assessment,
                },
            )
        ],
        findings=[finding] if finding is not None else [],
    )


def _crash_items(output: str) -> list[dict[str, Any]] | None:
    try:
        decoded = json.loads(output)
    except (json.JSONDecodeError, UnicodeError):
        return None
    items: list[Any] | None
    if isinstance(decoded, list):
        items = decoded
    elif isinstance(decoded, dict):
        items = next(
            (
                decoded[key]
                for key in ("crashes", "reports", "items")
                if isinstance(decoded.get(key), list)
            ),
            None,
        )
        if items is None:
            return None
    else:
        return None
    return items if all(isinstance(item, dict) for item in items) else None


def _item_value(item: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _item_timestamp(item: dict[str, Any]) -> datetime | None:
    value = item.get("timestamp")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def map_mobile_crash_list(
    *, target: AnyTarget, arguments: dict, result: ToolResult
) -> GraphMutation:
    del arguments
    output = result.output
    if (
        not result.success
        or not isinstance(target, DeviceTarget)
        or not isinstance(output, str)
        or len(output) > MAX_CRASH_OUTPUT_CHARS
    ):
        return GraphMutation()
    items = _crash_items(output)
    if items is None:
        return GraphMutation()

    package_name = target.package_name
    lower_bound = result.started_at.astimezone(timezone.utc) - CRASH_LOOKBACK
    upper_bound = result.finished_at.astimezone(timezone.utc) + timedelta(minutes=1)
    matching: list[tuple[datetime, str]] = []
    for item in items:
        crash_id = _item_value(item, _CRASH_ID_FIELDS)
        timestamp = _item_timestamp(item)
        if (
            _item_value(item, _PACKAGE_FIELDS) == package_name
            and crash_id is not None
            and timestamp is not None
            and lower_bound <= timestamp <= upper_bound
        ):
            matching.append((timestamp, crash_id))
    crash_ids = list(dict.fromkeys(crash_id for _, crash_id in sorted(matching, reverse=True)))
    facts: dict[str, Any] = {
        "mobile_crash_report_count": len(crash_ids),
        "mobile_crash_ids": crash_ids,
    }
    if crash_ids:
        facts["crash_id"] = crash_ids[0]
    return GraphMutation(
        nodes=[
            _target_node(
                target,
                attrs={"mobile_crash_observed": bool(crash_ids)},
                facts=facts,
            )
        ]
    )


def map_mobile_crash_detail(
    *, target: AnyTarget, arguments: dict, result: ToolResult
) -> GraphMutation:
    output = result.output
    crash_id = arguments.get("id")
    if (
        not result.success
        or not isinstance(target, DeviceTarget)
        or not isinstance(output, str)
        or not output.strip()
        or len(output) > MAX_CRASH_OUTPUT_CHARS
        or not isinstance(crash_id, str)
        or not crash_id.strip()
    ):
        return GraphMutation()

    observed = _contains_package_crash(output, target.package_name)
    assessment, expectation, verdict, reason = _assessment(
        field="package_crash_report_confirmed",
        description="A crash report contains a fatal signal for the configured package.",
        observed=observed,
        result=result,
    )
    finding = _finding(
        target=target,
        expectation=expectation,
        verdict=verdict,
        reason=reason,
        source="crash_report",
    )
    return GraphMutation(
        nodes=[
            _target_node(
                target,
                attrs={"mobile_runtime_crash_observed": True} if observed else {},
                facts={
                    "mobile_crash_detail_id": crash_id.strip(),
                    "mobile_crash_detail_assessment": assessment,
                },
            )
        ],
        findings=[finding] if finding is not None else [],
    )
