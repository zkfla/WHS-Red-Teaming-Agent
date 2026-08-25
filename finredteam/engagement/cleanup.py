"""테스트 중 생성한 리소스 추적 + CLEANUP phase (§11).

`target.cleanup_ops()`(`targets/configs/*.yaml` 의 `adapter.cleanup_ops`, 아직
아무 타겟도 정의하지 않았다 — vuln-bank는 계정 삭제 엔드포인트 자체가 없다)에
해당 resource_type 의 정리 방법이 없으면, 조용히 넘어가지 않고 "정리 방법이
설정되어 있지 않음"을 정직하게 기록한다(§11 "실패한 경우 숨기지 말고 명시").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from finredteam.probing.session import Session, SessionError
from finredteam.targets.loader import TargetConfig


@dataclass
class CreatedResource:
    resource_type: str
    resource_id: str
    created_by_step: int
    principal_id: str | None = None


@dataclass
class CleanupRecord:
    resource_type: str
    resource_id: str
    cleanup_status: str  # "success" | "failed" | "not_configured"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "cleanup_status": self.cleanup_status,
            "detail": self.detail,
        }


def run_cleanup(
    session: Session,
    target: TargetConfig,
    resources: list[CreatedResource],
    principal_by_id: dict[str, Any],
) -> list[CleanupRecord]:
    ops = target.cleanup_ops()
    records: list[CleanupRecord] = []

    for resource in resources:
        op = ops.get(resource.resource_type)
        if op is None:
            records.append(
                CleanupRecord(
                    resource_type=resource.resource_type,
                    resource_id=resource.resource_id,
                    cleanup_status="not_configured",
                    detail=f"target adapter 에 '{resource.resource_type}' 정리 방법이 정의되어 있지 않음",
                )
            )
            continue

        path = op["path"].replace("{resource_id}", resource.resource_id)
        principal = principal_by_id.get(resource.principal_id)
        try:
            if principal is not None:
                ex = session.authed_request(op.get("method", "DELETE"), path, principal)
            else:
                ex = session.anonymous_request(op.get("method", "DELETE"), path)
            ok = ex.status_code < 400
            records.append(
                CleanupRecord(
                    resource_type=resource.resource_type,
                    resource_id=resource.resource_id,
                    cleanup_status="success" if ok else "failed",
                    detail=f"{op.get('method', 'DELETE')} {path} -> {ex.status_code}",
                )
            )
        except SessionError as exc:
            records.append(
                CleanupRecord(
                    resource_type=resource.resource_type,
                    resource_id=resource.resource_id,
                    cleanup_status="failed",
                    detail=str(exc),
                )
            )

    return records
