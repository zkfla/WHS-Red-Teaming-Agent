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
from urllib.parse import urlsplit

import yaml
from pydantic import Field, ValidationError

from redteam.app.cli.display import safe_text
from redteam.app.cli.engagement_form import QUESTIONS, merge_edit
from redteam.app.web_policy import (
    DEPLOYMENT_KEYS,
    GATED_PREPARATION_KEY,
    WEB_POLICY_KEY,
    WebEndpointSelection,
    WebPolicySelection,
    WebSessionSelection,
    permissions_expand,
    validate_kali_network_policy,
)
from redteam.capability.gated_network_policy import SUPPORTED_GATED_PRESETS, GatedLimits
from redteam.capability.servers.config import HTTPMethod, read_methods
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
    web_permissions: Literal["read_only", "custom", "full"] = "read_only"
    web_methods: list[HTTPMethod] = Field(default_factory=read_methods)
    web_query_keys: list[str] = Field(default_factory=list)
    web_all_query_keys: bool = False
    web_request_headers: list[str] = Field(default_factory=list)
    web_all_request_headers: bool = False
    web_request_body: bool = False
    web_response_body_chars: int = Field(default=0, ge=0, le=16384)
    web_sessions: list[WebSessionSelection] = Field(default_factory=list)
    kali_network_mode: Literal["inherit", "closed", "configured", "prepare"] = "inherit"
    kali_network_policy_path: str | None = None
    kali_endpoints: list[WebEndpointSelection] = Field(default_factory=list, max_length=960)
    kali_limits_preset: Literal["probe", "standard", "custom"] | None = None
    kali_limit_deliveries: int | None = None
    kali_limit_output_bytes: int | None = None
    kali_limit_duration_seconds: int | float | None = None
    kali_limit_delivery_rate_per_second: int | float | None = None
    kali_sidecar_mode: Literal["managed", "pinned"] | None = None
    kali_sidecar_image: str | None = None
    kali_docker_context: str | None = None


WEB_PERMISSION_FIELDS = (
    "web_permissions", "web_methods", "web_query_keys", "web_all_query_keys",
    "web_request_headers", "web_all_request_headers", "web_request_body",
    "web_response_body_chars", "web_sessions", "kali_network_mode", "kali_network_policy_path",
    "kali_endpoints", "kali_limits_preset", "kali_limit_deliveries",
    "kali_limit_output_bytes", "kali_limit_duration_seconds",
    "kali_limit_delivery_rate_per_second", "kali_sidecar_mode",
    "kali_sidecar_image", "kali_docker_context",
)

FIELD_ORDER = (
    "name", "description", "objective",
    "allowed_hosts", "allowed_domains", "allowed_networks",
    "web_policy_mode", "web_paths",
    *WEB_PERMISSION_FIELDS,
    "allowed_capabilities", "denied_capabilities", "denied_operations",
    "availability_impact_allowed", "notes", "focus_areas",
    "max_actions", "max_tool_calls", "max_duration_seconds",
)
STEPS = (
    ("basic", "기본", ("name", "description", "objective")),
    ("scope", "범위", ("allowed_hosts", "allowed_domains", "allowed_networks",
                       "web_policy_mode", "web_paths")),
    ("permissions", "요청 권한", WEB_PERMISSION_FIELDS),
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
        if name in {"kali_limit_duration_seconds", "kali_limit_delivery_rate_per_second"}:
            kind = "number"
        labels = {
            "web_policy_mode": "웹 탐색 범위", "web_paths": "지정 경로 (쉼표로 구분)",
            "web_permissions": "웹 요청 권한", "web_methods": "허용 HTTP 메서드",
            "web_query_keys": "허용 query 이름 (쉼표로 구분)",
            "web_all_query_keys": "모든 query 이름 허용",
            "web_request_headers": "허용 요청 header 이름 (쉼표로 구분)",
            "web_all_request_headers": "모든 사용자 지정 header 허용 (인증·통신 제어 header 제외)",
            "web_request_body": "JSON·form 요청 본문 전송 허용",
            "web_response_body_chars": "모델에 제공할 응답 본문 최대 글자 수 (0: 제공 안 함)",
            "web_sessions": "테스트 계정 session", "kali_network_mode": "Kali 대상 통신",
            "kali_network_policy_path": "검토된 Kali 네트워크 정책 파일의 절대 경로",
            "kali_endpoints": "Kali 대상 통신 포트와 전송 방식",
            "kali_limits_preset": "Kali 네트워크 예산",
            "kali_limit_deliveries": "최대 패킷 수",
            "kali_limit_output_bytes": "최대 출력 바이트",
            "kali_limit_duration_seconds": "최대 실행 시간 (초)",
            "kali_limit_delivery_rate_per_second": "초당 최대 패킷 수 (선택)",
            "kali_sidecar_mode": "네트워크 sidecar 선택",
            "kali_sidecar_image": "sha256로 고정한 sidecar 이미지",
            "kali_docker_context": "실행에 사용할 Docker context (선택)",
        }
        item: dict[str, Any] = {"name": name, "label": labels.get(name, QUESTIONS.get(name)),
                                "kind": kind}
        if name == "web_policy_mode":
            item.update(default="site", choices=[
                {"value": "site", "label": "사이트 전체 — 정책 자동 생성"},
                {"value": "paths", "label": "지정 경로만 — 정책 자동 생성"},
                {"value": "manual", "label": "기존 MCP 설정 사용"},
            ])
        if name == "web_permissions":
            item.update(default="read_only", choices=[
                {"value": "read_only", "label": "읽기 위주 (GET·HEAD·OPTIONS)"},
                {"value": "custom", "label": "부분 허용 — 아래 항목 직접 선택"},
                {"value": "full", "label": "대상 범위 내 전체 웹 요청 허용"},
            ])
        if name == "web_methods":
            item["options"] = ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
            item["default"] = read_methods()
        if name == "kali_network_mode":
            item.update(default="closed", choices=[
                {"value": "closed", "label": "차단 — Kali 로컬 분석만"},
                {"value": "configured", "label": "허용 — 검토된 대상 통신 정책 사용"},
                {"value": "prepare", "label": "준비 — 선택한 포트와 유한 예산으로 사전 준비"},
                {"value": "inherit", "label": "기존 실행 환경 설정 유지"},
            ])
        if name == "kali_limits_preset":
            choices = []
            for preset, limits in SUPPORTED_GATED_PRESETS.items():
                values = limits.model_dump()
                choices.append({
                    "value": preset,
                    "label": (f"{preset}: {values['max_flow_init_deliveries']} 패킷 · "
                              f"{values['max_output_bytes']} 바이트 · "
                              f"{values['duration_seconds']:g}초"),
                })
            choices.append({"value": "custom", "label": "직접 입력 (모두 유한 값)"})
            item.update(choices=choices)
        if name == "kali_sidecar_mode":
            item.update(choices=[
                {"value": "managed", "label": "저장소에서 관리되는 sidecar 준비"},
                {"value": "pinned", "label": "이미 설치된 고정 이미지 사용"},
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
    if name in {"web_permissions", "kali_network_mode", "kali_limits_preset", "kali_sidecar_mode"}:
        if name in {"kali_limits_preset", "kali_sidecar_mode"} and value in (None, ""):
            return None
        choices = (
            {"read_only", "custom", "full"} if name == "web_permissions" else
            {"inherit", "closed", "configured", "prepare"} if name == "kali_network_mode" else
            {"probe", "standard", "custom"} if name == "kali_limits_preset" else
            {"managed", "pinned"}
        )
        if not isinstance(value, str) or value not in choices:
            raise ValueError("invalid_choice")
        return value
    if name == "web_sessions":
        if not isinstance(value, list) or len(value) > 60:
            raise ValueError("invalid_type")
        try:
            return [WebSessionSelection.model_validate(item) for item in value]
        except ValidationError:
            raise ValueError("invalid_session_reference") from None
    if name == "kali_network_policy_path":
        if value is not None and (not isinstance(value, str) or len(value) > 4096):
            raise ValueError("invalid_type")
        return value.strip() or None if isinstance(value, str) else None
    if name == "kali_endpoints":
        if not isinstance(value, list) or len(value) > 960:
            raise ValueError("invalid_type")
        try:
            return [WebEndpointSelection.model_validate(item) for item in value]
        except ValidationError:
            raise ValueError("invalid_endpoint_selection") from None
    if name in {"kali_limit_deliveries", "kali_limit_output_bytes"}:
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("invalid_type")
        return value
    if name in {"kali_limit_duration_seconds", "kali_limit_delivery_rate_per_second"}:
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("invalid_type")
        return value
    if name in {"kali_sidecar_image", "kali_docker_context"}:
        if value is not None and (not isinstance(value, str) or len(value) > 256):
            raise ValueError("invalid_type")
        return value.strip() or None if isinstance(value, str) else None
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
    if name in {"availability_impact_allowed", "web_all_query_keys", "web_all_request_headers",
                "web_request_body"}:
        if value is not None and not isinstance(value, bool):
            raise ValueError("invalid_type")
        return value
    if name in {"max_actions", "max_tool_calls", "max_duration_seconds", "web_response_body_chars"}:
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
                draft.update(
                    web_permissions=selection.permissions, web_methods=selection.methods,
                    web_query_keys=selection.query_keys,
                    web_all_query_keys=selection.allow_all_query_keys,
                    web_request_headers=selection.request_headers,
                    web_all_request_headers=selection.allow_all_request_headers,
                    web_request_body=selection.request_body_enabled,
                    web_response_body_chars=selection.response_body_max_chars,
                    web_sessions=[item.model_dump() for item in selection.sessions],
                    kali_network_mode=selection.kali_network_mode,
                    kali_network_policy_path=selection.kali_network_policy_path,
                    kali_endpoints=[item.model_dump() for item in selection.kali_endpoints],
                    kali_limits_preset=selection.kali_limits_preset,
                    kali_limit_deliveries=(selection.kali_limits.max_flow_init_deliveries
                                           if selection.kali_limits else None),
                    kali_limit_output_bytes=(selection.kali_limits.max_output_bytes
                                             if selection.kali_limits else None),
                    kali_limit_duration_seconds=(selection.kali_limits.duration_seconds
                                                 if selection.kali_limits else None),
                    kali_limit_delivery_rate_per_second=(
                        selection.kali_limits.delivery_rate_per_second
                        if selection.kali_limits else None
                    ),
                    kali_sidecar_mode=selection.kali_sidecar_mode,
                    kali_sidecar_image=selection.kali_sidecar_image,
                    kali_docker_context=selection.kali_docker_context,
                )
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
            if (draft.web_permissions != "read_only"
                    or draft.kali_network_mode in {"configured", "prepare"}
                    or draft.web_sessions):
                raise EngagementError("invalid", problems=[
                    {"field": "web_policy_mode", "code": "automatic_policy_required"},
                ])
            selection_data = None
            metadata.pop(WEB_POLICY_KEY, None)
        else:
            try:
                network_path, network_hash = None, None
                if draft.kali_network_mode == "configured":
                    network_path = draft.kali_network_policy_path
                    if not network_path:
                        raise ValueError("kali_network_policy_required")
                    network_hash, _ = validate_kali_network_policy(network_path, draft.web_origins)
                if draft.kali_network_mode == "prepare":
                    if draft.kali_network_policy_path:
                        raise ValueError("kali_configured_policy_conflict")
                    if draft.kali_limits_preset == "custom":
                        try:
                            limits = GatedLimits.model_validate({
                                "max_flow_init_deliveries": draft.kali_limit_deliveries,
                                "max_output_bytes": draft.kali_limit_output_bytes,
                                "duration_seconds": draft.kali_limit_duration_seconds,
                                "delivery_rate_per_second": (
                                    draft.kali_limit_delivery_rate_per_second
                                ),
                            })
                        except ValidationError:
                            raise ValueError("kali_custom_limits_invalid") from None
                    else:
                        if any(value is not None for value in (
                            draft.kali_limit_deliveries, draft.kali_limit_output_bytes,
                            draft.kali_limit_duration_seconds,
                            draft.kali_limit_delivery_rate_per_second,
                        )):
                            raise ValueError("kali_custom_limits_not_selected")
                        limits = None
                else:
                    if any((draft.kali_endpoints, draft.kali_limits_preset is not None,
                            draft.kali_limit_deliveries is not None,
                            draft.kali_limit_output_bytes is not None,
                            draft.kali_limit_duration_seconds is not None,
                            draft.kali_limit_delivery_rate_per_second is not None,
                            draft.kali_sidecar_mode is not None,
                            draft.kali_sidecar_image is not None,
                            draft.kali_docker_context is not None)):
                        raise ValueError("kali_prepare_fields_not_selected")
                    limits = None
                if draft.kali_network_mode not in {"prepare", "configured"} \
                        and draft.kali_network_policy_path:
                    raise ValueError("kali_network_policy_not_selected")
                custom = draft.web_permissions == "custom"
                selection = WebPolicySelection(
                    mode=draft.web_policy_mode, origins=draft.web_origins, paths=draft.web_paths,
                    permissions=draft.web_permissions,
                    methods=draft.web_methods if custom else read_methods(),
                    query_keys=draft.web_query_keys if custom else [],
                    allow_all_query_keys=custom and draft.web_all_query_keys,
                    request_headers=draft.web_request_headers if custom else [],
                    allow_all_request_headers=custom and draft.web_all_request_headers,
                    request_body_enabled=custom and draft.web_request_body,
                    response_body_max_chars=draft.web_response_body_chars,
                    sessions=draft.web_sessions,
                    kali_network_mode=draft.kali_network_mode,
                    kali_network_policy_path=network_path, kali_network_policy_sha256=network_hash,
                    kali_endpoints=draft.kali_endpoints,
                    kali_limits_preset=draft.kali_limits_preset,
                    kali_limits=limits,
                    kali_sidecar_mode=draft.kali_sidecar_mode,
                    kali_sidecar_image=draft.kali_sidecar_image,
                    kali_docker_context=draft.kali_docker_context,
                )
                from redteam.capability.deployment import check_origin_scope
                for origin in selection.origins:
                    check_origin_scope(origin, config.scope)
                if selection.kali_network_mode == "prepare":
                    from redteam.core.contracts.target import EndpointTarget
                    from redteam.core.scope_rules import check_target_scope

                    for endpoint in selection.kali_endpoints:
                        host = urlsplit(endpoint.origin).hostname
                        assert host is not None
                        allowed, _reasons, _severity = check_target_scope(
                            EndpointTarget(host=host, port=endpoint.port), config.scope,
                        )
                        if not allowed:
                            raise ValueError("kali_endpoint_outside_scope")
            except (ValueError, OSError) as exc:
                code = str(exc)
                if isinstance(exc, ValidationError):
                    errors = exc.errors()
                    cause = errors[0].get("ctx", {}).get("error") if errors else None
                    if cause is not None:
                        code = str(cause)
                field = (
                    "kali_endpoints" if code.startswith("kali_endpoint") else
                    "kali_limits_preset" if code.startswith("kali_custom_limits")
                    or code.startswith("kali_limits") else
                    "kali_sidecar_image" if code.startswith("kali_sidecar_image") else
                    "kali_sidecar_mode" if code.startswith("kali_sidecar_selection") else
                    "kali_limits_preset" if code.startswith("kali_limits_selection") else
                    "kali_docker_context" if code.startswith("kali_docker_context") else
                    "kali_network_policy_path" if code.startswith("kali_network_policy")
                    or code.startswith("kali_configured_policy") else
                    "kali_network_mode" if code.startswith("kali_prepare_fields") else
                    "web_permissions"
                )
                problem_code = code if code.startswith("kali_") else "web_policy_invalid"
                raise EngagementError("invalid", problems=[
                    {"field": field, "code": problem_code},
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
            added = added or permissions_expand(before_policy, after_policy)
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
        additions = {key: metadata[key] for key in DEPLOYMENT_KEYS if key in metadata}
        self._write(expected.model_copy(update={"metadata": {**expected.metadata, **additions}}))

    def install_gated_preparation(self, expected: EngagementConfig, record: dict) -> None:
        """Attach the immutable startup request only to the selection it binds."""
        if self.load() != expected:
            raise EngagementError("changed")
        metadata = {**expected.metadata, GATED_PREPARATION_KEY: record}
        self._write(expected.model_copy(update={"metadata": metadata}))
