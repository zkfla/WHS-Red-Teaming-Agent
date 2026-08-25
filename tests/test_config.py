"""config.py 안전장치 단위 테스트 — §18 조건 17: allowlist 밖 호스트는 즉시 실패."""
from __future__ import annotations

import pytest

from finredteam.config import AllowedHostError, assert_allowed_host


def test_allowed_hosts_pass():
    assert_allowed_host("http://localhost:5000/transfer")
    assert_allowed_host("http://127.0.0.1:5000/check_balance/1")


def test_disallowed_host_raises_immediately():
    with pytest.raises(AllowedHostError):
        assert_allowed_host("http://evil.example.com/steal")


def test_disallowed_host_with_similar_looking_domain_raises():
    # "localhost.evil.com" 같은 편법도 막혀야 한다 — hostname 전체 비교이지 부분일치가 아니다.
    with pytest.raises(AllowedHostError):
        assert_allowed_host("http://localhost.evil.com/transfer")
