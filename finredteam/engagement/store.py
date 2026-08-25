"""Engagement 저장 — 사람용(`engagement.md`) + 코드용(`engagement.yaml`) +
감사용(`manifest.json`, §15) 세 파일을 항상 같이 만든다."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from finredteam.engagement.compiler import compile_policy
from finredteam.engagement.loader import load_engagement
from finredteam.engagement.schema import Engagement


def render_markdown(engagement: Engagement) -> str:
    a = engagement.actions
    s = engagement.stop_conditions

    allowed = [
        ("정상 사용자 기능 실행/인증", True),
        ("상태 변경 요청(POST/PUT/PATCH)", a.allow_state_change),
        ("테스트 계정 생성", a.allow_account_creation),
        ("파일 업로드 시험", a.allow_file_upload),
    ]
    forbidden = [
        ("DELETE / 데이터 삭제", not a.allow_delete),
        ("DoS·가용성 영향 시험", not a.allow_dos),
        ("brute-force / password spraying", not a.allow_bruteforce),
        ("race-condition/burst 시험", not a.allow_race_testing),
        ("제3자·외부 서비스 접근", not engagement.external_services.allowed),
        ("실제 사용자 데이터 접근", not engagement.data_access.allow_real_user_data),
    ]

    allowed_hosts_lines = [f"- {h}" for h in engagement.scope.allowed_hosts] or ["- (없음)"]
    denied_hosts_lines = [f"- {h}" for h in engagement.scope.denied_hosts] or ["- (없음)"]
    allowed_lines = [f"- {name}" for name, ok in allowed if ok] or ["- (없음)"]
    forbidden_lines = [f"- {name}" for name, ok in forbidden if ok] or ["- (없음)"]

    lines = [
        "# Rules of Engagement",
        "",
        f"- Engagement ID: `{engagement.engagement_id}`",
        f"- 생성: {engagement.created_at.isoformat()}",
        f"- 승인: {engagement.approved_at.isoformat() if engagement.approved_at else '(미승인)'}",
        "",
        "## Target",
        f"- 어댑터 설정: `targets/configs/{engagement.target.name}.yaml`",
        f"- {engagement.target.base_url}",
        "",
        "## In Scope (allowed_hosts)",
        *allowed_hosts_lines,
        "",
        "## Out of Scope (denied_hosts)",
        *denied_hosts_lines,
        "",
        "## Allowed Activities",
        *allowed_lines,
        "",
        "## Prohibited Activities",
        *forbidden_lines,
        "",
        "## Rate Limits",
        f"- Maximum requests per second: {engagement.requests.max_requests_per_second}",
        f"- Maximum concurrency: {engagement.requests.max_concurrency}",
        f"- Maximum amount per request: {engagement.requests.max_amount if engagement.requests.max_amount is not None else '(전역 상한만 적용)'}",
        "",
        "## Cleanup",
        (
            f"테스트 중 생성한 리소스({', '.join(engagement.cleanup.resource_types) or '미지정'})는 "
            "종료 후 정리한다." if engagement.cleanup.required else "종료 후 별도 정리를 요구하지 않는다."
        ),
        "",
        "## Stop Conditions",
        *(
            [f"- 반복적인 HTTP 5xx ({s.repeated_5xx_threshold}회 연속)"] if s.repeated_5xx else []
        ),
        *(["- 요청/토큰 예산 초과"] if s.request_budget_exceeded else []),
        *(["- 응답 지연 급증"] if s.latency_degradation else []),
        *(["- 예상하지 못한 실제 사용자 데이터 노출"] if s.unexpected_sensitive_data else []),
        *(["- 외부 시스템에 영향을 준 것으로 확인됨"] if s.external_side_effect else []),
    ]

    if engagement.guidance:
        lines += ["", "## Strategic Guidance (soft — 코드가 강제하지 않음)"]
        lines += [f"- {g}" for g in engagement.guidance]

    return "\n".join(lines) + "\n"


def _write_manifest(dir_path: Path, engagement: Engagement) -> None:
    policy_hash = compile_policy(engagement).policy_hash
    manifest = {
        "engagement_id": engagement.engagement_id,
        "created_at": engagement.created_at.isoformat(),
        "approved_at": engagement.approved_at.isoformat() if engagement.approved_at else None,
        "target": engagement.target.base_url,
        "policy_hash": policy_hash,
    }
    (dir_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_engagement(engagement: Engagement, root: Path = Path("engagements")) -> Path:
    dir_path = root / engagement.engagement_id
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "engagement.yaml").write_text(
        yaml.safe_dump(
            json.loads(engagement.model_dump_json()), allow_unicode=True, sort_keys=False
        ),
        encoding="utf-8",
    )
    (dir_path / "engagement.md").write_text(render_markdown(engagement), encoding="utf-8")
    _write_manifest(dir_path, engagement)
    return dir_path


def approve(dir_path: Path) -> Engagement:
    """`approved_at` 을 채워서 다시 저장한다 — 승인 전엔 아무 실행도 이 파일을
    참조할 수 없다(§4)."""
    engagement = load_engagement(dir_path)
    engagement = engagement.model_copy(update={"approved_at": datetime.now(timezone.utc)})
    save_engagement(engagement, root=dir_path.parent)
    return engagement
