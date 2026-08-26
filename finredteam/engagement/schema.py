"""engagement.yaml 의 구조화 스키마. 전부 bool/enum/숫자/list 로 정규화한다 —
코드가 판단할 수 없는 자유 텍스트 뭉치는 `guidance`(소프트 가이드라인, LLM
프롬프트에만 실린다) 하나로 한정한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class EngagementCredential(BaseModel):
    """사용자가 이미 갖고 있는 테스트 계정. username/password 만 받는다 — 이걸
    어떻게 쓰는지(로그인 endpoint/필드명/token 경로)는 여전히 Legacy Target
    Adapter(targets/configs/<name>.yaml)의 몫이다, 인터뷰가 묻지 않는다."""

    username: str
    password: str


class EngagementTarget(BaseModel):
    name: str  # targets/configs/<name>.yaml — 이 Engagement 가 쓸 어댑터 설정
    base_url: str
    # target.settings.principals 순서대로 매칭된다(§ engagement/setup.py 최종 확인
    # 화면에서 매핑을 보여준다) — 부족한 만큼은 allow_account_creation 정책에 따라
    # 자동 등록되거나 실패한다(agent/react.py::bootstrap_principals).
    credentials: list[EngagementCredential] = Field(default_factory=list)


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
    # 단발성 스파이크 오탐을 피하기 위해 baseline 대비 "연속" 초과만 잡는다
    # (agent/react.py::policy_stop_reason). 아직 실측 안정성이 검증되지 않아
    # 기본은 opt-in(False) — 신뢰도가 확인되면 기본값을 True로 올리는 걸 고려.
    latency_degradation: bool = False
    latency_degradation_multiplier: float = 3.0
    latency_degradation_consecutive_threshold: int = 5
    # 안전/Scope 보호에 직결되는 조건이라 기본 활성화 — CRITICAL 등급 필드
    # (password/cvv/card_number/... knowledge/sensitive.py) 노출 시에만 잡는다.
    unexpected_sensitive_data: bool = True
    unexpected_sensitive_data_threshold: int = 1
    # ★ Hard Stop 미지원 — 블랙박스 환경에서는 실제 외부(제3자) 부작용 발생을
    # 결정적으로 관측할 방법이 없다(out-of-scope 호스트 요청 자체는 이미
    # CompiledPolicy.check_request 가 원천 차단하므로 여기서 볼 게 없다).
    # agent/react.py 는 응답 본문 키워드 기반 best-effort 신호를 Observation에
    # 참고 노트로만 남길 뿐, 이 플래그는 policy_stop_reason() 에 연결되지 않는다.
    # 향후 결정적인 감지 방법이 생기면 Stop Condition으로 승격을 고려한다.
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
