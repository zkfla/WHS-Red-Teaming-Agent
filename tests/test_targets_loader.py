"""targets/loader.py::minimal_target_config — 어댑터 YAML이 없어도 실행이 막히지
않게 하는 최소 구성(§ Human pre-analysis 제거)."""
from __future__ import annotations

from finredteam.targets.loader import load_target, minimal_target_config


def test_minimal_target_config_has_no_auth_section():
    cfg = minimal_target_config("newtarget", "http://localhost:9999")
    assert cfg.adapter.auth is None
    assert cfg.adapter.base_url == "http://localhost:9999"
    assert cfg.adapter.default_currency.code == "USD"


def test_minimal_target_config_generates_default_principal_names():
    cfg = minimal_target_config("newtarget", "http://localhost:9999")
    assert cfg.settings.principals == ["principal_A", "principal_B", "principal_C"]


def test_minimal_target_config_respects_larger_principal_count():
    cfg = minimal_target_config("newtarget", "http://localhost:9999", principal_count=5)
    assert len(cfg.settings.principals) == 5
    assert len(set(cfg.settings.principals)) == 5  # 전부 서로 달라야 함


def test_minimal_target_config_empty_extra_configs_do_not_crash():
    cfg = minimal_target_config("newtarget", "http://localhost:9999")
    assert cfg.observation_config() == {}
    assert cfg.baseline_ops() == []
    assert cfg.discovery_config() == {}
    assert cfg.cleanup_ops() == {}


def test_load_target_still_works_for_existing_adapter_yaml():
    # 회귀: 어댑터 YAML이 있는 기존 대상(vulnbank)은 여전히 그대로 로드된다 —
    # minimal_target_config 도입이 기존 경로를 안 건드렸는지 확인.
    cfg = load_target("vulnbank")
    assert cfg.adapter.auth is not None
    assert cfg.adapter.auth.login.path == "/login"
