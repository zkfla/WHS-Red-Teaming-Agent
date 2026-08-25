"""루프 단계 표지. §13 pseudocode: RECON → MODEL → PROBE → DEEPEN (+ BOOTSTRAP/ENTITLEMENT/BASELINE)."""
from __future__ import annotations

from enum import Enum


class Phase(str, Enum):
    BOOTSTRAP = "BOOTSTRAP"
    RECON = "RECON"
    ENTITLEMENT = "ENTITLEMENT"
    BASELINE = "BASELINE"
    MODEL = "MODEL"
    PROBE = "PROBE"
    DEEPEN = "DEEPEN"
