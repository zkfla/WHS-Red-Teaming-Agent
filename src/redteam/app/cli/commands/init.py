"""``redteam init`` — write a starter Engagement config.

Static template, not a wizard (v1 scope — section 5). The template is
``examples/engagement.yaml`` itself: the one file this repository already
promises is a valid, minimal ``EngagementConfig`` (exercised by
``tests/integration``), so copying it can never drift from the real
schema the way a hand-written CLI string could.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from redteam.app.cli._shared import EXIT_CONFIG_ERROR, EXIT_OK, eprint
from redteam.core.config import load_engagement_config

#: Repo-root-relative — this CLI is run from an editable git checkout
#: (the project's only documented install method; README "Quick Start"),
#: not a built wheel, so `examples/` is always present alongside `src/`.
_TEMPLATE_SOURCE = Path(__file__).resolve().parents[5] / "examples" / "engagement.yaml"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("init", help="Create a starter Engagement config")
    parser.add_argument("--output", default="engagement.yaml")
    parser.set_defaults(handler=run)


async def run(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if output.exists():
        eprint(f"ERROR: {output} already exists — refusing to overwrite.")
        return EXIT_CONFIG_ERROR
    if not _TEMPLATE_SOURCE.exists():  # pragma: no cover - packaging error, not a user error
        eprint(f"ERROR: template {_TEMPLATE_SOURCE} is missing from this install.")
        return EXIT_CONFIG_ERROR

    text = _TEMPLATE_SOURCE.read_text(encoding="utf-8")
    # Prove the template still parses as a real EngagementConfig before
    # handing it to the user — a broken template must fail loudly here,
    # not surface as a confusing `validate` error later.
    load_engagement_config(_TEMPLATE_SOURCE)
    output.write_text(text, encoding="utf-8")

    print(f"Created engagement config:\n  {output}")
    print(f"\nNext:\n  redteam validate {output}")
    return EXIT_OK
