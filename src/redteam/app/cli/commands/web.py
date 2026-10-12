"""``redteam web`` — serve the interactive session in a browser (loopback only).

The terminal ``redteam`` entry point is unchanged. This command opens the same
session through the web frontend. Each investigation has its own engagement file
(scope, ROE, budget) in the engagements directory, written and selected in the
browser; the LLM/profile settings are completed there too when the environment does
not already provide them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from redteam.app.cli._shared import EXIT_CONFIG_ERROR, EXIT_FAILURE, EXIT_OK, eprint
from redteam.core.config import load_engagement_config

DEFAULT_PORT = 8787


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "web", help="Serve the interactive session in a local browser UI (loopback only)"
    )
    parser.add_argument(
        "engagement_path",
        nargs="?",
        default=None,
        help="Engagement file to select at start (optional; otherwise choose in the browser)",
    )
    parser.add_argument(
        "--engagements-dir",
        default="engagements",
        help="Directory of per-investigation engagement files (created on first save)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Loopback address to bind")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        help="Exact extra Origin allowed to call the API (repeatable)",
    )
    parser.set_defaults(handler=run)


async def run(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from redteam.app.web.engagement_catalog import EngagementCatalog
        from redteam.app.web.host import RealLauncher, SessionHost
        from redteam.app.web.security import GuardPolicy, normalize_origin, validate_bind_host
        from redteam.app.web.server import create_app
        from redteam.app.web.settings import SettingsStore
    except ImportError:
        eprint('ERROR: web UI dependencies are missing; run pip install -e ".[web]"')
        return EXIT_CONFIG_ERROR

    from redteam.app.cli.commands.validate import check_roe, check_scope

    if not 1 <= args.port <= 65535:
        eprint("ERROR: --port must be between 1 and 65535")
        return EXIT_CONFIG_ERROR
    try:
        bind = validate_bind_host(args.host)
        origins = tuple(normalize_origin(item) for item in args.allow_origin)
    except ValueError as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_CONFIG_ERROR
    directory = Path(args.engagements_dir)
    if directory.exists() and not directory.is_dir():
        eprint(f"ERROR: --engagements-dir {directory} is not a directory")
        return EXIT_CONFIG_ERROR
    pinned: list[Path] = []
    if args.engagement_path is not None:
        path = Path(args.engagement_path)
        try:
            engagement = load_engagement_config(path)
        except (OSError, ValidationError, ValueError) as exc:
            eprint(f"ERROR: {exc}")
            return EXIT_CONFIG_ERROR
        problems = [*check_scope(engagement), *check_roe(engagement)]
        if problems:
            eprint("ERROR: invalid engagement configuration: " + "; ".join(problems))
            return EXIT_CONFIG_ERROR
        pinned.append(path)

    settings = SettingsStore()
    settings.load_saved()
    catalog = EngagementCatalog(directory, pinned)
    active_id = catalog.pinned_ids()[0] if pinned else None
    host = SessionHost(
        settings=settings,
        launcher=RealLauncher(pinned[0] if pinned else None),
        catalog=catalog,
        active_id=active_id,
        mirror=sys.__stderr__,
    )
    app = create_app(host, GuardPolicy.for_loopback(args.port, extra_origins=origins))
    config = uvicorn.Config(
        app,
        host=bind,
        port=args.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(config)
    print(f"redteam web: http://{bind}:{args.port}/  (Ctrl+C로 종료)", flush=True)
    try:
        await host.start()
        await server.serve()
    except SystemExit:
        eprint(f"ERROR: could not serve on {bind}:{args.port} (address in use?)")
        return EXIT_FAILURE
    finally:
        await host.shutdown()
    return EXIT_OK
