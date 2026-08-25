"""runs/<run_id>/manifest.json — LLM provider/model, 종료 사유, 라운드 수,
전략 전환 이력, 토큰·요청 수를 남긴다. 리포트는 이 매니페스트만 읽는다 (§10).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from finredteam.config import RunConfig
from finredteam.llm.base import Usage
from finredteam.storage.db import DB


class RunStore:
    def __init__(self, run_id: str, run_dir: Path, db: DB, manifest: dict[str, Any]) -> None:
        self.run_id = run_id
        self.run_dir = run_dir
        self.db = db
        self.manifest = manifest

    @classmethod
    def start(cls, cfg: RunConfig, runs_root: Path = Path("runs")) -> "RunStore":
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_dir = runs_root / run_id
        db = DB(run_dir)
        manifest: dict[str, Any] = {
            "run_id": run_id,
            "target": cfg.target_name,
            "smoke": cfg.smoke,
            "llm": {"provider": cfg.llm.provider, "model": cfg.llm.model},
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "round": 0,
            "strategy_history": [],
            "termination_reason": None,
            "tokens_in": 0,
            "tokens_out": 0,
            "requests": 0,
            "cost_estimate_usd": 0.0,
        }
        store = cls(run_id, run_dir, db, manifest)
        store._write_manifest()
        return store

    def _write_manifest(self) -> None:
        (self.run_dir / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def append(self, collection: str, record: dict[str, Any]) -> int:
        return self.db.append(collection, record)

    def set_llm_display(self, provider: str, model: str) -> None:
        """실제로 쓰인 LLM 클라이언트를 manifest 에 정확히 남긴다.

        `RunConfig.llm`(config.LLMConfig)은 provider 를 anthropic|openai 로만
        허용하므로(§2), 그 타입에 담을 수 없는 값(예: 테스트용 fake 클라이언트)을
        쓴 경우 이 메서드로 manifest 표시값만 따로 갱신한다.
        """
        self.manifest["llm"] = {"provider": provider, "model": model}
        self._write_manifest()

    def record_strategy(self, round_no: int, strategy_name: str) -> None:
        self.manifest["strategy_history"].append({"round": round_no, "strategy": strategy_name})
        self._write_manifest()

    def snapshot_round(self, round_no: int, extra: dict[str, Any] | None = None) -> None:
        self.manifest["round"] = round_no
        if extra:
            # 얕은 복사 — 호출부가 자기 쪽에서 계속 append 하는 list/dict 를 그대로
            # 넘기면, manifest 와 같은 객체를 공유하게 되어(list aliasing) 이후
            # record_strategy() 같은 다른 메서드의 변경이 호출부 객체까지 오염시킨다.
            self.manifest.update({k: (v.copy() if isinstance(v, (list, dict)) else v) for k, v in extra.items()})
        self._write_manifest()

    def finalize(self, termination_reason: str, usage: Usage | None = None) -> Path:
        self.manifest["termination_reason"] = termination_reason
        self.manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        if usage is not None:
            self.manifest["tokens_in"] = usage.tokens_in
            self.manifest["tokens_out"] = usage.tokens_out
            self.manifest["requests"] = usage.requests
            self.manifest["cost_estimate_usd"] = usage.cost_estimate_usd
        self._write_manifest()
        return self.run_dir
