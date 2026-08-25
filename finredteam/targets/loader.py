"""targets/configs/*.yaml → TargetConfig.

사람이 손으로 확인해 적어둔 값을 읽기만 한다 — 에이전트 코드가 대상의 소스나
DB를 직접 읽는 일은 없다 (§설계원칙 2, §20).

observation/entitlement/baseline_ops/discovery 등 이후 Day에서 쓰는 하위 설정은
아직 전용 스키마로 타입화하지 않고 `extra="allow"` 로 원시 dict 그대로 보존한다 —
Day3(entitlement)·Day4(baseline)에서 필요한 만큼만 전용 파서를 추가한다.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from finredteam.config import LLMConfig
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
    auth: AuthConfig


class TargetSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    principals: list[str]
    initial_balance: Decimal
    layers: list[str] = ["generic", "finance"]
    profile: str = "sandbox"
    llm: LLMConfig = LLMConfig()


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
