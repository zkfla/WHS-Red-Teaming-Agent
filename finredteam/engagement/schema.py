"""engagement.yaml 의 구조화 스키마. 전부 bool/enum/숫자/list 로 정규화한다 —
코드가 판단할 수 없는 자유 텍스트 뭉치는 `guidance`(소프트 가이드라인, LLM
프롬프트에만 실린다) 하나로 한정한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class EngagementTarget(BaseModel):
    name: str  # targets/configs/<name>.yaml — 이 Engagement 가 쓸 어댑터 설정
    base_url: str


class EngagementScope(BaseModel):
    allowed_hosts: list[str] = Field(default_factory=list)
    denied_hosts: list[str] = Field(default_factory=list)


class EngagementExternalServices(BaseModel):
    allowed: bool = False


class EngagementRequests(BaseModel):
    max_requests_per_second: float = 3.0
    max_concurrency: int = 1
    max_amount: Optional[Decimal] = None  # None = 전역 상한(config.MAX_AMOUNT)만 적용


class EngagementActions(BaseModel):
    allow_state_change: bool = False
    allow_account_creation: bool = False
    allow_file_upload: bool = False
    allow_delete: bool = False
    allow_race_testing: bool = False
    allow_dos: bool = False
    allow_bruteforce: bool = False


class EngagementDataAccess(BaseModel):
    allow_real_user_data: bool = False
    allow_raw_response_storage: bool = True  # evidence_ref 체인에 필요 — 기본 허용


class EngagementCleanup(BaseModel):
    required: bool = True
    resource_types: list[str] = Field(default_factory=list)


class EngagementStopConditions(BaseModel):
    repeated_5xx: bool = True
    repeated_5xx_threshold: int = 5
    latency_degradation: bool = False
    unexpected_sensitive_data: bool = False
    external_side_effect: bool = False
    request_budget_exceeded: bool = True


class Engagement(BaseModel):
    engagement_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    approved_at: datetime | None = None

    target: EngagementTarget
    scope: EngagementScope = EngagementScope()
    external_services: EngagementExternalServices = EngagementExternalServices()
    requests: EngagementRequests = EngagementRequests()
    actions: EngagementActions = EngagementActions()
    data_access: EngagementDataAccess = EngagementDataAccess()
    cleanup: EngagementCleanup = EngagementCleanup()
    stop_conditions: EngagementStopConditions = EngagementStopConditions()

    # 소프트 가이드라인 — ReAct 시스템 프롬프트에만 실린다, 코드가 강제하지 않는다 (§6).
    guidance: list[str] = Field(default_factory=list)

    @property
    def is_approved(self) -> bool:
        return self.approved_at is not None
