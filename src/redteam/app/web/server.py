"""HTTP/SSE surface. Handlers translate requests into host calls; nothing more."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from redteam.app.cli.session_types import BindingRef
from redteam.app.web.engagement import (
    STEPS,
    EngagementError,
    EngagementStore,
    field_definitions,
    parse_draft,
)
from redteam.app.web.engagement_catalog import ID_PATTERN as _ENGAGEMENT_ID
from redteam.app.web.events import event_stream
from redteam.app.web.host import HostError, Rejected, SessionHost
from redteam.app.web.interview import InterviewError, InterviewService, default_llm_factory
from redteam.app.web.security import GuardMiddleware, GuardPolicy
from redteam.app.web.settings import SettingsError

API = "/api/v1"
STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 64 * 1024
MAX_INPUT_CHARS = 12000
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_CONTROLS = frozenset({"pause", "resume", "stop"})
_DECISIONS = frozenset({"approve", "deny"})
_INTERVIEW_STATUS = {
    "settings_required": 409,
    "session_expired": 404,
    "message_too_long": 413,
    "turn_limit": 429,
    "assistant_failed": 502,
}


def _error(status: int, error: str, /, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": error, **extra}, status_code=status)


async def _json_body(request: Request) -> dict[str, Any] | JSONResponse:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
        return _error(413, "body_too_large")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return _error(413, "body_too_large")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return _error(400, "invalid_json")
    if not isinstance(body, dict):
        return _error(400, "invalid_json")
    return body


def _binding(value: Any) -> BindingRef | None | JSONResponse:
    if value is None:
        return None
    if not isinstance(value, dict):
        return _error(400, "invalid_binding")
    start, attachment, run_id = (
        value.get("start_request_id"), value.get("attachment_id"), value.get("run_id")
    )
    if not (isinstance(start, str) and _ID.match(start)
            and isinstance(attachment, str) and _ID.match(attachment)
            and (run_id is None or (isinstance(run_id, str) and _ID.match(run_id)))):
        return _error(400, "invalid_binding")
    return BindingRef(start, attachment, run_id)


def _host_failure(exc: HostError) -> JSONResponse:
    # 429: the controller refused the input (queue full, closing). Everything
    # else is a state conflict the caller can resolve by reloading the snapshot.
    status = 429 if isinstance(exc, Rejected) and exc.code == "input_not_accepted" else 409
    return _error(status, exc.code)


def create_app(
    host: SessionHost, policy: GuardPolicy, interview: InterviewService | None = None
) -> Starlette:
    interviews = interview or InterviewService(default_llm_factory(host.settings))

    async def index(_: Request) -> Response:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")

    async def snapshot(_: Request) -> Response:
        return JSONResponse(host.snapshot())

    async def events(request: Request) -> Response:
        raw = request.headers.get("last-event-id") or request.query_params.get("after")
        after: int | None = None
        if raw is not None:
            if not raw.isdigit():
                return _error(400, "invalid_event_id")
            after = int(raw)
        return EventSourceResponse(
            event_stream(host.log, after=after, closing=host.closing),
            ping=15,
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )

    async def graph(request: Request) -> Response:
        run_id = request.query_params.get("run_id")
        if run_id is not None and not _ID.match(run_id):
            return _error(400, "invalid_run_id")
        return JSONResponse(await host.graph(run_id))

    async def post_input(request: Request) -> Response:
        body = await _json_body(request)
        if isinstance(body, JSONResponse):
            return body
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return _error(400, "text_required")
        if len(text) > MAX_INPUT_CHARS:
            return _error(413, "text_too_long")
        request_id = body.get("request_id")
        if request_id is not None and not (
            isinstance(request_id, str) and _REQUEST_ID.match(request_id)
        ):
            return _error(400, "invalid_request_id")
        binding = _binding(body.get("binding"))
        if isinstance(binding, JSONResponse):
            return binding
        try:
            input_id, duplicate = host.submit_input(text, request_id=request_id, binding=binding)
        except HostError as exc:
            return _host_failure(exc)
        return JSONResponse({"input_id": input_id, "duplicate": duplicate}, status_code=202)

    async def approval(request: Request) -> Response:
        approval_id = request.path_params["approval_id"]
        decision = request.path_params["decision"]
        if decision not in _DECISIONS or not _ID.match(approval_id):
            return _error(404, "not_found")
        body = await _json_body(request)
        if isinstance(body, JSONResponse):
            return body
        run_id = body.get("run_id")
        if not (isinstance(run_id, str) and _ID.match(run_id)):
            return _error(400, "run_id_required")
        try:
            input_id = host.decide_approval(approval_id, decision, run_id)
        except HostError as exc:
            return _host_failure(exc)
        return JSONResponse({"input_id": input_id}, status_code=202)

    async def control(request: Request) -> Response:
        action = request.path_params["action"]
        if action not in _CONTROLS:
            return _error(404, "not_found")
        try:
            input_id = host.control(action)
        except HostError as exc:
            return _host_failure(exc)
        return JSONResponse({"input_id": input_id}, status_code=202)

    async def get_settings(_: Request) -> Response:
        return JSONResponse(host.settings.summary())

    async def get_profiles(_: Request) -> Response:
        return JSONResponse({"profiles": host.settings.profiles()})

    async def put_settings(request: Request) -> Response:
        body = await _json_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            result = await host.apply_settings(body)
        except SettingsError as exc:
            return _error(422, "invalid_settings", field=exc.field, code=exc.code)
        except HostError as exc:
            return _host_failure(exc)
        return JSONResponse({**result, "settings": host.settings.summary()})

    def _engagement_failure(exc: EngagementError) -> JSONResponse:
        if exc.code == "unknown_engagement":
            return _error(404, exc.code)
        if exc.code in {"not_managed", "unreadable"}:
            return _error(409, exc.code)
        if exc.code == "confirmation_required":
            return _error(422, exc.code, field=exc.field)
        if exc.code == "write_failed":
            return _error(500, exc.code)
        return _error(422, "invalid_engagement", problems=exc.problems[:50])

    def _requested_id(source: Any) -> str | None | JSONResponse:
        value = source.get("id")
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not _ENGAGEMENT_ID.match(value):
            return _error(404, "unknown_engagement")
        return value

    async def get_engagement(request: Request) -> Response:
        if host.catalog is None:
            return _error(404, "engagement_unavailable")
        engagement_id = _requested_id(request.query_params)
        if isinstance(engagement_id, JSONResponse):
            return engagement_id
        try:
            status = (
                host.catalog.store(engagement_id).status()
                if engagement_id
                else EngagementStore.blank_status()
            )
        except EngagementError as exc:
            return _engagement_failure(exc)
        return JSONResponse(
            {
                "id": engagement_id,
                "fields": field_definitions(),
                "steps": [
                    {"id": key, "label": label, "fields": list(names)}
                    for key, label, names in STEPS
                ],
                "status": status,
            }
        )

    async def list_engagements(_: Request) -> Response:
        if host.catalog is None:
            return _error(404, "engagement_unavailable")
        return JSONResponse(
            {"active": host.active_id, "items": host.catalog.entries()}
        )

    async def preview_engagement(request: Request) -> Response:
        if host.catalog is None:
            return _error(404, "engagement_unavailable")
        body = await _json_body(request)
        if isinstance(body, JSONResponse):
            return body
        engagement_id = _requested_id(body)
        if isinstance(engagement_id, JSONResponse):
            return engagement_id
        try:
            store = (
                host.catalog.store(engagement_id)
                if engagement_id
                else EngagementStore.blank()
            )
            return JSONResponse(store.preview(body.get("draft")))
        except EngagementError as exc:
            return _engagement_failure(exc)

    async def put_engagement(request: Request) -> Response:
        body = await _json_body(request)
        if isinstance(body, JSONResponse):
            return body
        engagement_id = _requested_id(body)
        if isinstance(engagement_id, JSONResponse):
            return engagement_id
        try:
            result = await host.save_engagement(
                engagement_id,
                body.get("draft"),
                confirm_scope=body.get("confirm_scope") is True,
                confirm_expansion=body.get("confirm_expansion") is True,
            )
        except EngagementError as exc:
            return _engagement_failure(exc)
        except HostError as exc:
            return _host_failure(exc)
        return JSONResponse(result)

    async def interview_turn(request: Request) -> Response:
        if host.catalog is None:
            return _error(404, "engagement_unavailable")
        body = await _json_body(request)
        if isinstance(body, JSONResponse):
            return body
        engagement_id = _requested_id(body)
        if isinstance(engagement_id, JSONResponse):
            return engagement_id
        base = None
        editing = False
        try:
            if body.get("session_id") in (None, ""):
                if engagement_id:
                    status = host.catalog.store(engagement_id).status()
                    if not status["editable"] or status["draft"] is None:
                        raise EngagementError("not_managed")
                    base, editing = parse_draft(status["draft"]), True
                else:
                    base = parse_draft(body.get("draft") or {})
            result = await interviews.turn(
                session_id=body.get("session_id"),
                message=body.get("message"),
                base=base,
                editing=editing,
            )
        except EngagementError as exc:
            return _engagement_failure(exc)
        except InterviewError as exc:
            extra = {"detail": exc.detail} if exc.detail else {}
            return _error(_INTERVIEW_STATUS.get(exc.code, 400), exc.code, **extra)
        return JSONResponse(result)

    async def activate_engagement(request: Request) -> Response:
        body = await _json_body(request)
        if isinstance(body, JSONResponse):
            return body
        engagement_id = _requested_id(body)
        if isinstance(engagement_id, JSONResponse):
            return engagement_id
        if engagement_id is None:
            return _error(400, "id_required")
        try:
            await host.activate_engagement(engagement_id)
        except EngagementError as exc:
            return _engagement_failure(exc)
        except HostError as exc:
            return _host_failure(exc)
        return JSONResponse({"active": host.active_id, "state": host.state}, status_code=202)

    async def restart(_: Request) -> Response:
        try:
            await host.restart()
        except HostError as exc:
            return _host_failure(exc)
        return JSONResponse({"state": host.state}, status_code=202)

    routes = [
        Route("/", index, methods=["GET"]),
        Route(f"{API}/snapshot", snapshot, methods=["GET"]),
        Route(f"{API}/events", events, methods=["GET"]),
        Route(f"{API}/graph", graph, methods=["GET"]),
        Route(f"{API}/input", post_input, methods=["POST"]),
        Route(f"{API}/approvals/{{approval_id}}/{{decision}}", approval, methods=["POST"]),
        Route(f"{API}/run/{{action}}", control, methods=["POST"]),
        Route(f"{API}/settings", get_settings, methods=["GET"]),
        Route(f"{API}/settings", put_settings, methods=["PUT"]),
        Route(f"{API}/settings/profiles", get_profiles, methods=["GET"]),
        Route(f"{API}/engagement", get_engagement, methods=["GET"]),
        Route(f"{API}/engagement", put_engagement, methods=["PUT"]),
        Route(f"{API}/engagements", list_engagements, methods=["GET"]),
        Route(f"{API}/engagement/preview", preview_engagement, methods=["POST"]),
        Route(f"{API}/engagement/interview", interview_turn, methods=["POST"]),
        Route(f"{API}/engagement/activate", activate_engagement, methods=["POST"]),
        Route(f"{API}/session/restart", restart, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(GuardMiddleware, policy=policy)
    return app
