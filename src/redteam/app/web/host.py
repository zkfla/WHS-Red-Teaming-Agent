"""Session lifecycle for the web surface.

The host starts the existing interactive session (``run_session``) once the
required settings are complete and routes browser requests into the same
``SessionController`` the terminal uses. It adds no execution, approval or
policy logic of its own.
"""

from __future__ import annotations

import asyncio
import io
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, TextIO

from redteam.app.cli.display import safe_text
from redteam.app.cli.session_types import BindingRef, SessionPhase
from redteam.app.web.engagement import EngagementError, EngagementStore
from redteam.app.web.engagement_catalog import EngagementCatalog
from redteam.app.web.events import SCHEMA_VERSION, EventLog
from redteam.app.web.frontend import WebFrontend
from redteam.app.web.graph_read import read_graph
from redteam.app.web.settings import SettingsStore

_CLIENT_REQUESTS = 512
_STOP_TIMEOUT = 20.0
_SNAPSHOT_ITEMS = 1000


class HostError(Exception):
    code = "host_error"


class NotReady(HostError):
    code = "session_not_ready"


class Busy(HostError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Rejected(HostError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Launcher(Protocol):
    def build_config(self) -> Any: ...

    async def launch(self, frontend: WebFrontend, config: Any) -> Any: ...


class RealLauncher:
    """Starts the real interactive session with the web frontend injected."""

    def __init__(self, engagement_path: Path | None = None) -> None:
        self.engagement_path = engagement_path  # chosen per investigation via ``select``
        self.environment: dict[str, str] | None = None

    def select(self, engagement_path: Path) -> None:
        self.engagement_path = engagement_path
        self.environment = None

    async def prepare(self) -> None:
        from redteam.app.web_policy import prepare_web_deployment
        if self.engagement_path is None:
            raise RuntimeError("no engagement selected")
        self.environment = await prepare_web_deployment(self.engagement_path)

    def build_config(self) -> Any:
        from redteam.app.cli._shared import load_app_config_for_run
        from redteam.core.environment import env_snapshot, environment_context

        if self.engagement_path is None:
            raise RuntimeError("no engagement selected")
        with environment_context(self.environment or env_snapshot()):
            return load_app_config_for_run(str(self.engagement_path))

    async def launch(self, frontend: WebFrontend, config: Any) -> Any:
        from rich.console import Console

        from redteam.app.bootstrap import build_llm_client
        from redteam.app.cli.conversation import ConversationAssistant
        from redteam.app.cli.interactive_commands import InteractiveSession
        from redteam.app.cli.session_runtime import run_session
        from redteam.core.environment import env_snapshot, environment_context

        if self.engagement_path is None:
            raise RuntimeError("no engagement selected")
        # The web frontend ignores the console; it only needs a valid sink.
        console = Console(file=io.StringIO(), width=100, color_system=None)
        session = InteractiveSession(
            config.engagement, config.llm, engagement_path=self.engagement_path
        )
        conversation = ConversationAssistant(build_llm_client(config.llm))
        with environment_context(self.environment or env_snapshot()):
            return await run_session(
                console,
                config,
                conversation=conversation,
                session=session,
                frontend=frontend,
                built=None,
                single_run=False,
            )


class SessionHost:
    def __init__(
        self,
        *,
        settings: SettingsStore,
        launcher: Launcher,
        engagement_name: str = "",
        scope: tuple[str, ...] = (),
        log: EventLog | None = None,
        mirror: TextIO | None = None,
        catalog: EngagementCatalog | None = None,
        active_id: str | None = None,
        reauthenticator: Callable[[], Awaitable[Any]] | None = None,
        reauthentication_available: Callable[[], bool] | None = None,
    ) -> None:
        self.mirror = mirror
        self.catalog = catalog
        self.active_id = active_id
        self.reauthenticator = reauthenticator
        self.reauthentication_available = reauthentication_available
        self.central_path: Path | None = None  # where the running session keeps its state
        self.settings = settings
        self.launcher = launcher
        self.engagement_name = engagement_name
        self.scope = scope
        self.log = log or EventLog()
        missing = catalog is not None and not self._active_ready()
        self.state = "needs_engagement" if missing else "needs_settings"
        self.error: str | None = None
        self.closing = asyncio.Event()
        self.frontend = self._new_frontend()
        self._task: asyncio.Task | None = None
        self._reauth_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._client_inputs: OrderedDict[str, str] = OrderedDict()

    # -- lifecycle ------------------------------------------------------

    def _new_frontend(self) -> WebFrontend:
        return WebFrontend(self.log, on_bind=self._bound, mirror=self.mirror)

    def _bound(self, _controller: Any) -> None:
        self._set("ready")

    def _set(self, state: str, error: str | None = None) -> None:
        self.state, self.error = state, error
        self.log.append(
            "host",
            {"state": state, "error": error, "problems": self.settings.problems()},
        )

    async def start(self) -> None:
        async with self._lock:
            await self._launch()

    def _active_store(self) -> EngagementStore | None:
        if self.catalog is None or self.active_id is None:
            return None
        try:
            return self.catalog.store(self.active_id)
        except EngagementError:
            return None

    def _active_ready(self) -> bool:
        store = self._active_store()
        return store is not None and store.ready()

    def _refresh_identity(self) -> None:
        # The engagement file is the source; the host only mirrors name and scope.
        if self.catalog is None:
            return
        store = self._active_store()
        summary = store.status()["summary"] if store else None
        if summary is None:
            self.engagement_name, self.scope = "", ()
            return
        self.engagement_name = summary["name"]
        self.scope = tuple(item for values in summary["scope"].values() for item in values)

    async def _launch(self) -> None:
        if self.closing.is_set():
            return
        self._refresh_identity()
        if self.catalog is not None:
            store = self._active_store()
            if store is None or not store.ready():
                self._set("needs_engagement")
                return
            select = getattr(self.launcher, "select", None)
            if select is not None:
                select(store.path)
        if self.settings.problems():
            self._set("needs_settings")
            return
        self.frontend = self._new_frontend()
        self._set("starting")
        self._task = asyncio.create_task(self._session(self.frontend), name="web-session")

    async def _session(self, frontend: WebFrontend) -> None:
        error: str | None = None
        try:
            prepare = getattr(self.launcher, "prepare", None)
            if prepare is not None:
                await prepare()
            config = self.launcher.build_config()
            self._remember_state_path(config)
            result = await self.launcher.launch(frontend, config)
            if getattr(result, "exit_code", 0) and getattr(result, "error", None):
                error = safe_text(result.error, limit=300)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported as a bounded, masked diagnostic
            error = safe_text(f"{type(exc).__name__}: {exc}", limit=300)
        if not self.closing.is_set() and frontend is self.frontend:
            self._set("error" if error else "stopped", error)

    def _remember_state_path(self, config: Any) -> None:
        raw = getattr(getattr(config, "orchestration", None), "central_state_path", None)
        self.central_path = Path(raw) if isinstance(raw, str) and raw else None

    async def _stop_session(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        controller = self.frontend.controller
        if controller is not None:
            controller.request_close()
        try:
            await asyncio.wait_for(asyncio.shield(task), _STOP_TIMEOUT)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self) -> None:
        self.closing.set()
        auth_task = self._reauth_task
        if auth_task is not None and not auth_task.done():
            auth_task.cancel()
            await asyncio.gather(auth_task, return_exceptions=True)
        async with self._lock:
            await self._stop_session()
            self.state = "closed"

    # -- guards ---------------------------------------------------------

    def _controller(self) -> Any:
        controller = self.frontend.controller
        if controller is None or self.state != "ready":
            raise NotReady()
        return controller

    def _ensure_idle(self) -> None:
        if self.state in {"starting", "authenticating"}:
            code = "session_starting" if self.state == "starting" else "authentication_in_progress"
            raise Busy(code)
        controller = self.frontend.controller
        if self.state != "ready" or controller is None:
            return
        if (
            controller.run_task is not None
            or controller.phase not in {SessionPhase.IDLE}
            or controller.pending_count
            or controller.deferred
        ):
            raise Busy("run_active")

    # -- input ----------------------------------------------------------

    def submit_input(
        self, text: str, *, request_id: str | None, binding: BindingRef | None
    ) -> tuple[str, bool]:
        controller = self._controller()
        if request_id and request_id in self._client_inputs:
            return self._client_inputs[request_id], True
        options: dict[str, Any] = {} if binding is None else {"binding": binding}
        if not controller.submit(text, **options):
            raise Rejected("input_not_accepted")
        input_id = f"input_{controller.sequence}"
        if request_id:
            self._client_inputs[request_id] = input_id
            while len(self._client_inputs) > _CLIENT_REQUESTS:
                self._client_inputs.popitem(last=False)
        return input_id, False

    def decide_approval(self, approval_id: str, decision: str, run_id: str) -> str:
        """Forward an explicit click. Controller and runner re-verify the ledger."""
        controller = self._controller()
        if not controller.is_pending_approval(approval_id, run_id):
            raise Rejected("approval_changed")
        binding = controller.input_binding
        if binding is None or str(binding.run_id) != run_id:
            raise Rejected("run_mismatch")
        if not controller.submit(
            f"{decision} {approval_id}", binding=binding, approval_id=approval_id
        ):
            raise Rejected("input_not_accepted")
        return f"input_{controller.sequence}"

    def control(self, action: str) -> str:
        controller = self._controller()
        if not controller.submit(f"/{action}"):
            raise Rejected("input_not_accepted")
        return f"input_{controller.sequence}"

    # -- settings and restart -------------------------------------------

    async def apply_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._ensure_idle()
            result = self.settings.apply(payload)
            await self._stop_session()
            await self._launch()
            return result

    async def begin_reauthentication(self) -> None:
        """Start an explicit provider login without holding the HTTP request open."""
        async with self._lock:
            self._ensure_idle()
            if self.reauthenticator is None:
                raise Rejected("reauth_unavailable")
            if (
                self.reauthentication_available is not None
                and not self.reauthentication_available()
            ):
                raise Rejected("chatgpt_reauth_not_applicable")
            await self._stop_session()
            self._set("authenticating")
            task = asyncio.create_task(
                self._complete_reauthentication(), name="web-provider-reauth"
            )
            self._reauth_task = task

    async def _complete_reauthentication(self) -> None:
        try:
            assert self.reauthenticator is not None
            await self.reauthenticator()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - expose only the stable provider failure code
            async with self._lock:
                if not self.closing.is_set():
                    self._set("error", "chatgpt_reauth_failed")
                self._reauth_task = None
            return
        async with self._lock:
            self._reauth_task = None
            if not self.closing.is_set():
                await self._launch()

    async def save_engagement(
        self,
        engagement_id: str | None,
        body: Any,
        *,
        confirm_scope: bool,
        confirm_expansion: bool,
    ) -> dict[str, Any]:
        """Create (``engagement_id`` None) or edit an engagement file.

        A new file becomes the active one. Editing the active file restarts the session;
        both need an idle session. Editing another file leaves the session untouched.
        """
        if self.catalog is None:
            raise Rejected("engagement_unavailable")
        async with self._lock:
            creating = engagement_id is None
            if creating:
                name = body.get("name") if isinstance(body, dict) else None
                engagement_id = self.catalog.new_id(name if isinstance(name, str) else None)
            store = self.catalog.store(engagement_id, must_exist=not creating)
            affects_session = creating or engagement_id == self.active_id
            if affects_session:
                self._ensure_idle()
            result = store.save(
                body, confirm_scope=confirm_scope, confirm_expansion=confirm_expansion
            )
            result["id"] = engagement_id
            if affects_session:
                self.active_id = engagement_id
                await self._stop_session()
                await self._launch()
            return result

    async def activate_engagement(self, engagement_id: str) -> None:
        if self.catalog is None:
            raise Rejected("engagement_unavailable")
        async with self._lock:
            store = self.catalog.store(engagement_id)
            if not store.ready():
                raise Rejected("engagement_not_ready")
            self._ensure_idle()
            self.active_id = engagement_id
            await self._stop_session()
            await self._launch()

    async def restart(self) -> None:
        async with self._lock:
            self._ensure_idle()
            await self._stop_session()
            await self._launch()

    # -- reading --------------------------------------------------------

    def current_run_id(self) -> str | None:
        controller = self.frontend.controller
        binding = controller.input_binding if controller is not None else None
        return binding.run_id if binding is not None else None

    async def graph(self, run_id: str | None = None) -> dict[str, Any]:
        """Read-only projection of stored state; starts no actor, provider or model."""
        run_id = run_id or self.current_run_id()
        if self.central_path is None:
            return {"available": False, "reason": "state_unconfigured", "run_id": run_id}
        if run_id is None:
            return {"available": False, "reason": "no_run_yet", "run_id": None}
        return await read_graph(self.central_path, run_id)

    def _engagement_snapshot(self) -> dict[str, Any] | None:
        if self.catalog is None:
            return None
        store = self._active_store()
        return {
            "active": self.active_id if store else None,
            "status": store.status() if store else None,
            "items": self.catalog.entries(),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "session": {
                "state": self.state,
                "error": self.error,
                "engagement": safe_text(self.engagement_name, limit=200),
                "scope": [safe_text(item, limit=200) for item in self.scope[:50]],
            },
            "state": self.frontend.state_data(),
            "settings": self.settings.summary(),
            "engagement": self._engagement_snapshot(),
            "last_seq": self.log.last_seq,
            "items": self.log.tail(_SNAPSHOT_ITEMS),
        }
