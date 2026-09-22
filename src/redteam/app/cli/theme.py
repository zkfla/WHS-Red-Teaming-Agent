"""Shared color/icon vocabulary for the interactive CLI (section 14).

CLI-layer only -- nothing under ``redteam.core``/``redteam.orchestration``/
``redteam.control``/``redteam.capability`` imports this module or Rich
itself. Core emits Events; this file is part of how the CLI chooses to
draw them (section 17).
"""

from __future__ import annotations

# Semantic color roles (Rich style strings).
DEFAULT = "default"
EMPHASIS = "cyan"
SUCCESS = "green"
WARNING = "yellow"
DANGER = "red"
MUTED = "dim"

# Status icons, one meaning each -- never reused for something else.
ICON_SUCCESS = "✓"
ICON_FAILURE = "✗"
ICON_WARNING = "⚠"
ICON_ACTIVE = "●"
ICON_PENDING = "○"
ICON_TRANSITION = "→"

BANNER_TITLE = "REDTEAM"
BANNER_SUBTITLE = "AI-Powered Security Assessment Framework"
