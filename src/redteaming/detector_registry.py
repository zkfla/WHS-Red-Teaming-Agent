from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from .detection import HttpObservation
from .models import VulnerabilityCandidate


class Detector(Protocol):
    name: str

    def detect(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        ...

    # Optional extension point.  A registered detector may provide
    # verify(candidate, observations, probe, scope, identities) and return a
    # VerificationResult.  Runtime keeps the fallback safe replay for simple
    # passive detectors, while custom specialists can define their own
    # evidence comparison without changing the orchestrator.


class DetectorRegistry:
    """Resolves detectors from task configuration, not from target-specific code."""

    def __init__(self, detectors: Iterable[Detector] = ()) -> None:
        self._detectors: dict[str, Detector] = {detector.name: detector for detector in detectors}

    def register(self, detector: Detector) -> None:
        self._detectors[detector.name] = detector

    def resolve(self, names: tuple[str, ...] = ()) -> list[Detector]:
        selected = names or tuple(self._detectors)
        missing = [name for name in selected if name not in self._detectors]
        if missing:
            raise KeyError(f"detectors are not registered: {', '.join(missing)}")
        return [self._detectors[name] for name in selected]

    def get(self, name: str) -> Detector:
        try:
            return self._detectors[name]
        except KeyError as exc:
            raise KeyError(f"detector is not registered: {name}") from exc
