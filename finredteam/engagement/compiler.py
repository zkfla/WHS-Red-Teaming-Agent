"""engagement.yaml(승인된 Engagement) → CompiledPolicy.

Global Safety Ceiling ∩ Engagement Policy = Effective Policy (§9). 숫자는
`min(engagement, 전역상수)`, "허용계열" 불리언은 AND(둘 다 True 여야 허용),
"위험계열"(DoS/레이스/무차별대입) 불리언은 전역 상한 자체가 False 로 고정돼
있어 Engagement 가 뭐라 하든 항상 False 다 — 지금 코드베이스에 그걸 실행할
액션 자체가 없다는 것도 §17 한계로 문서화돼 있다.
"""
from __future__ import annotations

import hashlib
import json

from finredteam import config
from finredteam.engagement.policy import CompiledPolicy
from finredteam.engagement.schema import Engagement


def _policy_hash(fields: dict) -> str:
    canonical = json.dumps(fields, sort_keys=True, default=str, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compile_policy(engagement: Engagement) -> CompiledPolicy:
    fields = {
        "engagement_id": engagement.engagement_id,
        "allowed_hosts": sorted(engagement.scope.allowed_hosts),
        "denied_hosts": sorted(engagement.scope.denied_hosts),
        "external_services_allowed": engagement.external_services.allowed,
        "allow_state_change": engagement.actions.allow_state_change,
        "allow_account_creation": engagement.actions.allow_account_creation,
        "allow_file_upload": engagement.actions.allow_file_upload,
        "allow_delete": engagement.actions.allow_delete,
        # 위험 계열 — 전역 상한 자체가 False 라 항상 AND 결과가 False.
        "allow_race_testing": engagement.actions.allow_race_testing and config.GLOBAL_ALLOW_RACE_TESTING,
        "allow_dos": engagement.actions.allow_dos and config.GLOBAL_ALLOW_DOS,
        "allow_bruteforce": engagement.actions.allow_bruteforce and config.GLOBAL_ALLOW_BRUTEFORCE,
        "max_requests_per_second": min(
            engagement.requests.max_requests_per_second, config.MAX_REQUESTS_PER_SECOND
        ),
        "max_concurrency": min(engagement.requests.max_concurrency, config.MAX_CONCURRENT),
        "max_amount": str(
            min(engagement.requests.max_amount, config.MAX_AMOUNT)
            if engagement.requests.max_amount is not None
            else config.MAX_AMOUNT
        ),
        "allow_real_user_data": engagement.data_access.allow_real_user_data,
        "allow_raw_response_storage": engagement.data_access.allow_raw_response_storage,
        "cleanup_required": engagement.cleanup.required,
        "cleanup_resource_types": sorted(engagement.cleanup.resource_types),
        "stop_repeated_5xx": engagement.stop_conditions.repeated_5xx,
        "stop_repeated_5xx_threshold": engagement.stop_conditions.repeated_5xx_threshold,
        "stop_request_budget_exceeded": engagement.stop_conditions.request_budget_exceeded,
    }

    return CompiledPolicy(
        engagement_id=engagement.engagement_id,
        allowed_hosts=frozenset(engagement.scope.allowed_hosts),
        denied_hosts=frozenset(engagement.scope.denied_hosts),
        external_services_allowed=fields["external_services_allowed"],
        allow_state_change=fields["allow_state_change"],
        allow_account_creation=fields["allow_account_creation"],
        allow_file_upload=fields["allow_file_upload"],
        allow_delete=fields["allow_delete"],
        allow_race_testing=fields["allow_race_testing"],
        allow_dos=fields["allow_dos"],
        allow_bruteforce=fields["allow_bruteforce"],
        max_requests_per_second=fields["max_requests_per_second"],
        max_concurrency=fields["max_concurrency"],
        max_amount=min(engagement.requests.max_amount, config.MAX_AMOUNT)
        if engagement.requests.max_amount is not None
        else config.MAX_AMOUNT,
        allow_real_user_data=fields["allow_real_user_data"],
        allow_raw_response_storage=fields["allow_raw_response_storage"],
        cleanup_required=fields["cleanup_required"],
        cleanup_resource_types=tuple(sorted(engagement.cleanup.resource_types)),
        stop_repeated_5xx=fields["stop_repeated_5xx"],
        stop_repeated_5xx_threshold=fields["stop_repeated_5xx_threshold"],
        stop_request_budget_exceeded=fields["stop_request_budget_exceeded"],
        guidance=tuple(engagement.guidance),
        policy_hash=_policy_hash(fields),
    )
