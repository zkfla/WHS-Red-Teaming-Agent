"""Backward-compatible CLI entrypoint.

The implementation remains in :mod:`redteaming.cli`; this module only keeps
the short ``redteam`` command and ``python -m redteam.cli`` working.
"""

from redteaming.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
