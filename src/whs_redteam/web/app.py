"""Small local web preview; all execution goes through :class:`RunService`."""

from __future__ import annotations

import json
from html import escape
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
body{font:16px system-ui;max-width:900px;margin:2rem auto;padding:0 1rem;color:#172033}
h1{margin-bottom:.35rem}.lead{margin:.25rem 0;color:#4b5563}
form{display:grid;gap:1rem;background:#f7f8fa;border:1px solid #d9dde5;
padding:1.25rem;border-radius:.75rem;margin-top:1.25rem}
.field{display:grid;gap:.35rem}.field label{font-weight:650}
.field input{box-sizing:border-box;width:100%;padding:.65rem;border:1px solid #aeb6c5;
border-radius:.4rem;background:white}
.options{display:flex;gap:1.25rem;align-items:center;flex-wrap:wrap}
.options label{display:flex;gap:.45rem;align-items:center}
select,button{padding:.55rem .7rem;border:1px solid #8892a3;border-radius:.4rem}
button{width:max-content;background:#1d4ed8;color:white;border-color:#1d4ed8;font-weight:650}
.card{background:#111827;color:#d1fae5;padding:1rem;margin:1rem 0;white-space:pre-wrap;
border-radius:.75rem;min-height:6rem;overflow:auto}
code{word-break:break-all}
</style></head>
<body><h1>WHS Red-Teaming Preview</h1>
<p class="lead">Use only an owned or explicitly authorised target.</p>
<p class="lead">Probes are bounded and require signed approval.</p>
<form id="run">
<div class="field"><label for="url">Authorised target URL</label>
<input id="url" type="url" placeholder="http://127.0.0.1:8765/sql?id=1" required></div>
<div class="options">
<label><input id="private" type="checkbox"> Allow local/private DNS</label>
<label>Planner <select id="planner"><option>rules</option><option>hybrid</option></select></label>
<label><input id="ai-report" type="checkbox"> AI explanation</label>
</div>
<div class="field"><label for="model">AI model</label>
<input id="model" placeholder="AI model (optional)"></div>
<div class="field"><label for="key-env">API key environment variable</label>
<input id="key-env" placeholder="Environment variable name only (optional)"></div>
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


def _report_page_html(report: dict[str, Any]) -> str:
    planning = report.get("planning") or {}
    decision = report.get("exploitability_decision") or {}
    findings = report.get("findings") or []
    ai_report = report.get("ai_report") or {}
    explanation = report.get("ai_explanation") or {}

    def text(value: object) -> str:
        return escape(str(value), quote=True)

    finding_rows = "".join(
        "<tr>"
        f"<td>{text(item.get('category', 'unknown'))}</td>"
        f"<td>{text(item.get('status', 'unknown'))}</td>"
        f"<td>{text(item.get('severity', 'unknown'))}</td>"
        f"<td>{text(item.get('reproduction_count', 0))}</td>"
        f"<td>{text(item.get('negative_control_passed', False))}</td>"
        "</tr>"
        for item in findings
        if isinstance(item, dict)
    ) or '<tr><td colspan="5">No findings recorded.</td></tr>'
    run_id = text(report.get("run_id", ""))
    verdict = text(decision.get("verdict", "UNKNOWN"))
    planner_mode = text(planning.get("mode", "rules"))
    planner_provider = text(planning.get("provider", "rules"))
    planner_model = text(planning.get("model", "deterministic"))
    api_calls = text(planning.get("api_calls", 0))
    input_tokens = text(planning.get("input_tokens", 0))
    output_tokens = text(planning.get("output_tokens", 0))
    report_api_calls = text(ai_report.get("api_calls", 0))
    report_input_tokens = text(ai_report.get("input_tokens", 0))
    report_output_tokens = text(ai_report.get("output_tokens", 0))
    rationale = text(decision.get("rationale", ""))
    bounded = text(decision.get("bounded_exploit_demonstrated", False))
    exploit_chain = text(decision.get("exploit_chain_demonstrated", False))
    ai_summary = text(explanation.get("summary", "Not generated"))
    ai_limitations = text(explanation.get("limitations", ""))
    ai_next_steps = text(explanation.get("next_steps", ""))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>WHS Run Report</title>
<style>
body{{font:16px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#172033}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:1rem}}
.card{{border:1px solid #d9dde5;border-radius:.75rem;padding:1rem;background:#f7f8fa}}
.value{{font-size:1.2rem;font-weight:750;margin-top:.35rem}}
.confirmed{{color:#b42318}}table{{width:100%;border-collapse:collapse;margin-top:1rem}}
th,td{{text-align:left;border-bottom:1px solid #d9dde5;padding:.7rem}}
.ai{{border-left:4px solid #2563eb;padding-left:1rem}}code{{word-break:break-all}}
</style></head><body>
<h1>WHS Red-Teaming Run Report</h1>
<p>Run <code>{run_id}</code></p>
<div class="grid">
<div class="card"><div>Machine verdict</div>
<div class="value confirmed">{verdict}</div></div>
<div class="card"><div>Planner</div>
<div class="value">{planner_mode} / {planner_provider}</div></div>
<div class="card"><div>Model</div><div class="value">{planner_model}</div></div>
<div class="card"><div>Planner AI usage</div>
<div class="value">{api_calls} call · {input_tokens}/{output_tokens} tokens</div></div>
<div class="card"><div>Report AI usage</div>
<div class="value">{report_api_calls} call ·
{report_input_tokens}/{report_output_tokens} tokens</div></div>
</div>
<h2>Validated findings</h2>
<table><thead><tr><th>Category</th><th>Status</th><th>Severity</th>
<th>Reproductions</th><th>Negative control</th></tr></thead>
<tbody>{finding_rows}</tbody></table>
<h2>Bounded exploitability</h2>
<p>{rationale}</p>
<p>Bounded primitive demonstrated: <strong>{bounded}</strong> ·
Full exploit chain: <strong>{exploit_chain}</strong></p>
<h2>AI explanation</h2>
<div class="ai"><p><strong>Summary:</strong> {ai_summary}</p>
<p><strong>Limitations:</strong> {ai_limitations}</p>
<p><strong>Next steps:</strong> {ai_next_steps}</p></div>
</body></html>"""


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
        if service is not None:
            return service
        for config_path in Path("outputs/web-runs").glob("*/engagement.yaml"):
            candidate = RunService.from_config_path(config_path)
            try:
                candidate.status(run_id)
            except (ValueError, RuntimeError):
                continue
            services[run_id] = candidate
            return candidate
        raise HTTPException(
            status_code=404,
            detail="run was not found in the persisted web preview profiles",
        )

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

    @app.get("/runs/{run_id}/report", response_class=HTMLResponse)
    async def run_report_page(run_id: str) -> str:
        return _report_page_html(await run_report(run_id))

    return app


__all__ = ["create_app"]
