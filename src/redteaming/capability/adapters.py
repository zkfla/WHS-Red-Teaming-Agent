from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from html import escape
import json
import re
from typing import Any, Protocol

from ..events import Event, EventBus
from ..graphs import AssetGraph, EvidenceStore, ExplorationGraph
from ..models import Asset, Evidence
from .process import CapabilityExecutor, ProcessResult


_SECRET_PATTERN = re.compile(
    r"(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie|api[_-]?key|token|password|secret)\s*[:=]\s*)([^\r\n]+)"
)
_PROMPT_INJECTION_PATTERN = re.compile(
    r"(?is)(ignore\s+(?:all|any|the)\s+previous|system\s+message|developer\s+message|"
    r"reveal\s+(?:the|all)\s+(?:prompt|instructions)|jailbreak|<\s*/?\s*(?:system|assistant|tool))"
)
_SENSITIVE_KEY_PATTERN = re.compile(r"(?i)(authorization|cookie|set-cookie|api[_-]?key|token|password|secret)")


def redact_tool_output(value: str, limit: int = 65536) -> str:
    """Bound and redact common credential-shaped fields before persistence."""
    bounded = value[:limit]
    return _SECRET_PATTERN.sub(r"\1<redacted>", bounded)


def quarantine_tool_output(value: str, origin: str, limit: int = 65536) -> tuple[str, bool]:
    """Mark target-controlled output as data, never as controller instructions."""
    redacted = redact_tool_output(value, limit)
    injection_detected = bool(_PROMPT_INJECTION_PATTERN.search(redacted))
    safe = escape(redacted, quote=False)
    wrapped = (f'<UNTRUSTED_TOOL_OUTPUT origin="{escape(origin, quote=True)}" '
               f'injection_suspected="{str(injection_detected).lower()}">{safe}'
               "</UNTRUSTED_TOOL_OUTPUT>")
    return wrapped, injection_detected


def contains_prompt_injection(value: str) -> bool:
    return bool(_PROMPT_INJECTION_PATTERN.search(value[:65536]))


def redact_structured(value: Any) -> Any:
    """Recursively redact credential-shaped structured tool metadata."""
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if _SENSITIVE_KEY_PATTERN.search(str(key))
            else redact_structured(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_structured(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_structured(item) for item in value)
    if isinstance(value, str):
        return redact_tool_output(value, 4096)
    return value


class ToolOutputParser(Protocol):
    """Convert an approved tool result into normalized domain observations."""

    def parse(self, result: ProcessResult, tool_name: str) -> tuple[tuple[Asset, ...], tuple[Evidence, ...]]:
        ...


@dataclass(frozen=True)
class JsonAssetEvidenceParser:
    """Parser for an adapter-owned JSON contract, never a target-specific format.

    Accepted document shape is either ``{"assets": [...], "evidence": [...]}``
    or a list of asset objects. Unknown fields are preserved in properties or
    metadata so the control plane does not discard tool context.
    """

    def parse(self, result: ProcessResult, tool_name: str) -> tuple[tuple[Asset, ...], tuple[Evidence, ...]]:
        try:
            document: Any = json.loads(result.stdout)
        except (TypeError, ValueError):
            return (), ()
        if isinstance(document, list):
            asset_values = document
            evidence_values: list[Any] = []
        elif isinstance(document, dict):
            asset_values = document.get("assets", [])
            evidence_values = document.get("evidence", [])
        else:
            return (), ()
        assets: list[Asset] = []
        for value in asset_values if isinstance(asset_values, list) else ():
            if not isinstance(value, dict):
                continue
            kind = value.get("kind")
            canonical = value.get("canonical_value", value.get("value"))
            if not isinstance(kind, str) or not kind or not isinstance(canonical, str) or not canonical:
                continue
            properties = value.get("properties", {})
            assets.append(Asset(kind=kind, canonical_value=canonical,
                                properties=properties if isinstance(properties, dict) else {}))
        evidence: list[Evidence] = []
        for value in evidence_values if isinstance(evidence_values, list) else ():
            if not isinstance(value, dict):
                continue
            summary = value.get("summary")
            if not isinstance(summary, str) or not summary:
                continue
            metadata = value.get("metadata", {})
            evidence.append(Evidence(kind=str(value.get("kind", "tool_observation")),
                                     summary=summary,
                                     metadata=metadata if isinstance(metadata, dict) else {}))
        return tuple(assets), tuple(evidence)


@dataclass(frozen=True)
class JsonLinesAssetEvidenceParser:
    """Normalize common JSONL scanner records without binding to one tool.

    The parser is intentionally lossy at the secret boundary: it keeps
    structural fields as metadata for audit, while CapabilityAdapter performs
    recursive redaction and quarantine before persistence.
    """

    def parse(self, result: ProcessResult, tool_name: str) -> tuple[tuple[Asset, ...], tuple[Evidence, ...]]:
        assets: list[Asset] = []
        evidence: list[Evidence] = []
        for raw_line in result.stdout.splitlines()[:2048]:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            url = record.get("url") or record.get("matched-at") or record.get("host")
            if isinstance(url, str) and url:
                kind = "endpoint" if "://" in url or "/" in url else "host"
                assets.append(Asset(kind=kind, canonical_value=url,
                                    properties={"tool": tool_name}))
            host = record.get("ip") or record.get("hostname") or record.get("host")
            if isinstance(host, str) and host:
                assets.append(Asset(kind="host", canonical_value=host,
                                    properties={"tool": tool_name}))
            port = record.get("port")
            if isinstance(host, str) and port is not None:
                assets.append(Asset(kind="service", canonical_value=f"{host}:{port}/tcp",
                                    properties={"tool": tool_name, "port": str(port), "state": "observed"}))
            summary_bits = [str(record.get(key)) for key in
                            ("template-id", "type", "status-code", "title", "name")
                            if record.get(key) is not None]
            summary = f"{tool_name} JSONL observation" + (": " + " | ".join(summary_bits) if summary_bits else "")
            evidence.append(Evidence(kind="tool_observation", summary=summary,
                                     metadata={"tool": tool_name, "record": record}))
        return tuple(assets), tuple(evidence)


@dataclass(frozen=True)
class ImportedToolResult:
    result: ProcessResult
    assets: tuple[Asset, ...]
    evidence: tuple[Evidence, ...]


class CapabilityAdapter:
    """Run an approved capability and import its output into the twin graphs."""

    def __init__(self, executor: CapabilityExecutor, parser: ToolOutputParser,
                 assets: AssetGraph, evidence: EvidenceStore,
                 exploration: ExplorationGraph | None = None,
                 events: EventBus | None = None) -> None:
        self.executor = executor
        self.parser = parser
        self.assets = assets
        self.evidence = evidence
        self.exploration = exploration
        self.events = events or EventBus()

    def run(self, tool_name: str, argv: list[str], scope, targets,
            approved: bool = False, timeout: float = 120.0) -> ImportedToolResult:
        result = self.executor.execute(tool_name, argv, scope, targets,
                                       approved=approved, timeout=timeout)
        parsed_assets, parsed_evidence = self.parser.parse(result, tool_name)
        authorized_assets: list[Asset] = []
        rejected_assets = 0
        for item in parsed_assets:
            try:
                allowed = scope.allows(item.canonical_value)
            except (TypeError, ValueError):
                allowed = False
            if allowed:
                authorized_assets.append(item)
            else:
                rejected_assets += 1
                self.events.publish(Event("capability.asset.rejected", {
                    "tool": tool_name, "asset": item.canonical_value,
                    "reason": "outside authorized Scope",
                }))
        imported_assets = tuple(self.assets.upsert(item) for item in authorized_assets)
        marked_evidence = tuple(
            replace(item, metadata={**redact_structured(item.metadata),
                                    "untrusted": True, "origin": f"tool:{tool_name}"})
            for item in parsed_evidence
        )
        stdout, stdout_injection = quarantine_tool_output(result.stdout, f"tool:{tool_name}:stdout")
        stderr, stderr_injection = quarantine_tool_output(result.stderr, f"tool:{tool_name}:stderr")
        raw_evidence = Evidence(
            kind="tool_output",
            summary=f"{tool_name} exited with code {result.returncode}",
            metadata={
                "tool": tool_name,
                "returncode": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "untrusted": True,
                "origin": f"tool:{tool_name}",
                "prompt_injection_suspected": stdout_injection or stderr_injection,
            },
        )
        imported_evidence = tuple(self.evidence.add(item) for item in (raw_evidence, *marked_evidence))
        intent_node = self.exploration.add("capability", tool_name) if self.exploration else None
        if intent_node is not None:
            for asset in imported_assets:
                node = self.exploration.add("asset", asset.id)
                self.exploration.link(intent_node, "discovered", node)
                self.exploration.anchor(intent_node, asset)
            for item in imported_evidence:
                node = self.exploration.add("evidence", item.id)
                self.exploration.link(intent_node, "produced", node)
        self.events.publish(Event("capability.output.imported", {
            "tool": tool_name, "asset_count": len(imported_assets),
            "evidence_count": len(imported_evidence), "rejected_asset_count": rejected_assets,
        }))
        return ImportedToolResult(result, imported_assets, imported_evidence)
