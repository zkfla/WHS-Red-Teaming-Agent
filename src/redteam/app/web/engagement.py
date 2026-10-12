"""Web interview for the engagement file.

Deterministic like the terminal form: fixed questions, explicit values, no model
call and no scope inferred from prose. The conversion, the scope checks and the
YAML writer are the existing ones (``intake.serializer``, ``commands.validate``).
Scope is authorization, so a save needs an explicit confirmation, an existing
file is never replaced silently, and scope that grows needs its own confirmation.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, ValidationError

from redteam.app.cli.display import safe_text
from redteam.app.cli.engagement_form import QUESTIONS, merge_edit
from redteam.app.web_policy import DEPLOYMENT_KEYS, WEB_POLICY_KEY, WebPolicySelection
from redteam.core.config import EngagementConfig, OrchestrationConfig, load_engagement_config
from redteam.intake.models import CANONICAL_CAPABILITIES, EngagementDraft
from redteam.intake.serializer import (
    DraftIncompleteError,
    UnsafeEditError,
    draft_to_engagement_config,
    dump_engagement_yaml,
    engagement_config_to_draft,
    scope_diff,
)
from redteam.intake.targets import explicit_target_origins


class WebEngagementDraft(EngagementDraft):
    """Explicit UI choices; the model's intake schema cannot grant whole-site access."""
    web_policy_mode: Literal["manual", "site", "paths"] = "manual"
    web_paths: list[str] = Field(default_factory=list)
    web_origins: list[str] = Field(default_factory=list, exclude=True)

FIELD_ORDER = (
    "name", "description", "objective",
    "allowed_hosts", "allowed_domains", "allowed_networks",
    "web_policy_mode", "web_paths",
    "allowed_capabilities", "denied_capabilities", "denied_operations",
    "availability_impact_allowed", "notes", "focus_areas",
    "max_actions", "max_tool_calls", "max_duration_seconds",
)
STEPS = (
    ("basic", "기본", ("name", "description", "objective")),
    ("scope", "범위", ("allowed_hosts", "allowed_domains", "allowed_networks",
                       "web_policy_mode", "web_paths")),
    ("limits", "제한", ("allowed_capabilities", "denied_capabilities", "denied_operations",
                       "availability_impact_allowed", "notes", "focus_areas")),
    ("budget", "예산", ("max_actions", "max_tool_calls", "max_duration_seconds")),
)
_TEXT_LIMIT = {"name": 200, "description": 2000, "objective": 1000}
_ITEM_LIMIT = 200
_LIST_LIMIT = 50
_INT_LIMIT = 10**9
SCOPE_FIELDS = ("allowed_hosts", "allowed_domains", "allowed_networks")


class EngagementError(Exception):
    def __init__(
        self, code: str, *, problems: list[dict[str, str]] | None = None, field: str | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.problems = problems or []
        self.field = field


def field_definitions() -> list[dict[str, Any]]:
    properties = WebEngagementDraft.model_json_schema()["properties"]
    try:
        default_actions: int | None = OrchestrationConfig.from_environment().max_steps
    except ValueError:
        default_actions = None  # the settings screen reports the invalid value
    definitions = []
    for name in FIELD_ORDER:
        schema = properties[name]
        options = schema.get("anyOf", [schema])
        kind = next((o.get("type") for o in options if o.get("type") != "null"), "string")
        labels = {"web_policy_mode": "웹 탐색 범위", "web_paths": "지정 경로 (쉼표로 구분)"}
        item: dict[str, Any] = {"name": name, "label": labels.get(name, QUESTIONS.get(name)),
                                "kind": kind}
        if name == "web_policy_mode":
            item.update(default="site", choices=[
                {"value": "site", "label": "사이트 전체 — 정책 자동 생성"},
                {"value": "paths", "label": "지정 경로만 — 정책 자동 생성"},
                {"value": "manual", "label": "기존 MCP 설정 사용"},
            ])
        if name in {"allowed_capabilities", "denied_capabilities"}:
            item["options"] = list(CANONICAL_CAPABILITIES)
        if name == "max_actions" and default_actions is not None:
            item["default"] = default_actions
        definitions.append(item)
    return definitions


def _items(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise ValueError("invalid_type")
    cleaned = list(dict.fromkeys(entry.strip() for entry in value if entry.strip()))
    if len(cleaned) > _LIST_LIMIT:
        raise ValueError("too_many")
    if any(len(entry) > _ITEM_LIMIT for entry in cleaned):
        raise ValueError("too_long")
    return cleaned


def coerce_field(name: str, value: Any) -> Any:
    if name == "web_policy_mode":
        if not isinstance(value, str) or value not in {"manual", "site", "paths"}:
            raise ValueError("invalid_choice")
        return value
    if name in _TEXT_LIMIT:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("invalid_type")
        text = value.strip()
        if len(text) > _TEXT_LIMIT[name]:
            raise ValueError("too_long")
        return text or None
    if name == "availability_impact_allowed":
        if value is not None and not isinstance(value, bool):
            raise ValueError("invalid_type")
        return value
    if name in {"max_actions", "max_tool_calls", "max_duration_seconds"}:
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("invalid_type")
        if not 0 <= value <= _INT_LIMIT:
            raise ValueError("out_of_range")
        return value
    items = _items(value)
    if name == "allowed_hosts" and items:
        try:
            return list(explicit_target_origins(",".join(items), address_list=True))
        except ValueError:
            raise ValueError("invalid_address") from None
    if name in {"allowed_capabilities", "denied_capabilities"}:
        if set(items) - set(CANONICAL_CAPABILITIES):
            raise ValueError("unknown_capability")
    return items


def parse_draft(body: Any) -> WebEngagementDraft:
    if not isinstance(body, dict):
        raise EngagementError("invalid", problems=[{"field": "draft", "code": "invalid_type"}])
    values: dict[str, Any] = {}
    problems: list[dict[str, str]] = []
    for key, value in body.items():
        if key not in FIELD_ORDER:
            problems.append({"field": safe_text(key, limit=60), "code": "unknown_field"})
            continue
        try:
            values[key] = coerce_field(key, value)
        except ValueError as exc:
            problems.append({"field": key, "code": str(exc)})
    if problems:
        raise EngagementError("invalid", problems=problems)
    try:
        hosts = _items(body.get("allowed_hosts"))
        origins = list(dict.fromkeys(origin for entry in hosts
                                     for origin in explicit_target_origins(
                                         entry, address_list=True,
                                     ).values()))
        return WebEngagementDraft.model_validate({**values, "web_origins": origins})
    except ValidationError as exc:
        raise EngagementError(
            "invalid",
            problems=[
                {"field": ".".join(str(part) for part in error["loc"])[:60], "code": "invalid"}
                for error in exc.errors()
            ],
        ) from None


def _missing_problems(missing: list[str]) -> list[dict[str, str]]:
    problems = []
    for entry in missing:
        if entry == "name":
            problems.append({"field": "name", "code": "missing"})
        elif entry.startswith("scope"):
            problems.append({"field": "allowed_hosts", "code": "scope_required"})
        elif entry.startswith("max_actions"):
            problems.append({"field": "max_actions", "code": "missing"})
        else:
            problems.append({"field": "draft", "code": "missing"})
    return problems


def _rule_problems(config: EngagementConfig) -> list[dict[str, str]]:
    from redteam.app.cli.commands.validate import check_roe, check_scope

    found = [
        {"field": "scope", "code": "rule", "message": safe_text(text, limit=300)}
        for text in check_scope(config)
    ]
    found += [
        {"field": "roe", "code": "rule", "message": safe_text(text, limit=300)}
        for text in check_roe(config)
    ]
    return found


def summarize(config: EngagementConfig) -> dict[str, Any]:
    rule = config.rules_of_engagement[0] if config.rules_of_engagement else None
    budget = config.execution_budget
    return {
        "web_policy": config.metadata.get(WEB_POLICY_KEY),
        "name": safe_text(config.name, limit=200),
        "description": safe_text(config.description, limit=2000),
        "objective": safe_text(config.metadata.get("objective") or "", limit=1000),
        "scope": {name: [safe_text(v, limit=200) for v in getattr(config.scope, name)]
                  for name in SCOPE_FIELDS},
        "allowed_capabilities": list(rule.allowed_capabilities) if rule else [],
        "denied_capabilities": list(rule.denied_capabilities) if rule else [],
        "denied_operations": [safe_text(v, limit=200) for v in rule.denied_operations]
        if rule else [],
        "availability_impact_allowed": bool(rule.availability_impact_allowed) if rule else False,
        "notes": safe_text(rule.notes, limit=1000) if rule and rule.notes else "",
        "focus_areas": [safe_text(v, limit=200) for v in config.metadata.get("focus_areas", [])],
        "budget": {
            "max_actions": budget.max_actions,
            "max_tool_calls": budget.max_tool_calls,
            "max_duration_seconds": budget.max_duration_seconds,
        },
    }


class EngagementStore:
    def __init__(self, path: Path, *, fresh: bool = False) -> None:
        self.path = path
        self._fresh = fresh  # a form for a new investigation: never reads or edits a file

    @classmethod
    def blank(cls) -> EngagementStore:
        return cls(Path(), fresh=True)

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> EngagementConfig | None:
        if self._fresh or not self.exists():
            return None
        try:
            return load_engagement_config(self.path)
        except (OSError, ValidationError, ValueError, yaml.YAMLError):
            raise EngagementError("unreadable") from None

    def status(self) -> dict[str, Any]:
        try:
            config = self.load()
        except EngagementError:
            return {"exists": True, "valid": False, "editable": False, "reason": "unreadable",
                    "problems": [], "draft": None, "summary": None}
        if config is None:
            return self.blank_status()
        problems = _rule_problems(config)
        try:
            draft = engagement_config_to_draft(config).model_dump()
            raw = config.metadata.get(WEB_POLICY_KEY)
            if raw is not None:
                selection = WebPolicySelection.model_validate(raw)
                draft.update(web_policy_mode=selection.mode, web_paths=selection.paths,
                             allowed_hosts=selection.origins)
            else:
                draft.update(web_policy_mode="manual", web_paths=[])
            editable, reason = True, None
        except UnsafeEditError:
            draft, editable, reason = None, False, "not_managed"
        return {"exists": True, "valid": not problems, "editable": editable, "reason": reason,
                "problems": problems, "draft": draft, "summary": summarize(config)}

    @staticmethod
    def blank_status() -> dict[str, Any]:
        return {"exists": False, "valid": False, "editable": True, "reason": None,
                "problems": [], "draft": None, "summary": None}

    def ready(self) -> bool:
        status = self.status()
        return bool(status["exists"] and status["valid"])

    def _convert(self, body: Any) -> tuple[EngagementConfig, dict[str, Any]]:
        existing = self.load()
        draft = parse_draft(body)
        before: EngagementDraft | None = None
        if existing is not None:
            try:
                before = engagement_config_to_draft(existing)
            except UnsafeEditError:
                raise EngagementError("not_managed") from None
        try:
            result = draft_to_engagement_config(draft)
            config = merge_edit(existing, result.engagement_config, draft) if existing \
                else result.engagement_config
        except DraftIncompleteError as exc:
            raise EngagementError("invalid", problems=_missing_problems(exc.missing)) from None
        except (ValidationError, ValueError):
            raise EngagementError(
                "invalid", problems=[{"field": "draft", "code": "invalid"}]
            ) from None
        previous_selection = existing.metadata.get(WEB_POLICY_KEY) if existing else None
        metadata = dict(config.metadata)
        if draft.web_policy_mode == "manual":
            selection_data = None
            metadata.pop(WEB_POLICY_KEY, None)
        else:
            try:
                selection = WebPolicySelection(
                    mode=draft.web_policy_mode, origins=draft.web_origins, paths=draft.web_paths,
                )
                from redteam.capability.deployment import check_origin_scope
                for origin in selection.origins:
                    check_origin_scope(origin, config.scope)
            except ValueError:
                raise EngagementError("invalid", problems=[
                    {"field": "web_paths" if draft.web_policy_mode == "paths"
                     else "allowed_hosts", "code": "web_policy_invalid"},
                ]) from None
            selection_data = selection.model_dump(mode="json")
            metadata[WEB_POLICY_KEY] = selection_data
        if previous_selection is not None and (
            previous_selection != selection_data
            or (existing is not None and existing.scope != config.scope)
        ):
            for key in DEPLOYMENT_KEYS:
                metadata.pop(key, None)
        config = config.model_copy(update={"metadata": metadata})
        problems = _rule_problems(config)
        if problems:
            raise EngagementError("invalid", problems=problems)
        changes = scope_diff(before, draft) if before is not None else {}
        added = any(added for added, _removed in changes.values())
        if previous_selection is not None and selection_data is not None:
            before_policy = WebPolicySelection.model_validate(previous_selection)
            after_policy = WebPolicySelection.model_validate(selection_data)
            added = added or (before_policy.mode == "paths" and after_policy.mode == "site")
            added = added or (before_policy.mode == after_policy.mode == "paths"
                              and bool(set(after_policy.paths) - set(before_policy.paths)))
        elif selection_data is not None and existing is not None:
            added = True  # Switching a manual policy requires explicit scope confirmation.
        return config, {
            "exists": existing is not None,
            "summary": summarize(config),
            "scope_diff": {name: {"added": a, "removed": r} for name, (a, r) in changes.items()},
            "scope_expanded": added,
            "unenforced_operations": [safe_text(v, limit=200)
                                      for v in result.unenforced_operations],
        }

    def preview(self, body: Any) -> dict[str, Any]:
        return self._convert(body)[1]

    def save(
        self, body: Any, *, confirm_scope: bool, confirm_expansion: bool
    ) -> dict[str, Any]:
        config, preview = self._convert(body)
        if confirm_scope is not True:
            raise EngagementError("confirmation_required", field="confirm_scope")
        if preview["scope_expanded"] and confirm_expansion is not True:
            raise EngagementError("confirmation_required", field="confirm_expansion")
        self._write(config)
        return {**preview, "saved": True}

    def _write(self, config: EngagementConfig) -> None:
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(dir=directory, prefix=".engagement-", suffix=".tmp")
        except OSError:
            raise EngagementError("write_failed") from None

        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(dump_engagement_yaml(config))
            os.replace(name, self.path)
        except OSError:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise EngagementError("write_failed") from None

    def install_deployment(self, expected: EngagementConfig, metadata: dict) -> None:
        """Attach only generated declarations to the unchanged confirmed input."""
        if self.load() != expected:
            raise EngagementError("changed")
        additions = {key: metadata[key] for key in DEPLOYMENT_KEYS}
        self._write(expected.model_copy(update={"metadata": {**expected.metadata, **additions}}))
