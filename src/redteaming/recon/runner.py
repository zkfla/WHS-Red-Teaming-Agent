from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import socket
import threading
import time
import json
from pathlib import Path
from urllib.parse import urlparse

from ..capability.process import ProcessResult
from ..events import Event, EventBus
from ..graphs import AssetGraph, EvidenceStore, ExplorationGraph
from ..models import Asset, Evidence, Intent, Scope, stable_id
from ..policies import ReconPolicy


@dataclass(frozen=True)
class ReconRun:
    target: str
    command: tuple[str, ...]
    result: ProcessResult
    assets: tuple[Asset, ...]
    evidence: Evidence
    intent: Intent


class PortReconRunner:
    """Dependency-free, bounded TCP discovery for authorized targets."""

    def __init__(self, assets: AssetGraph, exploration: ExplorationGraph, evidence: EvidenceStore,
                 events: EventBus | None = None, checkpoint_path: Path | None = None,
                 policy: ReconPolicy | None = None) -> None:
        self.assets = assets
        self.exploration = exploration
        self.evidence = evidence
        self.events = events or EventBus()
        self._completed_ports: dict[str, set[int]] = {}
        self._open_ports: dict[str, set[int]] = {}
        self._checkpoint_lock = threading.Lock()
        self.checkpoint_path = checkpoint_path
        self.policy = policy or ReconPolicy()
        self._load_checkpoint()

    def _load_checkpoint(self) -> None:
        if self.checkpoint_path is None or not self.checkpoint_path.exists():
            return
        try:
            data = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            for host, values in data.get("targets", {}).items():
                self._completed_ports[host] = {int(port) for port in values.get("completed", [])}
                self._open_ports[host] = {int(port) for port in values.get("open", [])}
        except (OSError, TypeError, ValueError):
            # A corrupt checkpoint must never prevent a fresh authorized run.
            self._completed_ports.clear()
            self._open_ports.clear()

    def _save_checkpoint_locked(self) -> None:
        if self.checkpoint_path is None:
            return
        payload = {
            "version": 1,
            "targets": {
                host: {"completed": sorted(completed),
                       "open": sorted(self._open_ports.get(host, set()))}
                for host, completed in self._completed_ports.items()
            },
        }
        try:
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            self.checkpoint_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            # Recon remains useful when the workspace is read-only.
            return

    def run(self, target: str, scope: Scope, ports: object | None = None) -> ReconRun:
        parsed_target = urlparse(target if "://" in target else f"//{target}")
        target_host = (parsed_target.hostname or target).rstrip(".").lower()
        # Resume state is scoped to the concrete host:port target when the
        # operator declared an exact port; a host-wide scan must not suppress
        # a later engagement on another port of the same host.
        checkpoint_key = (f"{target_host}:{parsed_target.port}"
                          if parsed_target.port is not None else target_host)
        if not scope.allows(target):
            raise ValueError(f"target is outside the task scope: {target_host}")
        started = time.monotonic()
        intent = Intent(
            objective="Discover authorized TCP ports using built-in recon",
            target_asset_ids=(), kind="network-recon",
            id=stable_id("intent", "python-port-scan", target_host),
            success_criteria=("Open TCP ports are normalized into Asset Graph",),
            stop_conditions=("Target is outside scope", "Rate policy blocks the action"),
        )
        intent_node = self.exploration.add("intent", intent.id)
        self.events.publish(Event("recon.started", {"target": target_host, "method": "python-tcp"}))

        def probe(port: int) -> int | None:
            try:
                with socket.create_connection((target_host, port), timeout=self.policy.timeout_seconds):
                    with self._checkpoint_lock:
                        self._open_ports.setdefault(checkpoint_key, set()).add(port)
                        self._save_checkpoint_locked()
                    return port
            except (OSError, TimeoutError):
                return None
            finally:
                with self._checkpoint_lock:
                    self._completed_ports.setdefault(checkpoint_key, set()).add(port)
                    self._save_checkpoint_locked()

        selected_ports = (parsed_target.port,) if parsed_target.port else tuple(
            self.policy.ports if ports is None else ports
        )
        if parsed_target.port is None:
            for allowed in scope.allowed_hosts:
                parsed_allowed = urlparse(allowed if "://" in allowed else f"//{allowed}")
                if (parsed_allowed.hostname or "").rstrip(".").lower() == target_host and parsed_allowed.port:
                    selected_ports = (parsed_allowed.port,)
                    break
        with self._checkpoint_lock:
            completed = self._completed_ports.setdefault(checkpoint_key, set())
            selected_ports = tuple(port for port in selected_ports if port not in completed)
        resumed = not selected_ports
        if resumed:
            self.events.publish(Event("recon.resumed", {"target": target_host, "remaining_ports": 0}))
        if len(selected_ports) > self.policy.max_ports:
            raise ValueError("port plan is too large; keep a bounded approved port set")
        if selected_ports:
            with ThreadPoolExecutor(max_workers=max(1, min(scope.max_concurrency, 32))) as pool:
                open_ports = tuple(port for port in pool.map(probe, selected_ports) if port is not None)
        else:
            open_ports = ()
        with self._checkpoint_lock:
            open_ports = tuple(sorted(set(open_ports) | self._open_ports.get(checkpoint_key, set())))
        host_asset = self.assets.upsert(Asset(kind="host", canonical_value=target_host))
        parsed_assets = [host_asset]
        for port in open_ports:
            parsed_assets.append(self.assets.upsert(Asset(
                kind="service", canonical_value=f"{target_host}:{port}/tcp", parent_id=host_asset.id,
                properties={"protocol": "tcp", "port": str(port), "state": "open", "detected_by": "python-tcp"},
            )))
        evidence = Evidence(
            kind="tcp-connect", summary=f"Built-in TCP discovery found {len(open_ports)} open port(s) on {target_host}",
            metadata={"target": target_host, "open_ports": list(open_ports), "resumed": resumed,
                      "duration_seconds": round(time.monotonic() - started, 3)},
        )
        self.evidence.add(evidence)
        for asset in parsed_assets:
            asset_node = self.exploration.add("asset", asset.id)
            self.exploration.anchor(intent_node, asset)
            self.exploration.link(intent_node, "discovered", asset_node)
        self.events.publish(Event("recon.completed", {"target": target_host,
                                                        "asset_count": len(parsed_assets), "method": "python-tcp"}))
        result = ProcessResult(("python-tcp-recon", target_host), 0, "", "")
        return ReconRun(target_host, result.argv, result, tuple(parsed_assets), evidence, intent)
