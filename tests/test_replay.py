"""replay.py 의 순수 로직(run 경로 해석, verdict 색상, LLM 호출 step 스탬핑) 테스트.

rich 출력 자체(색깔이 실제로 어떻게 그려지는지)는 검증하지 않는다 — 데이터를
올바르게 골라내는지만 본다.
"""
from __future__ import annotations

import json

import pytest

from finredteam.replay import _resolve_run_dir, _verdict_style


def _make_run(tmp_path, run_id: str) -> None:
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")


def test_resolve_run_dir_by_bare_id(tmp_path, monkeypatch):
    _make_run(tmp_path, "20260101-000000")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("finredteam.replay._RUNS_ROOT", tmp_path)
    resolved = _resolve_run_dir("20260101-000000")
    assert resolved.resolve() == (tmp_path / "20260101-000000").resolve()


def test_resolve_run_dir_by_direct_path(tmp_path, monkeypatch):
    _make_run(tmp_path, "20260101-000000")
    run_dir = tmp_path / "20260101-000000"
    resolved = _resolve_run_dir(str(run_dir))
    assert resolved == run_dir


def test_resolve_run_dir_latest_picks_most_recently_modified(tmp_path, monkeypatch):
    import time

    _make_run(tmp_path, "20260101-000000")
    time.sleep(0.01)
    _make_run(tmp_path, "20260102-000000")
    monkeypatch.setattr("finredteam.replay._RUNS_ROOT", tmp_path)

    resolved = _resolve_run_dir("latest")
    assert resolved == tmp_path / "20260102-000000"


def test_resolve_run_dir_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("finredteam.replay._RUNS_ROOT", tmp_path)
    with pytest.raises(SystemExit):
        _resolve_run_dir("no-such-run")


def test_resolve_run_dir_latest_empty_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("finredteam.replay._RUNS_ROOT", tmp_path)
    with pytest.raises(SystemExit):
        _resolve_run_dir("latest")


@pytest.mark.parametrize(
    "observation,expected",
    [
        ("INV-2: VIOLATION — 어쩌구", "bold red"),
        ("INV-1C: ANOMALY — 저쩌구", "bold yellow"),
        ("이상 없음 (PASS: ['INV-1A'])", "green"),
        ("입력 오류: 뭔가 실패", "yellow"),
        ("가설 등록됨: ...", "white"),
    ],
)
def test_verdict_style_maps_observation_to_color(observation, expected):
    assert _verdict_style(observation) == expected


def test_llm_trace_hook_stamps_step_per_call_started():
    """__main__.py::_make_trace_hook 이 CALL_STARTED 마다 step 을 하나씩 올리고,
    같은 호출의 ATTEMPT_RESPONSE/CALL_FINISHED 는 같은 step 을 갖는지 확인한다."""
    from finredteam.__main__ import _make_trace_hook
    from finredteam.llm.base import LLMEvent, LLMEventKind
    from finredteam.storage.db import DB
    from rich.console import Console
    import io

    class _FakeStore:
        def __init__(self, db):
            self.db = db

        def append(self, collection, record):
            return self.db.append(collection, record)

    def _make(kind):
        return LLMEvent(kind=kind, provider="anthropic", model="claude-x", attempt=1)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db = DB(Path(tmp))
        store = _FakeStore(db)
        quiet_console = Console(file=io.StringIO())
        hook = _make_trace_hook(store, quiet_console)

        # 첫 번째 스텝의 호출: started -> attempt_response -> finished
        hook(_make(LLMEventKind.CALL_STARTED))
        hook(_make(LLMEventKind.ATTEMPT_RESPONSE))
        hook(_make(LLMEventKind.CALL_FINISHED))
        # 두 번째 스텝의 호출
        hook(_make(LLMEventKind.CALL_STARTED))
        hook(_make(LLMEventKind.CALL_FINISHED))

        records = list(db.read_all("llm_calls"))
        assert [r["step"] for r in records] == [1, 1, 1, 2, 2]
