"""Deterministic configuration UI; no model calls or inference from prose."""

from __future__ import annotations

from pydantic import ValidationError

from redteam.core.config import EngagementConfig, OrchestrationConfig
from redteam.intake.models import CANONICAL_CAPABILITIES, EngagementDraft
from redteam.intake.serializer import (
    DraftIncompleteError,
    draft_to_engagement_config,
    engagement_config_to_draft,
    scope_diff,
)
from redteam.intake.targets import explicit_target_origins

# Display/input metadata only. Validation and conversion remain in the
# existing configuration contracts; this form owns no execution policy.
QUESTIONS = {
    "name": "점검 작업 이름",
    "allowed_hosts": "허용 호스트 (예: localhost:8081, 여러 값은 쉼표로 구분)",
    "allowed_domains": "허용 도메인 (없으면 Enter)",
    "allowed_networks": "허용 네트워크 CIDR (없으면 Enter)",
    "description": "작업 설명 (대상 URL 또는 주소 포함)",
    "allowed_capabilities": (
        f"허용 기능 ({', '.join(CANONICAL_CAPABILITIES)}; 빈 목록은 기능 제한 없음)"
    ),
    "max_actions": "최대 작업 횟수",
    "max_tool_calls": "최대 도구 호출 횟수",
    "max_duration_seconds": "최대 실행 시간 (초)",
    "focus_areas": "중점 조사 영역 (쉼표로 구분)",
    "denied_capabilities": "금지 기능 (쉼표로 구분)",
    "denied_operations": "금지 행동 (정식 기능.행동 이름; 쉼표로 구분)",
    "availability_impact_allowed": "가용성에 영향을 주는 행동 허용 여부 (y/n)",
    "notes": "추가 제약 (기록용이며 자동 집행되지 않음; 쉼표로 구분)",
    "objective": "작업 목표 (달성할 결과)",
}


class Cancelled(Exception):
    pass


def _ask(console, draft: EngagementDraft, field: str, *, editing: bool) -> EngagementDraft:
    schema = EngagementDraft.model_json_schema()["properties"][field]
    choices = schema.get("anyOf", [schema])
    kind = next((item.get("type") for item in choices if item.get("type") != "null"), None)
    current = getattr(draft, field)
    suffix = f" [현재값: {current}]" if editing else ""
    while True:
        answer = console.input(f"{QUESTIONS[field]}{suffix}: ").strip()
        if answer.lower() in {"q", "quit"}:
            raise Cancelled
        if not answer:
            if (
                field in {"name", "description", "objective"} and not current and not editing
            ) or (field == "max_actions" and current is None):
                console.print("값을 입력해 주세요. 취소하려면 q를 입력하세요.")
                continue
            return draft
        try:
            if answer == "-" and field in {"description", "objective"} and not editing:
                raise ValueError("작업 설명과 목표는 비울 수 없습니다.")
            if answer == "-":
                value = [] if kind == "array" else None
            elif kind == "array":
                value = [item.strip() for item in answer.split(",") if item.strip()]
                if field == "allowed_hosts":
                    hosts = explicit_target_origins(answer, address_list=True)
                    if not hosts:
                        raise ValueError("대상 URL 또는 호스트 주소를 입력해 주세요.")
                    value = list(hosts)
            elif kind == "integer":
                value = int(answer)
                if value < 0:
                    raise ValueError("횟수와 시간은 0 이상이어야 합니다.")
            elif kind == "boolean":
                if answer.lower() not in {"y", "yes", "n", "no"}:
                    raise ValueError("y 또는 n을 입력해 주세요.")
                value = answer.lower() in {"y", "yes"}
            else:
                value = answer
            if field in {"allowed_capabilities", "denied_capabilities"}:
                unknown = set(value) - set(CANONICAL_CAPABILITIES)
                if unknown:
                    raise ValueError("화면에 표시된 기능 이름을 입력해 주세요.")
            return EngagementDraft.model_validate({**draft.model_dump(), field: value})
        except (ValueError, ValidationError) as exc:
            console.print(str(exc))


def _edit_fields(console, draft: EngagementDraft) -> EngagementDraft:
    fields = list(QUESTIONS)
    console.print("Enter: 저장 / q: 취소. 값 입력에서 Enter: 유지 / -: 비우기.")
    while True:
        for index, field in enumerate(fields, 1):
            console.print(f"{index}. {QUESTIONS[field]}: {getattr(draft, field)}")
        choice = console.input("수정할 항목 번호 (Enter: 저장): ").strip()
        if not choice:
            return draft
        if choice.lower() in {"q", "quit"}:
            raise Cancelled
        if not choice.isdigit() or not 1 <= int(choice) <= len(fields):
            console.print("표시된 항목 번호를 입력해 주세요.")
            continue
        draft = _ask(console, draft, fields[int(choice) - 1], editing=True)


def collect_engagement(console, existing: EngagementConfig | None = None):
    """Collect explicit values, convert once, and preserve unrelated edit settings."""
    try:
        draft = engagement_config_to_draft(existing) if existing else EngagementDraft()
        before = draft.model_copy(deep=True)
        if existing:
            draft = _edit_fields(console, draft)
        else:
            console.print(
                "작업 설명과 목표를 입력하세요. 취소: q. 세부 설정은 이후 편집 가능합니다."
            )
            for field in ("description", "objective"):
                draft = _ask(console, draft, field, editing=False)
            hosts = list(explicit_target_origins(draft.description or ""))
            draft = draft.model_copy(update={"allowed_hosts": hosts})
            while not draft.allowed_hosts:
                console.print("설명에서 대상 주소를 찾지 못했습니다.")
                draft = _ask(console, draft, "allowed_hosts", editing=False)
            draft = draft.model_copy(
                update={
                    "name": draft.allowed_hosts[0],
                    "max_actions": OrchestrationConfig.from_environment().max_steps,
                }
            )
        while True:
            try:
                result = draft_to_engagement_config(draft)
                break
            except (DraftIncompleteError, ValidationError, ValueError) as exc:
                console.print(f"설정을 확인해 주세요: {exc}")
                draft = _edit_fields(console, draft)
        for operation in result.unenforced_operations:
            console.print(f"자동 집행되지 않는 제약으로 기록됨: {operation}")
        config = result.engagement_config
        if existing:
            # The serializer recognizes its own generated rule. Preserve other
            # contract fields and metadata rather than rebuilding the engagement.
            old_rule = existing.rules_of_engagement[0]
            rule_fields = (
                "allowed_capabilities",
                "denied_capabilities",
                "denied_operations",
                "availability_impact_allowed",
                "notes",
            )
            rule = old_rule.model_copy(
                update={
                    field: getattr(config.rules_of_engagement[0], field) for field in rule_fields
                }
            )
            scope = existing.scope.model_copy(
                update={
                    field: getattr(config.scope, field)
                    for field in ("allowed_hosts", "allowed_domains", "allowed_networks")
                }
            )
            config = EngagementConfig.model_validate(
                {
                    **existing.model_dump(),
                    "name": config.name,
                    "description": config.description,
                    "scope": scope,
                    "rules_of_engagement": [rule],
                    "execution_budget": config.execution_budget,
                    "metadata": {
                        **existing.metadata,
                        "focus_areas": draft.focus_areas,
                        **({"objective": draft.objective} if draft.objective else {}),
                    },
                }
            )
            if not draft.objective:
                config.metadata.pop("objective", None)
            for field, (added, removed) in scope_diff(before, draft).items():
                console.print(f"범위 변경 {field}: 추가 {added}, 제거 {removed}")
        return config
    except Cancelled:
        return None
