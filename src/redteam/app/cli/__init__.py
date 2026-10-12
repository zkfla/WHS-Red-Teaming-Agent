"""``redteam`` CLI entrypoint (``pyproject.toml`` ``[project.scripts]``).

Thin dispatcher only: argument parsing, then one call into a
``commands.*`` module, then rendering + exit code. All domain decisions
(ROE, Scope, Tool Selection, Approval policy, Agent planning) live in
``redteam.control``/``redteam.capability``/``redteam.orchestration`` —
this package never makes one.

``redteam run`` and ``resume`` reach those through the canonical bootstrap
(``redteam.app.bootstrap.build_application``). Explicit ``collect-originals``
reuses that assembly for existing-result queries only, never a fresh drive.
Other commands read durable state directly (Approval/Audit/Evidence
stores, the persisted terminal report) or, for ``tools``, builds just the
Capability gateway via the same ``capability.bootstrap`` helper
``build_application`` itself uses -- never a separate CLI-only runtime.

``redteam`` with **no** subcommand at all dispatches to
``commands.interactive`` — the natural-language setup/Preflight/run
entrypoint. Every explicit subcommand (``redteam run ...`` included)
is completely unaffected: this is purely an additional path taken only
when ``args.command`` comes back ``None`` (section 23 -- explicit
commands stay script/CI-safe, unchanged).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from redteam.app.cli._shared import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_OK,
    eprint,
    load_dotenv_if_present,
)
from redteam.app.cli.commands import (
    approvals,
    evidence,
    findings,
    init,
    interactive,
    report,
    resume,
    roe,
    status,
    tools,
    validate,
    web,
)
from redteam.app.cli.commands import (
    run as run_command,
)

_COMMAND_MODULES = (
    init,
    validate,
    run_command,
    resume,
    status,
    roe,
    tools,
    approvals,
    findings,
    report,
    evidence,
    web,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="redteam",
        description="WHS Red Teaming Agent -- authorized security testing only.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Interactive mode only: show internal ids/metadata alongside each event",
    )
    # Not required (unlike before): no subcommand at all means "run the
    # interactive setup/Preflight/run entrypoint" -- see `main()`.
    subparsers = parser.add_subparsers(dest="command", required=False)
    for module in _COMMAND_MODULES:
        module.add_parser(subparsers)
    return parser


async def _dispatch(handler, args: argparse.Namespace) -> int | None:  # noqa: ANN001
    """Runs ``handler`` and, if it pulled in ``litellm`` (a real
    ``LiteLLMClient`` call, in ``run``/``interactive``/an Approval
    resume), closes litellm's cached async HTTP clients on *this* loop
    before it returns.

    ``litellm`` caches a persistent aiohttp/httpx client per provider
    for connection reuse (``litellm.in_memory_llm_clients_cache``).
    ``redteam`` is a short one-shot ``asyncio.run()`` process, and
    litellm's own ``atexit``-registered cleanup
    (``async_client_cleanup.register_async_client_cleanup``, armed the
    first time any lazy ``litellm.*`` attribute is touched) closes
    those clients on a *new* event loop it creates after this one is
    already gone -- verified directly to still leave a stale
    ``SSLProtocol`` holding a reference to the dead original loop,
    which then fails a final TLS write against it during interpreter
    shutdown (``Bad file descriptor`` -> ``RuntimeError: Event loop is
    closed``, harmless but noisy on stderr). Closing explicitly here,
    on the loop that's still alive, avoids that race instead of relying
    on litellm's exit-time fallback.

    Only runs when ``"litellm"`` is already in ``sys.modules`` --
    never imports it just for this (``llm`` stays a genuinely optional
    extra for ``FakeLLMClient``-only installs).
    """
    try:
        return await handler(args)
    finally:
        litellm_module = sys.modules.get("litellm")
        if litellm_module is not None:
            close_clients = getattr(litellm_module, "close_litellm_async_clients", None)
            if close_clients is not None:
                try:
                    await close_clients()
                except Exception:  # noqa: BLE001 - best-effort cleanup, never masks the real result
                    pass


def _use_utf8_console_streams() -> None:
    """Reconfigure stdout/stderr to UTF-8 before anything prints.

    The renderer's theme icons (``theme.ICON_SUCCESS`` etc.) and Korean
    prompt text are non-ASCII. On a plain Windows console (the default
    legacy codepage for a Korean-locale machine is ``cp949``, not
    UTF-8), printing one of those raises ``UnicodeEncodeError`` and takes
    the whole interactive session down mid-Preflight -- the exact "CLI
    전체 crash" this project's own design notes say must not happen.
    ``TextIOWrapper.reconfigure`` is a no-op-safe, stdlib-only fix; it is
    skipped (never raises) when a stream doesn't support it (already
    UTF-8, replaced by a test harness, redirected through something
    else) so this can never itself break a working setup.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # noqa: PERF203 - defensive, never fatal
                pass


def main() -> None:
    _use_utf8_console_streams()
    # A real shell/CI export always wins -- this only fills in what a
    # previous `redteam` run's one-time setup (or an operator by hand) left
    # in `.env`, so it takes effect without a separate manual export/source
    # step before every invocation (see `_shared.load_dotenv_if_present`).
    load_dotenv_if_present()
    logging.basicConfig(level=os.environ.get("REDTEAM_LOG_LEVEL", "INFO"))
    parser = _build_parser()
    args = parser.parse_args()

    handler = getattr(args, "handler", None) or interactive.run

    try:
        exit_code = asyncio.run(_dispatch(handler, args))
    except KeyboardInterrupt:
        # Conventional SIGINT exit code; the handler already printed
        # whatever command-specific guidance applies (see commands.run).
        raise SystemExit(130) from None
    except (OSError, ValueError) as exc:
        eprint(f"ERROR: {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc
    except Exception as exc:  # noqa: BLE001 - never leak a raw traceback by default
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.exception("Command failed")
        eprint(f"ERROR: {exc}")
        raise SystemExit(EXIT_FAILURE) from exc

    raise SystemExit(exit_code if exit_code is not None else EXIT_OK)


if __name__ == "__main__":
    main()
