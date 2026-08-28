"""Small local web preview; all execution goes through :class:`RunService`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from whs_redteam.run_service import RunService

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse
    from pydantic import BaseModel, ConfigDict, Field
except ImportError as exc:  # pragma: no cover - exercised when the optional extra is absent
    raise RuntimeError(
        "FastAPI preview is optional; install with `pip install -e '.[web]'`."
    ) from exc


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_url: str = Field(min_length=8, max_length=2_048)
    follow_links: bool = False
    allow_private_dns: bool = False
    planner_mode: Literal["rules", "hybrid"] = "rules"
    ai_base_url: str = "http://127.0.0.1:11434/v1"
    ai_model: str = ""
    ai_api_key_env: str | None = None
    ai_report: bool = False


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=16, max_length=16_384)


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>WHS Red Team Preview</title>
<style>
body{font:16px system-ui;max-width:900px;margin:2rem auto;padding:0 1rem}
input{width:70%;padding:.5rem}button{padding:.5rem}
.card{background:#f4f4f4;padding:1rem;margin:1rem 0;white-space:pre-wrap}
code{word-break:break-all}
</style></head>
<body><h1>WHS Red-Teaming Preview</h1>
<p>Use only an owned or explicitly authorised target.</p>
<p>Probes are bounded and require signed approval.</p>
<form id="run">
<input id="url" type="url" placeholder="http://127.0.0.1:8765/sql?id=1" required>
<label><input id="private" type="checkbox"> allow local/private DNS</label>
<label>planner <select id="planner"><option>rules</option><option>hybrid</option></select></label>
<input id="model" placeholder="AI model (optional)">
<input id="key-env" placeholder="API key env name (optional)">
<label><input id="ai-report" type="checkbox"> AI explanation</label>
<button>Start</button></form>
<div id="output" class="card">Idle</div>
<script>
const out=document.getElementById('output');
function show(x){out.textContent=JSON.stringify(x,null,2)}
document.getElementById('run').onsubmit=async(e)=>{
 e.preventDefault();
 const r=await fetch('/api/runs',{method:'POST',headers:{'content-type':'application/json'},
 body:JSON.stringify({target_url:document.getElementById('url').value,
 allow_private_dns:document.getElementById('private').checked,
 planner_mode:document.getElementById('planner').value,
 ai_model:document.getElementById('model').value,
 ai_api_key_env:document.getElementById('key-env').value || null,
 ai_report:document.getElementById('ai-report').checked})});
 show(await r.json());
};
</script></body></html>"""


def create_app() -> FastAPI:
    app = FastAPI(
        title="WHS Red-Teaming Preview",
        description="Local preview for the safety-bounded SQLi pipeline.",
        version="0.1.0",
    )
    services: dict[str, RunService] = {}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _INDEX_HTML

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/ai/health")
    async def ai_health() -> dict[str, object]:
        service = next(reversed(services.values()), None)
        if service is None:
            return {
                "configured": False,
                "provider": "none",
                "model": None,
                "reachable": None,
            }
        ai = service.engagement.ai
        return {
            "configured": ai.provider != "none",
            "provider": ai.provider,
            "model": ai.model or None,
            "base_url": ai.base_url if ai.provider != "none" else None,
            "reachable": None,
            "note": "Provider reachability is checked during planning; this endpoint is read-only.",
        }

    @app.post("/api/runs")
    async def start_run(request: RunRequest) -> dict[str, Any]:
        try:
            service = RunService.from_sqli_url(
                request.target_url,
                output_dir=Path("outputs/web-runs"),
                discovery_mode="http",
                follow_discovered_links=request.follow_links,
                allow_private_dns=request.allow_private_dns,
                planner_mode=request.planner_mode,
                ai_base_url=request.ai_base_url,
                ai_model=request.ai_model,
                ai_api_key_env=request.ai_api_key_env,
                report_writer_enabled=request.ai_report,
            )
            result = await service.run()
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        services[result.run.run_id] = service
        return result.to_dict()

    def service_for(run_id: str) -> RunService:
        service = services.get(run_id)
        if service is None:
            raise HTTPException(
                status_code=404,
                detail="run is not in this preview process; restart from its config with the CLI",
            )
        return service

    @app.get("/api/runs/{run_id}")
    async def run_status(run_id: str) -> dict[str, Any]:
        try:
            return service_for(run_id).status(run_id).to_dict()
        except HTTPException:
            raise
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/actions")
    async def pending_actions(run_id: str) -> dict[str, Any]:
        try:
            actions = service_for(run_id).pending_approvals(run_id)
            return {"run_id": run_id, "actions": actions}
        except HTTPException:
            raise
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/actions/{queue_id}/approve")
    async def approve_action(
        run_id: str,
        queue_id: str,
        request: ApprovalRequest,
    ) -> dict[str, Any]:
        try:
            return service_for(run_id).approve(run_id, queue_id, request.token)
        except HTTPException:
            raise
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(run_id: str) -> dict[str, Any]:
        try:
            result = await service_for(run_id).run(run_id)
            return result.to_dict()
        except HTTPException:
            raise
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/report")
    async def run_report(run_id: str) -> dict[str, Any]:
        try:
            service = service_for(run_id)
            result = service.status(run_id)
            if not result.run.findings_path:
                raise HTTPException(status_code=409, detail="report is not ready")
            report_path = Path(result.run.findings_path).resolve()
            root = service.engagement.inventory_db_path.parent.resolve()
            if root not in report_path.parents:
                raise HTTPException(
                    status_code=403,
                    detail="report path is outside the run directory",
                )
            return json.loads(report_path.read_text(encoding="utf-8"))
        except HTTPException:
            raise
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


__all__ = ["create_app"]
