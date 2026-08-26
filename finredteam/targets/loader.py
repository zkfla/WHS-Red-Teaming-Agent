"""targets/configs/*.yaml → TargetConfig — Legacy Target Adapter.

★ Discovery(recon/auth_discovery.py 등)가 실패했을 때만 쓰이는 폴백이다 —
로그인/회원가입 endpoint, 필드명, token 경로, 인증 헤더 형식, 정상 송금
파라미터처럼 애플리케이션 내부 구조는 원칙적으로 Agent가 Black-box Recon으로
직접 발견한다. base_url/allowed_hosts/credentials 처럼 사람이 제공해도 되는
정보는 여기가 아니라 Engagement가 소유한다(finredteam/engagement/schema.py).

observation/baseline_ops/discovery 등의 하위 설정은 아직 전용 스키마로
타입화하지 않고 `extra="allow"` 로 원시 dict 그대로 보존한다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from finredteam.money import Currency


class AuthEndpoint(BaseModel):
    model_config = ConfigDict(extra="allow")

    method: str
    path: str
    content_type: str = "application/json"
    fields: dict[str, str] = {}
    token_path: str | None = None
    account_number_path: str | None = None
    initial_balance_path: str | None = None


class AuthConfig(BaseModel):
    # "register" 필드명은 pydantic 의 ABC 기반 메타클래스가 물려받는
    # abc.ABCMeta.register 클래스메서드를 가려서 경고가 뜬다 — alias 로 우회한다.
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    register_ep: AuthEndpoint = Field(alias="register")
    login: AuthEndpoint
    token_header: str


class AdapterConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    base_url: str
    default_currency: Currency
    # ★ 없어도 된다 — Black-box Auth Discovery(recon/auth_discovery.py)가
    # 실행 중에 직접 로그인/회원가입 방법을 찾는다. 이게 None 인데 discovery
    # 도 실패하면 `probing/session.py`가 그 시점에 명확한 SessionError 를
    # 낸다(조용히 죽지 않는다).
    auth: AuthConfig | None = None


class TargetSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    principals: list[str]


class TargetConfig(BaseModel):
    adapter: AdapterConfig
    settings: TargetSettings

    def observation_config(self) -> dict[str, Any]:
        """Day3(recon)·Day4(baseline)가 쓸 원시 observation 설정. 아직 미검증."""
        return dict(self.adapter.model_extra or {}).get("observation", {})

    def entitlement_config(self) -> dict[str, Any]:
        return dict(self.adapter.model_extra or {}).get("entitlement", {})

    def baseline_ops(self) -> list[dict[str, Any]]:
        return dict(self.adapter.model_extra or {}).get("baseline_ops", [])

    def discovery_config(self) -> dict[str, Any]:
        return dict(self.adapter.model_extra or {}).get("discovery", {})

    def cleanup_ops(self) -> dict[str, dict[str, Any]]:
        """resource_type -> {method, path(= "{resource_id}" 플레이스홀더 포함)}.
        정의 안 된 타겟(예: vuln-bank — 계정 삭제 엔드포인트 자체가 없음)은
        빈 dict — `engagement/cleanup.py` 가 이를 "정리 방법 미설정"으로
        정직하게 기록한다."""
        return dict(self.adapter.model_extra or {}).get("cleanup_ops", {})


def load_target_config(path: Path) -> TargetConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return TargetConfig.model_validate(raw)


def load_target(name: str, configs_dir: Path | None = None) -> TargetConfig:
    base = configs_dir or (Path(__file__).parent / "configs")
    path = base / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"target config 없음: {path}")
    return load_target_config(path)


# principal 논리 이름은 대상 고유의 사전지식이 아니다 — 사람이 지어줄 필요 없이
# 자동 생성한다(§ Human pre-analysis 제거). Engagement 가 제공한 credentials
# 개수가 이보다 많으면 __main__.py 가 알아서 늘려 쓴다.
_DEFAULT_PRINCIPAL_NAMES = ["principal_A", "principal_B", "principal_C"]


def minimal_target_config(name: str, base_url: str, *, principal_count: int = 3) -> TargetConfig:
    """어댑터 YAML이 아예 없을 때 쓰는 최소 구성 — base_url만 있으면 된다.
    `auth`는 비워둔다(Black-box Auth Discovery가 실행 중에 직접 찾는다) —
    그 발견마저 실패하면 `probing/session.py`가 명확한 에러를 낸다, 조용히
    아무 일도 안 일어난 것처럼 넘어가지 않는다."""
    principals = list(_DEFAULT_PRINCIPAL_NAMES)
    while len(principals) < principal_count:
        principals.append(f"principal_{len(principals) + 1}")
    return TargetConfig(
        adapter=AdapterConfig(name=name, base_url=base_url, default_currency=Currency(code="USD", minor_unit=2)),
        settings=TargetSettings(principals=principals[:max(principal_count, 1)]),
    )
